"""Db2 connection pool and query execution.

ibm_db is a blocking C extension and a single connection handle must not be used from two
threads at once. So: a fixed pool of handles, one borrower at a time per handle, and every
blocking call pushed onto a worker thread via anyio.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import anyio
import ibm_db
from anyio import to_thread

from .config import Settings
from .serialize import JsonValue
from .serialize import row as serialize_row

log = logging.getLogger(__name__)

#: Value for SQL_ATTR_READ_ONLY_CONNECTION (SQL_TRUE).
_SQL_TRUE = 1
#: ibm_db.set_option resource types.
_TYPE_CONNECTION = 1
_TYPE_STATEMENT = 0


class Db2Error(RuntimeError):
    """A database error, with the credentials kept out of the message."""


@dataclass(slots=True)
class QueryResult:
    columns: list[str]
    rows: list[list[JsonValue]] = field(default_factory=list)
    truncated: bool = False
    row_limit: int = 0


def _connect(settings: Settings) -> object:
    """Open one read-only, non-autocommit connection. Blocking."""
    options = {
        ibm_db.SQL_ATTR_AUTOCOMMIT: ibm_db.SQL_AUTOCOMMIT_OFF,
        ibm_db.SQL_ATTR_READ_ONLY_CONNECTION: _SQL_TRUE,
    }
    try:
        conn = ibm_db.connect(settings.connection_string(), "", "", options)
    except Exception as exc:  # ibm_db raises bare Exception
        raise Db2Error(
            f"Could not connect to {settings.describe_target()}: {_clean(str(exc), settings)}"
        ) from None
    if not conn:
        raise Db2Error(f"Could not connect to {settings.describe_target()}.")

    # Belt and braces: if the attribute was ignored at connect time, set it explicitly.
    try:
        ibm_db.set_option(conn, {ibm_db.SQL_ATTR_READ_ONLY_CONNECTION: _SQL_TRUE}, _TYPE_CONNECTION)
    except Exception as exc:
        log.warning("Could not set SQL_ATTR_READ_ONLY_CONNECTION: %s", exc)
    return conn


def _clean(message: str, settings: Settings) -> str:
    """Strip the password out of driver messages before they go anywhere."""
    secret = settings.pwd.get_secret_value()
    return message.replace(secret, "***") if secret else message


def _execute(
    conn: object,
    sql: str,
    params: Sequence[object],
    max_rows: int,
    timeout: int,
    max_cell_chars: int,
    settings: Settings,
) -> QueryResult:
    """Run one statement and fetch at most max_rows+1 rows. Blocking."""
    stmt = None
    try:
        stmt = ibm_db.prepare(conn, sql)
        if not stmt:
            raise Db2Error(_clean(ibm_db.stmt_errormsg() or "prepare failed", settings))
        try:
            ibm_db.set_option(stmt, {ibm_db.SQL_ATTR_QUERY_TIMEOUT: timeout}, _TYPE_STATEMENT)
        except Exception as exc:
            log.warning("Could not set query timeout: %s", exc)

        if not ibm_db.execute(stmt, tuple(params)):
            raise Db2Error(_clean(ibm_db.stmt_errormsg(stmt) or "execute failed", settings))

        columns = [ibm_db.field_name(stmt, i) for i in range(ibm_db.num_fields(stmt))]
        result = QueryResult(columns=columns, row_limit=max_rows)

        # Fetch one row beyond the limit so `truncated` is honest.
        while len(result.rows) <= max_rows:
            values = ibm_db.fetch_tuple(stmt)
            if values is False:
                break
            result.rows.append(serialize_row(values, max_cell_chars))
        if len(result.rows) > max_rows:
            result.rows.pop()
            result.truncated = True
        return result
    except Db2Error:
        raise
    except Exception as exc:
        raise Db2Error(_clean(str(exc), settings)) from None
    finally:
        if stmt:
            ibm_db.free_result(stmt)
        # Nothing here writes, but an open read transaction still holds locks.
        try:
            ibm_db.rollback(conn)
        except Exception as exc:
            log.warning("Rollback after query failed: %s", exc)


@dataclass(slots=True)
class _Lease:
    """A borrowed connection. Set `discard` to keep it out of the pool on release."""

    conn: object
    discard: bool = False


class Db2Pool:
    """A fixed-size pool of ibm_db connections, borrowed one caller at a time."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._free: list[object] = []
        self._open_count = 0
        self._limiter = anyio.Semaphore(settings.pool_size)

    async def start(self) -> None:
        """Open one connection eagerly so startup fails loudly on bad credentials."""
        conn = await to_thread.run_sync(_connect, self.settings)
        self._open_count += 1
        self._free.append(conn)
        log.info("Connected to %s", self.settings.describe_target())

    async def close(self) -> None:
        for conn in self._free:
            try:
                await to_thread.run_sync(ibm_db.close, conn)
            except Exception as exc:
                log.warning("Error closing connection: %s", exc)
        self._open_count -= len(self._free)
        self._free.clear()

    @asynccontextmanager
    async def _acquire(self) -> AsyncIterator[_Lease]:
        async with self._limiter:
            conn = self._free.pop() if self._free else None
            if conn is None:
                conn = await to_thread.run_sync(_connect, self.settings)
                self._open_count += 1
            lease = _Lease(conn)
            try:
                yield lease
            finally:
                if lease.discard or not _is_alive(conn):
                    # A connection whose query was abandoned is still in use by that thread;
                    # dropping the reference is the only safe move.
                    log.warning("Discarding connection (discard=%s)", lease.discard)
                    self._open_count -= 1
                else:
                    self._free.append(conn)

    async def server_info(self) -> object:
        """The driver's own view of the server (DBMS_NAME, DBMS_VER, ...)."""
        async with self._acquire() as lease:
            return await to_thread.run_sync(ibm_db.server_info, lease.conn)

    async def query(
        self,
        sql: str,
        params: Sequence[object] = (),
        *,
        max_rows: int | None = None,
        timeout: int | None = None,
    ) -> QueryResult:
        """Run a statement and return its rows. Raises Db2Error on failure."""
        limit = self.settings.clamp_rows(max_rows)
        seconds = timeout or self.settings.query_timeout
        async with self._acquire() as lease:
            try:
                # The driver's own timeout should fire first; this is the backstop for when
                # it doesn't. The worker thread is then abandoned, so the connection goes too.
                with anyio.fail_after(seconds + 5):
                    return await to_thread.run_sync(
                        _execute,
                        lease.conn,
                        sql,
                        params,
                        limit,
                        seconds,
                        self.settings.max_cell_chars,
                        self.settings,
                        abandon_on_cancel=True,
                    )
            except TimeoutError:
                lease.discard = True
                raise Db2Error(
                    f"Query exceeded the {seconds}s timeout and was abandoned."
                ) from None
            except anyio.get_cancelled_exc_class():
                # Client cancelled the call; the worker thread still owns this connection.
                lease.discard = True
                raise


def _is_alive(conn: object) -> bool:
    try:
        return bool(ibm_db.active(conn))
    except Exception:
        return False

"""Connect to Db2 and print a few facts — no MCP involved.

Run this first when something is wrong: it separates driver/credential problems from MCP problems.

    uv run python scripts/smoke.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

from mcp_db2.catalog import list_schemas
from mcp_db2.config import Settings
from mcp_db2.db import Db2Error, Db2Pool


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:
        print(f"Configuration incomplete: {exc}", file=sys.stderr)
        print("Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 2

    print(f"Connecting to {settings.describe_target()} ...")
    pool = Db2Pool(settings)
    try:
        await pool.start()
        info = await pool.server_info()
        print(f"Server:  {getattr(info, 'DBMS_NAME', '?')} {getattr(info, 'DBMS_VER', '?')}")

        current = await pool.query("SELECT CURRENT SCHEMA, SESSION_USER FROM SYSIBM.SYSDUMMY1")
        print(f"Session: schema={current.rows[0][0]!r} user={current.rows[0][1]!r}")

        schemas = await list_schemas(pool)
        print(f"Visible schemas ({len(schemas)}): {', '.join(s.name for s in schemas[:15])}")

        # Proves the read-only connection attribute is doing its job.
        try:
            await pool.query("CREATE TABLE MCP_DB2_SMOKE_TEST (ID INT)")
            print("WARNING: a CREATE TABLE succeeded — the connection is NOT read-only!")
        except Db2Error as exc:
            print(f"Write attempt correctly rejected: {str(exc).splitlines()[0]}")
    except Db2Error as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

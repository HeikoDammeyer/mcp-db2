"""Tests against a real Db2. Skipped unless DB2_HOSTNAME and friends are set.

    uv run pytest -m integration
"""

from __future__ import annotations

import os

import pytest

from mcp_db2.catalog import describe_table, list_schemas, list_tables
from mcp_db2.config import Settings
from mcp_db2.db import Db2Error, Db2Pool

pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(
        not os.getenv("DB2_HOSTNAME") or not os.getenv("DB2_DATABASE"),
        reason="DB2_* environment not configured",
    ),
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def pool():
    p = Db2Pool(Settings())  # type: ignore[call-arg]
    await p.start()
    try:
        yield p
    finally:
        await p.close()


async def test_connects_and_reports_itself(pool: Db2Pool):
    info = await pool.server_info()
    assert getattr(info, "DBMS_NAME", "")


async def test_catalog_is_readable(pool: Db2Pool):
    schemas = await list_schemas(pool)
    assert schemas, "no visible schemas — check DB2_SCHEMA_ALLOWLIST/DENYLIST"
    assert all(s.name == s.name.strip() for s in schemas), "catalog padding leaked into a name"

    tables = await list_tables(pool, schemas[0].name)
    assert isinstance(tables, list)
    if tables:
        described = await describe_table(pool, tables[0].schema_name, tables[0].name)
        assert described.columns, f"{described.schema_name}.{described.name} has no columns"


async def test_denied_schemas_are_refused(pool: Db2Pool):
    with pytest.raises(Db2Error, match="denylist"):
        await list_tables(pool, "SYSIBM")


async def test_row_cap_and_truncation_flag(pool: Db2Pool):
    result = await pool.query(
        "SELECT TABNAME FROM SYSCAT.TABLES ORDER BY TABNAME", max_rows=5
    )
    assert len(result.rows) <= 5
    assert result.row_limit == 5


async def test_the_connection_refuses_to_write(pool: Db2Pool):
    with pytest.raises(Db2Error):
        await pool.query("CREATE TABLE MCP_DB2_INTEGRATION_CHECK (ID INT)")

"""MCP-level tests: the real server object, a fake database underneath."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from mcp import Client

from mcp_db2 import server as server_module
from mcp_db2.config import Settings
from mcp_db2.db import QueryResult

ENV = {
    "DB2_HOSTNAME": "db2.example.com",
    "DB2_PORT": "50000",
    "DB2_DATABASE": "SAMPLE",
    "DB2_UID": "tester",
    "DB2_PWD": "hunter2",
}


class FakeServerInfo:
    DBMS_NAME = "DB2/DARWIN"
    DBMS_VER = "12.01.0000"


class FakePool:
    """Answers the catalog queries the tools make, keyed on a fragment of the SQL."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.seen: list[str] = []

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def server_info(self) -> FakeServerInfo:
        return FakeServerInfo()

    async def query(
        self,
        sql: str,
        params: Sequence[object] = (),
        *,
        max_rows: int | None = None,
        timeout: int | None = None,
    ) -> QueryResult:
        self.seen.append(sql)
        limit = self.settings.clamp_rows(max_rows)
        if "SYSDUMMY1" in sql:
            return QueryResult(columns=["1", "2"], rows=[["TESTER", "TESTER"]], row_limit=limit)
        if "SYSCAT.SCHEMATA" in sql:
            return QueryResult(
                columns=["SCHEMANAME", "OWNER", "REMARKS"],
                rows=[["DB2INST1", "SYSIBM", None], ["SYSCAT", "SYSIBM", None]],
                row_limit=limit,
            )
        if "SYSCAT.TABLES" in sql and "CARD" in sql and "TABNAME = ?" not in sql:
            return QueryResult(
                columns=["TABSCHEMA", "TABNAME", "TYPE", "CARD", "REMARKS"],
                rows=[["DB2INST1", "EMPLOYEE", "T", 42, "Staff"]],
                row_limit=limit,
            )
        if "SYSCAT.TABLES" in sql:
            return QueryResult(columns=["TYPE", "CARD", "REMARKS"], rows=[["T", 42, "Staff"]])
        if "SYSCAT.COLUMNS" in sql:
            return QueryResult(
                columns=["COLNAME"],
                rows=[["EMPNO", 0, "CHARACTER", 6, 0, "N", None, 1, "", "Employee number"]],
                row_limit=limit,
            )
        if "SYSCAT.REFERENCES" in sql or "SYSCAT.INDEXES" in sql:
            return QueryResult(columns=[], rows=[], row_limit=limit)
        return QueryResult(columns=["A"], rows=[[1]], row_limit=limit)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(server_module, "Db2Pool", FakePool)
    async with Client(server_module.mcp, raise_exceptions=True) as c:
        yield c


@pytest.mark.anyio
async def test_tools_are_listed_and_read_only(client: Client):
    tools = await client.list_tools()
    names = {t.name for t in tools.tools}
    assert names == {
        "db2_server_info",
        "db2_list_schemas",
        "db2_list_tables",
        "db2_describe_table",
        "db2_search_objects",
        "db2_query",
    }
    assert all(t.annotations and t.annotations.read_only_hint for t in tools.tools)


@pytest.mark.anyio
async def test_server_info(client: Client):
    result = await client.call_tool("db2_server_info", {})
    assert result.structured_content["dbms_name"] == "DB2/DARWIN"
    assert result.structured_content["read_only_connection"] is True


@pytest.mark.anyio
async def test_list_schemas_applies_the_denylist(client: Client):
    result = await client.call_tool("db2_list_schemas", {})
    names = [s["name"] for s in result.structured_content["result"]]
    assert names == ["DB2INST1"]  # SYSCAT is excluded by the default denylist


@pytest.mark.anyio
async def test_describe_table(client: Client):
    result = await client.call_tool(
        "db2_describe_table", {"schema": "db2inst1", "table": "employee"}
    )
    described = result.structured_content
    assert described["name"] == "EMPLOYEE"
    assert described["columns"][0]["type"] == "CHARACTER(6)"
    assert described["primary_key"] == ["EMPNO"]


@pytest.mark.anyio
async def test_query_rejects_writes_before_touching_the_database(client: Client):
    result = await client.call_tool("db2_query", {"sql": "DELETE FROM EMPLOYEE"})
    assert result.is_error
    assert "DELETE" in result.content[0].text


@pytest.mark.anyio
async def test_query_returns_rows(client: Client):
    result = await client.call_tool("db2_query", {"sql": "SELECT A FROM T -- comment"})
    outcome = result.structured_content
    assert outcome["rows"] == [[1]]
    assert outcome["sql"] == "SELECT A FROM T"
    assert outcome["truncated"] is False


@pytest.mark.anyio
async def test_denied_schema_is_refused(client: Client):
    result = await client.call_tool("db2_list_tables", {"schema": "SYSCAT"})
    assert result.is_error
    assert "denylist" in result.content[0].text

# mcp-db2

An MCP server that gives an AI client **read-only** access to an IBM Db2 LUW database:
schema discovery plus bounded SELECT queries.

Built on the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (v2,
`MCPServer`) and IBM's [`ibm_db`](https://github.com/ibmdb/python-ibmdb) driver.

> ## ⚠️ Private learning project — not for professional use
>
> This is a personal project written to learn how MCP servers, Db2 access and the surrounding
> tooling fit together. **Do not run it in a professional or production setting.**
>
> It has never been security-reviewed, audited or load-tested by anyone but its author. It is
> maintained irregularly, carries no support and no guarantee of fixes, and its interfaces may
> change at any time. The safety measures described under [Safety model](#safety-model) are
> genuine but were written by one person without external review — treat them as a learning
> exercise, not as an assurance. In particular, the HTTP transport uses a single shared token
> and no TLS.
>
> Anyone pointing this at real company data does so entirely at their own risk.

## Tools

| Tool | What it does |
|---|---|
| `db2_server_info` | Db2 product, version, current schema and user |
| `db2_list_schemas` | Schemas, minus the configured denylist |
| `db2_list_tables` | Tables and views in a schema, with row estimates |
| `db2_describe_table` | Columns, primary key, foreign keys, indexes |
| `db2_search_objects` | Find tables/views/columns by name across schemas |
| `db2_query` | Run one read-only SELECT/WITH/VALUES, capped and timed out |

Plus the resource `db2://schema/{schema}/table/{table}` and the prompt `analyze_table`.

## Setup

```bash
uv sync
./scripts/patch_clidriver_macos.sh    # macOS only, see below
cp .env.example .env                  # then fill in host, database, user, password
uv run python scripts/smoke.py        # verify the database connection before touching MCP
```

### macOS: the clidriver patch

The `ibm_db` wheel bundles IBM's CLI driver. For macOS x86_64 that is v11.5.9, which links against
GNU libstdc++ at `/usr/local/lib/gcc/8/libstdc++.6.dylib`. That path does not exist on a current
macOS, so dyld falls back to Apple's stub `/usr/lib/libstdc++.6.dylib` and `import ibm_db` fails
with `Symbol not found: __ZNKSt8__detail20_Prime_rehash_policy...`.

`scripts/patch_clidriver_macos.sh` repoints that dependency at Homebrew's GNU libstdc++
(`brew install gcc`) **inside the venv only**. Re-run it after any `uv sync` that reinstalls `ibm_db`.

## Running

```bash
uv run mcp-db2                          # stdio (what a local MCP client launches)
uv run mcp-db2 --transport http --port 3001   # streamable HTTP at /mcp
uv run mcp dev src/mcp_db2/server.py    # MCP Inspector
```

Register it with Claude Code:

```bash
claude mcp add db2 -- uv run --directory /path/to/mcp-db2 mcp-db2
```

## Serving over HTTP

The HTTP transport is meant for one deployment shape: a container on an internal host, reachable
from other machines on the same trusted network, with every request carrying a shared bearer token.

```bash
openssl rand -hex 32          # put the result in .env as DB2_HTTP_TOKEN
```

`.env` additionally needs `DB2_HTTP_ALLOWED_HOSTS` (the Host header values clients will send, e.g.
`db2mcp.intern:*`) — without it the Host check is off, and the server says so at startup. Raise
`DB2_POOL_SIZE` too: over HTTP several clients share the pool and each running query holds a
connection.

```bash
docker compose up --build
curl -fsS http://localhost:3001/healthz     # no token needed
```

Register a remote client:

```bash
claude mcp add --transport http db2 http://db2mcp.intern:3001/mcp \
  --header "Authorization: Bearer <token>"
```

The image is **linux/amd64 only** — `ibm_db` publishes no aarch64 manylinux wheel, so on Apple
Silicon it runs emulated (`platform: linux/amd64` is already set in `docker-compose.yml`).

**No TLS.** Token and query results travel in clear text, so this belongs on a trusted segment
only. To change that, put a TLS-terminating reverse proxy in front; the container stays as is.

## Safety model

None of this has been externally reviewed — see the warning at the top of this file. Read-only is
enforced in layers, strongest first:

1. **The database user.** Grant it `CONNECT` and `SELECT`, nothing else. This is the only layer
   that cannot be argued around, and it is the one to get right.
2. **The connection** is opened with `SQL_ATTR_READ_ONLY_CONNECTION` and autocommit off, and every
   query is followed by a rollback.
3. **The statement guard** (`sql_guard.py`) accepts one statement per call, only
   SELECT/WITH/VALUES, and rejects Db2 data-change table references such as
   `SELECT * FROM FINAL TABLE (INSERT ...)`, which write despite starting with SELECT.
4. **Limits**: row cap per call (`DB2_MAX_ROWS`, hard ceiling `DB2_MAX_ROWS_LIMIT`), query timeout
   (`DB2_QUERY_TIMEOUT`), and per-cell truncation (`DB2_MAX_CELL_CHARS`). LOBs are never returned,
   only described.
5. **Schema visibility**: `DB2_SCHEMA_ALLOWLIST` / `DB2_SCHEMA_DENYLIST`.
6. **Over HTTP**, a bearer token (`DB2_HTTP_TOKEN`) gates every request but `/healthz`, and
   `DB2_HTTP_ALLOWED_HOSTS` validates the Host header. The server refuses to start without a token.
7. Tools are annotated `readOnlyHint` — a hint to clients, not a control.

Credentials live in `.env` (gitignored) and are stripped out of driver error messages.

**Prompt injection:** rows returned by `db2_query` are untrusted content. They are data to report
on, never instructions to follow.

## Tests

```bash
uv run pytest -m "not integration"   # no database needed
uv run pytest -m integration         # needs DB2_* pointing at a real database
uv run ruff check .
```

`tests/test_server.py` drives the real server object through an in-memory MCP client with a fake
pool underneath, so tool schemas and error paths are covered without a database.

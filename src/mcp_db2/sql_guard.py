"""Read-only validation for user-supplied SQL.

This is one of several layers, and deliberately the least trusted one: the connection is opened
with SQL_ATTR_READ_ONLY_CONNECTION and the database user should hold nothing but CONNECT and
SELECT. This guard exists to reject obviously-wrong statements early, with a clear message.
"""

from __future__ import annotations

import re

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")

#: A read-only statement can only start with one of these.
ALLOWED_STARTS = frozenset({"SELECT", "WITH", "VALUES"})

#: Keywords that cannot appear in a genuine read-only query at all.
FORBIDDEN_KEYWORDS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "CREATE",
        "ALTER",
        "DROP",
        "GRANT",
        "REVOKE",
        "CALL",
        "COMMIT",
        "ROLLBACK",
        "SAVEPOINT",
        "TRUNCATE",
        "RENAME",
        "DECLARE",
        "EXECUTE",
        "IMPORT",
        "EXPORT",
        "TRANSFER",
        "REORG",
        "RUNSTATS",
        "REFRESH",
        "TERMINATE",
    }
)

#: Two-word phrases that are dangerous, where the first word alone is a plausible column name.
FORBIDDEN_PHRASES = frozenset(
    {
        ("SET", "INTEGRITY"),
        ("SET", "CURRENT"),
        ("SET", "SCHEMA"),
        ("SET", "PATH"),
        ("SET", "SESSION"),
        ("LOCK", "TABLE"),
        ("COMMENT", "ON"),
        ("LOAD", "FROM"),
        ("FLUSH", "PACKAGE"),
    }
)

#: Db2 data-change-table-references: `SELECT * FROM FINAL TABLE (INSERT ...)` writes despite
#: starting with SELECT.
_DATA_CHANGE_FIRST = frozenset({"FINAL", "OLD", "NEW", "INTERMEDIATE"})


class SqlNotAllowed(ValueError):
    """Raised when a statement is rejected by the guard."""


def strip_comments(sql: str) -> str:
    """Remove -- and /* */ comments without touching string literals or quoted identifiers."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'" or ch == '"':
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                out.append(sql[i])
                if sql[i] == quote:
                    # A doubled quote is an escaped quote, not the end of the literal.
                    if i + 1 < n and sql[i + 1] == quote:
                        out.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch == "-" and sql.startswith("--", i):
            while i < n and sql[i] != "\n":
                i += 1
            out.append(" ")
            continue
        if ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_statements(sql: str) -> list[str]:
    """Split on semicolons that are outside string literals and quoted identifiers."""
    statements: list[str] = []
    current: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"":
            quote = ch
            current.append(ch)
            i += 1
            while i < n:
                current.append(sql[i])
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:
                        current.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch == ";":
            statements.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    statements.append("".join(current))
    return [s for s in statements if s.strip()]


def _keywords(sql: str) -> list[str]:
    """Identifier-shaped tokens outside string literals and quoted identifiers, uppercased."""
    words: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"":
            quote = ch
            i += 1
            while i < n:
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        match = _TOKEN.match(sql, i)
        if match:
            words.append(match.group(0).upper())
            i = match.end()
            continue
        i += 1
    return words


def ensure_read_only(sql: str) -> str:
    """Validate `sql` and return it normalized (comments stripped, no trailing semicolon).

    Raises SqlNotAllowed with a message the model can act on.
    """
    if not sql or not sql.strip():
        raise SqlNotAllowed("Empty statement.")

    cleaned = strip_comments(sql).strip()
    statements = _split_statements(cleaned)
    if not statements:
        raise SqlNotAllowed("Statement contains nothing but comments.")
    if len(statements) > 1:
        raise SqlNotAllowed(
            f"Only one statement per call; found {len(statements)}. "
            "Semicolon-separated batches are not allowed."
        )

    statement = statements[0].strip()
    words = _keywords(statement)
    if not words:
        raise SqlNotAllowed("No SQL keywords found.")

    if words[0] not in ALLOWED_STARTS:
        raise SqlNotAllowed(
            f"Only read-only statements are allowed; this one starts with {words[0]}. "
            f"Use one of: {', '.join(sorted(ALLOWED_STARTS))}."
        )

    forbidden = sorted(set(words) & FORBIDDEN_KEYWORDS)
    if forbidden:
        raise SqlNotAllowed(f"Statement contains forbidden keyword(s): {', '.join(forbidden)}.")

    for first, second in zip(words, words[1:], strict=False):
        if (first, second) in FORBIDDEN_PHRASES:
            raise SqlNotAllowed(f"Statement contains forbidden clause: {first} {second}.")
        if first in _DATA_CHANGE_FIRST and second in {"TABLE", "ROW"}:
            raise SqlNotAllowed(
                f"'{first} {second}' is a data-change table reference — it modifies data "
                "even though the statement starts with SELECT."
            )

    return statement

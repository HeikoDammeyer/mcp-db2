"""Schema discovery via the Db2 system catalog (SYSCAT.*)."""

from __future__ import annotations

from .db import Db2Error, Db2Pool
from .types import (
    ColumnInfo,
    ForeignKeyInfo,
    IndexInfo,
    ObjectMatch,
    SchemaInfo,
    TableDescription,
    TableInfo,
)

#: Types that carry a length, and those that carry precision and scale.
_LENGTH_TYPES = {
    "CHARACTER",
    "VARCHAR",
    "LONG VARCHAR",
    "CLOB",
    "BLOB",
    "DBCLOB",
    "GRAPHIC",
    "VARGRAPHIC",
    "LONG VARGRAPHIC",
    "BINARY",
    "VARBINARY",
}
_PRECISION_TYPES = {"DECIMAL", "NUMERIC", "DECFLOAT"}

#: SYSCAT.REFERENCES rule codes.
_RULES = {"A": "NO ACTION", "C": "CASCADE", "N": "SET NULL", "R": "RESTRICT"}


def _text(value: object) -> str | None:
    """Catalog CHAR columns come back space-padded; empty means 'not set'."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required(value: object) -> str:
    return _text(value) or ""


def render_type(typename: str, length: int | None, scale: int | None) -> str:
    """Turn SYSCAT.COLUMNS' TYPENAME/LENGTH/SCALE into the type as you would write it."""
    name = typename.strip()
    if name in _PRECISION_TYPES and length:
        return f"{name}({length},{scale or 0})" if scale else f"{name}({length})"
    if name in _LENGTH_TYPES and length:
        return f"{name}({length})"
    return name


def _check_schema(pool: Db2Pool, schema: str) -> str:
    name = schema.strip().upper()
    if not name:
        raise Db2Error("No schema given.")
    if not pool.settings.schema_visible(name):
        raise Db2Error(f"Schema {name} is excluded by this server's schema allow/denylist.")
    return name


async def list_schemas(pool: Db2Pool) -> list[SchemaInfo]:
    visible, params = pool.settings.schema_sql_filter("SCHEMANAME")
    result = await pool.query(
        f"SELECT SCHEMANAME, OWNER, REMARKS FROM SYSCAT.SCHEMATA WHERE {visible} "
        "ORDER BY SCHEMANAME",
        params,
        max_rows=pool.settings.max_rows_limit,
    )
    return [
        SchemaInfo(name=_required(name), owner=_text(owner), remarks=_text(remarks))
        for name, owner, remarks in result.rows
        if pool.settings.schema_visible(str(name))
    ]


async def list_tables(
    pool: Db2Pool,
    schema: str,
    name_pattern: str | None = None,
    types: str = "TV",
    max_rows: int | None = None,
) -> list[TableInfo]:
    """List tables/views in one schema. `name_pattern` uses SQL LIKE syntax (% and _)."""
    schema = _check_schema(pool, schema)
    type_list = [t for t in types.upper() if t.isalpha()] or ["T", "V"]
    placeholders = ", ".join("?" for _ in type_list)
    sql = (
        "SELECT TABSCHEMA, TABNAME, TYPE, CARD, REMARKS FROM SYSCAT.TABLES "
        f"WHERE TABSCHEMA = ? AND TYPE IN ({placeholders})"
    )
    params: list[object] = [schema, *type_list]
    if name_pattern:
        sql += " AND TABNAME LIKE ?"
        params.append(name_pattern.upper())
    sql += " ORDER BY TABNAME"

    result = await pool.query(sql, params, max_rows=max_rows)
    return [
        TableInfo(
            schema_name=_required(tabschema),
            name=_required(tabname),
            type=_required(type_),
            row_estimate=None if card is None or int(card) < 0 else int(card),
            remarks=_text(remarks),
        )
        for tabschema, tabname, type_, card, remarks in result.rows
    ]


async def describe_table(pool: Db2Pool, schema: str, table: str) -> TableDescription:
    schema = _check_schema(pool, schema)
    table = table.strip().upper()

    head = await pool.query(
        "SELECT TYPE, CARD, REMARKS FROM SYSCAT.TABLES WHERE TABSCHEMA = ? AND TABNAME = ?",
        [schema, table],
        max_rows=1,
    )
    if not head.rows:
        raise Db2Error(f"No table or view {schema}.{table} — check db2_list_tables for spelling.")
    type_, card, remarks = head.rows[0]

    columns = await _columns(pool, schema, table)
    return TableDescription(
        schema_name=schema,
        name=table,
        type=_required(type_),
        remarks=_text(remarks),
        row_estimate=None if card is None or int(card) < 0 else int(card),
        columns=columns,
        primary_key=[
            c.name
            for c in sorted(
                (c for c in columns if c.primary_key_position),
                key=lambda c: c.primary_key_position or 0,
            )
        ],
        foreign_keys=await _foreign_keys(pool, schema, table),
        indexes=await _indexes(pool, schema, table),
    )


async def _columns(pool: Db2Pool, schema: str, table: str) -> list[ColumnInfo]:
    result = await pool.query(
        "SELECT COLNAME, COLNO, TYPENAME, LENGTH, SCALE, NULLS, DEFAULT, KEYSEQ, GENERATED, "
        "REMARKS FROM SYSCAT.COLUMNS WHERE TABSCHEMA = ? AND TABNAME = ? ORDER BY COLNO",
        [schema, table],
        max_rows=pool.settings.max_rows_limit,
    )
    return [
        ColumnInfo(
            name=_required(colname),
            position=int(colno) + 1,
            type=render_type(str(typename), _as_int(length), _as_int(scale)),
            nullable=_required(nulls) == "Y",
            default=_text(default),
            primary_key_position=_as_int(keyseq) or None,
            generated=bool(_text(generated)),
            remarks=_text(remarks),
        )
        for colname, colno, typename, length, scale, nulls, default, keyseq, generated, remarks in (
            result.rows
        )
    ]


async def _foreign_keys(pool: Db2Pool, schema: str, table: str) -> list[ForeignKeyInfo]:
    refs = await pool.query(
        "SELECT CONSTNAME, REFTABSCHEMA, REFTABNAME, REFKEYNAME, DELETERULE, UPDATERULE "
        "FROM SYSCAT.REFERENCES WHERE TABSCHEMA = ? AND TABNAME = ? ORDER BY CONSTNAME",
        [schema, table],
        max_rows=pool.settings.max_rows_limit,
    )
    keys: list[ForeignKeyInfo] = []
    for constname, refschema, reftable, refkey, delrule, updrule in refs.rows:
        keys.append(
            ForeignKeyInfo(
                name=_required(constname),
                columns=await _key_columns(pool, schema, table, _required(constname)),
                references_schema=_required(refschema),
                references_table=_required(reftable),
                references_columns=await _key_columns(
                    pool, _required(refschema), _required(reftable), _required(refkey)
                ),
                delete_rule=_RULES.get(_required(delrule), _required(delrule)),
                update_rule=_RULES.get(_required(updrule), _required(updrule)),
            )
        )
    return keys


async def _key_columns(pool: Db2Pool, schema: str, table: str, constname: str) -> list[str]:
    result = await pool.query(
        "SELECT COLNAME FROM SYSCAT.KEYCOLUSE "
        "WHERE TABSCHEMA = ? AND TABNAME = ? AND CONSTNAME = ? ORDER BY COLSEQ",
        [schema, table, constname],
        max_rows=pool.settings.max_rows_limit,
    )
    return [_required(name) for (name,) in result.rows]


async def _indexes(pool: Db2Pool, schema: str, table: str) -> list[IndexInfo]:
    result = await pool.query(
        "SELECT I.INDNAME, I.UNIQUERULE, C.COLNAME, C.COLORDER FROM SYSCAT.INDEXES I "
        "JOIN SYSCAT.INDEXCOLUSE C ON C.INDSCHEMA = I.INDSCHEMA AND C.INDNAME = I.INDNAME "
        "WHERE I.TABSCHEMA = ? AND I.TABNAME = ? ORDER BY I.INDNAME, C.COLSEQ",
        [schema, table],
        max_rows=pool.settings.max_rows_limit,
    )
    indexes: dict[str, IndexInfo] = {}
    for indname, uniquerule, colname, colorder in result.rows:
        name = _required(indname)
        index = indexes.get(name)
        if index is None:
            # U = unique, P = primary key, D = duplicates allowed.
            index = IndexInfo(name=name, unique=_required(uniquerule) in {"U", "P"}, columns=[])
            indexes[name] = index
        order = {"A": "ASC", "D": "DESC"}.get(_required(colorder), "")
        index.columns.append(f"{_required(colname)} {order}".strip())
    return list(indexes.values())


async def search_objects(
    pool: Db2Pool, pattern: str, max_rows: int | None = None
) -> list[ObjectMatch]:
    """Find tables, views and columns whose name matches a LIKE pattern."""
    like = pattern.upper()
    if "%" not in like and "_" not in like:
        like = f"%{like}%"
    limit = pool.settings.clamp_rows(max_rows)
    visible, visible_params = pool.settings.schema_sql_filter("TABSCHEMA")

    tables = await pool.query(
        "SELECT TABSCHEMA, TABNAME, TYPE, REMARKS FROM SYSCAT.TABLES "
        f"WHERE TABNAME LIKE ? AND TYPE IN ('T', 'V') AND {visible} "
        "ORDER BY TABSCHEMA, TABNAME",
        [like, *visible_params],
        max_rows=limit,
    )
    matches = [
        ObjectMatch(
            schema_name=_required(s),
            table=_required(t),
            type=_required(ty),
            remarks=_text(r),
        )
        for s, t, ty, r in tables.rows
        if pool.settings.schema_visible(str(s))
    ]

    column_visible, column_params = pool.settings.schema_sql_filter("C.TABSCHEMA")
    columns = await pool.query(
        "SELECT C.TABSCHEMA, C.TABNAME, T.TYPE, C.COLNAME, C.REMARKS FROM SYSCAT.COLUMNS C "
        "JOIN SYSCAT.TABLES T ON T.TABSCHEMA = C.TABSCHEMA AND T.TABNAME = C.TABNAME "
        f"WHERE C.COLNAME LIKE ? AND T.TYPE IN ('T', 'V') AND {column_visible} "
        "ORDER BY C.TABSCHEMA, C.TABNAME, C.COLNAME",
        [like, *column_params],
        max_rows=limit,
    )
    matches.extend(
        ObjectMatch(
            schema_name=_required(s),
            table=_required(t),
            type=_required(ty),
            column=_required(c),
            remarks=_text(r),
        )
        for s, t, ty, c, r in columns.rows
        if pool.settings.schema_visible(str(s))
    )
    return matches


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

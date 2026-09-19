"""Turn Db2 column values into something JSON-serializable and model-readable."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

JsonValue = str | int | float | bool | None


def cell(value: object, max_chars: int) -> JsonValue:
    """Convert one fetched value. Decimals become strings so no precision is lost."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<binary, {len(bytes(value))} bytes>"
    text = value if isinstance(value, str) else str(value)
    if len(text) > max_chars:
        return f"{text[:max_chars]}… <truncated, {len(text)} chars total>"
    return text


def row(values: tuple[object, ...], max_chars: int) -> list[JsonValue]:
    return [cell(v, max_chars) for v in values]

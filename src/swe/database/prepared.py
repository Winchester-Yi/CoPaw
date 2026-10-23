# -*- coding: utf-8 -*-
"""Bounded binary inserts for fixed, application-owned SQL templates."""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Iterable

_MAX_PARAMETERS = 65535
_CACHE_SIZE = 4


@dataclass(frozen=True, eq=False)
class PreparedInsert:
    """Trusted INSERT SQL parts; identifiers must never come from user input.

    Keep one module-level instance per INSERT so physical connections can reuse
    its prepared statements. Only values belong in the row parameters.
    """

    prefix: str
    row_template: str
    suffix: str = ""
    columns: int = field(init=False)

    def __post_init__(self):
        if not re.fullmatch(r"\(\s*%s(?:\s*,\s*%s)*\s*\)", self.row_template):
            raise ValueError("row_template must contain only %s placeholders")
        columns = self.row_template.count("%s")
        if columns > _MAX_PARAMETERS:
            raise ValueError("Too many prepared columns")
        object.__setattr__(self, "columns", columns)


def _value_bytes(value: Any) -> int:
    """Conservative binary value bound, including length prefixes."""
    if value is None:
        return 0
    if isinstance(value, (bool, int, float)):
        return 8
    if isinstance(value, (datetime, date, time, timedelta)):
        return 16
    if isinstance(value, (str, Decimal)):
        return len(str(value).encode("utf-8")) + 9
    if isinstance(value, (bytes, bytearray)):
        return len(value) + 9
    raise TypeError(
        f"Unsupported prepared parameter type: {type(value).__name__}",
    )


async def _execute_chunk(session, template, rows, params):
    cache = session._prepared_cache
    key = (template, rows)
    cached = cache.pop(key, None)
    if cached is None:
        if len(cache) >= _CACHE_SIZE:
            _old_key, (_sql, old_cursor) = cache.popitem(last=False)
            await session._run(old_cursor.close())
        # The fixed placeholder-only template can be translated directly.
        # This avoids Connector/Python's quadratic %s regexp on large shapes.
        row_sql = template.row_template.replace("%s", "?")
        sql = template.prefix + ",".join([row_sql] * rows) + template.suffix
        cursor = await session._run(session._connection.cursor(prepared=True))
        cached = (sql, cursor)
    cache[key] = cached
    sql, cursor = cached
    # Connector/Python checks SQL object identity, so use the cached str object.
    # prepared.executemany loops execute per row and must not be used here.
    await session._run(cursor.execute(sql, tuple(params)))


async def execute_prepared(
    session,
    template: PreparedInsert,
    rows: Iterable[tuple],
    *,
    max_rows: int = 100,
    max_bytes: int = 256 * 1024,
) -> int:
    """Insert a lazy row stream; transaction ownership belongs to the facade.

    Return processed rows, independent of MySQL's upsert affected-row count.
    A row can exceed the caller's batch budget but never the server packet bound.
    """
    if max_rows <= 0 or max_bytes <= 0:
        raise ValueError("Prepared batch limits must be positive")
    row_limit = min(max_rows, _MAX_PARAMETERS // template.columns)
    # Reserve protocol headers and use a conservative fraction of the server's
    # packet limit; count both prepare SQL and execution values for simplicity.
    server_limit = session._entry.max_allowed_packet
    packet_budget = max(0, min(server_limit - 1024, server_limit * 9 // 10))
    batch_budget = min(max_bytes, packet_budget)
    base_bytes = len((template.prefix + template.suffix).encode("utf-8")) + 16
    sql_row_bytes = len(template.row_template.encode("utf-8")) + 1
    params = []
    count = 0
    batch_bytes = base_bytes
    confirmed = 0
    for row in rows:
        if len(row) != template.columns:
            raise ValueError(f"Expected {template.columns} prepared columns")
        # Two type bytes plus at most one null-bitmap byte per parameter.
        row_bytes = sql_row_bytes + 3 * len(row) + sum(map(_value_bytes, row))
        if base_bytes + row_bytes > packet_budget:
            raise ValueError("Prepared row exceeds max_allowed_packet budget")
        if count and (
            count >= row_limit or batch_bytes + row_bytes > batch_budget
        ):
            await _execute_chunk(session, template, count, params)
            confirmed += count
            params = []
            count = 0
            batch_bytes = base_bytes
        params.extend(row)
        count += 1
        batch_bytes += row_bytes
        if count >= row_limit:
            await _execute_chunk(session, template, count, params)
            confirmed += count
            params = []
            count = 0
            batch_bytes = base_bytes
    if count:
        await _execute_chunk(session, template, count, params)
        confirmed += count
    return confirmed

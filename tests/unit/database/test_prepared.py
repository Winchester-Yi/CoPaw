# -*- coding: utf-8 -*-
"""Binary multi-row insert contracts, using the official prepared cursor."""

from collections import OrderedDict
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("mysql.connector.aio")

from mysql.connector.aio import MySQLConnection
from mysql.connector.aio.charsets import charsets
from mysql.connector.aio.cursor import MySQLCursorPrepared
from mysql.connector.protocol import MySQLProtocol


class ProtocolSession:
    def __init__(self, packet_limit=1024 * 1024):
        charsets.set_mysql_major_version(8)
        self._connection = MySQLConnection()
        self._prepared_cache = OrderedDict()
        self._entry = SimpleNamespace(max_allowed_packet=packet_limit)
        self.prepares = []
        self.executes = []
        self.closed = []
        self.packets = []
        self.encode_packets = True
        self._connection.cmd_stmt_prepare = self.prepare
        self._connection.cmd_stmt_execute = self.execute
        self._connection.cmd_stmt_reset = AsyncMock()
        self._connection.cmd_stmt_close = self.close_statement
        self._connection.cursor = self.cursor

    async def cursor(self, **kwargs):
        assert kwargs == {"prepared": True}
        return MySQLCursorPrepared(self._connection)

    async def prepare(self, sql, **kwargs):
        self.prepares.append(sql)
        return {
            "statement_id": len(self.prepares),
            "parameters": [None] * sql.count(b"?"),
            "columns": [],
        }

    async def execute(self, statement_id, data, parameters, **kwargs):
        self.executes.append((statement_id, tuple(data)))
        if self.encode_packets:
            self.packets.append(
                MySQLProtocol().make_stmt_execute(
                    statement_id,
                    data,
                    parameters,
                ),
            )
        return {"affected_rows": 0, "insert_id": 0, "warning_count": 0}

    async def close_statement(self, statement_id, **kwargs):
        self.closed.append(statement_id)

    async def _run(self, operation):
        return await operation


def template():
    from swe.database.prepared import PreparedInsert

    return PreparedInsert(
        "INSERT INTO example (id, payload) VALUES ",
        "(%s,%s)",
    )


@pytest.mark.asyncio
async def test_official_cursor_one_execute_per_chunk_and_reuses_shape(
    monkeypatch,
):
    from swe.database.prepared import execute_prepared

    def reject_placeholder_regexp(*args, **kwargs):
        raise AssertionError("Fixed templates must use native ? placeholders")

    monkeypatch.setattr(
        "mysql.connector.aio.cursor.re",
        SimpleNamespace(sub=reject_placeholder_regexp),
    )

    session = ProtocolSession()
    statement = template()
    payload = "中文😀'\\\n\x00"
    rows = [(i, payload) for i in range(5)]
    assert (
        await execute_prepared(session, statement, iter(rows), max_rows=2) == 5
    )
    assert len(session.executes) == 3
    assert len(session.prepares) == 2  # Two rows and the final single row.
    assert all(b"%s" not in sql for sql in session.prepares)
    assert all(payload.encode() not in sql for sql in session.prepares)
    assert all(payload.encode() in packet for packet in session.packets)
    assert (
        await execute_prepared(session, statement, iter(rows[:2]), max_rows=2)
        == 2
    )
    assert len(session.prepares) == 2
    assert session._connection.cmd_stmt_reset.await_count == 4


@pytest.mark.asyncio
async def test_prepared_cache_is_per_connection_and_lru_bounded():
    from swe.database.prepared import execute_prepared

    session = ProtocolSession()
    statement = template()
    for size in range(1, 6):
        await execute_prepared(
            session,
            statement,
            [(i, "v") for i in range(size)],
        )
    assert len(session._prepared_cache) == 4
    assert session.closed == [1]
    second = ProtocolSession()
    await execute_prepared(second, statement, [(0, "v")])
    assert len(second.prepares) == 1


@pytest.mark.asyncio
async def test_row_and_byte_limits_split_without_serializing_all_input():
    from swe.database.prepared import execute_prepared

    session = ProtocolSession()

    def rows():
        yield (1, "x" * 100)
        yield (2, "x" * 100)
        # First chunk must be sent before a third row is serialized.
        assert len(session.executes) == 1
        yield (3, "x" * 100)

    assert (
        await execute_prepared(session, template(), rows(), max_bytes=220) == 3
    )
    assert len(session.executes) == 3


@pytest.mark.asyncio
async def test_oversized_single_row_allowed_only_below_server_packet_limit():
    from swe.database.prepared import execute_prepared

    session = ProtocolSession(packet_limit=4096)
    assert (
        await execute_prepared(
            session,
            template(),
            [(1, "x" * 500)],
            max_bytes=100,
        )
        == 1
    )
    with pytest.raises(ValueError, match="max_allowed_packet"):
        await execute_prepared(session, template(), [(2, "x" * 5000)])
    assert len(session.executes) == 1


@pytest.mark.asyncio
async def test_parameter_limit_splits_even_with_large_row_limit():
    from swe.database.prepared import execute_prepared

    session = ProtocolSession(packet_limit=20 * 1024 * 1024)

    # Cardinality only: avoid the driver's expensive SQL placeholder regexp
    # for this synthetic maximum-size statement (normal Tracing batches <=100).
    async def record(sql, params):
        session.executes.append((0, params))

    session._connection.cursor = AsyncMock(
        return_value=SimpleNamespace(execute=record),
    )
    rows = ((i, None) for i in range(32768))
    assert (
        await execute_prepared(
            session,
            template(),
            rows,
            max_rows=40000,
            max_bytes=10**7,
        )
        == 32768
    )
    assert [len(params) for _, params in session.executes] == [65534, 2]


@pytest.mark.asyncio
async def test_binary_types_and_empty_input():
    from swe.database.prepared import PreparedInsert, execute_prepared

    session = ProtocolSession()
    statement = PreparedInsert(
        "INSERT INTO example VALUES ",
        "(%s,%s,%s,%s,%s,%s)",
    )
    row = (
        None,
        True,
        Decimal("1.25"),
        datetime(2026, 10, 16, 1, 2, 3, 123456),
        b"\x00\\",
        "😀",
    )
    assert await execute_prepared(session, statement, []) == 0
    assert session.prepares == []
    assert await execute_prepared(session, statement, [row]) == 1
    assert session.executes[0][1] == row


@pytest.mark.asyncio
async def test_wrong_row_width_and_invalid_budgets_fail_before_execute():
    from swe.database.prepared import execute_prepared

    session = ProtocolSession()
    with pytest.raises(ValueError, match="columns"):
        await execute_prepared(session, template(), [(1,)])
    with pytest.raises(ValueError, match="positive"):
        await execute_prepared(session, template(), [(1, "v")], max_rows=0)
    assert session.executes == []

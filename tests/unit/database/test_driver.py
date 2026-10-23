# -*- coding: utf-8 -*-
"""Do not let the official prepared cursor hide failed handle disposal."""

import pytest

pytest.importorskip("mysql.connector.aio")

from mysql.connector.aio import MySQLConnection
from mysql.connector.aio.charsets import charsets
from mysql.connector.aio.cursor import MySQLCursorPrepared
from mysql.connector.aio.cursor import MySQLCursorBufferedDict
from mysql.connector.conversion import MySQLConverter
from mysql.connector.errors import OperationalError
from unittest.mock import AsyncMock
from datetime import datetime
from decimal import Decimal


@pytest.mark.asyncio
async def test_prepared_cursor_close_failure_reaches_managed_session(
    monkeypatch,
):
    from swe.database.driver import ManagedMySQLConnection

    charsets.set_mysql_major_version(8)
    connection = ManagedMySQLConnection()

    async def fail_close(self, statement_id, **kwargs):
        raise OperationalError("statement close transport failed")

    async def prepare(sql, **kwargs):
        return {"statement_id": 1, "parameters": [None], "columns": []}

    async def reset(statement_id, **kwargs):
        pass

    async def execute(statement_id, **kwargs):
        return {"affected_rows": 1, "insert_id": 0, "warning_count": 0}

    monkeypatch.setattr(MySQLConnection, "cmd_stmt_close", fail_close)
    connection.cmd_stmt_prepare = prepare
    connection.cmd_stmt_reset = reset
    connection.cmd_stmt_execute = execute
    cursor = MySQLCursorPrepared(connection)
    await cursor.execute("INSERT INTO t VALUES (?)", ("value",))
    with pytest.raises(
        RuntimeError,
        match="prepared statement close",
    ) as failure:
        await cursor.close()
    assert isinstance(failure.value.__cause__, OperationalError)


@pytest.mark.asyncio
async def test_official_text_cursor_parameter_types_percent_and_write_results():
    charsets.set_mysql_major_version(8)
    connection = MySQLConnection()
    connection.converter_class = MySQLConverter
    connection.get_sql_mode = AsyncMock(return_value="")
    connection.cmd_query = AsyncMock(
        return_value={
            "affected_rows": 2,
            "insert_id": 19,
            "warning_count": 0,
        },
    )
    cursor = MySQLCursorBufferedDict(connection)
    await cursor.execute(
        "UPDATE t SET text=%s, amount=%s, created=%s, raw_data=%s, optional=%s "
        "WHERE text LIKE %s LIMIT %s",
        (
            "中文😀'\\\n\x00",
            Decimal("1.25"),
            datetime(2026, 10, 16, 1, 2, 3, 123456),
            b"\x00\\",
            None,
            "%value%",
            5,
        ),
    )
    sql = connection.cmd_query.call_args.args[0]
    assert "中文😀".encode() in sql
    assert b"amount=1.25" in sql
    assert b"2026-10-16 01:02:03.123456" in sql
    assert b"optional=NULL" in sql
    assert b"LIKE '%value%' LIMIT 5" in sql
    assert cursor.rowcount == 2
    assert cursor.lastrowid == 19
    await cursor.execute("SELECT DATE_FORMAT(created, '%Y-%m'), 10 % 3", ())
    assert b"'%Y-%m'" in connection.cmd_query.call_args.args[0]
    await cursor.execute("SELECT 1", None)
    assert connection.cmd_query.call_args.args[0] == b"SELECT 1"

# -*- coding: utf-8 -*-
"""Opt-in isolated MySQL/TDSQL smoke test; creates and drops one unique table."""

import os
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from swe.database import DatabaseConfig, DatabaseConnection
from swe.database.prepared import PreparedInsert


@pytest.mark.asyncio
async def test_official_mysql_crud_transactions_and_prepared():
    if not os.environ.get("SWE_TEST_DB_HOST") or not os.environ.get(
        "SWE_TEST_DB_NAME",
    ):
        pytest.skip(
            "Requires explicit isolated SWE_TEST_DB_HOST and SWE_TEST_DB_NAME",
        )
    pytest.importorskip("mysql.connector.aio")
    db = DatabaseConnection(
        DatabaseConfig(
            host=os.environ["SWE_TEST_DB_HOST"],
            port=int(os.environ.get("SWE_TEST_DB_PORT", "3306")),
            user=os.environ.get("SWE_TEST_DB_USER", "root"),
            password=os.environ.get("SWE_TEST_DB_PASSWORD", ""),
            database=os.environ["SWE_TEST_DB_NAME"],
            min_connections=0,
            max_connections=2,
        ),
    )
    # Identifier is generated locally; no user-controlled SQL identifiers.
    table = "swe_driver_test_" + uuid4().hex
    await db.connect()
    try:
        await db.execute(
            f"CREATE TABLE {table} (id BIGINT AUTO_INCREMENT PRIMARY KEY, payload TEXT, amount DECIMAL(12,2), created DATETIME(6), raw_data BLOB)",
        )
        statement = PreparedInsert(
            f"INSERT INTO {table} (payload, amount, created, raw_data) VALUES ",
            "(%s,%s,%s,%s)",
        )
        payload = "中文😀'\\\n\x00"
        created = datetime(2026, 10, 16, 1, 2, 3, 123456)
        async with db.named_lock(table):
            assert (
                await db.insert_prepared(
                    statement,
                    [(payload, Decimal("1.25"), created, b"\x00\\")] * 3,
                    max_rows=2,
                )
                == 3
            )
        rows = await db.fetch_all(f"SELECT * FROM {table} ORDER BY id")
        assert len(rows) == 3
        assert rows[0]["payload"] == payload
        assert rows[0]["amount"] == Decimal("1.25")
        assert rows[0]["created"] == created
        assert rows[0]["raw_data"] == b"\x00\\"
        with pytest.raises(ValueError, match="abort"):
            async with db.transaction() as tx:
                await tx.execute(f"DELETE FROM {table}")
                raise ValueError("abort")
        assert (await db.fetch_one(f"SELECT COUNT(*) AS count FROM {table}"))[
            "count"
        ] == 3
        inserted = await db.insert(
            f"INSERT INTO {table} (payload) VALUES (%s)",
            ("four",),
        )
        result = await db.execute_result(
            f"UPDATE {table} SET payload = %s WHERE id = %s",
            ("changed", inserted),
        )
        assert result.affected_rows == 1
        assert (
            await db.execute(f"DELETE FROM {table} WHERE id = %s", (inserted,))
            == 1
        )
    finally:
        try:
            await db.execute(f"DROP TABLE IF EXISTS {table}")
        finally:
            await db.close()

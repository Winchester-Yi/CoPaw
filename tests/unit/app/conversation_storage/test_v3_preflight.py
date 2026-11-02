# -*- coding: utf-8 -*-
# pylint: disable=use-dict-literal
"""The direct importer never receives a DDL-capable database facade."""

from unittest.mock import AsyncMock

import pytest


def test_direct_facade_rejects_ddl():
    from swe.app.conversation_storage.migration.preflight import DMLDatabase

    for sql in (
        "CREATE TABLE x(id INT)",
        "ALTER TABLE x ADD y INT",
        "DROP TABLE x",
        "RENAME TABLE x TO y",
        "TRUNCATE x",
        "SELECT 1; DROP TABLE x",
        "/* hidden */ CREATE TABLE x(id INT)",
    ):
        with pytest.raises(ValueError, match="DML"):
            DMLDatabase.check(sql)


@pytest.mark.asyncio
async def test_direct_facade_forwards_select_and_insert():
    from swe.app.conversation_storage.migration.preflight import DMLDatabase

    raw = AsyncMock()
    raw.config = object()
    db = DMLDatabase(raw)
    await db.fetch_one("SELECT DATABASE()")
    await db.insert(
        "INSERT INTO swe_conversation_migration_jobs (migration_id) VALUES (%s)",
        ("x",),
    )
    raw.fetch_one.assert_awaited_once_with("SELECT DATABASE()", None)
    raw.insert.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_facade_wraps_configless_session_and_nested_transaction():
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from swe.app.conversation_storage.migration.preflight import DMLDatabase

    session = SimpleNamespace(fetch_one=AsyncMock(return_value={"n": 1}))

    @asynccontextmanager
    async def transaction():
        yield session

    raw = SimpleNamespace(
        config=object(),
        is_connected=True,
        transaction=transaction,
    )
    async with DMLDatabase(raw).transaction() as tx:
        assert tx.config is raw.config
        assert tx.is_connected
        assert await tx.fetch_one("SELECT 1") == {"n": 1}
        with pytest.raises(ValueError, match="DML"):
            await tx.fetch_one("CREATE TABLE x(i INT)")
        async with tx.transaction() as nested:
            assert nested.config is raw.config
            await nested.fetch_one("SELECT 2")
    assert session.fetch_one.await_count == 2


@pytest.mark.parametrize(
    "defect",
    [
        "type",
        "nullable",
        "default",
        "extra",
        "encoding",
        "generated",
        "index",
        "missing",
    ],
)
async def test_control_schema_rejects_contract_drift(monkeypatch, defect):
    from swe.app.conversation_storage.migration import preflight
    from copy import deepcopy

    rows = control_columns("swe_conversation_migration_entries")
    if defect == "type":
        rows[0]["COLUMN_TYPE"] = "int"
    if defect == "nullable":
        rows[0]["IS_NULLABLE"] = "YES"
    if defect == "default":
        rows[0]["COLUMN_DEFAULT"] = "0"
    if defect == "extra":
        rows[0]["EXTRA"] = ""
    if defect == "encoding":
        rows[4]["COLLATION_NAME"] = "ascii_general_ci"
    if defect == "generated":
        rows[0]["GENERATION_EXPRESSION"] = "1+1"
    if defect == "missing":
        rows.pop()
    indexes = deepcopy(
        preflight.CONTROL_INDEXES["swe_conversation_migration_entries"],
    )
    if defect == "index":
        indexes["uq_conv_migration_source"] = (
            0,
            ("entry_kind", "migration_id", "source_key"),
        )
    monkeypatch.setattr(preflight, "_columns", AsyncMock(return_value=rows))
    monkeypatch.setattr(preflight, "_indexes", AsyncMock(return_value=indexes))
    with pytest.raises(ValueError, match="migration"):
        await preflight.check_control_schema(
            object(),
            "target",
            tables=["swe_conversation_migration_entries"],
        )


def control_columns(table):
    from swe.app.conversation_storage.migration import preflight

    rows = []
    for name, (kind, nullable, extra) in preflight.CONTROL_COLUMNS[
        table
    ].items():
        charset = (
            "ascii"
            if name == "state"
            else ("utf8mb4" if kind == "mediumtext" else None)
        )
        rows.append(
            dict(
                COLUMN_NAME=name,
                COLUMN_TYPE=kind,
                IS_NULLABLE=nullable,
                COLUMN_DEFAULT=None,
                EXTRA=extra,
                GENERATION_EXPRESSION="",
                CHARACTER_SET_NAME=charset,
                COLLATION_NAME=(
                    "ascii_bin"
                    if name == "state"
                    else ("utf8mb4_general_ci" if charset else None)
                ),
            ),
        )
    return rows


async def test_control_schema_matches_sql_contract(monkeypatch):
    from swe.app.conversation_storage.migration import preflight

    async def columns(_, schema, table):
        return control_columns(table)

    async def indexes(_, schema, table):
        return preflight.CONTROL_INDEXES[table]

    monkeypatch.setattr(preflight, "_columns", columns)
    monkeypatch.setattr(preflight, "_indexes", indexes)
    await preflight.check_control_schema(object(), "target")

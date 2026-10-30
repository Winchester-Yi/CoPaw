# -*- coding: utf-8 -*-
from __future__ import annotations

import os

import pytest

from swe.database.config import DatabaseConfig, get_database_config
from swe.database.connection import DatabaseConnection


@pytest.fixture
def conversation_test_database_config():
    host = os.environ.get("SWE_TEST_DB_HOST")
    database = os.environ.get("SWE_TEST_DB_NAME")
    if not host or not database:
        pytest.skip(
            "conversation target database not configured; not acceptance evidence",
        )
    if database == get_database_config().database:
        pytest.fail(
            "SWE_TEST_DB_NAME must not match the configured application schema",
        )
    if not (database.startswith("test_") or database.endswith("_test")):
        pytest.fail("SWE_TEST_DB_NAME must identify a dedicated test schema")

    return DatabaseConfig(
        host=host,
        port=int(os.environ.get("SWE_TEST_DB_PORT", "3306")),
        user=os.environ.get("SWE_TEST_DB_USER", "root"),
        password=os.environ.get("SWE_TEST_DB_PASSWORD", ""),
        database=database,
        min_connections=1,
        max_connections=2,
    )


@pytest.fixture
async def conversation_test_database(conversation_test_database_config):
    connection = DatabaseConnection(conversation_test_database_config)
    await connection.connect()
    try:
        current = await connection.fetch_one(
            "SELECT DATABASE() AS database_name",
        )
        if (
            not current
            or current.get("database_name") != connection.config.database
        ):
            pytest.fail("connected schema does not match SWE_TEST_DB_NAME")
        tables = await connection.fetch_all("SHOW TABLES")
        existing = {next(iter(row.values()), None) for row in tables if row}
        if existing:
            pytest.fail("conversation test schema must be empty before setup")
        yield connection
    finally:
        await connection.close()

# -*- coding: utf-8 -*-
"""Explicit MySQL opt-in; each test owns a newly generated empty schema."""

from __future__ import annotations

import os
from pathlib import Path
import re
from uuid import uuid4

import pytest

from swe.database.config import DatabaseConfig, get_database_config
from swe.database.connection import DatabaseConnection

TABLES = tuple(
    "swe_conversation_" + suffix
    for suffix in (
        "sessions",
        "chats",
        "turns",
        "publications",
        "messages",
        "retentions",
    )
)
SQL_ROOT = Path(__file__).resolve().parents[3] / "scripts" / "sql"


def isolated_database_config(environ, application_schemas):
    host = environ.get("SWE_TEST_DB_HOST")
    database = environ.get("SWE_TEST_DB_NAME")
    if not host or not database:
        pytest.skip(
            "conversation target database not configured; not acceptance evidence",
        )
    if database in application_schemas:
        raise ValueError(
            "SWE_TEST_DB_NAME must not match an application schema",
        )
    if not re.fullmatch(
        r"(?:test_[a-zA-Z0-9_]+|[a-zA-Z0-9_]+_test)",
        database,
    ):
        raise ValueError(
            "SWE_TEST_DB_NAME must identify a dedicated test schema",
        )
    return DatabaseConfig(
        host=host,
        port=int(environ.get("SWE_TEST_DB_PORT", "3306")),
        user=environ.get("SWE_TEST_DB_USER", "root"),
        password=environ.get("SWE_TEST_DB_PASSWORD", ""),
        database=database,
        min_connections=1,
        max_connections=2,
        time_zone="+00:00",
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
    )


async def assert_empty_schema(connection):
    if await connection.fetch_all("SHOW FULL TABLES"):
        raise ValueError("conversation test schema must be empty before setup")


def sql_statements(path):
    # The checked-in scripts use single-quoted strings without semicolons and
    # full-line comments. Do not use this helper as a general SQL parser.
    source = "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("--")
    )
    return [part.strip() for part in source.split(";") if part.strip()]


async def apply_sql_script(connection, path):
    # Share-extension variables and prepared handles belong to one connection.
    async with connection.acquire() as session:
        for statement in sql_statements(path):
            await session.execute(statement)


@pytest.fixture
def conversation_test_database_config():
    return isolated_database_config(
        os.environ,
        {get_database_config().database},
    )


@pytest.fixture
async def conversation_test_database(conversation_test_database_config):
    control = DatabaseConnection(conversation_test_database_config)
    await control.connect()
    child = None
    schema = "test_conversation_" + uuid4().hex
    created = False
    try:
        current = await control.fetch_one("SELECT DATABASE() AS database_name")
        if not current or current["database_name"] != control.config.database:
            raise ValueError(
                "connected schema does not match SWE_TEST_DB_NAME",
            )
        await assert_empty_schema(control)
        await control.execute(
            f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4",
        )
        created = True
        config = conversation_test_database_config.model_copy(
            update={"database": schema},
        )
        child = DatabaseConnection(config)
        await child.connect()
        await assert_empty_schema(child)
        await apply_sql_script(
            child,
            SQL_ROOT / "conversation_metadata_tables.sql",
        )
        yield child
    finally:
        try:
            if child is not None:
                await child.close()
        finally:
            try:
                if created:
                    # Only this fixture's generated schema may be dropped.
                    await control.execute(f"DROP DATABASE `{schema}`")
            finally:
                await control.close()

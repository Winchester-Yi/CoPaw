# -*- coding: utf-8 -*-
"""Fixture protections run without connecting to any database."""

from unittest.mock import AsyncMock, Mock

import pytest

from tests.integrated.conversation_storage import conftest as storage_fixtures

from tests.integrated.conversation_storage.conftest import (
    assert_empty_schema,
    isolated_database_config,
)


def test_missing_explicit_test_configuration_skips():
    with pytest.raises(pytest.skip.Exception, match="not configured"):
        isolated_database_config({}, {"business"})


@pytest.mark.parametrize("database", ["business", "test_business"])
def test_application_schema_is_rejected_even_with_test_name(database):
    with pytest.raises(ValueError, match="application schema"):
        isolated_database_config(
            {"SWE_TEST_DB_HOST": "localhost", "SWE_TEST_DB_NAME": database},
            {database},
        )


@pytest.mark.parametrize(
    "database",
    ["copaw_dev", "test_bad`name", "../test_data"],
)
def test_non_test_or_unsafe_schema_is_rejected(database):
    with pytest.raises(ValueError, match="dedicated test schema"):
        isolated_database_config(
            {"SWE_TEST_DB_HOST": "localhost", "SWE_TEST_DB_NAME": database},
            {"business"},
        )


async def test_nonempty_schema_fails_before_any_ddl():
    connection = AsyncMock()
    connection.fetch_all.return_value = [{"Tables_in_test_x": "foreign_table"}]
    with pytest.raises(ValueError, match="empty"):
        await assert_empty_schema(connection)
    connection.execute.assert_not_called()


def test_explicit_test_config_does_not_inherit_application_credentials():
    config = isolated_database_config(
        {"SWE_TEST_DB_HOST": "localhost", "SWE_TEST_DB_NAME": "test_isolated"},
        {"business"},
    )
    assert config.password == ""
    assert config.time_zone == "+00:00"


async def test_child_close_failure_still_drops_generated_schema_and_closes_control(
    monkeypatch,
):
    config = isolated_database_config(
        {"SWE_TEST_DB_HOST": "localhost", "SWE_TEST_DB_NAME": "test_control"},
        {"business"},
    )
    control = AsyncMock()
    control.config = config
    control.fetch_one.return_value = {"database_name": "test_control"}
    control.fetch_all.return_value = []
    child = AsyncMock()
    child.fetch_all.return_value = []
    child.close.side_effect = RuntimeError("child close failed")
    monkeypatch.setattr(
        storage_fixtures,
        "DatabaseConnection",
        Mock(side_effect=[control, child]),
    )
    monkeypatch.setattr(storage_fixtures, "apply_sql_script", AsyncMock())
    fixture = storage_fixtures.conversation_test_database.__wrapped__(config)
    assert await anext(fixture) is child
    with pytest.raises(RuntimeError, match="child close failed"):
        await fixture.aclose()
    create, drop = [call.args[0] for call in control.execute.await_args_list]
    schema = create.split("`")[1]
    assert schema.startswith("test_conversation_")
    assert drop == f"DROP DATABASE `{schema}`"
    control.close.assert_awaited_once()

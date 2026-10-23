# -*- coding: utf-8 -*-
"""Connection policy defaults, environment overrides and non-TLS regression."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from swe.database.config import DatabaseConfig, get_database_config
from swe.database.pool import ConnectionPool


@pytest.fixture(autouse=True)
def clean_database_environment(monkeypatch):
    for name in (
        "CHARSET",
        "USE_UNICODE",
        "SSL_DISABLED",
        "TIME_ZONE",
        "CONNECT_TIMEOUT",
        "READ_TIMEOUT",
        "WRITE_TIMEOUT",
        "ACCESS",
    ):
        monkeypatch.delenv(f"SWE_DB_{name}", raising=False)


def test_default_connection_policy_matches_deployment():
    for config in (DatabaseConfig(), get_database_config()):
        assert config.charset == "utf8mb4"
        assert config.use_unicode is True
        assert config.ssl_disabled is True
        assert config.time_zone == "+08:00"
        assert config.connect_timeout == 10
        assert config.read_timeout == 600
        assert config.write_timeout == 600


def test_environment_policy_reaches_driver_and_pool_deadlines(monkeypatch):
    for name, value in {
        "CHARSET": "utf8mb4",
        "USE_UNICODE": "false",
        "SSL_DISABLED": "false",
        "TIME_ZONE": "+00:00",
        "CONNECT_TIMEOUT": "12",
        "READ_TIMEOUT": "90",
        "WRITE_TIMEOUT": "120",
    }.items():
        monkeypatch.setenv(f"SWE_DB_{name}", value)
    config = get_database_config()
    factory = Mock()
    pool = ConnectionPool(config, factory)
    pool._new_entry()
    options = factory.call_args.kwargs
    assert options["charset"] == "utf8mb4"
    assert options["use_unicode"] is False
    assert options["ssl_disabled"] is False
    assert options["time_zone"] == "+00:00"
    assert options["connection_timeout"] == config.connect_timeout == 12
    assert options["read_timeout"] == config.read_timeout == 90
    assert options["write_timeout"] == config.write_timeout == 120


@pytest.mark.asyncio
async def test_default_pool_connect_skips_cipher_on_non_tls_transport(
    monkeypatch,
):
    driver = pytest.importorskip("mysql.connector.aio.connection")
    from mysql.connector.aio.charsets import charsets
    from swe.database.driver import ManagedMySQLConnection

    charsets.set_mysql_major_version(8)
    pool = ConnectionPool(DatabaseConfig(), ManagedMySQLConnection)
    entry = pool._new_entry()
    writer = SimpleNamespace(get_extra_info=Mock(return_value=None))
    socket = SimpleNamespace(
        _writer=writer,
        open_connection=AsyncMock(),
        close_connection=AsyncMock(),
    )
    monkeypatch.setattr(driver, "MySQLTcpSocket", Mock(return_value=socket))
    connection = entry.connection
    for name in ("_do_handshake", "_do_auth", "_post_connection"):
        monkeypatch.setattr(connection, name, AsyncMock())

    await connection.connect()

    connection._post_connection.assert_awaited_once()
    writer.get_extra_info.assert_not_called()

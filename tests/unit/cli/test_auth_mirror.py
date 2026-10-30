# -*- coding: utf-8 -*-
"""CLI refresh owns one mirror connection across all selected tenants."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from click.testing import CliRunner

from swe.cli import auth_cmd
from swe import database
from swe.app.crons import auth_mirror
from swe.app.crons.auth_state import ResolvedAuthToken


@pytest.mark.parametrize(
    "host",
    ["mysql", "localhost", "127.0.0.1", "::1"],
)
def test_refresh_command_uses_one_connection_and_preserves_output(
    tmp_path,
    monkeypatch,
    host,
):
    connection = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    factory = Mock(return_value=connection)
    monkeypatch.setattr(database, "DatabaseConnection", factory)
    monkeypatch.setattr(
        database,
        "get_database_config",
        lambda: SimpleNamespace(host=host),
    )
    monkeypatch.setattr(
        auth_cmd,
        "_discover_cron_auth_tenant_ids",
        lambda: ["alice", "bob"],
    )
    monkeypatch.setattr(
        auth_cmd,
        "get_current_effective_tenant_id",
        lambda: None,
    )
    monkeypatch.setattr(
        auth_cmd,
        "get_tenant_secrets_dir",
        lambda tenant: tmp_path / tenant / ".secret",
    )
    seen = []

    async def prefetch(*, tenant_id):
        assert auth_mirror._repository.db is connection
        seen.append(tenant_id)
        return ResolvedAuthToken(
            token="token",
            expires_at=None,
            reused=True,
            cookie_header="cookie",
        )

    monkeypatch.setattr(auth_cmd, "prefetch_auth_token", prefetch)
    result = CliRunner().invoke(auth_cmd.auth_group, ["refresh-token"])
    assert result.exit_code == 0, result.output
    assert "Cron auth synced for 2 tenant(s); 0 refreshed." in result.output
    assert seen == ["alice", "bob"]
    factory.assert_called_once()
    connection.connect.assert_awaited_once()
    connection.close.assert_awaited_once()
    assert auth_mirror._repository is None
    for tenant in seen:
        assert (tmp_path / tenant / ".secret" / "envs.json").is_file()

# -*- coding: utf-8 -*-
"""HTTP-backed tenant environment CLI tests."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from swe.cli import env_cmd
from swe.cli.main import cli


class _Response:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def _mock_client(method: str, response: _Response):
    mock_http = MagicMock()
    mock_http.__enter__.return_value = mock_http
    getattr(mock_http, method).return_value = response
    mock_client = MagicMock(return_value=mock_http)
    return mock_client, mock_http


def test_env_list_masks_values_and_passes_runtime_scope_headers(monkeypatch):
    monkeypatch.setenv("SWE_TENANT_ID", "tenant-a")
    monkeypatch.setenv("SWE_SOURCE_ID", "source-a")
    mock_client, mock_http = _mock_client(
        "get",
        _Response(
            [
                {"key": "API_TOKEN", "value": "secret"},
                {"key": "EMPTY", "value": ""},
            ],
        ),
    )

    with patch.object(env_cmd, "client", mock_client, create=True):
        result = CliRunner().invoke(
            env_cmd.env_group,
            [
                "list",
            ],
        )

    assert result.exit_code == 0
    assert "API_TOKEN" in result.output
    assert "********" in result.output
    assert "secret" not in result.output
    _, kwargs = mock_http.get.call_args
    assert kwargs["headers"] == {
        "X-Tenant-Id": "tenant-a",
        "X-Source-Id": "source-a",
    }


def test_env_list_show_values_prints_explicit_values(monkeypatch):
    monkeypatch.setenv("SWE_TENANT_ID", "tenant-a")
    monkeypatch.setenv("SWE_SOURCE_ID", "source-a")
    mock_client, _ = _mock_client(
        "get",
        _Response([{"key": "API_TOKEN", "value": "secret"}]),
    )

    with patch.object(env_cmd, "client", mock_client, create=True):
        result = CliRunner().invoke(
            env_cmd.env_group,
            [
                "list",
                "--show-values",
            ],
        )

    assert result.exit_code == 0
    assert "API_TOKEN" in result.output
    assert "secret" in result.output


def test_env_set_sends_patch_without_echoing_value(monkeypatch):
    monkeypatch.setenv("SWE_TENANT_ID", "tenant-a")
    monkeypatch.setenv("SWE_SOURCE_ID", "source-a")
    mock_client, mock_http = _mock_client("patch", _Response([]))

    with patch.object(env_cmd, "client", mock_client, create=True):
        result = CliRunner().invoke(
            env_cmd.env_group,
            [
                "set",
                "API_TOKEN",
                "secret",
            ],
        )

    assert result.exit_code == 0
    assert "API_TOKEN" in result.output
    assert "secret" not in result.output
    _, kwargs = mock_http.patch.call_args
    assert kwargs["json"] == {"values": {"API_TOKEN": "secret"}}
    assert kwargs["headers"] == {
        "X-Tenant-Id": "tenant-a",
        "X-Source-Id": "source-a",
    }


def test_env_delete_sends_encoded_key_and_scope_headers(monkeypatch):
    monkeypatch.setenv("SWE_TENANT_ID", "tenant-a")
    monkeypatch.setenv("SWE_SOURCE_ID", "source-a")
    mock_client, mock_http = _mock_client("delete", _Response([]))

    with patch.object(env_cmd, "client", mock_client, create=True):
        result = CliRunner().invoke(
            env_cmd.env_group,
            [
                "delete",
                "OLD_KEY",
            ],
        )

    assert result.exit_code == 0
    assert "OLD_KEY" in result.output
    _, kwargs = mock_http.delete.call_args
    assert mock_http.delete.call_args.args == ("/envs/OLD_KEY",)
    assert kwargs["headers"] == {
        "X-Tenant-Id": "tenant-a",
        "X-Source-Id": "source-a",
    }


def test_env_commands_reject_external_scope_options(monkeypatch):
    monkeypatch.setenv("SWE_TENANT_ID", "tenant-a")
    monkeypatch.setenv("SWE_SOURCE_ID", "source-a")
    runner = CliRunner()

    for args in (
        ["list", "--tenant-id", "external"],
        ["set", "KEY", "value", "--source-id", "external"],
        ["delete", "KEY", "--tenant-id", "external"],
    ):
        result = runner.invoke(env_cmd.env_group, args)
        assert result.exit_code == 2
        assert "no such option" in result.output.lower()


def test_env_commands_require_runtime_scope_claims(monkeypatch):
    monkeypatch.delenv("SWE_TENANT_ID", raising=False)
    monkeypatch.delenv("SWE_SOURCE_ID", raising=False)
    result = CliRunner().invoke(env_cmd.env_group, ["list"])

    assert result.exit_code == 1
    assert "SWE_TENANT_ID" in result.output


def test_env_list_uses_root_cli_host_and_port(monkeypatch):
    monkeypatch.setenv("SWE_TENANT_ID", "tenant-a")
    monkeypatch.setenv("SWE_SOURCE_ID", "source-a")
    mock_client, _ = _mock_client("get", _Response([]))

    with patch.object(env_cmd, "client", mock_client, create=True):
        result = CliRunner().invoke(
            cli,
            [
                "--host",
                "127.0.0.1",
                "--port",
                "8099",
                "env",
                "list",
            ],
        )

    assert result.exit_code == 0
    mock_client.assert_called_once_with("http://127.0.0.1:8099")


def test_env_delete_api_error_returns_nonzero_exit_code(monkeypatch):
    monkeypatch.setenv("SWE_TENANT_ID", "tenant-a")
    monkeypatch.setenv("SWE_SOURCE_ID", "source-a")
    mock_client, _ = _mock_client(
        "delete",
        _Response({"detail": "Env var 'MISSING' not found"}, 404),
    )

    with patch.object(env_cmd, "client", mock_client, create=True):
        result = CliRunner().invoke(
            env_cmd.env_group,
            [
                "delete",
                "MISSING",
            ],
        )

    assert result.exit_code == 1
    assert "Env var 'MISSING' not found" in result.output

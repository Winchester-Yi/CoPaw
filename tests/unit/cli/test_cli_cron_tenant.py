# -*- coding: utf-8 -*-
"""Cron CLI tenant header regression tests."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from swe.cli.cron_cmd import cron_group
from swe.config.context import tenant_context


class _Response:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True}


def test_cron_create_passes_scope_headers():
    runner = CliRunner()

    with patch("swe.cli.cron_cmd.client") as mock_client:
        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.post.return_value = _Response()
        mock_client.return_value = mock_http

        result = runner.invoke(
            cron_group,
            [
                "create",
                "--type",
                "agent",
                "--name",
                "tenant cron",
                "--cron",
                "* * * * *",
                "--channel",
                "console",
                "--target-user",
                "user-a",
                "--target-session",
                "session-a",
                "--text",
                "ping",
                "--timezone",
                "UTC",
                "--tenant-id",
                "tenant-a",
                "--source-id",
                "source-a",
            ],
        )

    assert result.exit_code == 0
    _, kwargs = mock_http.post.call_args
    assert kwargs["headers"]["X-Tenant-Id"] == "tenant-a"
    assert kwargs["headers"]["X-Source-Id"] == "source-a"


def test_cron_create_does_not_invent_default_source_header():
    runner = CliRunner()

    with patch("swe.cli.cron_cmd.client") as mock_client:
        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.post.return_value = _Response()
        mock_client.return_value = mock_http

        result = runner.invoke(
            cron_group,
            [
                "create",
                "--type",
                "agent",
                "--name",
                "default cron",
                "--cron",
                "* * * * *",
                "--channel",
                "console",
                "--target-user",
                "user-a",
                "--target-session",
                "session-a",
                "--text",
                "ping",
                "--timezone",
                "UTC",
            ],
        )

    assert result.exit_code == 0
    _, kwargs = mock_http.post.call_args
    assert kwargs["headers"]["X-Tenant-Id"] == "default"
    assert "X-Source-Id" not in kwargs["headers"]


def test_cli_workflow_file_uses_target_user_for_task_ownership(tmp_path):
    spec = {
        "id": "",
        "name": "skill task",
        "schedule": {"type": "cron", "cron": "0 9 * * *"},
        "task_type": "workflow",
        "skill_ids": "skill-a",
        "dispatch": {
            "type": "channel",
            "channel": "console",
            "target": {"user_id": "user-a", "session_id": "session-a"},
        },
    }
    path = tmp_path / "workflow-job.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    runner = CliRunner()

    with patch("swe.cli.cron_cmd.client") as mock_client:
        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.post.return_value = _Response()
        mock_client.return_value = mock_http
        result = runner.invoke(
            cron_group,
            ["create", "-f", str(path), "--source-id", "RMASSIST"],
        )

    assert result.exit_code == 0
    _, kwargs = mock_http.post.call_args
    assert kwargs["headers"]["X-User-Id"] == "user-a"


def test_cron_create_uses_current_scope_context_for_headers():
    runner = CliRunner()

    with patch("swe.cli.cron_cmd.client") as mock_client:
        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.post.return_value = _Response()
        mock_client.return_value = mock_http

        with tenant_context(tenant_id="tenant-a", source_id="source-a"):
            result = runner.invoke(
                cron_group,
                [
                    "create",
                    "--type",
                    "agent",
                    "--name",
                    "context cron",
                    "--cron",
                    "* * * * *",
                    "--channel",
                    "console",
                    "--target-user",
                    "user-a",
                    "--target-session",
                    "session-a",
                    "--text",
                    "ping",
                    "--timezone",
                    "UTC",
                ],
            )

    assert result.exit_code == 0
    _, kwargs = mock_http.post.call_args
    assert kwargs["headers"]["X-Tenant-Id"] == "tenant-a"
    assert kwargs["headers"]["X-Source-Id"] == "source-a"


def test_cron_create_agent_sets_model_slot() -> None:
    runner = CliRunner()

    with patch("swe.cli.cron_cmd.client") as mock_client:
        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.post.return_value = _Response()
        mock_client.return_value = mock_http

        result = runner.invoke(
            cron_group,
            [
                "create",
                "--type",
                "agent",
                "--name",
                "tenant cron",
                "--cron",
                "* * * * *",
                "--channel",
                "console",
                "--target-user",
                "user-a",
                "--target-session",
                "session-a",
                "--text",
                "ping",
                "--model-provider",
                "openai",
                "--model",
                "gpt-5.4",
            ],
        )

    assert result.exit_code == 0
    _, kwargs = mock_http.post.call_args
    assert kwargs["json"]["model_slot"] == {
        "provider_id": "openai",
        "model": "gpt-5.4",
    }


def test_cron_create_defaults_notification_delay_minutes() -> None:
    runner = CliRunner()

    with patch("swe.cli.cron_cmd.client") as mock_client:
        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.post.return_value = _Response()
        mock_client.return_value = mock_http

        result = runner.invoke(
            cron_group,
            [
                "create",
                "--type",
                "agent",
                "--name",
                "tenant cron",
                "--cron",
                "* * * * *",
                "--channel",
                "console",
                "--target-user",
                "user-a",
                "--target-session",
                "session-a",
                "--text",
                "ping",
            ],
        )

    assert result.exit_code == 0
    _, kwargs = mock_http.post.call_args
    assert kwargs["json"]["meta"]["notification_delay_minutes"] == 0


def test_cron_create_sets_notification_delay_minutes() -> None:
    runner = CliRunner()

    with patch("swe.cli.cron_cmd.client") as mock_client:
        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.post.return_value = _Response()
        mock_client.return_value = mock_http

        result = runner.invoke(
            cron_group,
            [
                "create",
                "--type",
                "agent",
                "--name",
                "tenant cron",
                "--cron",
                "* * * * *",
                "--channel",
                "console",
                "--target-user",
                "user-a",
                "--target-session",
                "session-a",
                "--text",
                "ping",
                "--notification-delay-minutes",
                "120",
            ],
        )

    assert result.exit_code == 0
    _, kwargs = mock_http.post.call_args
    assert kwargs["json"]["meta"]["notification_delay_minutes"] == 120


def test_cron_create_text_ignores_model_slot() -> None:
    runner = CliRunner()

    with patch("swe.cli.cron_cmd.client") as mock_client:
        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.post.return_value = _Response()
        mock_client.return_value = mock_http

        result = runner.invoke(
            cron_group,
            [
                "create",
                "--type",
                "text",
                "--name",
                "tenant cron",
                "--cron",
                "* * * * *",
                "--channel",
                "console",
                "--target-user",
                "user-a",
                "--target-session",
                "session-a",
                "--text",
                "ping",
                "--model-provider",
                "openai",
                "--model",
                "gpt-5.4",
            ],
        )

    assert result.exit_code == 0
    _, kwargs = mock_http.post.call_args
    assert "model_slot" not in kwargs["json"]


def test_cron_create_requires_model_provider_and_model_together() -> None:
    runner = CliRunner()

    result = runner.invoke(
        cron_group,
        [
            "create",
            "--type",
            "agent",
            "--name",
            "tenant cron",
            "--cron",
            "* * * * *",
            "--channel",
            "console",
            "--target-user",
            "user-a",
            "--target-session",
            "session-a",
            "--text",
            "ping",
            "--model-provider",
            "openai",
        ],
    )

    assert result.exit_code != 0
    assert (
        "--model-provider and --model must be provided together"
        in result.output
    )


def test_cron_update_without_model_flags_preserves_existing_model_slot() -> (
    None
):
    runner = CliRunner()

    existing = {
        "id": "job-1",
        "name": "tenant cron",
        "task_type": "agent",
        "schedule": {"type": "cron", "cron": "* * * * *", "timezone": "UTC"},
        "dispatch": {
            "type": "channel",
            "channel": "console",
            "target": {"user_id": "user-a", "session_id": "session-a"},
            "mode": "final",
            "meta": {},
        },
        "request": {
            "input": [
                {
                    "role": "user",
                    "type": "message",
                    "content": [{"type": "text", "text": "ping"}],
                },
            ],
            "session_id": "session-a",
            "user_id": "cron",
        },
        "runtime": {
            "max_concurrency": 1,
            "timeout_seconds": 7200,
            "misfire_grace_seconds": 300,
        },
        "meta": {},
        "model_slot": {"provider_id": "openai", "model": "gpt-5.4"},
    }

    with patch("swe.cli.cron_cmd.client") as mock_client:
        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.get.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value=existing),
        )
        mock_http.put.return_value = _Response()
        mock_client.return_value = mock_http

        result = runner.invoke(
            cron_group,
            ["update", "job-1", "--name", "updated cron"],
        )

    assert result.exit_code == 0
    _, kwargs = mock_http.put.call_args
    assert kwargs["json"]["model_slot"] == {
        "provider_id": "openai",
        "model": "gpt-5.4",
    }


def test_cron_update_with_model_flags_replaces_existing_model_slot() -> None:
    runner = CliRunner()

    existing = {
        "id": "job-1",
        "name": "tenant cron",
        "task_type": "agent",
        "schedule": {"type": "cron", "cron": "* * * * *", "timezone": "UTC"},
        "dispatch": {
            "type": "channel",
            "channel": "console",
            "target": {"user_id": "user-a", "session_id": "session-a"},
            "mode": "final",
            "meta": {},
        },
        "request": {
            "input": [
                {
                    "role": "user",
                    "type": "message",
                    "content": [{"type": "text", "text": "ping"}],
                },
            ],
            "session_id": "session-a",
            "user_id": "cron",
        },
        "runtime": {
            "max_concurrency": 1,
            "timeout_seconds": 7200,
            "misfire_grace_seconds": 300,
        },
        "meta": {},
        "model_slot": {"provider_id": "openai", "model": "gpt-5.4"},
    }

    with patch("swe.cli.cron_cmd.client") as mock_client:
        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.get.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value=existing),
        )
        mock_http.put.return_value = _Response()
        mock_client.return_value = mock_http

        result = runner.invoke(
            cron_group,
            [
                "update",
                "job-1",
                "--model-provider",
                "anthropic",
                "--model",
                "claude-sonnet-4",
            ],
        )

    assert result.exit_code == 0
    _, kwargs = mock_http.put.call_args
    assert kwargs["json"]["model_slot"] == {
        "provider_id": "anthropic",
        "model": "claude-sonnet-4",
    }


def test_cron_update_to_text_clears_model_slot() -> None:
    runner = CliRunner()

    existing = {
        "id": "job-1",
        "name": "tenant cron",
        "task_type": "agent",
        "schedule": {"type": "cron", "cron": "* * * * *", "timezone": "UTC"},
        "dispatch": {
            "type": "channel",
            "channel": "console",
            "target": {"user_id": "user-a", "session_id": "session-a"},
            "mode": "final",
            "meta": {},
        },
        "request": {
            "input": [
                {
                    "role": "user",
                    "type": "message",
                    "content": [{"type": "text", "text": "ping"}],
                },
            ],
            "session_id": "session-a",
            "user_id": "cron",
        },
        "runtime": {
            "max_concurrency": 1,
            "timeout_seconds": 7200,
            "misfire_grace_seconds": 300,
        },
        "meta": {},
        "model_slot": {"provider_id": "openai", "model": "gpt-5.4"},
    }

    with patch("swe.cli.cron_cmd.client") as mock_client:
        mock_http = MagicMock()
        mock_http.__enter__.return_value = mock_http
        mock_http.get.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value=existing),
        )
        mock_http.put.return_value = _Response()
        mock_client.return_value = mock_http

        result = runner.invoke(
            cron_group,
            ["update", "job-1", "--type", "text", "--text", "plain text"],
        )

    assert result.exit_code == 0
    _, kwargs = mock_http.put.call_args
    assert "model_slot" not in kwargs["json"]

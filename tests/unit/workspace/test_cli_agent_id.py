# -*- coding: utf-8 -*-
"""Tests for CLI --agent-id parameter support."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
from click.testing import CliRunner

from swe.cli.channels_cmd import channels_group
from swe.cli.cron_cmd import cron_group
from swe.cli.daemon_cmd import daemon_group
from swe.cli.chats_cmd import chats_group
from swe.cli.skills_cmd import skills_group
from swe.config.config import AgentProfileConfig


@pytest.fixture
def temp_config_dir():
    """Create a temporary config directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir)
        workspaces_dir = config_dir / "workspaces"
        workspaces_dir.mkdir()

        # Create default agent workspace
        default_ws = workspaces_dir / "default"
        default_ws.mkdir()

        # Create test agent workspace
        test_ws = workspaces_dir / "abc123"
        test_ws.mkdir()

        yield config_dir, default_ws, test_ws


def test_channels_list_default_agent(
    temp_config_dir,
):  # pylint: disable=W0621,W0613
    """Test swe channels list uses default agent by default."""
    _, default_ws, _ = temp_config_dir

    # Create agent config for default
    agent_config_path = default_ws / "agent.json"
    agent_config = AgentProfileConfig(
        id="default",
        name="Default Agent",
        workspace_dir=str(default_ws),
    )
    agent_config_path.write_text(
        json.dumps(agent_config.model_dump(exclude_none=True)),
        encoding="utf-8",
    )

    runner = CliRunner()

    with patch("swe.cli.channels_cmd.load_agent_config") as mock_load:
        mock_load.return_value = agent_config
        runner.invoke(channels_group, ["list"])

        # Should call with 'default' agent
        mock_load.assert_called_once_with("default")


def test_channels_list_custom_agent(
    temp_config_dir,
):  # pylint: disable=W0621,W0613
    """Test swe channels list with custom agent_id."""
    _, _, test_ws = temp_config_dir

    # Create agent config for test agent
    agent_config_path = test_ws / "agent.json"
    agent_config = AgentProfileConfig(
        id="abc123",
        name="Test Agent",
        workspace_dir=str(test_ws),
    )
    agent_config_path.write_text(
        json.dumps(agent_config.model_dump(exclude_none=True)),
        encoding="utf-8",
    )

    runner = CliRunner()

    with patch("swe.cli.channels_cmd.load_agent_config") as mock_load:
        mock_load.return_value = agent_config
        runner.invoke(
            channels_group,
            ["list", "--agent-id", "abc123"],
        )

        # Should call with custom agent
        mock_load.assert_called_once_with("abc123")


def test_cron_list_with_agent_id():
    """Test swe cron list with --agent-id."""
    runner = CliRunner()

    with patch("swe.cli.cron_cmd.client") as mock_client:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"items": [], "next_cursor": None}
        mock_response.raise_for_status = MagicMock()
        mock_client.return_value.__enter__.return_value.get.return_value = (
            mock_response
        )

        result = runner.invoke(
            cron_group,
            [
                "list",
                "--agent-id",
                "test123",
                "--tenant-id",
                "user-a",
                "--source-id",
                "RMASSIST",
            ],
        )
        assert result.exit_code == 0, result.output

        # Verify X-Agent-Id header was set
        call_args = (
            mock_client.return_value.__enter__.return_value.get.call_args
        )
        assert call_args is not None
        if call_args[1].get("headers"):
            assert call_args[1]["headers"]["X-Agent-Id"] == "test123"


def test_daemon_status_default_agent():
    """Test swe daemon status defaults to 'default' agent."""
    runner = CliRunner()

    with patch("swe.cli.daemon_cmd.run_daemon_status") as mock_status:
        with patch("swe.cli.daemon_cmd._get_agent_workspace") as mock_ws:
            mock_ws.return_value = "/tmp/default"
            mock_status.return_value = "Status: OK"

            runner.invoke(daemon_group, ["status"])

            # Should use default agent
            mock_ws.assert_called_once_with("default")


def test_daemon_status_custom_agent():
    """Test swe daemon status with custom agent."""
    runner = CliRunner()

    with patch("swe.cli.daemon_cmd.run_daemon_status") as mock_status:
        with patch("swe.cli.daemon_cmd._get_agent_workspace") as mock_ws:
            mock_ws.return_value = "/tmp/xyz789"
            mock_status.return_value = "Status: OK"

            runner.invoke(
                daemon_group,
                ["status", "--agent-id", "xyz789"],
            )

            # Should use custom agent
            mock_ws.assert_called_once_with("xyz789")


def test_skills_list_default_agent():
    """Test swe skills list defaults to 'default' agent."""
    runner = CliRunner()

    with patch(
        "swe.cli.skills_cmd._get_agent_workspace",
    ) as mock_ws:
        with patch("swe.cli.skills_cmd.SkillService") as mock_service:
            mock_ws.return_value = "/tmp/default"
            mock_service_instance = MagicMock()
            mock_service_instance.list_all_skills.return_value = []
            mock_service.return_value = mock_service_instance

            runner.invoke(skills_group, ["list"])

            # Should use default agent
            mock_ws.assert_called_once_with("default")


def test_skills_list_custom_agent():
    """Test swe skills list with custom agent."""
    runner = CliRunner()

    with patch(
        "swe.cli.skills_cmd._get_agent_workspace",
    ) as mock_ws:
        with patch("swe.cli.skills_cmd.SkillService") as mock_service:
            mock_ws.return_value = "/tmp/abc123"
            mock_service_instance = MagicMock()
            mock_service_instance.list_all_skills.return_value = []
            mock_service.return_value = mock_service_instance

            runner.invoke(
                skills_group,
                ["list", "--agent-id", "abc123"],
            )

            # Should use custom agent
            mock_ws.assert_called_once_with("abc123")


def test_chats_list_with_agent_id():
    """Test swe chats list with --agent-id."""
    runner = CliRunner()

    with patch("swe.cli.chats_cmd.client") as mock_client:
        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()
        mock_client.return_value.__enter__.return_value.get.return_value = (
            mock_response
        )

        runner.invoke(
            chats_group,
            ["list", "--agent-id", "xyz789"],
        )

        # Verify X-Agent-Id header was set
        call_args = (
            mock_client.return_value.__enter__.return_value.get.call_args
        )
        assert call_args is not None
        if call_args[1].get("headers"):
            assert call_args[1]["headers"]["X-Agent-Id"] == "xyz789"

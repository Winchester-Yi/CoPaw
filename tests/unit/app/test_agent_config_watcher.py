# -*- coding: utf-8 -*-
"""Tests for tenant-aware AgentConfigWatcher config loading."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from swe.app.agent_config_watcher import AgentConfigWatcher
from swe.app.workspace.service_factories import create_agent_config_watcher


class TestAgentConfigWatcherTenantScope:
    """AgentConfigWatcher must load config from the owning tenant scope."""

    def test_snapshot_loads_agent_config_with_tenant_scope(self, tmp_path):
        """Initial snapshot should use the watcher tenant_id."""
        workspace_dir = tmp_path / "tenant-a" / "workspaces" / "default"
        workspace_dir.mkdir(parents=True)
        (workspace_dir / "agent.json").write_text("{}", encoding="utf-8")

        watcher = AgentConfigWatcher(
            agent_id="default",
            workspace_dir=workspace_dir,
            channel_manager=None,
            tenant_id="tenant-a",
        )

        with patch(
            "swe.app.agent_config_watcher.load_agent_config",
        ) as mock_load:
            mock_load.return_value = Mock(channels=None, heartbeat=None)
            watcher._snapshot()

        mock_load.assert_called_once_with(
            "default",
            tenant_id="tenant-a",
        )

    async def test_check_loads_agent_config_with_tenant_scope(self, tmp_path):
        """Reload path should use the watcher tenant_id after file change."""
        workspace_dir = tmp_path / "tenant-a" / "workspaces" / "default"
        workspace_dir.mkdir(parents=True)
        config_path = workspace_dir / "agent.json"
        config_path.write_text("{}", encoding="utf-8")

        watcher = AgentConfigWatcher(
            agent_id="default",
            workspace_dir=workspace_dir,
            channel_manager=None,
            tenant_id="tenant-a",
        )
        watcher._last_mtime = 0.0

        with patch(
            "swe.app.agent_config_watcher.load_agent_config",
        ) as mock_load:
            mock_load.return_value = Mock(channels=None, heartbeat=None)
            await watcher._check()

        mock_load.assert_called_once_with(
            "default",
            tenant_id="tenant-a",
        )

    async def test_service_factory_passes_workspace_tenant_id(self, tmp_path):
        """Workspace factory should pass tenant_id into AgentConfigWatcher."""
        workspace_dir = tmp_path / "tenant-a" / "workspaces" / "default"
        workspace_dir.mkdir(parents=True)

        ws = Mock()
        ws.agent_id = "default"
        ws.workspace_dir = workspace_dir
        ws.tenant_id = "tenant-a"
        ws._service_manager = Mock()  # pylint: disable=protected-access
        ws._service_manager.services = {
            "channel_manager": AsyncMock(),
            "cron_manager": AsyncMock(),
        }

        with patch(
            "swe.app.agent_config_watcher.AgentConfigWatcher",
        ) as mock_watcher:
            watcher = Mock()
            mock_watcher.return_value = watcher

            result = await create_agent_config_watcher(ws, None)

        assert result is watcher
        mock_watcher.assert_called_once_with(
            agent_id="default",
            workspace_dir=workspace_dir,
            channel_manager=ws._service_manager.services["channel_manager"],
            cron_manager=ws._service_manager.services["cron_manager"],
            tenant_id="tenant-a",
        )

# -*- coding: utf-8 -*-
"""系统配置读取不修复文件、不读取任务JSON，也不启动任何工作区。"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.crons.system_config import SystemConfigReader
from swe.config import utils as config_utils
from swe.config.context import resolve_storage_tenant_id


@pytest.fixture
def config_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(config_utils, "WORKING_DIR", tmp_path)
    root = tmp_path / resolve_storage_tenant_id("alice", "bank")
    workspace = root / "workspaces/default"
    workspace.mkdir(parents=True)
    (root / "config.json").write_text(
        json.dumps(
            {
                "user_timezone": "Asia/Shanghai",
                "agents": {
                    "profiles": {
                        "default": {
                            "id": "default",
                            "workspace_dir": str(workspace),
                        },
                    },
                },
            },
        ),
        encoding="utf-8",
    )
    (workspace / "agent.json").write_text(
        json.dumps(
            {
                "id": "default",
                "name": "Default",
                "heartbeat": {"enabled": True, "every": "1h"},
                "running": {"memory_summary": {"dream_cron": "0 3 * * *"}},
            },
        ),
        encoding="utf-8",
    )
    (workspace / "jobs.json").write_text(
        "not valid task JSON",
        encoding="utf-8",
    )
    original = Path.read_text

    def read(path, *args, **kwargs):
        assert path.name != "jobs.json"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    return root, workspace


def _job(kind):
    return {
        "task_type": kind,
        "scope_kind": "agent" if kind in {"heartbeat", "dream"} else "source",
        "tenant_id": "alice",
        "source_id": "bank",
        "agent_id": "default",
        "route_tenant_id": "alice",
        "route_agent_id": "default",
    }


def _snapshot(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,cron",
    [("heartbeat", "0 0/1 * * *"), ("dream", "0 3 * * *")],
)
async def test_read_agent_current_configuration_without_task_file_or_runtime(
    config_scope,
    kind,
    cron,
):
    root, _ = config_scope
    before = _snapshot(root)
    value = await SystemConfigReader(None).read(_job(kind))
    assert value["enabled"] and value["cron"] == cron
    assert value["timezone"] == "Asia/Shanghai"
    assert _snapshot(root) == before


@pytest.mark.asyncio
async def test_invalid_profile_json_is_unavailable_without_backup_or_repair(
    config_scope,
):
    root, workspace = config_scope
    (workspace / "agent.json").write_text("{broken", encoding="utf-8")
    before = _snapshot(root)
    with pytest.raises(ValueError):
        await SystemConfigReader(None).read(_job("heartbeat"))
    assert _snapshot(root) == before


@pytest.mark.asyncio
async def test_source_configuration_uses_fresh_source_settings_without_user_profile(
    config_scope,
):
    root, _ = config_scope
    source = SimpleNamespace(
        resolve_config=AsyncMock(
            return_value={
                "cron_task_session_cleanup": {
                    "enabled": True,
                    "cron": "0 2 * * *",
                },
            },
        ),
    )
    before = _snapshot(root)
    value = await SystemConfigReader(source).read(_job("task_session_cleanup"))
    assert value["enabled"] and value["cron"] == "0 2 * * *"
    source.resolve_config.assert_awaited_once_with("bank", force_refresh=True)
    assert _snapshot(root) == before


@pytest.mark.asyncio
async def test_stale_source_settings_cannot_be_used_to_start_maintenance(
    config_scope,
):
    source = SimpleNamespace(
        resolve_config=AsyncMock(return_value=SimpleNamespace(stale=True)),
    )
    with pytest.raises(RuntimeError, match="system_config_unavailable"):
        await SystemConfigReader(source).read(_job("archive_maintenance"))

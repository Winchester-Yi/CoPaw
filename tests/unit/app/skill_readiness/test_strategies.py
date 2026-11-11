# -*- coding: utf-8 -*-
"""技能可执行性检查策略测试。"""

from __future__ import annotations

import json
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from swe.app.crons.auth_state import CRON_AUTH_FILE_NAME, utc_now
from swe.app.crons.models import CronJobSpec
from swe.app.skill_readiness.models import (
    SkillReadinessCheckConfig,
    SkillReadinessOwner,
)
from swe.app.skill_readiness.strategies import (
    BoundCronJobStrategy,
    CronAuthValidStrategy,
    CronModelConnectionStrategy,
    McpToolsAvailableStrategy,
    ProfileIdentityBlockStrategy,
    SkillReadinessCheckContext,
    SkillReadinessStrategyRegistry,
)
from swe.config.context import encode_scope_id
from swe.config import utils as config_utils
from swe.app.skill_readiness import strategies as strategies_module
from swe.app.runner import runner as runner_module


def _context(tmp_path, monkeypatch, cron_manager=None):
    monkeypatch.setattr(config_utils, "WORKING_DIR", tmp_path / "work")
    monkeypatch.setattr(config_utils, "SECRET_DIR", tmp_path / "secret")
    return SkillReadinessCheckContext(
        source_id="source-a",
        skill_id="skill-a",
        owner=SkillReadinessOwner(user_id="alice"),
        job_reader=cron_manager or _CronManager([]),
    )


def test_check_context_resolves_default_source_to_storage_tenant():
    """default source 用户要读取 default_{source} 模板目录。"""
    context = SkillReadinessCheckContext(
        source_id="CMSJY",
        skill_id="skill-a",
        owner=SkillReadinessOwner(user_id="default"),
    )

    assert context.scope_id == "default_CMSJY"


def test_check_context_keeps_non_default_source_scope():
    """非 default 用户继续使用 tenant/source scope 隔离。"""
    context = SkillReadinessCheckContext(
        source_id="CMSJY",
        skill_id="skill-a",
        owner=SkillReadinessOwner(user_id="alice"),
    )

    assert context.scope_id == encode_scope_id("alice", "CMSJY")


def _job(**overrides):
    payload = {
        "id": "job-1",
        "name": "skill cron",
        "enabled": True,
        "skill_ids": "skill-a",
        "schedule": {"type": "cron", "cron": "0 9 * * *", "timezone": "UTC"},
        "task_type": "agent",
        "request": {"input": {"text": "ping"}},
        "dispatch": {
            "type": "channel",
            "channel": "console",
            "target": {"user_id": "alice", "session_id": "session-1"},
            "mode": "stream",
            "meta": {},
        },
    }
    payload.update(overrides)
    return CronJobSpec.model_validate(payload)


@pytest.mark.asyncio
async def test_profile_identity_block_requires_heading_and_fields(
    tmp_path,
    monkeypatch,
):
    context = _context(tmp_path, monkeypatch)
    profile_path = (
        config_utils.get_tenant_working_dir(context.scope_id) / "PROFILE.md"
    )
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        "### 用户身份信息\n"
        "分行号：001\n"
        "网点机构编号：002\n"
        "岗位编号：003\n"
        "客户经理ID：alice\n",
        encoding="utf-8",
    )

    result = await ProfileIdentityBlockStrategy().run(
        context,
        SkillReadinessCheckConfig(name="profile_identity_block"),
    )

    assert result.status == "pass"


@pytest.mark.asyncio
async def test_profile_identity_block_accepts_second_level_heading(
    tmp_path,
    monkeypatch,
):
    context = _context(tmp_path, monkeypatch)
    profile_path = (
        config_utils.get_tenant_working_dir(context.scope_id) / "PROFILE.md"
    )
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        "## 用户身份信息\n"
        "分行号：001\n"
        "网点机构编号：002\n"
        "岗位编号：003\n"
        "客户经理ID：alice\n",
        encoding="utf-8",
    )

    result = await ProfileIdentityBlockStrategy().run(
        context,
        SkillReadinessCheckConfig(name="profile_identity_block"),
    )

    assert result.status == "pass"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity_line",
    [
        "# 用户身份信息\n",
        "#### 用户身份信息\n",
        "###### 用户身份信息\n",
        "用户身份信息\n",
    ],
)
async def test_profile_identity_block_accepts_standalone_identity_line(
    tmp_path,
    monkeypatch,
    identity_line,
):
    context = _context(tmp_path, monkeypatch)
    profile_path = (
        config_utils.get_tenant_working_dir(context.scope_id) / "PROFILE.md"
    )
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        identity_line
        + "分行号：001\n"
        + "网点机构编号：002\n"
        + "岗位编号：003\n"
        + "客户经理ID：alice\n",
        encoding="utf-8",
    )

    result = await ProfileIdentityBlockStrategy().run(
        context,
        SkillReadinessCheckConfig(name="profile_identity_block"),
    )

    assert result.status == "pass"


@pytest.mark.asyncio
async def test_profile_identity_block_uses_current_workspace_profile(
    tmp_path,
    monkeypatch,
):
    context = _context(tmp_path, monkeypatch)
    workspace_dir = tmp_path / "work" / "alice" / "workspaces" / "default"
    workspace_dir.mkdir(parents=True)
    workspace_dir.joinpath("PROFILE.md").write_text(
        "### 用户身份信息\n"
        "分行号：001\n"
        "网点机构编号：002\n"
        "岗位编号：003\n"
        "客户经理ID：alice\n",
        encoding="utf-8",
    )
    context.workspace = type(
        "Workspace",
        (),
        {"workspace_dir": workspace_dir},
    )()

    result = await ProfileIdentityBlockStrategy().run(
        context,
        SkillReadinessCheckConfig(name="profile_identity_block"),
    )

    assert result.status == "pass"
    assert result.details == {}


class _CronManager:
    def __init__(self, jobs):
        self.jobs = jobs

    async def list_jobs(self, *identity):
        return [job.model_dump(mode="json") for job in self.jobs]


@pytest.mark.asyncio
async def test_bound_new_job_accepts_year_and_workday_schedule(
    tmp_path,
    monkeypatch,
):
    from swe.app.crons.managed_models import load_managed_spec

    raw = _job().model_dump(mode="json")
    raw["schedule"]["cron"] = "0 9 LW * * 2026-2029"
    raw["run_date_range"] = {
        "start_date": "2026-01-01",
        "end_date": "2029-12-31",
    }
    context = _context(
        tmp_path,
        monkeypatch,
        _CronManager([load_managed_spec(raw)]),
    )
    result = await BoundCronJobStrategy().run(
        context,
        SkillReadinessCheckConfig(name="bound_cron_job"),
    )
    assert result.status == "pass"


@pytest.mark.asyncio
async def test_scheduler_unavailable_is_check_failure_not_no_bound_jobs(
    tmp_path,
    monkeypatch,
):
    from unittest.mock import AsyncMock

    context = _context(tmp_path, monkeypatch)
    context.job_reader = SimpleNamespace(
        list_jobs=AsyncMock(side_effect=RuntimeError("scheduler unavailable")),
    )
    registry = SkillReadinessStrategyRegistry([BoundCronJobStrategy()])
    result = await registry.run_check(
        context,
        SkillReadinessCheckConfig(name=BoundCronJobStrategy.name),
    )
    assert result.status == "fail"
    assert result.message == "自检执行失败"


@pytest.mark.asyncio
async def test_bound_cron_job_counts_paused_but_not_disabled(
    tmp_path,
    monkeypatch,
):
    disabled_context = _context(
        tmp_path,
        monkeypatch,
        cron_manager=_CronManager([_job(enabled=False)]),
    )
    paused_context = _context(
        tmp_path,
        monkeypatch,
        cron_manager=_CronManager(
            [_job(meta={"pause_reason": "manual"})],
        ),
    )

    disabled = await BoundCronJobStrategy().run(
        disabled_context,
        SkillReadinessCheckConfig(name="bound_cron_job"),
    )
    paused = await BoundCronJobStrategy().run(
        paused_context,
        SkillReadinessCheckConfig(name="bound_cron_job"),
    )

    assert disabled.status == "fail"
    assert paused.status == "pass"


@pytest.mark.asyncio
async def test_cron_auth_valid_requires_future_user_info_expiry(
    tmp_path,
    monkeypatch,
):
    context = _context(tmp_path, monkeypatch)
    auth_path = config_utils.get_tenant_secrets_dir(context.scope_id) / (
        CRON_AUTH_FILE_NAME
    )
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text(
        json.dumps(
            {
                "user_info": {"user": "alice"},
                "user_info_expires_at": (
                    utc_now() + timedelta(hours=1)
                ).isoformat(),
            },
        ),
        encoding="utf-8",
    )

    result = await CronAuthValidStrategy().run(
        context,
        SkillReadinessCheckConfig(name="cron_auth_valid"),
    )

    assert result.status == "pass"


@pytest.mark.asyncio
async def test_mcp_tools_empty_config_is_skip(tmp_path, monkeypatch):
    result = await McpToolsAvailableStrategy().run(
        _context(tmp_path, monkeypatch),
        SkillReadinessCheckConfig(name="mcp_tools_available", params={}),
    )

    assert result.status == "skip"


class _Provider:
    def has_model(self, model_id):
        return model_id == "model-a"

    async def check_model_connection(self, model_id):
        return False, "network down"


class _ProviderManager:
    def get_active_model(self):
        return None

    def get_provider(self, provider_id):
        if provider_id == "provider-a":
            return _Provider()
        return None


@pytest.mark.asyncio
async def test_cron_model_connection_reports_failed_model_check(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        strategies_module,
        "_get_provider_manager",
        lambda scope_id: _ProviderManager(),
    )
    context = _context(
        tmp_path,
        monkeypatch,
        cron_manager=_CronManager(
            [
                _job(
                    model_slot={
                        "provider_id": "provider-a",
                        "model": "model-a",
                    },
                ),
            ],
        ),
    )

    result = await CronModelConnectionStrategy().run(
        context,
        SkillReadinessCheckConfig(name="cron_model_connection"),
    )

    assert result.status == "fail"
    assert result.message == "部分模型不可联通"
    assert result.details["failures"][0]["message"] == "network down"


@pytest.mark.asyncio
async def test_mcp_tools_missing_server_is_fail(tmp_path, monkeypatch):
    workspace = type(
        "Workspace",
        (),
        {
            "config": type(
                "Config",
                (),
                {"mcp": type("Mcp", (), {"clients": {}})()},
            )(),
        },
    )()
    context = _context(tmp_path, monkeypatch)
    context.workspace = workspace

    result = await McpToolsAvailableStrategy().run(
        context,
        SkillReadinessCheckConfig(
            name="mcp_tools_available",
            params={"servers": [{"name": "toolbox", "tools": ["search"]}]},
        ),
    )

    assert result.status == "fail"
    assert result.details["failures"][0]["message"] == "server not found"


@pytest.mark.asyncio
async def test_mcp_tools_uses_fresh_agent_config_when_workspace_cache_is_stale(
    tmp_path,
    monkeypatch,
):
    """MCP 配置更新后，自检应读取最新 agent.json 而不是旧 workspace 缓存。"""
    stale_workspace = SimpleNamespace(
        agent_id="default",
        tenant_id="alice_source-a",
        config=SimpleNamespace(mcp=SimpleNamespace(clients={})),
    )
    fresh_client = SimpleNamespace(enabled=True, name="toolbox")
    context = _context(tmp_path, monkeypatch)
    context.workspace = stale_workspace

    monkeypatch.setattr(
        strategies_module,
        "load_agent_config",
        lambda agent_id, tenant_id=None: SimpleNamespace(
            mcp=SimpleNamespace(clients={"toolbox": fresh_client}),
        ),
    )

    class _McpClient:
        async def connect(self):
            return None

        async def list_tools(self, timeout=None):
            return [SimpleNamespace(name="search")]

    async def _create_client(client_config, **kwargs):
        assert client_config is fresh_client
        return _McpClient()

    monkeypatch.setattr(
        runner_module,
        "_create_mcp_client_with_headers",
        _create_client,
    )

    async def _cleanup_clients(clients):
        return None

    monkeypatch.setattr(
        runner_module,
        "_cleanup_mcp_clients",
        _cleanup_clients,
    )

    result = await McpToolsAvailableStrategy().run(
        context,
        SkillReadinessCheckConfig(
            name="mcp_tools_available",
            params={"servers": [{"name": "toolbox", "tools": ["search"]}]},
        ),
    )

    assert result.status == "pass"


class _SlowMcpClient:
    async def connect(self):
        await asyncio.sleep(0.05)

    async def list_tools(self, timeout=None):
        return []


@pytest.mark.asyncio
async def test_mcp_tools_timeout_is_fail(tmp_path, monkeypatch):
    client_config = type(
        "ClientConfig",
        (),
        {"enabled": True, "name": "toolbox"},
    )()
    workspace = type(
        "Workspace",
        (),
        {
            "config": type(
                "Config",
                (),
                {
                    "mcp": type(
                        "Mcp",
                        (),
                        {"clients": {"toolbox": client_config}},
                    )(),
                },
            )(),
        },
    )()
    context = _context(tmp_path, monkeypatch)
    context.workspace = workspace
    monkeypatch.setattr(
        strategies_module,
        "_MCP_SERVER_TIMEOUT_SECONDS",
        0.001,
    )

    async def _create_client(*args, **kwargs):
        return _SlowMcpClient()

    monkeypatch.setattr(
        runner_module,
        "_create_mcp_client_with_headers",
        _create_client,
    )

    async def _cleanup(clients):
        return None

    monkeypatch.setattr(runner_module, "_cleanup_mcp_clients", _cleanup)

    result = await McpToolsAvailableStrategy().run(
        context,
        SkillReadinessCheckConfig(
            name="mcp_tools_available",
            params={"servers": [{"name": "toolbox", "tools": ["search"]}]},
        ),
    )

    assert result.status == "fail"
    assert result.details["failures"][0]["missing_tools"] == ["search"]


class _BrokenStrategy:
    name = "broken"
    display_name = "Broken"

    async def run(self, context, config):
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_strategy_registry_converts_technical_error_to_fail(
    tmp_path,
    monkeypatch,
):
    registry = SkillReadinessStrategyRegistry([_BrokenStrategy()])

    result = await registry.run_check(
        _context(tmp_path, monkeypatch),
        SkillReadinessCheckConfig(name="broken"),
    )

    assert result.status == "fail"
    assert result.message == "自检执行失败"

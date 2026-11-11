# -*- coding: utf-8 -*-
"""旧系统自动注册与回调停止，新用户与旧用户业务边界互不回退。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException
import pytest

from swe.app.crons.manager import CronManager
from swe.app.crons import system_bridge
from swe.app.routers import internal
from swe.app.source_system_config.task_scheduler import (
    SourceSchedulerIdentity,
    SourceSystemTaskScheduler,
)


@pytest.mark.asyncio
async def test_manager_startup_submits_fixed_new_intent_not_old_platform(
    monkeypatch,
):
    manager = object.__new__(CronManager)
    manager._agent_id = "default"
    manager._get_external_scheduler_business_identity = lambda spec=None: (
        "alice",
        "bank",
    )
    manager._scheduler_adapter = SimpleNamespace(
        register_job=AsyncMock(),
        update_job=AsyncMock(),
    )
    manager._save_system_job_ids = Mock(
        side_effect=AssertionError("old binding write"),
    )
    submit = AsyncMock(return_value={"submission_status": "accepted"})
    monkeypatch.setattr(system_bridge, "submit_agent_refresh", submit)
    await manager._register_system_jobs()
    submit.assert_awaited_once_with("alice", "bank", "default")
    manager._scheduler_adapter.register_job.assert_not_awaited()
    manager._scheduler_adapter.update_job.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["heartbeat", "dream"])
async def test_old_agent_system_callback_rejected_before_workspace_bootstrap(
    monkeypatch,
    kind,
):
    bootstrap = AsyncMock(side_effect=AssertionError("must not init"))
    monkeypatch.setattr(internal, "_require_callback_cron_manager", bootstrap)
    with pytest.raises(HTTPException) as error:
        await internal._run_agent_callback(
            SimpleNamespace(),
            {},
            "alice",
            "bank",
            "default",
            kind,
            "_old",
        )
    assert error.value.status_code == 410
    bootstrap.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["cleanup", "task_session_cleanup", "archive_maintenance"],
)
async def test_old_source_system_callback_rejected_without_business(kind):
    with pytest.raises(HTTPException) as error:
        await internal._run_source_callback(SimpleNamespace(), kind, "bank")
    assert error.value.status_code == 410


@pytest.mark.asyncio
async def test_source_refresh_only_submits_new_intent_without_reading_old_binding(
    monkeypatch,
):
    scheduler = SourceSystemTaskScheduler(None, None, "")
    identity = SourceSchedulerIdentity("alice", "alice", "alice")
    submit = AsyncMock(return_value={"submission_status": "accepted"})
    monkeypatch.setattr(system_bridge, "submit_source_refresh", submit)
    result = await scheduler.refresh_task_session_cleanup(
        "bank",
        object(),
        identity,
    )
    assert result.action == "pending"
    submit.assert_awaited_once_with("bank", identity, "task_session_cleanup")


@pytest.mark.asyncio
async def test_dream_config_save_precedes_submission_and_exposes_failure(
    monkeypatch,
):
    from swe.app.routers import agent
    from swe.config import AgentsRunningConfig

    order = []

    async def refresh(*args):
        order.append("submit")
        return {
            "submission_status": "failed",
            "error_code": "system_sync_not_submitted",
        }

    workspace = SimpleNamespace(
        agent_id="default",
        tenant_id="alice",
        cron_manager=SimpleNamespace(register_dream=refresh),
    )

    @asynccontextmanager
    async def update_scope(_request):
        yield workspace, SimpleNamespace()

    monkeypatch.setattr(agent, "agent_config_update_scope", update_scope)
    monkeypatch.setattr(system_bridge, "submit_workspace_refresh", refresh)
    monkeypatch.setattr(
        agent,
        "save_agent_config",
        lambda *a, **k: order.append("saved"),
    )
    monkeypatch.setattr(agent, "schedule_agent_reload", lambda *a, **k: None)
    config = AgentsRunningConfig()
    result = await agent.put_agents_running_config(config, SimpleNamespace())
    assert order == ["saved", "submit"]
    assert result.scheduler_sync["submission_status"] == "failed"
    assert result.memory_summary == config.memory_summary

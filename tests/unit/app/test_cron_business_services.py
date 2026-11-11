# -*- coding: utf-8 -*-
"""运行服务不借用旧管理器，也不把调度管理重新搬回 SWE。"""

from datetime import datetime, timezone
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.config.context import encode_scope_id


class RuntimeWorkspace:
    def __init__(self, path):
        self.workspace_dir = path
        self.agent_id = "research"
        self.tenant_id = encode_scope_id("alice", "bank")
        self.runner = SimpleNamespace(workspace_dir=path)
        self.channel_manager = SimpleNamespace(send_text=AsyncMock())
        self._source_system_config_service = None
        self._continuous_governance_service = None

    @property
    def cron_manager(self):
        raise AssertionError("business execution must not access CronManager")


@pytest.mark.asyncio
async def test_independent_notification_preserves_channel_identity_and_link(
    tmp_path,
    monkeypatch,
):
    from swe.app.crons.notification_sender import CronNotificationSender
    from tests.unit.app.test_managed_cron_notifications import _spec

    workspace = RuntimeWorkspace(tmp_path)
    sender = CronNotificationSender.from_workspace(workspace)
    monkeypatch.setattr(
        sender,
        "_get_zhaohu_push_config",
        lambda: SimpleNamespace(
            session_end_push_link_prefix="https://example.test/chat",
            session_end_push_link_id_type="session_id",
        ),
    )
    await sender.send_task_success_notification("job-1", job=_spec())
    payload = workspace.channel_manager.send_text.await_args.kwargs
    assert payload["channel"] == "zhaohu"
    assert payload["user_id"] == "alice"
    assert payload["session_id"] == "cron-task:job-1"
    assert (
        payload["meta"]["link_url"]
        == "https://example.test/chat?sessionId=cron-task:job-1"
    )
    assert "执行时的名称" in payload["text"]
    assert not (tmp_path / "jobs.json").exists()


@pytest.mark.asyncio
async def test_system_actions_run_heartbeat_without_old_manager(
    tmp_path,
    monkeypatch,
):
    from swe.app.crons.system_actions import SystemBusinessActions
    from swe.app.crons import heartbeat

    workspace = RuntimeWorkspace(tmp_path)
    execute = AsyncMock()
    monkeypatch.setattr(heartbeat, "run_heartbeat_once", execute)
    manager = SimpleNamespace(get_agent=AsyncMock(return_value=workspace))
    action = await SystemBusinessActions(
        SimpleNamespace(state=SimpleNamespace(multi_agent_manager=manager)),
    ).prepare(
        {
            "scope_kind": "agent",
            "task_type": "heartbeat",
            "tenant_id": "alice",
            "source_id": "bank",
            "agent_id": "research",
        },
    )
    await action()
    payload = execute.await_args.kwargs
    assert payload["runner"] is workspace.runner
    assert payload["agent_id"] == "research"
    assert payload["managed_execution"] is True
    assert payload["tenant_id"] == encode_scope_id("alice", "bank")
    assert not (tmp_path / "jobs.json").exists()


@pytest.mark.asyncio
async def test_dream_runs_real_business_without_legacy_manager(tmp_path):
    from swe.app.crons.system_actions import SystemBusinessActions

    workspace = RuntimeWorkspace(tmp_path)
    dream = AsyncMock()
    workspace.runner.memory_manager = SimpleNamespace(dream_memory=dream)
    action = await SystemBusinessActions(
        SimpleNamespace(
            state=SimpleNamespace(
                multi_agent_manager=SimpleNamespace(
                    get_agent=AsyncMock(return_value=workspace),
                ),
            ),
        ),
    ).prepare(
        {
            "scope_kind": "agent",
            "task_type": "dream",
            "tenant_id": "alice",
            "source_id": "bank",
            "agent_id": "research",
        },
    )
    await action()
    dream.assert_awaited_once_with(
        tenant_id=encode_scope_id("alice", "bank"),
        trigger="cron",
    )
    assert not (tmp_path / "jobs.json").exists()


@pytest.mark.asyncio
async def test_legacy_cleanup_adapter_preserves_file_task_metadata(tmp_path):
    from swe.app.crons.legacy_session_cleanup import (
        cleanup_legacy_workspace_sessions,
    )
    from swe.app.crons.models import JobsFile
    from swe.app.crons.repo.json_repo import JsonJobRepository
    from tests.unit.app.test_cron_task_session_cleanup import (
        _Session,
        _SourceSystemConfigService,
        _job,
    )

    workspace = RuntimeWorkspace(tmp_path)
    workspace._source_system_config_service = _SourceSystemConfigService()
    session = _Session(
        {
            "task_messages": [
                {"timestamp": "2026-01-01T00:00:00Z", "content": "expired"},
            ],
        },
    )
    workspace.runner.session = session
    repo = JsonJobRepository(tmp_path / "jobs.json")
    await repo.save(
        JobsFile(
            jobs=[
                _job(
                    meta={
                        "task_session_id": "task-1",
                        "creator_user_id": "alice",
                        "task_unread_execution_count": 5,
                    },
                ),
            ],
        ),
    )
    result = await cleanup_legacy_workspace_sessions(
        workspace,
        now=datetime(2026, 10, 22, tzinfo=timezone.utc),
    )
    assert result["messages_removed"] == 1
    assert result["sessions_cleaned"] == 1
    saved = await repo.get_job("job-1")
    assert saved.meta["task_unread_execution_count"] == 0
    assert saved.meta["task_has_scheduled_result"] is False
    assert session.lock_keys == [("task-1", "alice", 5.0)]


def test_shared_pruning_preserves_unrelated_session_data():
    from swe.app.crons.session_cleanup import _prune_task_session_state

    state = {
        "custom": {"value": 1},
        "task_messages": [
            {"timestamp": "2026-01-01T00:00:00Z", "content": "old"},
        ],
    }
    snapshot = _prune_task_session_state(
        state,
        datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    assert snapshot.state["custom"] == {"value": 1}
    assert snapshot.state["task_messages"] == []
    assert len(state["task_messages"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["heartbeat", "dream"])
async def test_configuration_save_submits_refresh_without_old_manager(
    tmp_path,
    monkeypatch,
    kind,
):
    from swe.app.crons import system_bridge
    from swe.app.routers import config, agent
    from swe.config import AgentsRunningConfig
    from swe.app import agent_context
    import importlib

    workspace = RuntimeWorkspace(tmp_path)
    saved = []

    async def submit(*args):
        assert saved == [True]
        assert args == ("alice", "bank", "research", kind)
        return {"submission_status": "accepted"}

    monkeypatch.setattr(system_bridge, "submit_agent_refresh", submit)
    context_owner = agent_context if kind == "heartbeat" else agent
    save_owner = (
        importlib.import_module("swe.config.config")
        if kind == "heartbeat"
        else agent
    )

    @asynccontextmanager
    async def update_scope(_request):
        yield workspace, SimpleNamespace()

    monkeypatch.setattr(
        context_owner,
        "agent_config_update_scope",
        update_scope,
    )
    monkeypatch.setattr(
        save_owner,
        "save_agent_config",
        lambda *a, **k: saved.append(True),
    )
    if kind == "heartbeat":
        result = await config.put_heartbeat(
            SimpleNamespace(),
            config.HeartbeatBody(),
        )
        sync = result["scheduler_sync"]
    else:
        monkeypatch.setattr(
            agent,
            "schedule_agent_reload",
            lambda *a, **k: None,
        )
        result = await agent.put_agents_running_config(
            AgentsRunningConfig(),
            SimpleNamespace(),
        )
        sync = result.scheduler_sync
    assert sync["submission_status"] == "accepted"


@pytest.mark.asyncio
async def test_legacy_cleanup_failure_does_not_block_managed_cleanup():
    from swe.app.source_system_config.task_scheduler import (
        SourceSystemTaskScheduler,
    )

    workspace = SimpleNamespace(
        cron_manager=SimpleNamespace(
            run_task_session_cleanup=AsyncMock(
                side_effect=RuntimeError("legacy broken"),
            ),
        ),
    )
    managed = AsyncMock(
        return_value={"sessions_seen": 2, "sessions_cleaned": 2},
    )
    scheduler = SourceSystemTaskScheduler(
        None,
        None,
        "",
        tenant_scope_store=SimpleNamespace(
            get_by_source=AsyncMock(
                return_value=[
                    {"tenant_id": "alice", "source_id": "bank"},
                ],
            ),
        ),
        multi_agent_manager=SimpleNamespace(
            get_agent=AsyncMock(return_value=workspace),
        ),
    )
    result = await scheduler.run_task_session_cleanup(
        source_id="bank",
        managed_cleanup=managed,
    )
    managed.assert_awaited_once_with(workspace, "alice", "bank")
    assert result["scopes_failed"] == 1
    assert result["sessions_cleaned"] == 2
    assert "legacy broken" in result["errors"][0]["error"]
    old_result = await scheduler.run_task_session_cleanup(source_id="bank")
    assert old_result["scopes_failed"] == 1
    assert old_result["results"] == []


@pytest.mark.asyncio
async def test_workspace_refresh_hook_is_independent_and_best_effort(
    tmp_path,
    monkeypatch,
):
    from swe.app.workspace.service_factories import refresh_system_cron
    from swe.app.crons import system_bridge

    workspace = RuntimeWorkspace(tmp_path)
    submit = AsyncMock(return_value={"submission_status": "accepted"})
    monkeypatch.setattr(system_bridge, "submit_workspace_refresh", submit)
    await refresh_system_cron(workspace, None)
    submit.assert_awaited_once_with(workspace, None)
    submit.side_effect = RuntimeError("platform unavailable")
    await refresh_system_cron(workspace, None)


@pytest.mark.asyncio
async def test_workspace_registers_independent_refresh_without_removing_legacy(
    tmp_path,
    monkeypatch,
):
    from swe.app.workspace import Workspace
    from swe.app.crons import system_bridge
    from swe.app.crons.manager import CronManager

    workspace = Workspace(
        "research",
        str(tmp_path),
        encode_scope_id("alice", "bank"),
    )
    services = workspace._service_manager
    assert services.descriptors["cron_manager"].service_class is CronManager
    submit = AsyncMock(return_value={"submission_status": "accepted"})
    monkeypatch.setattr(system_bridge, "submit_agent_refresh", submit)
    await services._start_service(services.descriptors["system_cron_refresh"])
    assert workspace.cron_manager is None
    submit.assert_awaited_once_with("alice", "bank", "research", None)


@pytest.mark.asyncio
async def test_legacy_startup_still_restores_user_timers_without_duplicate_system_refresh():
    from swe.app.crons.manager import CronManager

    cron = CronManager(
        repo=SimpleNamespace(),
        runner=SimpleNamespace(),
        channel_manager=None,
    )
    cron._restore_external_job_ids = AsyncMock()
    cron._prefetch_loop = AsyncMock()
    cron._register_system_jobs = AsyncMock()
    try:
        await cron.start()
        await cron.start()
        cron._restore_external_job_ids.assert_awaited_once()
        cron._register_system_jobs.assert_not_awaited()
    finally:
        await cron.stop()

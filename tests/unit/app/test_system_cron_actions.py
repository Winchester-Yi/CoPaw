# -*- coding: utf-8 -*-
"""固定系统业务适配不启动无关runtime，也不混回旧系统路径。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.crons.system_actions import SystemBusinessActions


@pytest.mark.asyncio
async def test_archive_runs_source_maintenance_without_initializing_agent():
    source = SimpleNamespace(
        run_archive_maintenance=AsyncMock(return_value={"files_archived": 2}),
        run_task_session_cleanup=AsyncMock(),
    )
    manager = SimpleNamespace(get_agent=AsyncMock())
    app = SimpleNamespace(
        state=SimpleNamespace(
            source_system_task_scheduler=source,
            multi_agent_manager=manager,
        ),
    )
    action = await SystemBusinessActions(app).prepare(
        {
            "scope_kind": "source",
            "task_type": "archive_maintenance",
            "source_id": "bank",
        },
    )
    assert await action() == {"files_archived": 2}
    manager.get_agent.assert_not_awaited()
    source.run_task_session_cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_dream_excludes_archive_and_keeps_correct_scope(
    monkeypatch,
):
    from swe.config.context import encode_scope_id
    from swe.app.crons.system_runtime import SystemTaskRuntime

    cron = SimpleNamespace(run_dream=AsyncMock(), run_heartbeat=AsyncMock())
    monkeypatch.setattr(SystemTaskRuntime, "run_dream", cron.run_dream)
    monkeypatch.setattr(SystemTaskRuntime, "run_heartbeat", cron.run_heartbeat)
    manager = SimpleNamespace(
        get_agent=AsyncMock(
            return_value=SimpleNamespace(
                runner=object(),
                channel_manager=None,
                agent_id="default",
                tenant_id=encode_scope_id("alice", "bank"),
            ),
        ),
    )
    app = SimpleNamespace(state=SimpleNamespace(multi_agent_manager=manager))
    action = await SystemBusinessActions(app).prepare(
        {
            "scope_kind": "agent",
            "task_type": "dream",
            "tenant_id": "alice",
            "source_id": "bank",
            "agent_id": "default",
        },
    )
    manager.get_agent.assert_awaited_once_with(
        "default",
        tenant_id=encode_scope_id("alice", "bank"),
    )
    await action()
    cron.run_dream.assert_awaited_once_with(include_archive_maintenance=False)
    cron.run_heartbeat.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_source_cleanup_includes_database_sessions_in_original_scope():
    from swe.app.source_system_config.task_scheduler import (
        SourceSystemTaskScheduler,
    )

    legacy = SimpleNamespace(
        run_task_session_cleanup=AsyncMock(
            return_value={"sessions_seen": 1, "sessions_cleaned": 1},
        ),
    )
    workspace = SimpleNamespace(cron_manager=legacy)
    manager = SimpleNamespace(get_agent=AsyncMock(return_value=workspace))
    source = SourceSystemTaskScheduler(
        None,
        None,
        "",
        tenant_scope_store=SimpleNamespace(
            get_by_source=AsyncMock(
                return_value=[{"tenant_id": "alice", "source_id": "bank"}],
            ),
        ),
        multi_agent_manager=manager,
    )
    managed = AsyncMock(
        return_value={"sessions_seen": 2, "sessions_cleaned": 2},
    )
    result = await source.run_task_session_cleanup(
        source_id="bank",
        managed_cleanup=managed,
    )
    managed.assert_awaited_once_with(workspace, "alice", "bank")
    legacy.run_task_session_cleanup.assert_awaited_once()
    assert (
        result["scopes_seen"] == 1
        and result["sessions_seen"] == result["sessions_cleaned"] == 3
    )


@pytest.mark.asyncio
async def test_managed_cleanup_failure_keeps_completed_legacy_counts_as_partial():
    from swe.app.crons.system_execution import business_outcome
    from swe.app.source_system_config.task_scheduler import (
        SourceSystemTaskScheduler,
    )

    workspace = SimpleNamespace(
        cron_manager=SimpleNamespace(
            run_task_session_cleanup=AsyncMock(
                return_value={"sessions_seen": 1, "sessions_cleaned": 1},
            ),
        ),
    )
    scheduler = SourceSystemTaskScheduler(
        None,
        None,
        "",
        tenant_scope_store=SimpleNamespace(
            get_by_source=AsyncMock(
                return_value=[{"tenant_id": "alice", "source_id": "bank"}],
            ),
        ),
        multi_agent_manager=SimpleNamespace(
            get_agent=AsyncMock(return_value=workspace),
        ),
    )
    result = await scheduler.run_task_session_cleanup(
        source_id="bank",
        managed_cleanup=AsyncMock(
            side_effect=RuntimeError("database unavailable"),
        ),
    )
    assert result["scopes_failed"] == 1 and result["sessions_cleaned"] == 1
    assert business_outcome(result)[0] == "partial"

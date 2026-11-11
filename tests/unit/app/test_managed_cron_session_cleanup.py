# -*- coding: utf-8 -*-
"""新版会话清理使用完整DB身份，复用会话锁且不触碰jobs.json或历史SQL。"""

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from swe.app.crons.managed_session_cleanup import ManagedTaskSessionCleaner
from tests.fakes.system_cron_database import SystemDatabase

NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)
POLICY = SimpleNamespace(enabled=True, retention_days=30)


class Session:
    """仅替换文件边界，原裁剪算法仍由生产代码执行。"""

    def __init__(self):
        self.states = {}
        self.calls = []
        self.before = None
        self.locked = False

    async def mutate_session_state(
        self,
        *,
        session_id,
        user_id,
        mutator,
        **kwargs,
    ):
        self.calls.append((session_id, user_id, kwargs))
        if self.locked:
            raise TimeoutError("busy")
        if self.before is not None:
            await self.before()
        self.states[session_id] = mutator(deepcopy(self.states[session_id]))


@pytest.fixture
def cleanup_setup():
    db = SystemDatabase()
    db.conn.create_collation(
        "utf8mb4_bin",
        lambda left, right: (left > right) - (left < right),
    )
    db.conn.executescript("""
        CREATE TABLE swe_cron_jobs_v2 (tenant_id TEXT COLLATE NOCASE,source_id TEXT COLLATE NOCASE,agent_id TEXT COLLATE NOCASE,job_id TEXT COLLATE NOCASE,creator_user_id TEXT,management_owner TEXT,deleted_at TEXT);
        CREATE TABLE swe_cron_job_states (tenant_id TEXT COLLATE NOCASE,source_id TEXT COLLATE NOCASE,agent_id TEXT COLLATE NOCASE,job_id TEXT COLLATE NOCASE,task_session_id TEXT,has_scheduled_result INTEGER,latest_scheduled_preview TEXT,last_scheduled_run_at TEXT,unread_execution_count INTEGER,state_version INTEGER,updated_at TEXT);
    """)
    session = Session()
    workspace = SimpleNamespace(
        agent_id="default",
        runner=SimpleNamespace(session=session),
    )
    yield db, session, workspace
    db.conn.close()


async def binding(
    setup,
    job_id="job-1",
    *,
    source="bank",
    agent="default",
    recent=False,
):
    db, session, _ = setup
    stamp = "2026-10-19T00:00:00" if recent else "2026-08-01T00:00:00"
    await db.execute(
        "INSERT INTO swe_cron_jobs_v2 VALUES (%s,%s,%s,%s,'alice','scheduler',NULL)",
        ("alice", source, agent, job_id),
    )
    await db.execute(
        "INSERT INTO swe_cron_job_states VALUES (%s,%s,%s,%s,%s,1,'preview',%s,8,1,NULL)",
        ("alice", source, agent, job_id, job_id, stamp),
    )
    runs = [
        {
            "run_id": "old",
            "ended_at": "2026-08-01T00:00:00Z",
            "memory_start": 0,
            "memory_end": 1,
            "preview_text": "old",
        },
    ]
    content = ["old"]
    if recent:
        runs.append(
            {
                "run_id": "recent",
                "ended_at": "2026-10-19T00:00:00Z",
                "memory_start": 1,
                "memory_end": 2,
                "preview_text": "recent",
            },
        )
        content.append("recent")
    session.states[job_id] = {
        "task_runs": runs,
        "agent": {"memory": {"content": content}},
    }


@pytest.mark.asyncio
async def test_expired_managed_session_is_pruned_and_state_cleared_without_file_projection(
    cleanup_setup,
):
    db, session, workspace = cleanup_setup
    await binding(cleanup_setup)
    result = await ManagedTaskSessionCleaner(db, clock=lambda: NOW).cleanup(
        workspace,
        "alice",
        "bank",
        POLICY,
    )
    assert result["sessions_cleaned"] == result["runs_removed"] == 1
    assert session.states["job-1"]["task_runs"] == []
    row = await db.fetch_one("SELECT * FROM swe_cron_job_states")
    assert row["has_scheduled_result"] == row["unread_execution_count"] == 0
    assert row["state_version"] == 2
    assert all("swe_cron_executions" not in query for query in db.statements)
    assert session.calls[0][2]["timeout_seconds"] == 5.0


@pytest.mark.asyncio
async def test_retained_recent_result_keeps_authoritative_database_preview_and_unread(
    cleanup_setup,
):
    db, session, workspace = cleanup_setup
    await binding(cleanup_setup, recent=True)
    result = await ManagedTaskSessionCleaner(db, clock=lambda: NOW).cleanup(
        workspace,
        "alice",
        "bank",
        POLICY,
    )
    assert result["runs_removed"] == 1
    assert [run["run_id"] for run in session.states["job-1"]["task_runs"]] == [
        "recent",
    ]
    row = await db.fetch_one("SELECT * FROM swe_cron_job_states")
    assert row["unread_execution_count"] == 8 and row["state_version"] == 1


@pytest.mark.asyncio
async def test_new_result_racing_cleanup_cannot_be_overwritten(cleanup_setup):
    db, session, workspace = cleanup_setup
    await binding(cleanup_setup)

    async def new_result():
        await db.execute(
            "UPDATE swe_cron_job_states SET state_version=2,latest_scheduled_preview='new',last_scheduled_run_at='2026-10-20T00:00:00',unread_execution_count=9",
        )

    session.before = new_result
    await ManagedTaskSessionCleaner(db, clock=lambda: NOW).cleanup(
        workspace,
        "alice",
        "bank",
        POLICY,
    )
    row = await db.fetch_one("SELECT * FROM swe_cron_job_states")
    assert (
        row["latest_scheduled_preview"] == "new"
        and row["unread_execution_count"] == 9
    )
    assert row["has_scheduled_result"] == 1


@pytest.mark.asyncio
async def test_cleanup_reads_all_pages_but_not_other_source_or_agent(
    cleanup_setup,
):
    db, session, workspace = cleanup_setup
    for index in range(201):
        await binding(cleanup_setup, f"job-{index:04}")
    await binding(cleanup_setup, "foreign", source="other")
    await binding(cleanup_setup, "other-agent", agent="research")
    result = await ManagedTaskSessionCleaner(db, clock=lambda: NOW).cleanup(
        workspace,
        "alice",
        "bank",
        POLICY,
    )
    assert result["sessions_seen"] == result["sessions_cleaned"] == 201
    assert len(session.states["foreign"]["task_runs"]) == 1
    assert len(session.states["other-agent"]["task_runs"]) == 1


@pytest.mark.asyncio
async def test_locked_session_is_skipped_without_clearing_state(cleanup_setup):
    db, session, workspace = cleanup_setup
    await binding(cleanup_setup)
    session.locked = True
    result = await ManagedTaskSessionCleaner(db, clock=lambda: NOW).cleanup(
        workspace,
        "alice",
        "bank",
        POLICY,
    )
    assert (
        result["sessions_skipped_locked"] == 1
        and result["sessions_cleaned"] == 0
    )
    assert (await db.fetch_one("SELECT * FROM swe_cron_job_states"))[
        "state_version"
    ] == 1


@pytest.mark.asyncio
async def test_missing_managed_state_is_not_silently_treated_as_no_tasks(
    cleanup_setup,
):
    db, _, workspace = cleanup_setup
    await binding(cleanup_setup)
    await db.execute("DELETE FROM swe_cron_job_states")
    with pytest.raises(RuntimeError, match="cron_state_record_missing"):
        await ManagedTaskSessionCleaner(db, clock=lambda: NOW).cleanup(
            workspace,
            "alice",
            "bank",
            POLICY,
        )


@pytest.mark.asyncio
async def test_system_source_action_reaches_database_cleanup_after_scope_enumeration(
    cleanup_setup,
):
    from unittest.mock import AsyncMock
    from swe.app.crons.system_actions import SystemBusinessActions
    from swe.app.source_system_config.task_scheduler import (
        SourceSystemTaskScheduler,
    )

    db, session, workspace = cleanup_setup
    await binding(cleanup_setup)
    workspace.cron_manager = SimpleNamespace(
        run_task_session_cleanup=AsyncMock(return_value={}),
    )
    source = SourceSystemTaskScheduler(
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
    config = SimpleNamespace(
        resolve_config=AsyncMock(
            return_value={
                "cron_task_session_cleanup": {
                    "enabled": True,
                    "retention_days": 30,
                    "cron": "0 1 * * *",
                },
            },
        ),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            source_system_task_scheduler=source,
            source_system_config_service=config,
            db_connection=db,
        ),
    )
    action = await SystemBusinessActions(app).prepare(
        {
            "scope_kind": "source",
            "task_type": "task_session_cleanup",
            "source_id": "bank",
        },
    )
    result = await action()
    assert result["sessions_cleaned"] == 1
    assert session.states["job-1"]["task_runs"] == []
    config.resolve_config.assert_awaited_once_with("bank", force_refresh=True)


@pytest.mark.asyncio
async def test_case_insensitive_database_must_not_expand_cleanup_scope(
    cleanup_setup,
):
    db, session, workspace = cleanup_setup
    await binding(cleanup_setup, "foreign", source="BANK")
    result = await ManagedTaskSessionCleaner(db, clock=lambda: NOW).cleanup(
        workspace,
        "alice",
        "bank",
        POLICY,
    )
    assert result["sessions_seen"] == 0 and session.calls == []


@pytest.mark.asyncio
async def test_case_distinct_state_cannot_supply_a_foreign_session_binding(
    cleanup_setup,
):
    db, session, workspace = cleanup_setup
    await binding(cleanup_setup)
    await db.execute("UPDATE swe_cron_job_states SET source_id='BANK'")
    with pytest.raises(RuntimeError, match="cron_state_record_missing"):
        await ManagedTaskSessionCleaner(db, clock=lambda: NOW).cleanup(
            workspace,
            "alice",
            "bank",
            POLICY,
        )
    assert session.calls == []

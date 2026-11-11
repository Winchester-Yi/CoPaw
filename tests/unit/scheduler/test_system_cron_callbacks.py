# -*- coding: utf-8 -*-
"""系统每次触发独立登记；同身份在途和待核实均不能排队重跑。"""

import asyncio
from datetime import timedelta

import pytest

from scheduler.app.services.cron.system.executions import SystemExecutionStore
from scheduler.app.services.cron.system.platform import PROTOCOL
from tests.unit.scheduler.test_system_cron_management import (
    ADMIN,
    USER,
    CONFIG,
    system_setup,
)


async def ready(setup, kind="heartbeat"):
    """通过控制面形成有效的新绑定，不接管旧任务。"""
    db, management, now = setup
    row = await management.refresh(ADMIN, kind, "save")
    claimed = await management.claim(row["job_id"], "sync")
    await management.prepare(claimed, CONFIG)
    await management.finish(claimed, "123", CONFIG)
    job = await management.get(row["job_id"])
    store = SystemExecutionStore(db, clock=lambda: now[0])
    body = {
        "job_id": job["job_id"],
        "task_type": job["task_type"],
        "operation_id": job["effective_operation_id"],
        "callback_protocol": PROTOCOL,
        "tenant_id": "alice",
        "source_id": "bank",
        "agent_id": "default",
        "fromId": "alice",
    }
    return store, job, body


@pytest.mark.asyncio
async def test_corrupt_execution_scope_cannot_be_dispatched_under_matching_job_id(
    system_setup,
):
    store, _, body = await ready(system_setup)
    run = await store.accept(USER, body)
    await system_setup[0].execute(
        "UPDATE swe_cron_system_executions SET source_id='foreign' WHERE execution_id=%s",
        (run["execution_id"],),
    )
    with pytest.raises(LookupError):
        await store.claim_dispatch(run["execution_id"], "dispatch")


@pytest.mark.asyncio
async def test_same_body_creates_new_records_but_only_one_active_execution(
    system_setup,
):
    db = system_setup[0]
    store, job, body = await ready(system_setup)
    first = await store.accept(USER, body)
    second = await store.accept(USER, body)
    assert first["accepted"] and first["status"] == "pending"
    assert not second["accepted"] and second["status"] == "skipped"
    assert second["reason"] == "system_task_in_flight"
    assert first["execution_id"] != second["execution_id"]
    rows = await db.fetch_all("SELECT * FROM swe_cron_system_executions")
    assert len(rows) == 2
    current = await system_setup[1].get(job["job_id"])
    assert (
        current["active_execution_id"] == first["execution_id"]
        and current["execution_fence"] == 1
    )
    assert all(
        "dispatch_intents" not in sql and "swe_cron_jobs_v2" not in sql
        for sql in db.statements
    )


@pytest.mark.asyncio
async def test_source_callback_needs_no_manager_header_or_user_pool(
    system_setup,
):
    store, _, body = await ready(system_setup, "task_session_cleanup")
    response = await store.accept(USER, body)
    assert response["accepted"]


@pytest.mark.asyncio
async def test_concurrent_callbacks_never_allocate_two_active_runs(
    system_setup,
):
    store, _, body = await ready(system_setup)
    results = await asyncio.gather(
        store.accept(USER, body),
        store.accept(USER, body),
    )
    assert sum(result["accepted"] for result in results) == 1


@pytest.mark.asyncio
async def test_system_tasks_do_not_apply_user_holiday_filter(system_setup):
    store, _, body = await ready(system_setup)
    system_setup[2][0] += timedelta(days=5)
    assert system_setup[2][0].weekday() == 6
    assert (await store.accept(USER, body))["accepted"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"operation_id": "unknown"},
        {"callback_protocol": "scheduler_managed_v1"},
        {"task_type": "dream"},
        {"external_job_id": "999"},
    ],
)
async def test_invalid_binding_references_never_create_business_work(
    system_setup,
    changes,
):
    db = system_setup[0]
    store, _, body = await ready(system_setup)
    with pytest.raises((ValueError, LookupError)):
        await store.accept(USER, {**body, **changes})
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_system_executions",
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_known_binding_disabled_in_database_records_skipped_trigger(
    system_setup,
):
    _, management, _ = system_setup
    store, job, body = await ready(system_setup)
    await management.refresh(ADMIN, "heartbeat", "disable")
    claimed = await management.claim(job["job_id"], "sync")
    await management.prepare(claimed, {**CONFIG, "enabled": False})
    result = await store.accept(USER, body)
    assert (
        not result["accepted"] and result["reason"] == "system_task_disabled"
    )


@pytest.mark.asyncio
async def test_unknown_execution_blocks_until_admin_confirms_ended_without_rerun(
    system_setup,
):
    db, management, now = system_setup
    store, job, body = await ready(system_setup)
    first = await store.accept(USER, body)
    await db.execute(
        "UPDATE swe_cron_system_executions SET status='running',run_owner='swe',run_lease_until=%s WHERE execution_id=%s",
        (now[0] - timedelta(seconds=1), first["execution_id"]),
    )
    assert await store.mark_unknown(first["execution_id"])
    blocked = await store.accept(USER, body)
    assert blocked["reason"] == "system_result_unconfirmed"
    run = await store.get(first["execution_id"])
    command = {
        "expected_version": run["version"],
        "request_id": "confirm",
        "confirmed_ended": True,
        "reason": "已核实旧进程结束",
    }
    with pytest.raises(PermissionError):
        await store.confirm_ended(
            USER,
            job["job_id"],
            run["execution_id"],
            command,
        )
    await store.confirm_ended(
        ADMIN,
        job["job_id"],
        run["execution_id"],
        command,
    )
    duplicate = await store.confirm_ended(
        ADMIN,
        job["job_id"],
        run["execution_id"],
        command,
    )
    assert duplicate["status"] == "unknown"
    current = await management.get(job["job_id"])
    assert current["active_execution_id"] is None
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_system_executions",
        ).fetchone()[0]
        == 2
    )
    next_run = await store.accept(USER, body)
    assert (
        next_run["accepted"]
        and next_run["execution_id"] != first["execution_id"]
    )
    await store.confirm_ended(
        ADMIN,
        job["job_id"],
        run["execution_id"],
        command,
    )
    assert (await management.get(job["job_id"]))[
        "active_execution_id"
    ] == next_run["execution_id"]


@pytest.mark.asyncio
async def test_dispatch_reuses_execution_id_and_fences_transferred_transport(
    system_setup,
):
    _, _, now = system_setup
    store, _, body = await ready(system_setup)
    accepted = await store.accept(USER, body)
    first = await store.claim_dispatch(accepted["execution_id"], "first")
    assert first["status"] == "dispatching" and first["dispatch_attempt"] == 1
    assert (
        await store.claim_dispatch(accepted["execution_id"], "other") is None
    )
    now[0] += timedelta(seconds=61)
    second = await store.claim_dispatch(accepted["execution_id"], "second")
    assert second["execution_id"] == first["execution_id"]
    assert (
        second["dispatch_owner"] == "second"
        and second["dispatch_attempt"] == 2
    )
    assert not await store.dispatch_failed(first["execution_id"], "first")


@pytest.mark.asyncio
async def test_transport_failure_never_marks_already_running_work_failed(
    system_setup,
):
    db = system_setup[0]
    store, job, body = await ready(system_setup)
    accepted = await store.accept(USER, body)
    run = await store.claim_dispatch(accepted["execution_id"], "transport")
    await db.execute(
        "UPDATE swe_cron_system_executions SET status='running',run_owner='swe' WHERE execution_id=%s",
        (run["execution_id"],),
    )
    assert not await store.dispatch_failed(run["execution_id"], "transport")
    assert (await store.get(run["execution_id"]))["status"] == "running"

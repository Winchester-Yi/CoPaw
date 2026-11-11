# -*- coding: utf-8 -*-
"""SWE直接写系统共库结果，开始前复核启用，迟到执行者不能清除新围栏。"""

import json

import pytest

from swe.app.crons.system_results import (
    SystemResultWriter,
    SystemExecutionLeaseLost,
)
from tests.unit.scheduler.test_system_cron_callbacks import ready
from tests.unit.scheduler.test_system_cron_management import (
    ADMIN,
    USER,
    CONFIG,
    system_setup,
)


async def accepted_run(setup):
    db, _, now = setup
    scheduler, job, body = await ready(setup)
    accepted = await scheduler.accept(USER, body)
    dispatched = await scheduler.claim_dispatch(
        accepted["execution_id"],
        "dispatch",
    )
    writer = SystemResultWriter(db, clock=lambda: now[0])
    await writer.accept(dispatched["execution_id"], job["job_id"], "dispatch")
    return writer, scheduler, job, dispatched, body


@pytest.mark.asyncio
async def test_accept_and_start_are_idempotent_and_finish_never_touches_user_tables(
    system_setup,
):
    db, management, _ = system_setup
    writer, scheduler, job, run, _ = await accepted_run(system_setup)
    repeated = await writer.accept(
        run["execution_id"],
        job["job_id"],
        "dispatch",
    )
    assert repeated["status"] == "accepted"
    started = await writer.start(
        run["execution_id"],
        job["job_id"],
        "swe",
        CONFIG,
    )
    assert started["status"] == "running"
    assert (
        await writer.start(run["execution_id"], job["job_id"], "other", CONFIG)
        is None
    )
    assert await writer.finish(
        run["execution_id"],
        job["job_id"],
        "swe",
        "success",
        result={
            "sessions_cleaned": 2,
            "password": "secret",
            "preview": "private",
        },
    )
    final = await scheduler.get(run["execution_id"])
    assert final["status"] == "success"
    assert json.loads(final["result_json"]) == {"sessions_cleaned": 2}
    assert (await management.get(job["job_id"]))["active_execution_id"] is None
    assert all(
        "swe_cron_job_states" not in query
        and "swe_cron_executions" not in query
        for query in db.statements
    )


@pytest.mark.asyncio
async def test_stale_dispatch_token_cannot_accept_new_work(system_setup):
    db, _, now = system_setup
    scheduler, job, body = await ready(system_setup)
    accepted = await scheduler.accept(USER, body)
    await scheduler.claim_dispatch(accepted["execution_id"], "current")
    writer = SystemResultWriter(db, clock=lambda: now[0])
    with pytest.raises(SystemExecutionLeaseLost):
        await writer.accept(accepted["execution_id"], job["job_id"], "old")
    assert (await scheduler.get(accepted["execution_id"]))[
        "status"
    ] == "dispatching"


@pytest.mark.asyncio
async def test_saved_disable_stops_start_even_when_scheduler_refresh_was_lost(
    system_setup,
):
    _, management, _ = system_setup
    writer, scheduler, job, run, _ = await accepted_run(system_setup)
    assert (await management.get(job["job_id"]))["enabled"]
    assert (
        await writer.start(
            run["execution_id"],
            job["job_id"],
            "swe",
            {**CONFIG, "enabled": False},
        )
        is None
    )
    final = await scheduler.get(run["execution_id"])
    assert final["status"] == "skipped" and final["started_at"] is None
    assert (await management.get(job["job_id"]))["active_execution_id"] is None


@pytest.mark.asyncio
async def test_unknown_run_cannot_complete_or_overwrite_after_admin_resolution(
    system_setup,
):
    _, management, _ = system_setup
    writer, scheduler, job, run, body = await accepted_run(system_setup)
    await writer.start(run["execution_id"], job["job_id"], "swe", CONFIG)
    assert await writer.unknown(
        run["execution_id"],
        job["job_id"],
        "swe",
        "system_execution_timeout",
    )
    assert not await writer.finish(
        run["execution_id"],
        job["job_id"],
        "swe",
        "success",
    )
    old = await scheduler.get(run["execution_id"])
    await scheduler.confirm_ended(
        ADMIN,
        job["job_id"],
        run["execution_id"],
        {
            "expected_version": old["version"],
            "request_id": "confirm",
            "confirmed_ended": True,
            "reason": "已核实旧进程结束",
        },
    )
    newer = await scheduler.accept(USER, body)
    assert newer["accepted"]
    assert not await writer.finish(
        run["execution_id"],
        job["job_id"],
        "swe",
        "success",
    )
    assert (await management.get(job["job_id"]))[
        "active_execution_id"
    ] == newer["execution_id"]


@pytest.mark.asyncio
async def test_corrupt_execution_identity_never_accepts_under_matching_job_id(
    system_setup,
):
    db, _, now = system_setup
    scheduler, job, body = await ready(system_setup)
    accepted = await scheduler.accept(USER, body)
    await scheduler.claim_dispatch(accepted["execution_id"], "dispatch")
    await db.execute(
        "UPDATE swe_cron_system_executions SET source_id='foreign' WHERE execution_id=%s",
        (accepted["execution_id"],),
    )
    writer = SystemResultWriter(db, clock=lambda: now[0])
    with pytest.raises(LookupError):
        await writer.accept(
            accepted["execution_id"],
            job["job_id"],
            "dispatch",
        )

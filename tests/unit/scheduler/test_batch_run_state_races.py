from datetime import datetime

import pytest

from tests.unit.scheduler.test_batch_run_state import control_db  # noqa: F401


async def queued_claim(db, *, paused=False, enabled=True):
    from scheduler.app.services.cron import batch_run_state as state

    db.add_job()
    db.add_job(
        "job-1",
        enabled=enabled,
        meta={
            "broadcast_dispatch_intents_enabled": True,
            "broadcast_source_job_id": "parent",
        },
    )
    await state.initialize_missing_controls(db)
    db.insert(
        "swe_cron_dispatch_batches",
        batch_id="batch-1",
        source_id="source-a",
        tenant_id="tenant-1",
        parent_job_id="parent",
    )
    db.add_intent(status="claimed", claim_token="token-1")
    if paused:
        await db.execute("UPDATE swe_cron_dispatch_controls SET paused=1")
    return dict(
        id=7,
        batch_id="batch-1",
        source_id="source-a",
        job_id="job-1",
        tenant_id="tenant-1",
        attempt_count=1,
        claim_token="token-1",
    )


@pytest.mark.asyncio
async def test_pause_before_handoff_refunds_and_rejects_stale_claim(
    control_db,
):
    from scheduler.app.services.cron import dispatch_gate as gate

    row = await queued_claim(control_db, paused=True)
    assert (
        await gate.prepare_handoff(control_db, row, "worker-1", datetime.now())
        == "paused"
    )
    assert control_db.intent()["status"] == "pending"
    assert control_db.intent()["attempt_count"] == 0
    await control_db.execute("UPDATE swe_cron_dispatch_controls SET paused=0")
    await control_db.execute(
        "UPDATE swe_cron_dispatch_intents SET status='claimed', "
        "attempt_count=1,claim_token='token-2',lock_owner='worker-1'"
    )
    assert (
        await gate.prepare_handoff(control_db, row, "worker-1", datetime.now())
        == "stale"
    )
    assert control_db.intent()["claim_token"] == "token-2"


@pytest.mark.asyncio
async def test_handoff_before_pause_is_not_refunded(control_db):
    from scheduler.app.services.cron import dispatch_gate as gate
    from scheduler.app.services.cron import batch_run_state as state

    row = await queued_claim(control_db)
    assert (
        await gate.prepare_handoff(control_db, row, "worker-1", datetime.now())
        == "ready"
    )
    await state.set_run_state(
        "source-a", "tenant-1", "parent", "admin", True, 1
    )
    assert control_db.intent()["status"] == "acknowledged"
    assert control_db.intent()["attempt_count"] == 1


@pytest.mark.asyncio
async def test_disabled_child_is_terminal_skip_not_failed(control_db):
    from scheduler.app.services.cron import dispatch_gate as gate

    row = await queued_claim(control_db, enabled=False)
    assert (
        await gate.prepare_handoff(control_db, row, "worker-1", datetime.now())
        == "skipped"
    )
    stored = control_db.intent()
    assert stored["status"] == "skipped"
    assert stored["lock_owner"] == ""
    assert stored["locked_at"] is None
    assert stored["attempt_count"] == 1
    assert stored["max_attempts"] == 3
    assert "关闭" in stored["error_message"]


@pytest.mark.asyncio
async def test_disabled_parent_does_not_gate_enabled_child(control_db):
    from scheduler.app.services.cron import dispatch_gate as gate

    row = await queued_claim(control_db)
    await control_db.execute(
        "UPDATE swe_cron_jobs SET enabled=0,status='paused' WHERE id='parent'"
    )
    assert (
        await gate.prepare_handoff(control_db, row, "worker-1", datetime.now())
        == "ready"
    )

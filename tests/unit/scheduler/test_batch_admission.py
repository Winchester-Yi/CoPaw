from datetime import datetime, timedelta

import pytest

from tests.unit.scheduler.test_batch_run_state import control_db  # noqa: F401


@pytest.mark.asyncio
async def test_running_control_admits_disabled_parent_and_duplicate_is_idempotent(
    control_db,
):
    from scheduler.app.services.cron.batch_admission import admit_batch
    from scheduler.app.services.cron import batch_run_state as state

    db = control_db
    for column in (
        "parent_external_job_id",
        "provider_id",
        "model_id",
        "agent_id",
        "scheduled_fire_at",
        "callback_received_at",
        "callback_metadata",
    ):
        await db.execute(
            f"ALTER TABLE swe_cron_dispatch_batches ADD COLUMN {column} TEXT"
        )
    for column in (
        "intent_role",
        "agent_id",
        "parent_job_id",
        "scheduled_fire_at",
        "viewer_heat_score",
        "payload",
    ):
        await db.execute(
            f"ALTER TABLE swe_cron_dispatch_intents ADD COLUMN {column} TEXT"
        )
    db.add_job(enabled=False)
    await state.initialize_missing_controls(db)
    await db.execute("UPDATE swe_cron_dispatch_controls SET paused=0")
    now = datetime(2026, 9, 9)
    batch = dict(
        batch_id="b",
        source_id="source-a",
        tenant_id="tenant-1",
        parent_job_id="parent",
        parent_external_job_id="",
        provider_id="default",
        model_id="default",
        agent_id="default",
        scheduled_fire_at=now,
        callback_received_at=now,
        callback_metadata={},
    )
    row = dict(
        intent_role="parent",
        source_id="source-a",
        provider_id="default",
        model_id="default",
        tenant_id="tenant-1",
        agent_id="default",
        job_id="parent",
        parent_job_id="parent",
        due_at=now,
        dispatch_order=0,
        viewer_heat_score=0,
        payload={},
    )
    first = await admit_batch(db, batch, [row], physical_fire_at=now)
    assert len(first["intent_ids"]) == 1
    second = await admit_batch(db, batch, [row], physical_fire_at=now)
    assert second["intent_ids"] == first["intent_ids"]
    assert (
        len(await db.fetch_all("SELECT * FROM swe_cron_dispatch_intents")) == 1
    )
    events = await db.fetch_all(
        "SELECT event_type FROM swe_cron_dispatch_events WHERE batch_id='b' ORDER BY id"
    )
    assert [row["event_type"] for row in events] == [
        "batch_callback_received",
        "parent_execution_intent_queued",
    ]


@pytest.mark.asyncio
async def test_paused_parent_does_not_create_even_empty_batch(control_db):
    from scheduler.app.services.cron.batch_admission import admit_batch
    from scheduler.app.services.cron import batch_run_state as state

    db = control_db
    db.add_job(enabled=False)
    await state.initialize_missing_controls(db)
    batch = dict(
        batch_id="b",
        source_id="source-a",
        tenant_id="tenant-1",
        parent_job_id="parent",
        scheduled_fire_at=datetime(2026, 9, 9),
    )
    result = await admit_batch(
        db, batch, [], physical_fire_at=datetime(2026, 9, 9)
    )
    assert result["skipped"] == "batch_dispatch_paused"
    assert await db.fetch_all("SELECT * FROM swe_cron_dispatch_batches") == []


@pytest.mark.asyncio
async def test_late_pre_resume_callback_does_not_create_batch(control_db):
    from scheduler.app.services.cron.batch_admission import admit_batch
    from scheduler.app.services.cron import batch_run_state as state

    db = control_db
    db.add_job()
    await state.initialize_missing_controls(db)
    now = datetime(2026, 9, 9)
    await db.execute(
        "UPDATE swe_cron_dispatch_controls SET resumed_at=%s", (now,)
    )
    batch = dict(
        batch_id="b",
        source_id="source-a",
        tenant_id="tenant-1",
        parent_job_id="parent",
        scheduled_fire_at=now,
    )
    result = await admit_batch(
        db, batch, [], physical_fire_at=now - timedelta(hours=1)
    )
    assert result["skipped"] == "before_batch_resume"
    assert await db.fetch_all("SELECT * FROM swe_cron_dispatch_batches") == []

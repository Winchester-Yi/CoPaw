from datetime import datetime
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from tests.unit.scheduler.test_batch_run_state import control_db  # noqa: F401


@pytest.mark.asyncio
async def test_claim_skips_paused_batches_and_assigns_fresh_token(
    monkeypatch, control_db
):
    from scheduler.app.services.cron import batch_run_state as state
    from scheduler.app.services.cron import dispatch_intent_service as module

    db = control_db
    db.add_job("paused", enabled=False)
    db.add_job("running")
    await state.initialize_missing_controls(db)
    for index, parent in enumerate(("paused", "running"), 1):
        batch = f"batch-{index}"
        db.insert(
            "swe_cron_dispatch_batches",
            batch_id=batch,
            source_id="source-a",
            tenant_id="tenant-1",
            parent_job_id=parent,
        )
        db.add_intent(
            id=index,
            batch_id=batch,
            status="pending",
            attempt_count=0,
            lock_owner="",
            locked_at=None,
        )
    monkeypatch.setattr(module, "get_db_connection", lambda: db)
    store = module.CronDispatchIntentService()
    store._refresh_batch_counts_for_rows = AsyncMock()
    ids = await store._claim_due_intent_ids(
        lock_owner="worker",
        now_utc=datetime(2026, 9, 9),
        limit=1,
        stale_lock_seconds=600,
        dispatched_stale_seconds=7800,
        source_ids=["source-a"],
        provider_id="default",
        model_id="default",
        claim_token="new-token",
    )
    assert ids == [2]
    assert (
        await db.fetch_one(
            "SELECT status FROM swe_cron_dispatch_intents WHERE id=1"
        )
    )["status"] == "pending"
    claimed = await db.fetch_one(
        "SELECT * FROM swe_cron_dispatch_intents WHERE id=2"
    )
    assert claimed["claim_token"] == "new-token"
    assert claimed["attempt_count"] == 1


@pytest.mark.asyncio
async def test_old_claim_cannot_mark_a_new_handoff_dispatched(
    monkeypatch, control_db
):
    from scheduler.app.services.cron import dispatch_intent_service as module

    control_db.add_intent(status="acknowledged", claim_token="new-token")
    monkeypatch.setattr(module, "get_db_connection", lambda: control_db)
    store = module.CronDispatchIntentService()
    store._record_event_best_effort = AsyncMock()
    assert not await store.mark_intent_dispatched(
        intent_id=7,
        worker_id="worker-1",
        dispatched_at=datetime.now(),
        claim_token="old-token",
    )
    assert await store.mark_intent_dispatched(
        intent_id=7,
        worker_id="worker-1",
        dispatched_at=datetime.now(),
        claim_token="new-token",
    )
    assert control_db.intent()["status"] == "dispatched"


@pytest.mark.asyncio
async def test_handoff_does_not_expire_at_unstarted_claim_timeout(
    monkeypatch, control_db
):
    from scheduler.app.services.cron import dispatch_intent_service as module

    now = datetime(2026, 9, 9, 12)
    control_db.add_intent(
        status="acknowledged",
        claim_token="new-token",
        locked_at=now - timedelta(minutes=15),
    )
    monkeypatch.setattr(module, "get_db_connection", lambda: control_db)
    store = module.CronDispatchIntentService()
    store._record_event_best_effort = AsyncMock()
    store._refresh_batch_counts_for_rows = AsyncMock()
    assert await store.recover_stale_dispatched_intents(now_utc=now) == 0
    assert control_db.intent()["status"] == "acknowledged"


@pytest.mark.asyncio
async def test_unstarted_claim_timeout_refunds_without_failure(
    monkeypatch, control_db
):
    from scheduler.app.services.cron import dispatch_intent_service as module

    control_db.add_intent(
        status="claimed", claim_token="new-token", attempt_count=3
    )
    monkeypatch.setattr(module, "get_db_connection", lambda: control_db)
    store = module.CronDispatchIntentService()
    store._record_event_best_effort = AsyncMock()
    store._refresh_batch_counts_for_rows = AsyncMock()
    assert (
        await store.recover_stale_dispatched_intents(
            now_utc=datetime(2026, 9, 9)
        )
        == 1
    )
    assert control_db.intent()["status"] == "pending"
    assert control_db.intent()["attempt_count"] == 2
    assert control_db.intent()["claim_token"] == ""

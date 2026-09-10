from datetime import datetime

import pytest

from tests.unit.scheduler.test_batch_run_state import control_db  # noqa: F401


@pytest.mark.asyncio
async def test_skipped_and_legacy_cancelled_finish_without_counting_as_success(
    monkeypatch,
    control_db,
):
    from scheduler.app.services.cron import batch_operations as ops

    db = control_db
    for column in (
        "total_count INTEGER",
        "completed_count INTEGER",
        "failed_count INTEGER",
        "completed_at TEXT",
        "updated_at TEXT",
    ):
        await db.execute(
            f"ALTER TABLE swe_cron_dispatch_batches ADD COLUMN {column}"
        )
    db.insert(
        "swe_cron_dispatch_batches", batch_id="batch-1", source_id="source-a"
    )
    db.add_intent(status="skipped")
    db.add_intent(id=8, status="cancelled")
    db.add_intent(id=9, status="completed")
    monkeypatch.setattr(ops, "get_db_connection", lambda: db)
    await ops.refresh_batch_counts("batch-1", datetime(2026, 9, 9))
    batch = await db.fetch_one("SELECT * FROM swe_cron_dispatch_batches")
    assert batch["status"] == "completed"
    assert batch["total_count"] == 3
    assert batch["completed_count"] == 1
    assert batch["failed_count"] == 0
    assert batch["skipped_count"] == 2
    assert batch["completed_at"]


@pytest.mark.asyncio
async def test_manual_retry_in_paused_batch_only_queues(
    monkeypatch, control_db
):
    from scheduler.app.services.cron import batch_operations as ops
    from scheduler.app.services.cron import batch_run_state as state

    db = control_db
    for column in (
        "total_count INTEGER",
        "completed_count INTEGER",
        "failed_count INTEGER",
        "completed_at TEXT",
        "updated_at TEXT",
    ):
        await db.execute(
            f"ALTER TABLE swe_cron_dispatch_batches ADD COLUMN {column}"
        )
    db.add_job("parent", enabled=False)
    db.add_job(
        "job-1",
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
        status="failed",
    )
    db.add_intent(
        status="failed", attempt_count=3, error_message="network error"
    )
    monkeypatch.setattr(ops, "get_db_connection", lambda: db)
    result = await ops.retry_failed(
        "source-a",
        "batch-1",
        "admin",
        ops.RetryRequest(
            candidates=[{"id": 7, "attempt_count": 3}],
        ),
    )
    assert result["queued"] == 1
    assert result["dispatch_paused"] is True
    assert db.intent()["status"] == "pending"
    assert db.intent()["attempt_count"] == 3
    assert db.intent()["max_attempts"] == 4

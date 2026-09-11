from datetime import timedelta

import pytest

from scheduler.app.services.cron.capacity_claim import available_capacity
from tests.unit.scheduler.test_cron_execution_reconciliation import (
    _SqliteDb,
    NOW,
)


@pytest.mark.asyncio
async def test_stale_free_slot_snapshot_cannot_overclaim():
    db = _SqliteDb()
    db.connection.executescript("""
        CREATE TABLE swe_cron_dispatch_scope_leases (
            source_id TEXT, provider_id TEXT, model_id TEXT,
            lock_owner TEXT, lease_expires_at TEXT
        );
        CREATE TABLE swe_cron_dispatch_worker_capacity (
            id INTEGER, source_id TEXT, provider_id TEXT, model_id TEXT,
            strategy_id TEXT, effective_workers INTEGER, created_at TEXT
        );
    """)
    scope = ("source-a", "default", "default")
    db.insert(
        "swe_cron_dispatch_scope_leases",
        source_id=scope[0],
        provider_id=scope[1],
        model_id=scope[2],
        lock_owner="worker-1",
        lease_expires_at=NOW + timedelta(minutes=5),
    )
    db.insert(
        "swe_cron_dispatch_worker_capacity",
        id=1,
        source_id=scope[0],
        provider_id=scope[1],
        model_id=scope[2],
        strategy_id="default",
        effective_workers=2,
        created_at=NOW,
    )
    policy = {
        "strategy_id": "default",
        "effective_workers": 10,
        "min_workers": 1,
        "max_workers": 20,
    }
    db.add_intent(id=1, status="dispatched")
    assert (
        await available_capacity(db.cursor(), scope, "worker-1", NOW, policy)
        == 1
    )
    db.add_intent(id=2, status="claimed")
    # A second caller's stale capacity=10 cannot reserve the same slot again.
    assert (
        await available_capacity(db.cursor(), scope, "worker-1", NOW, policy)
        == 0
    )
    assert (
        await available_capacity(db.cursor(), scope, "worker-2", NOW, policy)
        == 0
    )
    assert (
        await available_capacity(
            db.cursor(), scope, "worker-1", NOW + timedelta(minutes=6), policy
        )
        == 0
    )
    db.connection.execute(
        "UPDATE swe_cron_dispatch_intents SET source_id='source-b' WHERE id=2"
    )
    assert (
        await available_capacity(db.cursor(), scope, "worker-1", NOW, policy)
        == 1
    )
    db.connection.close()

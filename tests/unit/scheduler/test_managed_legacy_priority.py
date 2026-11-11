# -*- coding: utf-8 -*-
"""真实候选 SQL 与旧领取事务验证新版优先，剩余容量仍可供旧队列使用。"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from scheduler.app.services.cron import capacity_claim
from scheduler.app.services.cron.managed.operations import utc_now
from tests.unit.scheduler.test_managed_cron_capacity import (
    Cursor,
    configure_pool,
)
from tests.unit.scheduler.test_managed_cron_capacity_dispatch import run_for
from tests.unit.scheduler.test_managed_cron_executions import execution_setup

SCOPE = ("bank", "tenant-provider", "tenant-model")
POLICY = {
    "strategy_id": "default",
    "effective_workers": 10,
    "min_workers": 1,
    "max_workers": 20,
}


@pytest.fixture
def priority_pool(execution_setup, monkeypatch):
    from scheduler.app.services.cron.managed import executions

    db = execution_setup[0]
    now = utc_now()
    configure_pool(db, now + timedelta(hours=8))
    db.conn.execute(
        "UPDATE swe_cron_dispatch_worker_capacity SET effective_workers=10",
    )
    monkeypatch.setattr(capacity_claim, "MANAGED_CAPACITY_ACCOUNTING", True)
    monkeypatch.setattr(
        capacity_claim,
        "MANAGED_PRIORITY_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(executions, "utc_now", lambda: now)
    return db, now


async def legacy_slots(pool, *, owner="worker"):
    db, now = pool
    return await capacity_claim.legacy_available_capacity(
        Cursor(db),
        SCOPE,
        owner,
        now + timedelta(hours=8),
        POLICY,
    )


@pytest.mark.asyncio
async def test_legacy_uses_only_capacity_left_after_ready_new_jobs(
    execution_setup,
    priority_pool,
):
    db, now = priority_pool
    for index in range(3):
        await run_for(execution_setup, index)
    assert await legacy_slots(priority_pool) == 7
    # 新版领取不扣自己的待运行名额，不会与旧入口互相等待。
    assert (
        await capacity_claim.available_capacity(
            Cursor(db),
            SCOPE,
            "worker",
            now + timedelta(hours=8),
            POLICY,
        )
        == 10
    )
    db.conn.execute("UPDATE swe_cron_execution_runs SET status='running'")
    assert await legacy_slots(priority_pool) == 7
    db.conn.execute(
        "INSERT INTO swe_cron_dispatch_intents VALUES (1,'bank','tenant-provider','tenant-model','dispatched')",
    )
    assert await legacy_slots(priority_pool) == 6
    assert (
        db.conn.execute(
            "SELECT status FROM swe_cron_dispatch_intents",
        ).fetchone()[0]
        == "dispatched"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "delayed",
        "leased",
        "disabled",
        "deleted",
        "paused",
        "invalidated",
        "not_started",
        "expired",
        "other_model",
        "other_provider",
        "other_source",
        "legacy_owner",
        "terminal",
        "old_protocol",
    ],
)
async def test_non_runnable_new_work_does_not_hold_legacy_capacity(
    execution_setup,
    priority_pool,
    change,
):
    db, now = priority_pool
    await run_for(execution_setup, 0)
    statements = {
        "delayed": (
            "UPDATE swe_cron_execution_runs SET not_before_at=?",
            ((now + timedelta(minutes=1)).isoformat(" "),),
        ),
        "leased": (
            "UPDATE swe_cron_execution_runs SET dispatch_lease_until=?",
            ((now + timedelta(minutes=1)).isoformat(" "),),
        ),
        "disabled": ("UPDATE swe_cron_jobs_v2 SET enabled=0", ()),
        "deleted": ("UPDATE swe_cron_jobs_v2 SET deleted_at='2026-01-01'", ()),
        "paused": ("UPDATE swe_cron_job_states SET pause_reason='manual'", ()),
        "invalidated": (
            "UPDATE swe_cron_job_states SET queue_invalidation_version=9999",
            (),
        ),
        "not_started": (
            "UPDATE swe_cron_jobs_v2 SET run_start_date=?,run_end_date=?",
            (
                (now + timedelta(hours=8, days=1)).date().isoformat(),
                (now + timedelta(hours=8, days=2)).date().isoformat(),
            ),
        ),
        "expired": (
            "UPDATE swe_cron_jobs_v2 SET run_start_date='2020-01-01',run_end_date='2020-01-02'",
            (),
        ),
        "other_model": (
            "UPDATE swe_cron_execution_runs SET model_id='other'",
            (),
        ),
        "other_provider": (
            "UPDATE swe_cron_execution_runs SET provider_id='other'",
            (),
        ),
        "other_source": (
            "UPDATE swe_cron_execution_runs SET source_id='other'",
            (),
        ),
        "legacy_owner": (
            "UPDATE swe_cron_jobs_v2 SET management_owner='legacy'",
            (),
        ),
        "terminal": ("UPDATE swe_cron_execution_runs SET status='error'", ()),
        "old_protocol": (
            "UPDATE swe_cron_execution_runs SET dispatch_meta_json='{}'",
            (),
        ),
    }
    sql, params = statements[change]
    db.conn.execute(sql, params)
    assert await legacy_slots(priority_pool) == 10


@pytest.mark.asyncio
async def test_same_job_waiters_only_reserve_its_remaining_concurrency(
    execution_setup,
    priority_pool,
):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )

    db, caller, _ = execution_setup
    first = await run_for(execution_setup, 0)
    await ExecutionStore(db).enqueue(
        caller,
        first["job_id"],
        ExecutionRequest(
            "ordinary",
            "second",
            scheduled_at=priority_pool[1].replace(tzinfo=timezone.utc),
        ),
    )
    assert await legacy_slots(priority_pool) == 9
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET status='running' WHERE execution_id=?",
        (first["execution_id"],),
    )
    # 单任务并发已满：只计一次真实占位，不再为等待周期扣容量。
    assert await legacy_slots(priority_pool) == 9
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET status='success',settled_status=NULL WHERE execution_id=?",
        (first["execution_id"],),
    )
    assert await legacy_slots(priority_pool) == 9


@pytest.mark.asyncio
async def test_frozen_concurrency_is_simulated_in_existing_queue_order(
    execution_setup,
    priority_pool,
):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )

    db, caller, _ = execution_setup
    first = await run_for(execution_setup, 0)
    identifiers = [first["execution_id"]]
    for index in range(3):
        row, _ = await ExecutionStore(db).enqueue(
            caller,
            first["job_id"],
            ExecutionRequest(
                "ordinary",
                f"next-{index}",
                scheduled_at=(
                    priority_pool[1] + timedelta(minutes=index)
                ).replace(tzinfo=timezone.utc),
            ),
        )
        identifiers.append(row["execution_id"])
    spec = json.loads(first["spec_json"])
    spec["runtime"]["max_concurrency"] = 3
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET spec_json=? WHERE execution_id=?",
        (json.dumps(spec), identifiers[-1]),
    )
    # 前三个记录上限为 1，最后一个为 3：按序只能新占两个名额，而非 3/4 个。
    assert await legacy_slots(priority_pool) == 8


@pytest.mark.asyncio
async def test_new_queue_saturates_pool_without_negative_capacity(
    execution_setup,
    priority_pool,
):
    db, _ = priority_pool
    for index in range(3):
        await run_for(execution_setup, index)
    db.conn.execute(
        "UPDATE swe_cron_dispatch_worker_capacity SET effective_workers=2",
    )
    assert await legacy_slots(priority_pool) == 0
    assert await legacy_slots(priority_pool, owner="another-worker") == 0


@pytest.mark.asyncio
async def test_legacy_only_runtime_never_queries_new_tables(
    priority_pool,
    monkeypatch,
):
    db, _ = priority_pool
    monkeypatch.setattr(capacity_claim, "MANAGED_CAPACITY_ACCOUNTING", False)
    db.conn.execute("DROP TABLE swe_cron_execution_runs")
    assert await legacy_slots(priority_pool) == 10


@pytest.mark.asyncio
async def test_unavailable_managed_worker_does_not_hold_waiting_capacity(
    execution_setup,
    priority_pool,
    monkeypatch,
):
    db, _ = priority_pool
    await run_for(execution_setup, 0)
    monkeypatch.setattr(capacity_claim, "MANAGED_PRIORITY_ENABLED", False)
    assert await legacy_slots(priority_pool) == 10
    db.conn.execute("UPDATE swe_cron_execution_runs SET status='running'")
    assert await legacy_slots(priority_pool) == 9


@pytest.mark.asyncio
async def test_priority_query_failure_is_not_treated_as_empty_queue(
    execution_setup,
    priority_pool,
):
    db, _ = priority_pool
    await run_for(execution_setup, 0)
    db.conn.execute("DROP TABLE swe_cron_job_states")
    with pytest.raises(Exception, match="swe_cron_job_states"):
        await legacy_slots(priority_pool)


@pytest.mark.asyncio
async def test_priority_dates_use_beijing_day_not_utc_day(
    execution_setup,
    priority_pool,
):
    db, _ = priority_pool
    await run_for(execution_setup, 0)
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET not_before_at='2026-11-02 16:00:00'",
    )
    db.conn.execute(
        "UPDATE swe_cron_jobs_v2 SET run_start_date='2026-11-03',run_end_date='2026-11-03'",
    )
    db.conn.execute(
        "UPDATE swe_cron_dispatch_scope_leases SET lease_expires_at='2026-11-03 01:00:00'",
    )
    assert await legacy_slots((db, datetime(2026, 11, 2, 16, 30))) == 9


def legacy_database(db):
    from tests.unit.scheduler.test_cron_execution_reconciliation import (
        _Cursor,
        _SqliteDb,
    )

    class LegacyCursor(_Cursor):
        async def execute(self, sql, params=()):
            await super().execute(sql, params)
            return self.rowcount

    class LegacyDatabase(_SqliteDb):
        def __init__(self):
            self.connection = db.conn
            self.before_update = None

        def cursor(self):
            return LegacyCursor(self)

    db.conn.executescript("""
        DROP TABLE swe_cron_dispatch_intents;
        CREATE TABLE swe_cron_dispatch_intents (
            id INTEGER PRIMARY KEY, batch_id TEXT, job_id TEXT, tenant_id TEXT,
            source_id TEXT, provider_id TEXT, model_id TEXT, status TEXT,
            attempt_count INTEGER, max_attempts INTEGER, due_at TEXT,
            locked_at TEXT, lock_owner TEXT, dispatch_order INTEGER,
            claim_token TEXT DEFAULT '',error_message TEXT
        );
        CREATE TABLE swe_cron_dispatch_batches (
            batch_id TEXT,source_id TEXT,tenant_id TEXT,parent_job_id TEXT,
            lock_owner TEXT DEFAULT '', locked_at TEXT
        );
        CREATE TABLE swe_cron_dispatch_controls (
            source_id TEXT,tenant_id TEXT,parent_job_id TEXT,paused INTEGER,
            version INTEGER,resumed_at TEXT,updated_by TEXT,created_at TEXT,updated_at TEXT
        );
        INSERT INTO swe_cron_dispatch_batches(batch_id,source_id,tenant_id,parent_job_id)
            VALUES ('old-batch','bank','alice','old-parent');
        INSERT INTO swe_cron_dispatch_controls(source_id,tenant_id,parent_job_id,paused)
            VALUES ('bank','alice','old-parent',0);
        ALTER TABLE swe_cron_executions ADD COLUMN dispatch_intent_id INTEGER;
        ALTER TABLE swe_cron_executions ADD COLUMN dispatch_batch_id TEXT;
        ALTER TABLE swe_cron_executions ADD COLUMN dispatch_attempt INTEGER;
    """)
    for index in range(10):
        db.conn.execute(
            "INSERT INTO swe_cron_dispatch_intents(id,batch_id,job_id,tenant_id,source_id,provider_id,model_id,status,attempt_count,max_attempts,due_at,dispatch_order) VALUES (?,'old-batch',?,'alice','bank','tenant-provider','tenant-model','pending',0,3,'2020-01-01',?)",
            (index + 1, f"old-{index}", index),
        )
    return LegacyDatabase()


@pytest.mark.asyncio
async def test_actual_legacy_claim_respects_new_queue_and_shared_lock(
    execution_setup,
    priority_pool,
    monkeypatch,
):
    from scheduler.app.services.cron import dispatch_intent_service as legacy

    db, now = priority_pool
    for index in range(3):
        await run_for(execution_setup, index)
    adapter = legacy_database(db)
    monkeypatch.setattr(legacy, "get_db_connection", lambda: adapter)
    queries = []
    db.conn.set_trace_callback(queries.append)
    service = legacy.CronDispatchIntentService()
    arguments = {
        "lock_owner": "worker",
        "now_utc": now.replace(tzinfo=timezone.utc),
        "limit": 10,
        "stale_lock_seconds": 600,
        "dispatched_stale_seconds": 7800,
        "source_ids": ["bank"],
        "provider_id": "tenant-provider",
        "model_id": "tenant-model",
        "capacity": POLICY,
        "claim_token": "old-token",
    }
    claimed = await service._claim_due_intent_ids(**arguments)
    assert claimed == [1, 2, 3, 4, 5, 6, 7]
    statuses = [
        row[0]
        for row in db.conn.execute(
            "SELECT status FROM swe_cron_dispatch_intents ORDER BY id",
        )
    ]
    assert statuses == ["claimed"] * 7 + ["pending"] * 3
    reads = [sql for sql in queries if sql.lstrip().startswith("SELECT")]
    assert "swe_cron_dispatch_scope_leases" in reads[0]
    assert not db.conn.in_transaction
    # 下一个领取者即使仍拿着 limit=10，也不能再占新版的三个名额。
    assert await service._claim_due_intent_ids(**arguments) == []
    db.conn.execute("UPDATE swe_cron_execution_runs SET status='running'")
    assert await service._claim_due_intent_ids(**arguments) == []
    db.conn.execute("UPDATE swe_cron_execution_runs SET status='error'")
    assert await service._claim_due_intent_ids(**arguments) == [8, 9, 10]


@pytest.mark.asyncio
async def test_old_claim_rolls_back_if_priority_lookup_fails(
    execution_setup,
    priority_pool,
    monkeypatch,
):
    from scheduler.app.services.cron import dispatch_intent_service as legacy

    db, now = priority_pool
    await run_for(execution_setup, 0)
    adapter = legacy_database(db)
    monkeypatch.setattr(legacy, "get_db_connection", lambda: adapter)
    db.conn.execute("DROP TABLE swe_cron_job_states")
    with pytest.raises(Exception, match="swe_cron_job_states"):
        await legacy.CronDispatchIntentService()._claim_due_intent_ids(
            lock_owner="worker",
            now_utc=now.replace(tzinfo=timezone.utc),
            limit=10,
            stale_lock_seconds=600,
            dispatched_stale_seconds=7800,
            source_ids=["bank"],
            provider_id="tenant-provider",
            model_id="tenant-model",
            capacity=POLICY,
            claim_token="old-token",
        )
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_dispatch_intents WHERE status='pending'",
        ).fetchone()[0]
        == 10
    )
    assert not db.conn.in_transaction

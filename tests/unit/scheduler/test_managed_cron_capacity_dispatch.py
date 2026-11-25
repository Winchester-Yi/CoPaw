# -*- coding: utf-8 -*-
"""实际领取事务共用模型池行锁，满额不超领，接管不重复占位。"""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from tests.unit.scheduler.test_managed_cron_capacity import configure_pool
from tests.unit.scheduler.test_managed_cron_executions import (
    enqueue,
    execution_setup,
)
from tests.unit.scheduler.test_managed_cron_management import create_payload
from tests.unit.scheduler.test_managed_cron_callbacks import (
    callback_setup,
    create_source,
)


class RuntimePolicy:
    _worker_id = "worker"

    async def _resolve_worker_strategy(self, scope, now):
        from scheduler.app.services.cron.scheduling_service import (
            WorkerStrategy,
        )

        return WorkerStrategy(baseline_workers=2, max_workers=2)

    async def _acquire_scope_lease(self, scope, strategy, now):
        return True

    async def _effective_workers_for_scope(self, scope, strategy, now):
        return 2


async def run_for(setup, index):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )

    db, caller, management = setup
    created = await management.create(
        caller,
        create_payload(f"capacity-job-{index}"),
    )
    row, _ = await ExecutionStore(db).enqueue(
        caller,
        created["job_id"],
        ExecutionRequest(
            "batch",
            f"claim-{index}",
            datetime.now(timezone.utc),
        ),
    )
    return row


def dispatcher(db, now, monkeypatch):
    from scheduler.app.services.cron import capacity_claim
    from scheduler.app.services.cron.managed.capacity import (
        ManagedCapacityGate,
    )
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )

    configure_pool(db, now[0] + timedelta(hours=8))
    monkeypatch.setattr(capacity_claim, "MANAGED_CAPACITY_ACCOUNTING", True)
    gate = ManagedCapacityGate(RuntimePolicy(), clock=lambda: now[0])
    return ExecutionDispatcher(db, None, clock=lambda: now[0], capacity=gate)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ordinary", "manual"])
@pytest.mark.parametrize("pool_state", ["full", "leased"])
async def test_standalone_claim_bypasses_batch_capacity(
    execution_setup,
    monkeypatch,
    kind,
    pool_state,
):
    from scheduler.app.services.cron.managed.operations import utc_now
    from scheduler.app.services.cron.managed.work_query import ManagedWorkQuery

    db, _, _ = execution_setup
    standalone, _ = await enqueue(execution_setup, kind=kind)
    other = await run_for(execution_setup, 2)
    service = dispatcher(db, [utc_now()], monkeypatch)
    if pool_state == "full":
        db.conn.execute(
            "INSERT INTO swe_cron_dispatch_intents VALUES "
            "(1,'bank','tenant-provider','tenant-model','dispatched'), "
            "(2,'bank','tenant-provider','tenant-model','dispatched')",
        )
    else:
        db.conn.execute(
            "UPDATE swe_cron_dispatch_scope_leases SET lock_owner='another-worker'",
        )
    work = ManagedWorkQuery(db, 10)
    assert await work.rows(running=False, direct=True) == [
        {"execution_id": standalone["execution_id"]},
    ]
    assert (await service._claim(standalone, True))["status"] == "dispatching"
    assert await service._claim(other, True) is None
    assert await work.rows(running=False) == [
        {"execution_id": other["execution_id"]},
    ]


@pytest.mark.asyncio
async def test_text_batch_bypasses_pool_and_keeps_batch_gates(callback_setup):
    from scheduler.app.services.cron.managed.batch_pause import (
        set_batch_paused,
    )
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )
    from scheduler.app.services.cron.managed.operations import utc_now
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )
    from scheduler.app.services.cron.managed.work_query import ManagedWorkQuery

    db, caller, _ = callback_setup
    caller = replace(caller, is_manager=True)
    parent = await create_source(callback_setup, "batch_enabled")
    batch = await ManagedSchedulingService(db).run_batch(
        caller,
        parent["job_id"],
    )
    row = await db.fetch_one("SELECT * FROM swe_cron_execution_runs")
    capacity = AsyncMock()
    capacity.prepare.return_value = None
    service = ExecutionDispatcher(db, None, capacity=capacity)
    work = ManagedWorkQuery(db, 10)

    await set_batch_paused(db, caller, batch["batch_id"], True)
    assert await work.rows(running=False, direct=True) == []
    assert await service._claim(row, True) is None
    await set_batch_paused(db, caller, batch["batch_id"], False)
    db.conn.execute(
        "UPDATE swe_cron_execution_runs SET not_before_at=?",
        ((utc_now() + timedelta(minutes=5)).isoformat(" "),),
    )
    assert await work.rows(running=False, direct=True) == []
    assert await service._claim(row, True) is None
    db.conn.execute("UPDATE swe_cron_execution_runs SET not_before_at=NULL")
    assert await work.scopes() == []
    assert await work.rows(running=False, direct=True) == [
        {"execution_id": row["execution_id"]},
    ]
    assert (await service._claim(row, True))["status"] == "dispatching"
    capacity.prepare.assert_not_awaited()

    next_run, _ = await ExecutionStore(db).enqueue(
        caller,
        parent["job_id"],
        ExecutionRequest(
            "batch",
            "next",
            utc_now().replace(tzinfo=timezone.utc),
        ),
    )
    assert await work.rows(running=False, direct=True) == []
    assert await service._claim(next_run, True) is None


@pytest.mark.asyncio
async def test_two_claimers_cannot_overbook_shared_model_capacity(
    execution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.operations import utc_now

    db, _, _ = execution_setup
    rows = [await run_for(execution_setup, index) for index in range(3)]
    service = dispatcher(db, [utc_now()], monkeypatch)
    db.conn.execute(
        "INSERT INTO swe_cron_dispatch_intents VALUES (1,'bank','tenant-provider','tenant-model','dispatched')",
    )
    result = await asyncio.gather(*(service._claim(row, True) for row in rows))
    assert sum(row is not None for row in result) == 1
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs WHERE status='dispatching'",
        ).fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
async def test_expired_dispatch_reclaims_its_own_slot_and_keeps_execution_id(
    execution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.operations import utc_now

    db, _, _ = execution_setup
    rows = [await run_for(execution_setup, index) for index in range(2)]
    now = [utc_now()]
    service = dispatcher(db, now, monkeypatch)
    first, _ = await asyncio.gather(
        *(service._claim(row, True) for row in rows),
    )
    now[0] += timedelta(minutes=1)
    reclaimed = await service._claim(rows[0], True)
    assert reclaimed is not None
    assert reclaimed["execution_id"] == first["execution_id"]
    assert reclaimed["dispatch_owner"] != first["dispatch_owner"]
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 2
    )


@pytest.mark.asyncio
async def test_accepted_transport_recovery_does_not_require_a_second_slot(
    execution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.operations import utc_now
    from swe.app.crons.managed_results import ExecutionWriter

    db, caller, _ = execution_setup
    rows = [await run_for(execution_setup, index) for index in range(2)]
    now = [utc_now()]
    service = dispatcher(db, now, monkeypatch)
    claimed, _ = await asyncio.gather(
        *(service._claim(row, True) for row in rows),
    )
    await ExecutionWriter(db).accept(
        claimed["execution_id"],
        caller.job_key(claimed["job_id"]),
        dispatch_token=claimed["dispatch_owner"],
    )
    now[0] += timedelta(minutes=1)
    recovered = await service._claim(claimed, True)
    assert recovered is not None
    assert recovered["status"] == "accepted"


@pytest.mark.asyncio
async def test_scope_owner_is_checked_inside_claim_transaction(
    execution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.operations import utc_now

    db, _, _ = execution_setup
    row = await run_for(execution_setup, 1)
    service = dispatcher(db, [utc_now()], monkeypatch)
    db.conn.execute(
        "UPDATE swe_cron_dispatch_scope_leases SET lock_owner='another-worker'",
    )
    assert await service._claim(row, True) is None
    assert (
        db.conn.execute(
            "SELECT dispatch_attempt FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_disabled_old_queue_can_be_skipped_even_when_pool_is_full(
    execution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.controls import ControlService
    from scheduler.app.services.cron.managed.operations import utc_now
    from swe.app.crons.managed_results import ExecutionWriter

    db, caller, management = execution_setup
    rows = [await run_for(execution_setup, index) for index in range(3)]
    service = dispatcher(db, [utc_now()], monkeypatch)
    await asyncio.gather(*(service._claim(row, True) for row in rows[:2]))
    third = rows[2]
    version = db.conn.execute(
        "SELECT management_version FROM swe_cron_job_states WHERE job_id=?",
        (third["job_id"],),
    ).fetchone()[0]
    await ControlService(db, management.platform).set_enabled(
        caller,
        third["job_id"],
        {
            "request_id": "pause-third",
            "enabled": False,
            "expected_management_version": version,
        },
    )
    skipped = await service._claim(third, True)
    assert skipped is not None
    writer = ExecutionWriter(db)
    key = caller.job_key(third["job_id"])
    await writer.accept(
        third["execution_id"],
        key,
        dispatch_token=skipped["dispatch_owner"],
    )
    assert await writer.start(third["execution_id"], key, "worker") is None
    assert (await writer.get(third["execution_id"], key))[
        "status"
    ] == "skipped"


@pytest.mark.asyncio
async def test_job_concurrency_limit_does_not_reserve_pool_for_waiting_periods(
    execution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )
    from scheduler.app.services.cron.managed.operations import utc_now

    db, caller, _ = execution_setup
    first = await run_for(execution_setup, 1)
    second, _ = await ExecutionStore(db).enqueue(
        caller,
        first["job_id"],
        ExecutionRequest(
            "batch",
            "another-period",
            scheduled_at=utc_now().replace(tzinfo=timezone.utc),
        ),
    )
    other = await run_for(execution_setup, 2)
    service = dispatcher(db, [utc_now()], monkeypatch)
    assert await service._claim(first, True) is not None
    assert await service._claim(second, True) is None
    assert await service._claim(other, True) is not None


@pytest.mark.asyncio
async def test_scope_lock_precedes_definition_snapshot_read(
    execution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron.managed.operations import utc_now

    db, _, _ = execution_setup
    row = await run_for(execution_setup, 1)
    service = dispatcher(db, [utc_now()], monkeypatch)
    queries = []
    fetch = db.fetch_all

    async def record(sql, params=None):
        queries.append(sql)
        return await fetch(sql, params)

    monkeypatch.setattr(db, "fetch_all", record)
    assert await service._claim(row, True) is not None
    first_lock = next(sql for sql in queries if "FOR UPDATE" in sql)
    assert "swe_cron_dispatch_scope_leases" in first_lock


@pytest.mark.asyncio
async def test_gate_reuses_actual_existing_policy_and_scope_lease_methods(
    execution_setup,
    monkeypatch,
):
    from scheduler.app.services.cron import dispatch_intent_service
    from scheduler.app.services.cron.managed.capacity import (
        ManagedCapacityGate,
    )
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )
    from scheduler.app.services.cron.managed.operations import utc_now
    from scheduler.app.services.cron.scheduling_service import (
        CronSchedulingService,
    )

    db, _, _ = execution_setup
    row = await run_for(execution_setup, 1)
    now = [utc_now()]
    dispatcher(db, now, monkeypatch)
    db.conn.executescript("""
        ALTER TABLE swe_cron_dispatch_worker_capacity ADD COLUMN decision_reason TEXT;
        ALTER TABLE swe_cron_dispatch_scope_leases ADD COLUMN locked_at TEXT;
        ALTER TABLE swe_cron_dispatch_scope_leases ADD COLUMN heartbeat_at TEXT;
        CREATE UNIQUE INDEX uk_test_scope ON swe_cron_dispatch_scope_leases(source_id,provider_id,model_id);
        CREATE TABLE swe_cron_dispatch_model_worker_policy (
            source_id TEXT, provider_id TEXT, model_id TEXT,
            default_strategy_id TEXT, strategy_schedule TEXT, enabled INTEGER
        );
        CREATE TABLE swe_cron_dispatch_worker_strategy (
            strategy_id TEXT, min_workers INTEGER, baseline_workers INTEGER,
            max_workers INTEGER, adjust_interval_seconds INTEGER,
            feedback_window_seconds INTEGER, stale_execution_seconds INTEGER,
            error_rate_rules TEXT, enabled INTEGER
        );
    """)
    execute = db.execute

    async def sqlite_execute(sql, params=None):
        return await execute(
            sql.replace("INSERT IGNORE", "INSERT OR IGNORE"),
            params,
        )

    monkeypatch.setattr(db, "execute", sqlite_execute)
    monkeypatch.setattr(
        dispatch_intent_service,
        "get_db_connection",
        lambda: db,
    )
    runtime = CronSchedulingService(
        dispatch_store=dispatch_intent_service.CronDispatchIntentService(),
        worker_id="worker",
        baseline_workers=2,
        max_workers=2,
    )
    gate = ManagedCapacityGate(runtime, clock=lambda: now[0])
    service = ExecutionDispatcher(
        db,
        None,
        capacity=gate,
        clock=lambda: now[0],
    )
    claimed = await service._claim(row, True)
    assert claimed is not None
    lease = db.conn.execute(
        "SELECT * FROM swe_cron_dispatch_scope_leases",
    ).fetchone()
    assert lease["lock_owner"] == "worker"
    assert lease["heartbeat_at"] is not None
    other = CronSchedulingService(
        dispatch_store=dispatch_intent_service.CronDispatchIntentService(),
        worker_id="other-worker",
    )
    assert (
        await ManagedCapacityGate(other, clock=lambda: now[0]).prepare(row)
        is None
    )

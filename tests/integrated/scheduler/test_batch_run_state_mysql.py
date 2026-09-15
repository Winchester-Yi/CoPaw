"""Opt-in real InnoDB validation on an explicitly identified disposable server.

Never reads application DB settings. Each test creates and drops only its own
random schema after verifying the loopback server's data directory.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("CRON_MYSQL_TEST_PORT"),
        reason="requires an explicit disposable MySQL instance",
    ),
]


@pytest_asyncio.fixture
async def mysql_db(monkeypatch):
    aiomysql = pytest.importorskip("aiomysql")
    from monitor.app.database.schema import CREATE_CRON_JOBS_TABLE
    from scheduler.app.database import schema
    from scheduler.app.database.batch_run_state_schema import (
        CREATE_CONTROL_TABLE,
    )
    from scheduler.app.database.connection import DatabaseConnection
    from scheduler.app.services.cron import batch_operations, batch_run_state
    from scheduler.app.services.cron import dispatch_intent_service
    from scheduler.config.constant import SchedulerDatabaseConfig

    port = int(os.environ["CRON_MYSQL_TEST_PORT"])
    expected = Path(os.environ["CRON_MYSQL_TEST_DATADIR"]).resolve()
    assert 10000 <= port <= 65535 and port != 3306
    assert expected.parent.name.startswith("copaw-cron-mysql-")
    password = os.environ["CRON_MYSQL_TEST_PASSWORD"]
    admin = await aiomysql.connect(
        host="127.0.0.1",
        port=port,
        user="root",
        password=password,
        autocommit=True,
        connect_timeout=5,
    )
    name = "cron_dispatch_test_" + uuid4().hex
    created = False
    db = None
    try:
        async with admin.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT @@datadir AS data_dir, @@server_id AS sid"
            )
            server = await cur.fetchone()
            assert Path(server["data_dir"]).resolve() == expected
            assert server["sid"] == 6060910
            await cur.execute(
                f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4"
            )
            created = True
        db = DatabaseConnection(
            SchedulerDatabaseConfig(
                host="127.0.0.1",
                port=port,
                user="root",
                password=password,
                database=name,
                min_connections=2,
                max_connections=8,
            )
        )
        await db.connect()
        for statement in (
            CREATE_CRON_JOBS_TABLE,
            CREATE_CONTROL_TABLE,
            schema.CREATE_CRON_DISPATCH_BATCHES_TABLE,
            schema.CREATE_CRON_DISPATCH_INTENTS_TABLE,
            schema.CREATE_CRON_DISPATCH_EVENTS_TABLE,
            schema.CREATE_CRON_EXECUTIONS_TABLE,
            schema.CREATE_CRON_DISPATCH_SCOPE_LEASES_TABLE,
            schema.CREATE_CRON_DISPATCH_WORKER_CAPACITY_TABLE,
        ):
            await db.execute(statement)
        for module in (
            batch_operations,
            batch_run_state,
            dispatch_intent_service,
        ):
            monkeypatch.setattr(module, "get_db_connection", lambda: db)
        yield db
    finally:
        if db is not None:
            await db.close()
        if created:
            # name is generated above, never supplied by configuration or user.
            async with admin.cursor() as cur:
                await cur.execute(f"DROP DATABASE `{name}`")
        admin.close()


async def job(db, name, *, parent=None, enabled=True):
    meta = {"broadcast_dispatch_intents_enabled": True}
    if parent:
        meta["broadcast_source_job_id"] = parent
    tenant = "recipient" if parent else "owner"
    await db.execute(
        "INSERT INTO swe_cron_jobs "
        "(id,name,tenant_id,source_id,task_type,cron_expr,"
        "channel,enabled,status,meta) "
        "VALUES (%s,%s,%s,'source','text','0 9 * * *','console',%s,%s,%s)",
        (
            name,
            name,
            tenant,
            int(enabled),
            "active" if enabled else "paused",
            json.dumps(meta),
        ),
    )
    return await db.fetch_one(
        "SELECT * FROM swe_cron_jobs WHERE id=%s", (name,)
    )


async def seed_batch(db, *, count=1, claimed=False):
    from scheduler.app.services.cron import batch_run_state as state

    parent = await job(db, "parent")
    await state.initialize_parent(db, parent)
    now = state.now_local()
    await db.execute(
        "INSERT INTO swe_cron_dispatch_batches "
        "(batch_id,parent_job_id,tenant_id,source_id,scheduled_fire_at,"
        "callback_received_at,total_count,status) "
        "VALUES ('batch','parent','owner','source',%s,%s,%s,'pending')",
        (now, now, count),
    )
    for index in range(count):
        name = f"child-{index}"
        await job(db, name, parent="parent")
        await db.execute(
            "INSERT INTO swe_cron_dispatch_intents "
            "(batch_id,intent_role,source_id,tenant_id,job_id,parent_job_id,"
            "due_at,dispatch_order,status,"
            "lock_owner,locked_at,claim_token,attempt_count,payload) "
            "VALUES ('batch','child','source','recipient',%s,'parent',"
            "%s,%s,%s,'worker',%s,%s,%s,'{}')",
            (
                name,
                now - timedelta(minutes=1),
                index,
                "claimed" if claimed else "pending",
                now if claimed else None,
                f"token-{index}" if claimed else "",
                int(claimed),
            ),
        )
    return await db.fetch_all(
        "SELECT * FROM swe_cron_dispatch_intents ORDER BY id"
    )


async def wait_for_innodb_wait(db):
    """Observe a real lock wait instead of assuming sleep implies overlap."""
    async with asyncio.timeout(8):
        while True:
            row = await db.fetch_one(
                "SELECT COUNT(*) AS n "
                "FROM performance_schema.data_lock_waits w "
                "JOIN performance_schema.data_locks l "
                "ON l.ENGINE=w.ENGINE "
                "AND l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID "
                "WHERE l.OBJECT_SCHEMA=%s",
                (db.config.database,),
            )
            if row["n"]:
                return
            await asyncio.sleep(0.01)


async def locked_pair(db, first, second, entered, release):
    tasks = [asyncio.create_task(first(), name="first")]
    try:
        await asyncio.wait_for(entered.wait(), 8)
        tasks.append(asyncio.create_task(second(), name="second"))
        await wait_for_innodb_wait(db)
        assert not tasks[1].done()
        release.set()
        return await asyncio.wait_for(asyncio.gather(*tasks), 8)
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("different_parents", [False, True])
async def test_concurrent_initialization_is_idempotent(
    mysql_db, monkeypatch, different_parents
):
    from scheduler.app.services.cron import batch_run_state as state

    db = mysql_db
    count = 4
    parents = [
        await job(db, f"p-{n}", enabled=False)
        for n in range(count if different_parents else 1)
    ]
    reached = 0
    all_read = asyncio.Event()
    read = state.cursor_rows

    async def synchronize_empty_controls(cur, sql, params=(), columns=""):
        nonlocal reached
        rows = await read(cur, sql, params, columns)
        if (
            different_parents
            and "FROM swe_cron_dispatch_controls" in sql
            and not rows
        ):
            reached += 1
            if reached == count:
                all_read.set()
            await asyncio.wait_for(all_read.wait(), 8)
        return rows

    monkeypatch.setattr(state, "cursor_rows", synchronize_empty_controls)
    results = await asyncio.wait_for(
        asyncio.gather(
            *[
                state.initialize_parent(
                    db, parents[n] if different_parents else parents[0]
                )
                for n in range(count)
            ],
            return_exceptions=True,
        ),
        12,
    )
    assert not [
        result for result in results if isinstance(result, BaseException)
    ], results
    expected = count if different_parents else 1
    assert sum(results) == expected
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_cron_dispatch_controls "
            "WHERE paused=1"
        )
    )["n"] == expected
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_cron_dispatch_events"
        )
    )["n"] == expected
    await db.execute("UPDATE swe_cron_jobs SET enabled=1,status='active'")
    assert not any(
        await asyncio.gather(
            *[state.initialize_parent(db, parent) for parent in parents]
        )
    )
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_cron_dispatch_controls "
            "WHERE paused=1"
        )
    )["n"] == expected


async def test_pause_wins_before_handoff_and_refunds_claim(
    mysql_db, monkeypatch
):
    from scheduler.app.services.cron import (
        batch_run_state as state,
        dispatch_gate as gate,
    )

    db = mysql_db
    row = (await seed_batch(db, claimed=True))[0]
    entered, release = asyncio.Event(), asyncio.Event()
    refund = state.refund_unstarted_claims

    async def hold_pause(*args):
        entered.set()
        await release.wait()
        return await refund(*args)

    monkeypatch.setattr(state, "refund_unstarted_claims", hold_pause)
    result = await locked_pair(
        db,
        lambda: state.set_run_state(
            "source", "owner", "parent", "admin", True, 1
        ),
        lambda: gate.prepare_handoff(db, row, "worker", state.now_local()),
        entered,
        release,
    )
    assert result[0]["paused"] is True
    assert result[1] == "stale"
    stored = await db.fetch_one(
        "SELECT * FROM swe_cron_dispatch_intents WHERE id=%s", (row["id"],)
    )
    assert stored["status"] == "pending"
    assert stored["attempt_count"] == 0
    assert stored["claim_token"] == ""


async def test_handoff_wins_before_pause_and_remains_inflight(
    mysql_db, monkeypatch
):
    from scheduler.app.services.cron import (
        batch_run_state as state,
        dispatch_gate as gate,
    )

    db = mysql_db
    row = (await seed_batch(db, claimed=True))[0]
    entered, release = asyncio.Event(), asyncio.Event()
    check = gate._check_claim

    async def hold_handoff(*args):
        result = await check(*args)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(gate, "_check_claim", hold_handoff)
    result = await locked_pair(
        db,
        lambda: gate.prepare_handoff(db, row, "worker", state.now_local()),
        lambda: state.set_run_state(
            "source", "owner", "parent", "admin", True, 1
        ),
        entered,
        release,
    )
    assert result[0] == "ready"
    assert result[1]["inflight_count"] == 1
    stored = await db.fetch_one(
        "SELECT * FROM swe_cron_dispatch_intents WHERE id=%s", (row["id"],)
    )
    assert stored["status"] == "acknowledged"
    assert stored["attempt_count"] == 1


async def test_same_model_concurrent_claims_share_one_capacity(
    mysql_db, monkeypatch
):
    from scheduler.app.services.cron import batch_run_state as state
    from scheduler.app.services.cron import dispatch_intent_service as module

    db = mysql_db
    await seed_batch(db, count=3)
    now = state.now_local()
    await db.execute(
        "INSERT INTO swe_cron_dispatch_scope_leases "
        "(source_id,provider_id,model_id,lock_owner,lease_expires_at) "
        "VALUES ('source','default','default','worker',%s)",
        (now + timedelta(minutes=10),),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    available = module.available_capacity

    async def hold_scope(*args):
        count = await available(*args)
        if asyncio.current_task().get_name() == "first":
            entered.set()
            await release.wait()
        return count

    monkeypatch.setattr(module, "available_capacity", hold_scope)
    store = module.CronDispatchIntentService()

    async def claim():
        return await store.claim_due_intents(
            lock_owner="worker",
            now_utc=now,
            limit=3,
            source_ids=["source"],
            capacity={
                "strategy_id": "test",
                "effective_workers": 1,
                "min_workers": 1,
                "max_workers": 1,
            },
        )

    results = await locked_pair(db, claim, claim, entered, release)
    assert [len(result) for result in results] == [1, 0]
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_cron_dispatch_intents "
            "WHERE status='claimed'"
        )
    )["n"] == 1
    assert results[0][0].claim_token


async def test_definition_changed_while_waiting_is_checked_fresh(mysql_db):
    from scheduler.app.services.cron import (
        batch_run_state as state,
        dispatch_gate as gate,
    )

    db = mysql_db
    row = (await seed_batch(db, claimed=True))[0]
    task = None
    async with db.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT paused FROM swe_cron_dispatch_controls FOR UPDATE"
                )
                task = asyncio.create_task(
                    gate.prepare_handoff(db, row, "worker", state.now_local())
                )
                await wait_for_innodb_wait(db)
                await cur.execute(
                    "UPDATE swe_cron_jobs SET enabled=0,status='paused' "
                    "WHERE id=%s",
                    (row["job_id"],),
                )
            await conn.commit()
        finally:
            await conn.rollback()
    try:
        assert await asyncio.wait_for(task, 8) == "skipped"
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_failed_admission_rolls_back_and_releases_control(mysql_db):
    from scheduler.app.services.cron import (
        batch_run_state as state,
        batch_admission as admission,
    )

    db = mysql_db
    await state.initialize_parent(db, await job(db, "parent"))
    now = state.now_local()
    batch = dict(
        batch_id="b",
        parent_job_id="parent",
        parent_external_job_id="",
        tenant_id="owner",
        source_id="source",
        provider_id="default",
        model_id="default",
        agent_id="default",
        scheduled_fire_at=now,
        callback_received_at=now,
        callback_metadata={},
    )
    row = dict(
        intent_role="parent",
        source_id="source",
        provider_id="default",
        model_id="default",
        tenant_id="owner",
        agent_id="default",
        job_id="parent",
        parent_job_id="parent",
        due_at=now,
        dispatch_order=0,
        viewer_heat_score=0,
        payload={},
    )
    with pytest.raises(Exception, match="Duplicate"):
        await admission.admit_batch(
            db, batch, [row, row], physical_fire_at=now
        )
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_cron_dispatch_batches"
        )
    )["n"] == 0
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_cron_dispatch_intents"
        )
    )["n"] == 0
    assert (
        len(
            (
                await admission.admit_batch(
                    db, batch, [row], physical_fire_at=now
                )
            )["intent_ids"]
        )
        == 1
    )


async def test_manual_retry_rechecks_job_closed_while_waiting(mysql_db):
    from scheduler.app.services.cron import batch_operations as ops

    db = mysql_db
    row = (await seed_batch(db))[0]
    await db.execute(
        "UPDATE swe_cron_dispatch_intents SET status='failed',"
        "attempt_count=1,error_message='rate limit'"
    )
    body = ops.RetryRequest(candidates=[{"id": row["id"], "attempt_count": 1}])
    task = None
    async with db.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT paused FROM swe_cron_dispatch_controls FOR UPDATE"
                )
                task = asyncio.create_task(
                    ops.retry_failed("source", "batch", "admin", body)
                )
                await wait_for_innodb_wait(db)
                await cur.execute(
                    "UPDATE swe_cron_jobs SET enabled=0,status='paused' "
                    "WHERE id=%s",
                    (row["job_id"],),
                )
            await conn.commit()
        finally:
            await conn.rollback()
    try:
        result = await asyncio.wait_for(task, 8)
        assert result["queued"] == 0
        assert (
            await db.fetch_one("SELECT status FROM swe_cron_dispatch_intents")
        )["status"] == "failed"
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_resume_fences_old_token_even_when_parent_itself_is_disabled(
    mysql_db,
):
    from scheduler.app.services.cron import (
        batch_run_state as state,
        dispatch_gate as gate,
    )
    from scheduler.app.services.cron.dispatch_intent_service import (
        CronDispatchIntentService,
    )

    db = mysql_db
    old = (await seed_batch(db, claimed=True))[0]
    await state.set_run_state("source", "owner", "parent", "admin", True, 1)
    await db.execute(
        "UPDATE swe_cron_jobs SET enabled=0,status='paused' WHERE id='parent'"
    )
    await state.set_run_state("source", "owner", "parent", "admin", False, 2)
    fresh = (
        await CronDispatchIntentService().claim_due_intents(
            lock_owner="worker",
            now_utc=state.now_local(),
            limit=1,
            source_ids=["source"],
        )
    )[0]
    assert fresh.attempt_count == old["attempt_count"]
    assert fresh.claim_token != old["claim_token"]
    assert (
        await gate.prepare_handoff(db, old, "worker", state.now_local())
        == "stale"
    )
    assert (
        await gate.prepare_handoff(db, fresh, "worker", state.now_local())
        == "ready"
    )


async def test_resume_timestamp_retains_subsecond_cutoff(
    mysql_db, monkeypatch
):
    from scheduler.app.services.cron import batch_run_state as state

    db = mysql_db
    await seed_batch(db)
    await state.set_run_state("source", "owner", "parent", "admin", True, 1)
    cutoff = datetime(2026, 9, 10, 12, 34, 56, 123456)
    monkeypatch.setattr(state, "now_local", lambda: cutoff)
    await state.set_run_state("source", "owner", "parent", "admin", False, 2)
    assert (
        await db.fetch_one("SELECT resumed_at FROM swe_cron_dispatch_controls")
    )["resumed_at"] == cutoff

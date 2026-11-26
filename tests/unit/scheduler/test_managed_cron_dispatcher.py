# -*- coding: utf-8 -*-
"""验证派发重用持久执行身份，并以共库受理事实为准。"""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tests.unit.scheduler.test_managed_cron_executions import (
    enqueue,
    execution_setup,
)
from tests.unit.scheduler.test_managed_cron_callbacks import (
    callback_setup,
    create_source,
)


class SweClient:
    def __init__(self, db, *, lose_response=False, accept=True):
        self.db = db
        self.lose_response = lose_response
        self.accept = accept
        self.calls = []

    async def execute(self, row, caller):
        from swe.app.crons.managed_results import ExecutionWriter

        self.calls.append(row["execution_id"])
        if self.accept:
            await ExecutionWriter(self.db).accept(
                row["execution_id"],
                caller.job_key(row["job_id"]),
            )
        if self.lose_response:
            raise TimeoutError("SWE response was lost")
        return {"execution_id": row["execution_id"], "accepted": True}


@pytest.mark.asyncio
async def test_lost_acceptance_response_uses_db_fact_not_new_execution(
    execution_setup,
):
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )

    db, caller, _ = execution_setup
    row, _ = await enqueue(execution_setup)
    client = SweClient(db, lose_response=True)
    result = await ExecutionDispatcher(db, client).dispatch(
        caller,
        row["job_id"],
        row["execution_id"],
    )
    assert result["status"] == "accepted"
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 1
    )
    assert client.calls == [row["execution_id"]]


@pytest.mark.asyncio
async def test_http_ack_without_db_acceptance_does_not_report_accepted(
    execution_setup,
):
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )

    db, caller, _ = execution_setup
    row, _ = await enqueue(execution_setup)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    dispatcher = ExecutionDispatcher(
        db,
        SweClient(db, accept=False),
        clock=lambda: now,
    )
    for _ in range(3):
        result = await dispatcher.dispatch(
            caller,
            row["job_id"],
            row["execution_id"],
        )
        now += timedelta(minutes=2)
    assert result["status"] == "dispatch_failed"
    assert result["error_code"] == "cron_dispatch_unconfirmed"
    assert (
        db.conn.execute("SELECT COUNT(*) FROM swe_cron_executions").fetchone()[
            0
        ]
        == 0
    )


@pytest.mark.asyncio
async def test_unconfirmed_dispatch_waits_for_each_retry_deadline(
    execution_setup,
):
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )

    db, _, _ = execution_setup
    row, _ = await enqueue(execution_setup)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    client = SweClient(db, accept=False, lose_response=True)
    dispatcher = ExecutionDispatcher(db, client, clock=lambda: now)
    for attempt, delay in enumerate((5, 10, 20, 30, 60, 60, 60), start=1):
        result = await dispatcher.dispatch_registered(row["execution_id"])
        assert result["dispatch_attempt"] == attempt
        assert result["dispatch_lease_until"] == now + timedelta(seconds=delay)
        assert result["status"] == (
            "queued" if attempt < 3 else "dispatch_failed"
        )
        now += timedelta(seconds=delay - 1)
        await dispatcher.dispatch_registered(row["execution_id"])
        assert client.calls == [row["execution_id"]] * attempt
        now += timedelta(seconds=1)
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous_attempts", "requeue_status"),
    [(0, "queued"), (2, "dispatch_failed")],
)
async def test_requeue_refreshes_follow_level_after_dispatch_attempt(
    callback_setup,
    previous_attempts,
    requeue_status,
):
    from scheduler.app.services.cron.managed.batch_follow import (
        set_batch_follow_level,
    )
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )
    from scheduler.app.services.cron.managed.scheduling import (
        ManagedSchedulingService,
    )

    db, caller, _ = callback_setup
    created = await create_source(callback_setup, mode="batch_enabled")
    batch = await ManagedSchedulingService(db).run_batch(
        caller,
        created["job_id"],
    )
    row = await db.fetch_one(
        "SELECT execution_id FROM swe_cron_execution_runs",
    )
    await db.execute(
        "UPDATE swe_cron_execution_runs SET dispatch_attempt=%s",
        (previous_attempts,),
    )

    class Unconfirmed:
        async def execute(self, claimed, _caller):
            assert claimed["status"] == "dispatching"
            await set_batch_follow_level(
                db,
                replace(caller, is_manager=True),
                batch["batch_id"],
                5,
            )

    result = await ExecutionDispatcher(db, Unconfirmed()).dispatch_registered(
        row["execution_id"],
    )
    assert result["status"] == requeue_status
    saved = await db.fetch_one(
        "SELECT dispatch_meta_json FROM swe_cron_execution_runs WHERE execution_id=%s",
        (row["execution_id"],),
    )
    assert json.loads(saved["dispatch_meta_json"])["batch_follow_level"] == 5


@pytest.mark.asyncio
async def test_two_dispatch_workers_cannot_claim_same_run_simultaneously(
    execution_setup,
):
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
    )

    db, caller, _ = execution_setup
    row, _ = await enqueue(execution_setup)
    entered, release = asyncio.Event(), asyncio.Event()
    client = SweClient(db)
    original = client.execute

    async def slow(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    client.execute = slow
    first = asyncio.create_task(
        ExecutionDispatcher(db, client).dispatch(
            caller,
            row["job_id"],
            row["execution_id"],
        ),
    )
    await entered.wait()
    await ExecutionDispatcher(db, client).dispatch(
        caller,
        row["job_id"],
        row["execution_id"],
    )
    release.set()
    await first
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_actual_http_client_and_swe_route_share_one_execution_record(
    execution_setup,
    monkeypatch,
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from scheduler.app.services.cron.managed.dispatcher import (
        ExecutionDispatcher,
        ManagedSweExecutionClient,
    )
    from swe.app.crons import managed_execution_api
    from swe.app.crons.managed_execution import ManagedExecutionService
    from swe.app.crons.managed_results import ExecutionWriter
    from tests.unit.app.test_managed_cron_execution import Engine
    from tests.fakes.managed_source_config import ManagedSourceConfigService

    db, caller, _ = execution_setup
    row, _ = await enqueue(execution_setup)
    engine = Engine()

    async def service(_request):
        return ManagedExecutionService(ExecutionWriter(db), engine)

    monkeypatch.setattr(
        managed_execution_api,
        "get_execution_service",
        service,
    )
    app = FastAPI()
    app.state.db_connection = db
    app.state.source_system_config_service = ManagedSourceConfigService()
    app.include_router(managed_execution_api.router, prefix="/api")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        dispatcher = ExecutionDispatcher(
            db,
            ManagedSweExecutionClient("http://test", http=client),
        )
        accepted = await dispatcher.dispatch(
            caller,
            row["job_id"],
            row["execution_id"],
        )
        assert accepted["status"] in {"accepted", "running", "success"}
        await asyncio.gather(*getattr(app.state, "managed_cron_tasks", set()))
    current = db.conn.execute(
        "SELECT * FROM swe_cron_execution_runs",
    ).fetchone()
    assert current["status"] == "success"
    assert current["history_execution_id"]
    assert len(engine.calls) == 1

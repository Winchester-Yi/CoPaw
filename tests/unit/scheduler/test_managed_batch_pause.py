# -*- coding: utf-8 -*-
from dataclasses import replace

import pytest

from tests.unit.scheduler.test_managed_cron_callbacks import callback_setup
from tests.unit.scheduler.test_managed_batch_retry import (
    failed_batch,
    retry_body,
)
from tests.unit.scheduler.test_managed_cron_dispatcher import SweClient
from scheduler.app.services.cron.managed.batch_pause import set_batch_paused
from scheduler.app.services.cron.managed.batch_retry import (
    BatchRetryConflict,
    BatchRetryService,
)
from scheduler.app.services.cron.managed.auto_retry import AutoBatchRetry
from scheduler.app.services.cron.managed.dispatcher import ExecutionDispatcher
from scheduler.app.services.cron.managed.work_query import ManagedWorkQuery


@pytest.mark.asyncio
async def test_pause_blocks_auto_and_manual_retry_and_resume_preserves_attempts(
    callback_setup,
):
    db = callback_setup[0]
    caller, batch = await failed_batch(callback_setup)
    service = BatchRetryService(db)
    command = retry_body(await service.preview(caller, batch))
    assert (await set_batch_paused(db, caller, batch, True))["batch_paused"]
    assert await AutoBatchRetry(db).tick() == 0
    with pytest.raises(BatchRetryConflict, match="cron_batch_paused"):
        await service.retry(caller, batch, command)
    member = await db.fetch_one("SELECT * FROM swe_cron_managed_batch_members")
    assert member["business_attempt"] == 1
    await set_batch_paused(db, caller, batch, False)
    assert await AutoBatchRetry(db).tick() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("ordinary_origin", [False, True])
async def test_pause_holds_queued_dispatch_until_resume(
    callback_setup,
    ordinary_origin,
):
    db = callback_setup[0]
    caller, batch = await failed_batch(callback_setup)
    service = BatchRetryService(db)
    result = await service.retry(
        caller,
        batch,
        retry_body(await service.preview(caller, batch)),
    )
    execution = result["results"][0]["execution_id"]
    if ordinary_origin:
        import json

        row = await db.fetch_one(
            "SELECT dispatch_meta_json FROM swe_cron_execution_runs WHERE execution_id=%s",
            (execution,),
        )
        meta = json.loads(row["dispatch_meta_json"])
        meta.pop("batch_id", None)
        await db.execute(
            "UPDATE swe_cron_execution_runs SET kind='ordinary',dispatch_meta_json=%s WHERE execution_id=%s",
            (json.dumps(meta), execution),
        )
    client = SweClient(db)
    dispatcher = ExecutionDispatcher(db, client)
    await set_batch_paused(db, caller, batch, True)
    assert (await dispatcher.dispatch_registered(execution))[
        "status"
    ] == "queued"
    assert not client.calls
    assert await ManagedWorkQuery(db, 32).rows(running=False) == []
    await set_batch_paused(db, caller, batch, False)
    assert (await dispatcher.dispatch_registered(execution))[
        "status"
    ] == "accepted"
    assert client.calls == [execution]
    await set_batch_paused(db, caller, batch, True)
    assert (
        await db.fetch_one(
            "SELECT status FROM swe_cron_execution_runs WHERE execution_id=%s",
            (execution,),
        )
    )["status"] == "accepted"


@pytest.mark.asyncio
async def test_paused_queue_uses_no_worker_and_inflight_releases_after_settlement(
    callback_setup,
):
    from scheduler.app.services.cron.capacity_claim import _managed_occupants
    from scheduler.app.services.cron.managed.capacity import TransactionCursor
    from scheduler.app.services.cron.managed.settlement import (
        ExecutionSettlement,
    )
    from swe.app.crons.managed_results import ExecutionWriter, ExecutionOutcome

    db = callback_setup[0]
    caller, batch = await failed_batch(callback_setup)
    service = BatchRetryService(db)
    result = await service.retry(
        caller,
        batch,
        retry_body(await service.preview(caller, batch)),
    )
    execution = result["results"][0]["execution_id"]
    run = await db.fetch_one(
        "SELECT * FROM swe_cron_execution_runs WHERE execution_id=%s",
        (execution,),
    )
    scope = tuple(
        run[name] for name in ("source_id", "provider_id", "model_id")
    )
    cursor = TransactionCursor(db)
    await set_batch_paused(db, caller, batch, True)
    assert await _managed_occupants(cursor, scope) == 0
    await set_batch_paused(db, caller, batch, False)
    await ExecutionDispatcher(db, SweClient(db)).dispatch_registered(execution)
    await set_batch_paused(db, caller, batch, True)
    assert await _managed_occupants(cursor, scope) == 1
    writer = ExecutionWriter(db)
    key = tuple(
        run[name] for name in ("tenant_id", "source_id", "agent_id", "job_id")
    )
    assert await writer.start(execution, key, "pause-test-owner")
    await writer.complete(
        execution,
        key,
        "pause-test-owner",
        ExecutionOutcome("success"),
    )
    assert await _managed_occupants(cursor, scope) == 1
    await db.execute(
        "UPDATE swe_cron_executions SET async_status='success' "
        "WHERE id=(SELECT history_execution_id FROM swe_cron_execution_runs "
        "WHERE execution_id=%s)",
        (execution,),
    )
    await ExecutionSettlement(db).tick()
    assert await _managed_occupants(cursor, scope) == 0


@pytest.mark.asyncio
async def test_pause_is_source_and_role_scoped_and_visible_on_board(
    callback_setup,
):
    from monitor.app.services.cron.managed_board import ManagedBoardQuery
    from monitor.app.models.managed_board import BoardDetailQuery

    db = callback_setup[0]
    caller, batch = await failed_batch(callback_setup)
    with pytest.raises(PermissionError):
        await set_batch_paused(
            db,
            replace(caller, is_manager=False),
            batch,
            True,
        )
    with pytest.raises(LookupError):
        await set_batch_paused(
            db,
            replace(caller, source_id="other"),
            batch,
            True,
        )
    await set_batch_paused(db, caller, batch, True)
    listing = await ManagedBoardQuery(db).batches(caller.source_id)
    detail = await ManagedBoardQuery(db).detail(
        caller.source_id,
        batch,
        BoardDetailQuery(),
    )
    assert listing["items"][0]["batch_paused"]
    assert detail["batch"]["batch_paused"]
    assert detail["batch"]["status"] == "failed"


@pytest.mark.asyncio
async def test_pause_api_requires_manager_source_and_boolean_body(
    callback_setup,
):
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport
    from scheduler.app.routers import managed_batch_operations as api

    db = callback_setup[0]
    caller, batch = await failed_batch(callback_setup)
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.management_database] = lambda: db
    headers = {
        "X-User-Id": caller.actor_id,
        "X-Source-Id": caller.source_id,
        "X-User-Role": "manager",
    }
    path = f"/api/scheduler/cron/managed-dispatch/batches/{batch}/pause"
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        for value in [True, False]:
            result = await client.put(
                path,
                headers=headers,
                json={"paused": value},
            )
            assert result.status_code == 200
            assert result.json()["batch_paused"] is value
        assert (
            await client.put(path, headers=headers, json={"paused": "false"})
        ).status_code == 422
        assert (
            await client.put(
                path,
                headers=headers,
                json={"paused": True, "source_id": "other"},
            )
        ).status_code == 422
        assert (
            await client.put(
                path,
                headers={**headers, "X-User-Role": "user"},
                json={"paused": True},
            )
        ).status_code == 403
        assert (
            await client.put(
                path,
                headers={**headers, "X-Source-Id": "other"},
                json={"paused": True},
            )
        ).status_code == 404

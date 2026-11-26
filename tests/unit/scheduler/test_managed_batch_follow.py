# -*- coding: utf-8 -*-
"""Followed batch scope uses the receipt's parent at selection and admission."""

import json
from dataclasses import replace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from monitor.app.services.cron.managed_board import ManagedBoardQuery
from scheduler.app.routers import managed_batch_operations
from scheduler.app.services.cron.managed.batch_follow import (
    set_batch_follow_level,
)
from tests.unit.scheduler.test_daily_failure_retry import scenario
from scheduler.app.services.cron.managed import daily_retry_service
from scheduler.app.services.cron.managed.daily_retry_service import (
    DailyFailureRetry,
)


async def scan_followed(scenario, *, job_ids=None):
    service = DailyFailureRetry(scenario.db, clock=scenario.clock)
    call = await service.calls.submit(
        "bank",
        task_scope="followed_batch",
        job_ids=job_ids,
    )
    await service.process(call["call_id"])
    return await service.detail("bank", call["call_id"])


@pytest.mark.asyncio
async def test_followed_scope_uses_parent_for_reused_ordinary_child(scenario):
    _, followed, failure = await scenario.batch(attempts=3, children=True)
    await scenario.batch(attempts=3)
    await scenario.run()
    assert (await scan_followed(scenario))["call"]["scanned_count"] == 0
    caller = replace(scenario.caller, is_manager=True)
    await set_batch_follow_level(scenario.db, caller, followed, 3)
    await scenario.db.execute(
        "UPDATE swe_cron_execution_runs SET kind='ordinary' WHERE execution_id=%s",
        (failure["execution_id"],),
    )
    assert (await scan_followed(scenario, job_ids=[]))["call"][
        "scanned_count"
    ] == 0
    result = await scan_followed(scenario, job_ids=[failure["job_id"]])
    assert result["call"]["queued_count"] == 1
    assert [item["previous_execution_id"] for item in result["items"]] == [
        failure["execution_id"],
    ]
    requeued = await scenario.db.fetch_one(
        "SELECT dispatch_meta_json FROM swe_cron_execution_runs WHERE execution_id=%s",
        (result["items"][0]["execution"]["execution_id"],),
    )
    assert (
        json.loads(requeued["dispatch_meta_json"])["batch_follow_level"] == 3
    )


@pytest.mark.asyncio
async def test_unfollow_after_scan_is_rechecked_before_enqueue(
    scenario, monkeypatch
):
    _, batch_id, _ = await scenario.batch(attempts=3)
    manager = replace(scenario.caller, is_manager=True)
    await set_batch_follow_level(scenario.db, manager, batch_id, 3)
    prepare = daily_retry_service.prepare_retry

    async def cancel_then_prepare(db, row):
        await set_batch_follow_level(db, manager, batch_id, 0)
        return await prepare(db, row)

    monkeypatch.setattr(
        daily_retry_service, "prepare_retry", cancel_then_prepare
    )
    result = await scan_followed(scenario)
    assert result["call"]["queued_count"] == 0
    assert (
        result["items"][0]["reason"] == "cron_daily_retry_batch_not_followed"
    )


@pytest.mark.asyncio
async def test_follow_is_scoped_idempotent_and_updates_parent(scenario):
    created, batch_id, _ = await scenario.batch(children=True)
    db = scenario.db
    with pytest.raises(PermissionError):
        await set_batch_follow_level(db, scenario.caller, batch_id, 3)
    manager = replace(scenario.caller, is_manager=True)
    with pytest.raises(LookupError):
        await set_batch_follow_level(
            db, replace(manager, source_id="BANK"), batch_id, 3
        )
    assert await set_batch_follow_level(db, manager, batch_id, 4) == {
        "batch_id": batch_id,
        "batch_follow_level": 4,
    }
    await set_batch_follow_level(
        db, replace(manager, actor_id="second"), batch_id, 4
    )
    rows = await db.fetch_all(
        "SELECT job_id,batch_follow_updated_by FROM swe_cron_job_states "
        "WHERE batch_follow_level=4",
    )
    assert rows == [
        {
            "job_id": created["job_id"],
            "batch_follow_updated_by": manager.actor_id,
        }
    ]
    listing = await ManagedBoardQuery(db).batches(manager.source_id)
    assert listing["items"][0]["batch_follow_level"] == 4


@pytest.mark.asyncio
async def test_follow_api_accepts_only_level_and_scoped_manager(scenario):
    _, batch_id, _ = await scenario.batch()
    app = FastAPI()
    app.include_router(managed_batch_operations.router, prefix="/api")
    app.dependency_overrides[managed_batch_operations.management_database] = (
        lambda: scenario.db
    )
    path = f"/api/scheduler/cron/managed-dispatch/batches/{batch_id}/follow"
    identity = {"X-User-Id": "operator", "X-Source-Id": "bank"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(path, json={"level": 3}, headers=identity)
        assert response.status_code == 403
        headers = {**identity, "X-User-Role": "manager"}
        response = await client.put(path, json={"level": "3"}, headers=headers)
        assert response.status_code == 422
        response = await client.put(
            path, json={"followed": True}, headers=headers
        )
        assert response.status_code == 422
        response = await client.put(path, json={"level": 6}, headers=headers)
        assert response.status_code == 422
        response = await client.put(path, json={"level": 3}, headers=headers)
        assert response.status_code == 200
        assert response.json() == {
            "batch_id": batch_id,
            "batch_follow_level": 3,
        }

# -*- coding: utf-8 -*-
"""外部平台补跑的受理/查询契约和脱敏错误日志。"""

import logging

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from tests.unit.scheduler.test_daily_failure_retry import scenario
from scheduler.app.routers import daily_failure_retry as api
from scheduler.app.services.cron.managed.daily_retry_service import (
    DailyFailureRetry,
)

PATH = "/api/scheduler/cron/failure-reruns"


@pytest.mark.asyncio
async def test_body_only_202_queries_and_source_isolation(scenario):
    await scenario.run()
    service = DailyFailureRetry(scenario.db, clock=scenario.clock)
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.daily_service] = lambda: service
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(PATH, json={"source_id": "bank"})
        assert response.status_code == 202
        call_id = response.json()["call"]["call_id"]
        assert (
            scenario.db.conn.execute(
                "SELECT COUNT(*) FROM swe_cron_execution_runs",
            ).fetchone()[0]
            == 1
        )
        await service.process(call_id)
        result = await client.get(
            f"{PATH}/{call_id}",
            params={"source_id": "bank"},
        )
        assert result.status_code == 200
        assert result.json()["call"]["status"] == "completed"
        assert result.json()["items"][0]["execution"]["status"] == "queued"
        assert (
            await client.get(f"{PATH}/{call_id}", params={"source_id": "BANK"})
        ).status_code == 404
        assert (
            await client.get(
                f"{PATH}/{call_id}",
                params={"source_id": "bank", "limit": 201},
            )
        ).status_code == 422


@pytest.mark.asyncio
async def test_invalid_body_runtime_and_storage_have_no_fake_acceptance(
    scenario,
    caplog,
):
    service = DailyFailureRetry(scenario.db, clock=scenario.clock)
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.daily_service] = lambda: service
    caplog.set_level(logging.WARNING)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        for body in (
            {},
            {"source_id": ""},
            {"source_id": "bank", "tenant_id": "private-user"},
            {"source_id": " bank"},
            {"source_id": "bank", "task_scope": "invalid"},
            {"source_id": "bank", "job_ids": [""]},
        ):
            assert (await client.post(PATH, json=body)).status_code == 422
        app.state.managed_cron_runtime_prepared = True
        app.state.managed_cron_runtime_ready = False
        assert (
            await client.post(PATH, json={"source_id": "bank"})
        ).status_code == 503
        app.state.managed_cron_runtime_ready = True
        scenario.db.conn.execute("DROP TABLE swe_cron_failure_retry_calls")
        response = await client.post(PATH, json={"source_id": "bank"})
        assert response.status_code == 503
        assert response.json() == {
            "detail": "cron_daily_retry_storage_unavailable",
        }
    assert "补跑接口失败" in caplog.text
    assert "private-user" not in caplog.text


@pytest.mark.asyncio
async def test_filtered_call_persists_scope_and_targets_batch_parent(
    scenario,
    monkeypatch,
):
    from scheduler.app.services.cron.managed import daily_retry_scan

    monkeypatch.setattr(daily_retry_scan, "CRON_BATCH_MAX_ATTEMPTS", 1)
    ordinary = await scenario.run()
    another = await scenario.run()
    await scenario.run()
    parent, _, child = await scenario.batch(
        children=True,
        fail_all_children=True,
    )
    await scenario.db.execute(
        "UPDATE swe_cron_execution_runs SET kind='ordinary' WHERE execution_id=%s",
        (child["execution_id"],),
    )
    await scenario.batch()
    service = DailyFailureRetry(scenario.db, clock=scenario.clock, page_size=1)
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.daily_service] = lambda: service
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        for scope, job_ids, count in (
            ("batch", [parent["job_id"], ordinary["job_id"]], 2),
            (
                "ordinary",
                [ordinary["job_id"], another["job_id"], parent["job_id"]],
                2,
            ),
            ("all", [], 0),
        ):
            response = await client.post(
                PATH,
                json={
                    "source_id": "bank",
                    "task_scope": scope,
                    "job_ids": job_ids,
                },
            )
            assert response.status_code == 202, response.text
            call_id = response.json()["call"]["call_id"]
            assert response.json()["call"]["status"] == "queued"
            stored = await scenario.db.fetch_one(
                "SELECT status FROM swe_cron_failure_retry_calls WHERE call_id=%s",
                (call_id,),
            )
            assert stored["status"] == "filtered_queued"
            resumed = DailyFailureRetry(
                scenario.db,
                clock=scenario.clock,
                page_size=1,
            )
            for _ in range(count + 1):
                await resumed.tick()
            result = await resumed.detail("bank", call_id)
            assert result["call"]["status"] == "completed"
            assert result["call"]["scanned_count"] == count
            assert result["call"]["queued_count"] == count
            if scope == "batch":
                assert {item["tenant_id"] for item in result["items"]} == {
                    "bob",
                    "carol",
                }
            elif scope == "ordinary":
                assert {item["job_id"] for item in result["items"]} == {
                    ordinary["job_id"],
                    another["job_id"],
                }


@pytest.mark.asyncio
async def test_filtered_call_never_expands_if_scope_event_is_missing(scenario):
    await scenario.run()
    service = DailyFailureRetry(scenario.db, clock=scenario.clock)
    call = await service.calls.submit("bank", task_scope="ordinary")
    await scenario.db.execute(
        "DELETE FROM swe_cron_managed_dispatch_events "
        "WHERE event_type='daily_retry_scope'",
    )

    assert await service.tick() == 0
    result = await service.detail("bank", call["call_id"])
    assert result["call"]["status"] == "queued"
    assert result["call"]["error_code"] == "cron_daily_retry_scan_failed"
    assert result["call"]["scanned_count"] == 0
    assert result["items"] == []


@pytest.mark.asyncio
async def test_query_pages_and_each_request_has_its_own_call(scenario):
    await scenario.run()
    await scenario.run()
    service = DailyFailureRetry(scenario.db, clock=scenario.clock)
    first = await service.calls.submit("bank")
    second = await service.calls.submit("bank")
    assert first["call_id"] != second["call_id"]
    await service.process(first["call_id"])
    one = await service.detail("bank", first["call_id"], limit=1)
    assert len(one["items"]) == 1
    assert one["next_event_order"]
    two = await service.detail(
        "bank",
        first["call_id"],
        after=one["next_event_order"],
        limit=1,
    )
    assert len(two["items"]) == 1
    assert (
        one["items"][0]["execution"]["execution_id"]
        != two["items"][0]["execution"]["execution_id"]
    )

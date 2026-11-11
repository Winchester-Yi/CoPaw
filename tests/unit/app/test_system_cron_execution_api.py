# -*- coding: utf-8 -*-
"""内部快速受理后后台执行；错误Header/令牌不能初始化业务资源。"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from swe.app.crons import system_api
from tests.unit.scheduler.test_system_cron_callbacks import ready
from tests.unit.scheduler.test_system_cron_management import USER, system_setup

PATH = "/api/internal/cron/system/execute"
HEADERS = {
    "X-User-Id": "alice",
    "X-Tenant-Id": "alice",
    "X-Source-Id": "bank",
    "X-Agent-Id": "default",
}


@pytest.fixture
async def execute_api(system_setup, monkeypatch):
    db, _, now = system_setup
    now[0] = datetime.now(timezone.utc)
    store, job, body = await ready(system_setup)
    run = await store.accept(USER, body)
    await store.claim_dispatch(run["execution_id"], "dispatch")
    app = FastAPI()
    app.state.db_connection = db
    app.include_router(system_api.router, prefix="/api")
    release = asyncio.Event()
    service = SimpleNamespace(
        execute=AsyncMock(side_effect=lambda *a: release.wait()),
    )

    async def execute(*args):
        await release.wait()

    service.execute.side_effect = execute
    factory = AsyncMock(return_value=service)
    monkeypatch.setattr(system_api, "get_system_execution_service", factory)
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
    ) as client:
        yield client, {
            "job_id": job["job_id"],
            "execution_id": run["execution_id"],
            "dispatch_token": "dispatch",
        }, service, factory
    release.set()
    await asyncio.gather(*getattr(app.state, "system_cron_tasks", {}).values())


@pytest.mark.asyncio
async def test_execute_is_durably_accepted_and_local_duplicates_dont_prepare_twice(
    execute_api,
):
    client, body, service, factory = execute_api
    first = await client.post(PATH, headers=HEADERS, json=body)
    second = await client.post(PATH, headers=HEADERS, json=body)
    assert first.status_code == second.status_code == 202
    assert first.json()["status"] == "accepted"
    await asyncio.sleep(0)
    factory.assert_awaited_once()
    service.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_route_or_dispatch_cannot_prepare_resources(execute_api):
    client, body, service, factory = execute_api
    wrong_route = await client.post(
        PATH,
        headers={**HEADERS, "X-Source-Id": "other"},
        json=body,
    )
    wrong_token = await client.post(
        PATH,
        headers=HEADERS,
        json={**body, "dispatch_token": "stale"},
    )
    assert wrong_route.status_code == 404
    assert wrong_token.status_code == 409
    factory.assert_not_awaited()
    service.execute.assert_not_awaited()

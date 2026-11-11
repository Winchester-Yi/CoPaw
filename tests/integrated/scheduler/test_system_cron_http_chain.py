# -*- coding: utf-8 -*-
"""两服务真实HTTP与共库状态闭环，只有原业务/当前配置边界替身，不替换受理和结果层。"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from scheduler.app.routers import system_cron, system_cron_execution
from scheduler.app.services.cron.system.dispatch_worker import (
    SystemDispatchWorker,
)
from scheduler.app.services.cron.system.swe_client import SystemSweClient
from swe.app.crons import system_api
from tests.unit.scheduler.test_system_cron_callbacks import ready
from tests.unit.scheduler.test_system_cron_management import (
    CONFIG,
    system_setup,
)


@pytest.mark.asyncio
async def test_platform_to_scheduler_to_swe_writes_final_result_without_callback(
    system_setup,
    monkeypatch,
):
    db, management, now = system_setup
    now[0] = datetime.now(timezone.utc)
    store, job, body = await ready(system_setup)
    scheduler = FastAPI()
    scheduler.include_router(system_cron_execution.router, prefix="/api")
    scheduler.dependency_overrides[system_cron.system_database] = lambda: db
    swe = FastAPI()
    swe.state.db_connection = db
    swe.include_router(system_api.router, prefix="/api")
    monkeypatch.setattr(
        system_api.SystemConfigReader,
        "read",
        AsyncMock(return_value=CONFIG),
    )
    release = asyncio.Event()
    started = asyncio.Event()

    async def business():
        started.set()
        await release.wait()
        return {"sessions_cleaned": 3, "preview": "private"}

    prepare = AsyncMock(return_value=business)
    monkeypatch.setattr(system_api.SystemBusinessActions, "prepare", prepare)
    async with (
        AsyncClient(
            transport=ASGITransport(scheduler),
            base_url="http://scheduler",
        ) as platform,
        AsyncClient(
            transport=ASGITransport(swe),
            base_url="http://swe",
        ) as http,
    ):
        path = "/api/scheduler/cron/system/callback"
        trigger = await platform.post(path, json=body)
        assert trigger.status_code == 202
        execution_id = trigger.json()["execution_id"]
        worker = SystemDispatchWorker(
            store,
            management,
            SystemSweClient("http://swe", http=http),
        )
        await worker.process(execution_id)
        await asyncio.wait_for(started.wait(), timeout=2)
        assert (await store.get(execution_id))["status"] == "running"
        second = await platform.post(path, json=body)
        assert second.json()["status"] == "skipped"
        assert second.json()["execution_id"] != execution_id
        tasks = list(swe.state.system_cron_tasks.values())
        release.set()
        await asyncio.gather(*tasks)
    result = await store.get(execution_id)
    assert result["status"] == "success"
    assert result["result_json"] == '{"sessions_cleaned":3}'
    assert (await management.get(job["job_id"]))["active_execution_id"] is None
    prepare.assert_awaited_once()

# -*- coding: utf-8 -*-
"""SWE系统配置入口先核对共库操作和Header，再进行纯只读配置解析。"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from swe.app.crons import system_api
from tests.unit.app.test_system_cron_config_reader import config_scope
from tests.unit.scheduler.test_system_cron_management import (
    ADMIN,
    system_setup,
)

PATH = "/api/internal/cron/system/config"
HEADERS = {
    "X-User-Id": "alice",
    "X-Tenant-Id": "alice",
    "X-Source-Id": "bank",
    "X-Agent-Id": "default",
}


@pytest.fixture
async def system_config_api(system_setup, config_scope):
    db, store, _ = system_setup
    store.clock = lambda: datetime.now(timezone.utc)
    row = await store.refresh(ADMIN, "heartbeat", "save")
    claimed = await store.claim(row["job_id"], "worker")
    app = FastAPI()
    app.include_router(system_api.router, prefix="/api")
    app.state.db_connection = db
    app.state.source_system_config_service = None
    app.state.multi_agent_manager = SimpleNamespace(get_agent=AsyncMock())
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
    ) as client:
        yield client, claimed, app.state.multi_agent_manager


@pytest.mark.asyncio
async def test_config_reads_current_profile_without_loading_workspace(
    system_config_api,
):
    client, row, manager = system_config_api
    response = await client.get(
        PATH,
        headers=HEADERS,
        params={"job_id": row["job_id"], "operation_id": row["operation_id"]},
    )
    assert response.status_code == 200
    assert response.json()["cron"] == "0 0/1 * * *"
    manager.get_agent.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {**HEADERS, "X-Source-Id": "other"},
        {**HEADERS, "X-Tenant-Id": "bob"},
        {**HEADERS, "X-User-Id": "bob"},
    ],
)
async def test_wrong_route_never_reads_profile(
    system_config_api,
    monkeypatch,
    headers,
):
    client, row, manager = system_config_api
    read = AsyncMock(side_effect=AssertionError("unauthorized read"))
    monkeypatch.setattr(system_api.SystemConfigReader, "read", read)
    response = await client.get(
        PATH,
        headers=headers,
        params={"job_id": row["job_id"], "operation_id": row["operation_id"]},
    )
    assert response.status_code == 404
    read.assert_not_awaited()
    manager.get_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_operation_never_reads_or_rewrites_profile(
    system_config_api,
    monkeypatch,
):
    client, row, _ = system_config_api
    read = AsyncMock(side_effect=AssertionError("stale operation"))
    monkeypatch.setattr(system_api.SystemConfigReader, "read", read)
    response = await client.get(
        PATH,
        headers=HEADERS,
        params={"job_id": row["job_id"], "operation_id": "old-operation"},
    )
    assert response.status_code == 409
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_sync_reads_current_profile_through_real_swe_http(
    system_setup,
    config_scope,
):
    """两服务共用SQL，通过真实HTTP路由取得当前配置后才调用平台边界。"""
    from scheduler.app.services.cron.system.swe_client import SystemSweClient
    from scheduler.app.services.cron.system.sync_worker import SystemSyncWorker
    from tests.unit.scheduler.test_system_cron_sync_worker import Platform

    db, store, _ = system_setup
    store.clock = lambda: datetime.now(timezone.utc)
    row = await store.refresh(ADMIN, "heartbeat", "save")
    app = FastAPI()
    app.include_router(system_api.router, prefix="/api")
    app.state.db_connection = db
    app.state.source_system_config_service = None
    app.state.multi_agent_manager = SimpleNamespace(get_agent=AsyncMock())
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://swe.test",
    ) as http:
        worker = SystemSyncWorker(
            store,
            SystemSweClient("http://swe.test", http=http),
            Platform(db),
        )
        assert await worker.process(row["job_id"])
    final = await store.get(row["job_id"])
    assert final["sync_status"] == "synced"
    assert '"cron":"0 0/1 * * *"' in final["effective_config_json"]
    app.state.multi_agent_manager.get_agent.assert_not_awaited()

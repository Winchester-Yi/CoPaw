# -*- coding: utf-8 -*-
"""工作区内部按需受理固定四种任务，不伪造管理员也不接纳业务配置。"""

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from scheduler.app.routers import system_cron, system_cron_workspace
from tests.unit.scheduler.test_system_cron_management import system_setup
from tests.unit.scheduler.test_system_cron_management_api import HEADERS

PATH = "/api/scheduler/internal/cron/system/workspaces/refresh"


@pytest.mark.asyncio
async def test_workspace_refresh_is_fixed_and_source_identity_does_not_follow_admin(
    system_setup,
):
    db = system_setup[0]
    app = FastAPI()
    app.include_router(system_cron_workspace.router, prefix="/api")
    app.dependency_overrides[system_cron.system_database] = lambda: db
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            PATH,
            headers=HEADERS,
            json={"request_id": "startup"},
        )
        assert response.status_code == 202
        assert len(response.json()["items"]) == 4
        repeated = await client.post(
            PATH,
            headers=HEADERS,
            json={"request_id": "startup"},
        )
        assert all(row["generation"] == 1 for row in repeated.json()["items"])
        forged = await client.post(
            PATH,
            headers={**HEADERS, "X-User-Id": "other"},
            json={"request_id": "forged"},
        )
        assert forged.status_code == 403
        injected = await client.post(
            PATH,
            headers=HEADERS,
            json={"request_id": "injected", "task_type": "command"},
        )
        assert injected.status_code == 422
    rows = await db.fetch_all("SELECT * FROM swe_cron_system_jobs")
    assert len(rows) == 4
    assert all(
        row["tenant_id"] == row["agent_id"] == ""
        for row in rows
        if row["scope_kind"] == "source"
    )

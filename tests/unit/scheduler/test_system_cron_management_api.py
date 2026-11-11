# -*- coding: utf-8 -*-
"""系统管理接口只受理配置意图，范围由Header决定，不接受配置正文。"""

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from scheduler.app.routers import system_cron
from tests.unit.scheduler.test_system_cron_management import (
    ADMIN,
    USER,
    system_setup,
)

PATH = "/api/scheduler/cron/system/jobs"
HEADERS = {
    "X-User-Id": "alice",
    "X-Tenant-Id": "alice",
    "X-Source-Id": "bank",
    "X-Agent-Id": "default",
}


@pytest.fixture
async def system_api(system_setup):
    db, _, _ = system_setup
    app = FastAPI()
    app.include_router(system_cron.router, prefix="/api")
    app.dependency_overrides[system_cron.system_database] = lambda: db
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_refresh_is_accepted_without_waiting_for_platform(
    system_api,
    system_setup,
):
    db, _, _ = system_setup
    response = await system_api.post(
        PATH + "/refresh",
        headers=HEADERS,
        json={"task_type": "heartbeat", "request_id": "save"},
    )
    assert response.status_code == 202
    job = response.json()
    assert job["sync_status"] == "pending" and job["generation"] == 1
    assert job["effective_config"] is None and not job["binding_known"]
    assert "sync_owner" not in job and "sync_lease_until" not in job
    repeated = await system_api.post(
        PATH + "/refresh",
        headers=HEADERS,
        json={"task_type": "heartbeat", "request_id": "save"},
    )
    assert repeated.json()["generation"] == 1
    assert (
        db.conn.execute(
            "SELECT sync_stage FROM swe_cron_system_jobs",
        ).fetchone()[0]
        == "refresh"
    )


@pytest.mark.asyncio
async def test_source_management_requires_current_source_manager(system_api):
    body = {"task_type": "task_session_cleanup", "request_id": "save"}
    denied = await system_api.post(
        PATH + "/refresh",
        headers=HEADERS,
        json=body,
    )
    assert denied.status_code == 403
    accepted = await system_api.post(
        PATH + "/refresh",
        headers={**HEADERS, "X-User-Role": "manager"},
        json=body,
    )
    assert accepted.status_code == 202
    job = accepted.json()
    assert (
        job["scope_kind"] == "source"
        and job["tenant_id"] == job["agent_id"] == ""
    )


@pytest.mark.asyncio
async def test_system_get_and_list_keep_identity_isolation(
    system_api,
    system_setup,
):
    _, store, _ = system_setup
    own = await store.refresh(USER, "heartbeat", "own")
    source = await store.refresh(ADMIN, "cleanup", "source")
    page = await system_api.get(PATH, headers=HEADERS)
    assert [item["job_id"] for item in page.json()["items"]] == [own["job_id"]]
    other = await system_api.get(
        PATH + "/" + own["job_id"],
        headers={**HEADERS, "X-Source-Id": "other"},
    )
    assert other.status_code == 404
    hidden = await system_api.get(
        PATH + "/" + source["job_id"],
        headers=HEADERS,
    )
    assert hidden.status_code == 403
    admin_page = await system_api.get(
        PATH,
        headers={**HEADERS, "X-User-Role": "manager"},
    )
    assert len(admin_page.json()["items"]) == 2


@pytest.mark.asyncio
async def test_management_cannot_supply_frozen_config_or_arbitrary_identity(
    system_api,
    system_setup,
):
    db = system_setup[0]
    response = await system_api.post(
        PATH + "/refresh",
        headers=HEADERS,
        json={
            "task_type": "heartbeat",
            "request_id": "save",
            "config": {"enabled": True},
            "tenant_id": "bob",
        },
    )
    assert response.status_code == 422
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_system_jobs",
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_retry_requires_current_version_and_preserves_conflict_details(
    system_api,
    system_setup,
):
    _, store, _ = system_setup
    row = await store.refresh(USER, "heartbeat", "save")
    response = await system_api.post(
        PATH + "/" + row["job_id"] + "/retry",
        headers=HEADERS,
        json={"request_id": "retry", "expected_version": row["version"] + 10},
    )
    assert (
        response.status_code == 409
        and response.json()["detail"] == "system_version_conflict"
    )

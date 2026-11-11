# -*- coding: utf-8 -*-
"""平台固定Body无Header触发、管理员核实与公开结果脱敏。"""

from datetime import timedelta

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from scheduler.app.routers import system_cron, system_cron_execution
from tests.unit.scheduler.test_system_cron_callbacks import ready
from tests.unit.scheduler.test_system_cron_management import (
    CONFIG,
    USER,
    system_setup,
)
from tests.unit.scheduler.test_system_cron_management_api import HEADERS

BASE = "/api/scheduler/cron/system"


@pytest.fixture
async def execution_api(system_setup):
    app = FastAPI()
    app.include_router(system_cron_execution.router, prefix="/api")
    app.dependency_overrides[system_cron.system_database] = (
        lambda: system_setup[0]
    )
    async with AsyncClient(
        transport=ASGITransport(app),
        base_url="http://test",
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_fixed_callback_body_without_headers_is_not_deduplicated(
    execution_api,
    system_setup,
):
    _, _, body = await ready(system_setup)
    first = await execution_api.post(BASE + "/callback", json=body)
    second = await execution_api.post(BASE + "/callback", json=body)
    assert first.status_code == second.status_code == 202
    assert first.json()["accepted"] and second.json()["status"] == "skipped"
    assert first.json()["execution_id"] != second.json()["execution_id"]


@pytest.mark.asyncio
async def test_admin_history_is_identity_scoped_and_does_not_leak_tokens(
    execution_api,
    system_setup,
):
    store, job, body = await ready(system_setup)
    accepted = await store.accept(USER, body)
    await store.claim_dispatch(
        accepted["execution_id"],
        "private-dispatch-token",
    )
    path = BASE + "/jobs/" + job["job_id"] + "/executions"
    assert (await execution_api.get(path, headers=HEADERS)).status_code == 403
    result = await execution_api.get(
        path,
        headers={**HEADERS, "X-User-Role": "manager"},
    )
    assert result.status_code == 200
    assert (
        result.json()["items"][0]["execution_id"] == accepted["execution_id"]
    )
    assert "private-dispatch-token" not in result.text
    assert "dispatch_owner" not in result.text
    assert (
        await execution_api.get(
            path,
            headers={
                **HEADERS,
                "X-User-Role": "manager",
                "X-Source-Id": "other",
            },
        )
    ).status_code == 404


@pytest.mark.asyncio
async def test_admin_resolution_is_versioned_audited_and_never_reruns(
    execution_api,
    system_setup,
):
    from swe.app.crons.system_results import SystemResultWriter

    db, _, now = system_setup
    store, job, body = await ready(system_setup)
    accepted = await store.accept(USER, body)
    dispatched = await store.claim_dispatch(accepted["execution_id"], "token")
    writer = SystemResultWriter(db, clock=lambda: now[0])
    await writer.accept(dispatched["execution_id"], job["job_id"], "token")
    await writer.start(
        dispatched["execution_id"],
        job["job_id"],
        "swe",
        CONFIG,
    )
    now[0] += timedelta(seconds=421)
    await store.mark_unknown(dispatched["execution_id"])
    run = await store.get(dispatched["execution_id"])
    path = (
        BASE
        + "/jobs/"
        + job["job_id"]
        + "/executions/"
        + run["execution_id"]
        + "/confirm-ended"
    )
    command = {
        "expected_version": run["version"],
        "request_id": "confirmed",
        "confirmed_ended": True,
        "reason": "已核实实际进程结束",
    }
    headers = {**HEADERS, "X-User-Role": "manager"}
    conflict = await execution_api.post(
        path,
        headers=headers,
        json={**command, "expected_version": 1},
    )
    assert conflict.status_code == 409
    response = await execution_api.post(path, headers=headers, json=command)
    assert response.status_code == 200
    assert response.json()["status"] == "unknown"
    assert response.json()["resolved_by"] == "alice"
    repeated = await execution_api.post(path, headers=headers, json=command)
    assert repeated.json() == response.json()
    assert (
        len(await db.fetch_all("SELECT * FROM swe_cron_system_executions"))
        == 1
    )

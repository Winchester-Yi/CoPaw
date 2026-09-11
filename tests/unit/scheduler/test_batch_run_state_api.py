import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from tests.unit.scheduler.test_batch_run_state import control_db  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_routes_validate_identity_version_and_do_not_initialize_on_get(
    monkeypatch,
    control_db,
    configured,
):
    from scheduler.app.routers.batch_run_state import router

    monkeypatch.delenv("SWE_INTERNAL_TOKEN", raising=False)
    monkeypatch.delenv("SCHEDULER_SWE_INTERNAL_TOKEN", raising=False)
    if configured:
        monkeypatch.setenv("SWE_INTERNAL_TOKEN", "test-only-secret")
    app = FastAPI()
    app.include_router(router)
    client = AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    )
    path = (
        "/scheduler/cron/dispatch/parents/parent/run-state?tenant_id=tenant-1"
    )
    headers = {
        "X-Source-Id": "source-a",
        "X-User-Id": "admin",
    }
    control_db.add_job()
    assert (await client.get(path)).status_code == 400
    assert (await client.get(path, headers=headers)).status_code == 503
    initialized = await client.post(
        path.replace("/run-state?", "/run-state/initialize?"), headers=headers
    )
    assert initialized.status_code == 200
    assert (await client.get(path, headers=headers)).json()["paused"] is False
    assert (
        await client.put(
            path,
            headers=headers,
            json={
                "paused": "false",
                "expected_version": 1,
            },
        )
    ).status_code == 422
    assert (
        await client.put(
            path,
            headers=headers,
            json={
                "paused": True,
                "expected_version": 1,
                "tenant_id": "x",
            },
        )
    ).status_code == 422
    assert (
        await client.put(
            path,
            headers=headers,
            json={
                "paused": True,
                "expected_version": 1,
            },
        )
    ).json()["version"] == 2
    assert (
        await client.put(
            path,
            headers=headers,
            json={
                "paused": False,
                "expected_version": 1,
            },
        )
    ).status_code == 409
    assert (
        await client.get(path, headers={**headers, "X-Source-Id": "other"})
    ).status_code == 404
    await control_db.execute("UPDATE swe_cron_jobs SET meta='{}'")
    assert (
        await client.put(
            path,
            headers=headers,
            json={
                "paused": False,
                "expected_version": 2,
            },
        )
    ).status_code == 400
    await client.aclose()

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, None])
async def test_callback_only_reports_skip_for_explicit_false(accepted):
    from src.swe.app.routers.internal import _run_job_callback
    from starlette.requests import Request

    mgr = SimpleNamespace(
        get_job=AsyncMock(return_value=SimpleNamespace(meta={})),
        run_job=AsyncMock(return_value=accepted),
    )
    response = await _run_job_callback(
        Request({"type": "http", "headers": []}),
        mgr,
        {
            "callback_source": "dispatch_service",
            "dispatch_intent_id": 7,
            "dispatch_batch_id": "batch",
            "dispatch_attempt": 1,
        },
        "owner",
        "source",
        "default",
        "job",
        "job-id",
    )
    if accepted is False:
        assert response["skipped"] == "job_disabled"
    else:
        assert response is None


@pytest.mark.asyncio
async def test_disabled_run_reports_no_background_execution():
    from src.swe.app.crons.manager import CronManager

    mgr = object.__new__(CronManager)
    mgr._repo = SimpleNamespace(
        get_job=AsyncMock(return_value=SimpleNamespace(enabled=False))
    )
    assert await mgr.run_job("disabled") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("task_type", ["job", "heartbeat"])
@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Internal-Token": "wrong"}, {"Authorization": "Bearer wrong"}],
)
async def test_cron_callback_ignores_tokens_but_other_internal_routes_do_not(
    monkeypatch,
    encoded,
    task_type,
    headers,
):
    import base64
    import json
    import httpx
    from fastapi import FastAPI
    from src.swe.app.routers import internal

    monkeypatch.setattr(
        internal, "_INTERNAL_TOKEN", "required-for-other-routes"
    )
    run_agent = AsyncMock(return_value=None)
    monkeypatch.setattr(internal, "_run_agent_callback", run_agent)
    app = FastAPI()
    app.include_router(internal.router)
    payload = {
        "tenant_id": "tenant-a",
        "source_id": "source-a",
        "agent_id": "default",
        "task_type": task_type,
        "job_id": "job-a",
    }
    body = (
        {
            "jobParam": base64.urlsafe_b64encode(
                json.dumps(payload).encode()
            ).decode()
        }
        if encoded
        else payload
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/internal/cron/callback", json=body, headers=headers
        )
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "task_type": task_type}
        run_agent.assert_awaited_once()
        assert run_agent.await_args.args[2:6] == (
            "tenant-a",
            "source-a",
            "default",
            task_type,
        )
        assert (
            await client.post("/internal/cron/callback", json={})
        ).status_code == 400
        protected = await client.post(
            "/internal/agents/default/reload?tenant_id=tenant-a&source_id=source-a"
        )
        assert protected.status_code == 401

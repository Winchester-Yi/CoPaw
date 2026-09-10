from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,status",
    [("ok", 200), ("conflict", 409), ("timeout", 502), ("no-token", 503)],
)
async def test_http_proxy_keeps_parent_tenant_separate_from_operator(
    monkeypatch, outcome, status
):
    from src.swe.app.crons import batch_run_state_client as client

    monkeypatch.setenv("SWE_INTERNAL_TOKEN", "test-only-secret")
    monkeypatch.setattr(
        client, "get_scheduler_api_url", lambda: "http://scheduler.test/api"
    )
    job = SimpleNamespace(id="parent", source_id="source-a", tenant_id="owner")

    def handle(request):
        assert request.url.params["tenant_id"] == "owner"
        assert request.headers["X-Source-Id"] == "source-a"
        assert request.headers["X-User-Id"] == "admin"
        assert request.headers["X-Internal-Token"] == "Bearer test-only-secret"
        if outcome == "timeout":
            raise httpx.ReadTimeout("simulated timeout")
        return httpx.Response(
            409 if outcome == "conflict" else 200,
            json={"detail": "version conflict", "paused": True},
        )

    factory = httpx.AsyncClient
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(
        client.httpx,
        "AsyncClient",
        lambda **kwargs: factory(transport=transport, **kwargs),
    )
    if outcome == "no-token":
        monkeypatch.delenv("SWE_INTERNAL_TOKEN")
    if status == 200:
        assert (await client.request_run_state(job, "admin"))["paused"] is True
    else:
        with pytest.raises(HTTPException) as exc:
            await client.request_run_state(job, "admin")
        assert exc.value.status_code == status

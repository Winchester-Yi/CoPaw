import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "monitor" / "src")
)


@pytest.mark.asyncio
async def test_token_routes_require_matching_scope_and_do_not_trust_role():
    from monitor.app.routers.token_diagnostics import router

    class Service:
        async def get_run(self, identity, scope):
            assert scope == ("source", "alice")
            return None

    app = FastAPI()
    app.include_router(router)
    app.state.token_queries = Service()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (
            await client.get("/token-diagnostics/runs/x")
        ).status_code == 400
        headers = {
            "X-Source-Id": "source",
            "X-User-Id": "alice",
            "X-Tenant-Id": "bob",
            "X-User-Role": "admin",
        }
        assert (
            await client.get("/token-diagnostics/runs/x", headers=headers)
        ).status_code == 403
        headers["X-Tenant-Id"] = "alice"
        assert (
            await client.get("/token-diagnostics/runs/x", headers=headers)
        ).status_code == 404
        app.state.token_queries = None
        assert (
            await client.get("/token-diagnostics/runs/x", headers=headers)
        ).status_code == 503


@pytest.mark.asyncio
async def test_db_errors_return_safe_unavailable_and_bad_cursor_is_rejected():
    from monitor.app.routers.token_diagnostics import router

    class Service:
        async def list_runs(self, *args, **kwargs):
            raise RuntimeError("password=secret")

    app = FastAPI()
    app.include_router(router)
    app.state.token_queries = Service()
    headers = {"X-Source-Id": "s", "X-User-Id": "t", "X-Tenant-Id": "t"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        result = await client.get("/token-diagnostics/runs", headers=headers)
        assert result.status_code == 503
        assert "secret" not in result.text
        assert (
            await client.get(
                "/token-diagnostics/runs?cursor=bad", headers=headers
            )
        ).status_code == 400

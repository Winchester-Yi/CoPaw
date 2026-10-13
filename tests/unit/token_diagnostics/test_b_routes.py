# -*- coding: utf-8 -*-
"""ASGI checks for the scoped, read-only B API."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from tests.unit.token_diagnostics.test_relational_storage import (
    relational_db,
)  # noqa: F401

HEADERS = {"X-Source-Id": "s", "X-Tenant-Id": "alice", "X-User-Id": "alice"}
ROOT = "/api/token-diagnostics/v2"
RESOURCES = (
    ("/dashboard", "dashboard"),
    ("/tasks", "tasks"),
    ("/tasks/task", "task"),
    ("/tasks/task/branches", "branches"),
    ("/tasks/task/calls", "calls"),
    ("/contexts/context", "context"),
    ("/calls/call/prefix", "prefix"),
    ("/tasks/task/operations", "operations"),
    ("/issues", "issues"),
    ("/issues/group/instances", "instances"),
    ("/evaluations/eval", "evaluation"),
)


def test_application_router_registers_b_and_retains_a():
    from monitor.app.routers import api_router

    paths = {route.path for route in api_router.routes}
    assert "/token-diagnostics/v2/dashboard" in paths
    assert "/token-diagnostics/runs" in paths


class Service:
    def __init__(self):
        self.reads = []
        self.result = {"items": []}
        self.error: Exception | None = None

    def __getattr__(self, name):
        async def read(*args, **kwargs):
            self.reads.append((name, args, kwargs))
            if isinstance(self.error, Exception):
                raise RuntimeError(str(self.error))
            return self.result

        read.__name__ = name
        return read


@pytest.fixture
def app():
    from monitor.app.routers.token_diagnostics_v2 import router

    application = FastAPI()
    application.include_router(router, prefix="/api")
    application.state.token_diagnostic_queries = Service()
    return application


@pytest.mark.asyncio
@pytest.mark.parametrize("path,method", RESOURCES)
async def test_every_resource_is_read_only_scoped_and_safe(app, path, method):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
    ) as client:
        assert (
            await client.post(ROOT + path, headers=HEADERS)
        ).status_code == 405
        assert (await client.get(ROOT + path)).status_code == 400
        mismatch = {**HEADERS, "X-Tenant-Id": "bob", "X-User-Role": "admin"}
        assert (
            await client.get(ROOT + path, headers=mismatch)
        ).status_code == 403
        service = app.state.token_diagnostic_queries
        response = await client.get(
            ROOT + path,
            headers={**HEADERS, "X-User-Role": "admin"},
        )
        assert response.status_code == 200
        name, args, _ = service.reads[-1]
        assert name == method
        assert ("s", "alice") in args
        service.error = RuntimeError("mysql password=secret")
        response = await client.get(ROOT + path, headers=HEADERS)
        assert response.status_code == 503
        assert "secret" not in response.text
        app.state.token_diagnostic_queries = None
        assert (
            await client.get(ROOT + path, headers=HEADERS)
        ).status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    (
        "start=bad",
        "start=2026-01-01T00:00:00Z&end=2026-03-01T00:00:00Z",
        "start=2026-01-01T00:00:00",
        "limit=0",
        "limit=101",
        "limit=oops",
        "rule_id=unknown-rule",
        "state=unknown-state",
        "cursor=bad",
        "agent_id=" + "a" * 65,
        "model_name=" + "m" * 257,
    ),
)
async def test_bad_filters_return_400_before_reads(app, query):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
    ) as client:
        response = await client.get(ROOT + "/tasks?" + query, headers=HEADERS)
        assert response.status_code == 400
        assert app.state.token_diagnostic_queries.reads == []


@pytest.mark.asyncio
async def test_missing_or_foreign_detail_is_404_and_id_is_bounded(app):
    app.state.token_diagnostic_queries.result = None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
    ) as client:
        for path, _ in RESOURCES[2:8] + RESOURCES[9:]:
            assert (
                await client.get(ROOT + path, headers=HEADERS)
            ).status_code == 404
        assert (
            await client.get(ROOT + "/tasks/" + "t" * 65, headers=HEADERS)
        ).status_code == 400


@pytest.mark.asyncio
async def test_cursor_bound_to_scope_filter_and_resource(app):
    from monitor.app.services.token_diagnostics.diagnostic_filters import (
        DiagnosticFilters,
        encode_cursor,
    )

    filters = DiagnosticFilters(
        start="2026-10-11T00:00:00Z",
        end="2026-10-12T00:00:00Z",
    )
    cursor = encode_cursor(
        ("s", "bob"),
        filters,
        "tasks",
        filters.start,
        "task",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            ROOT + "/tasks",
            params={**filters.canonical(), "cursor": cursor},
            headers=HEADERS,
        )
        assert response.status_code == 400
        assert app.state.token_diagnostic_queries.reads == []


@pytest.mark.asyncio
async def test_valid_cursor_retains_window_and_object_binding(app):
    from monitor.app.services.token_diagnostics.diagnostic_filters import (
        DiagnosticFilters,
        encode_cursor,
    )

    query = DiagnosticFilters(
        start="2026-10-11T00:00:00Z",
        end="2026-10-12T00:00:00Z",
        limit=1,
    )
    token = encode_cursor(
        ("s", "alice"),
        query,
        "calls:task",
        query.start,
        "call",
    )
    params = {
        "start": "2026-10-11T00:00:00Z",
        "end": "2026-10-12T00:00:00Z",
        "limit": "1",
        "cursor": token,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
    ) as client:
        assert (
            await client.get(
                ROOT + "/tasks/task/calls",
                params=params,
                headers=HEADERS,
            )
        ).status_code == 200
        assert (
            await client.get(
                ROOT + "/tasks/other/calls",
                params=params,
                headers=HEADERS,
            )
        ).status_code == 400
        assert (
            await client.get(
                ROOT + "/tasks/task/calls",
                params={**params, "model_name": "different"},
                headers=HEADERS,
            )
        ).status_code == 400
        assert len(app.state.token_diagnostic_queries.reads) == 1


@pytest.mark.asyncio
async def test_api_reads_actual_relational_projection(app, relational_db):
    from tests.unit.token_diagnostics.test_b_queries import (
        seed_task,
        service_for,
    )

    await seed_task(relational_db)
    app.state.token_diagnostic_queries = service_for(relational_db)
    headers = {
        "X-Source-Id": "source",
        "X-User-Id": "tenant",
        "X-Tenant-Id": "tenant",
        "X-User-Role": "admin",
    }
    params = {"start": "2026-10-12T00:00:00Z", "end": "2026-10-13T00:00:00Z"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
    ) as client:
        dashboard = await client.get(
            ROOT + "/dashboard",
            params=params,
            headers=headers,
        )
        assert dashboard.status_code == 200
        assert (
            dashboard.json()["window_attempt_usage"]["known_total_tokens"]
            == 50
        )
        task = await client.get(
            ROOT + "/tasks/task",
            params=params,
            headers=headers,
        )
        assert task.status_code == 200
        assert task.json()["lifecycle_usage"]["known_total_tokens"] == 60
        foreign = {**headers, "X-User-Id": "other", "X-Tenant-Id": "other"}
        assert (
            await client.get(
                ROOT + "/tasks/task",
                params=params,
                headers=foreign,
            )
        ).status_code == 404
        assert (
            await client.get(
                ROOT + "/tasks/task/calls",
                params=params,
                headers=foreign,
            )
        ).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "end",
    [
        "9999-12-31T23:59:59-23:59",
        "0001-01-01T00:00:00+23:59",
        "0001-01-01T00:00:00Z",
    ],
)
async def test_overflowing_date_is_safe_bad_filter(app, end):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get(
            ROOT + "/tasks",
            params={"end": end},
            headers=HEADERS,
        )
        assert response.status_code == 400
        assert app.state.token_diagnostic_queries.reads == []


@pytest.mark.asyncio
async def test_operation_filter_invalidates_primary_cursor_before_read(app):
    from monitor.app.services.token_diagnostics.diagnostic_filters import (
        DiagnosticFilters,
        encode_cursor,
    )

    query = DiagnosticFilters(
        start="2026-10-11T00:00:00Z",
        end="2026-10-12T00:00:00Z",
    )
    cursor = encode_cursor(
        ("s", "alice"),
        query,
        "operations:task",
        query.start,
        "op",
    )
    params = {
        "start": "2026-10-11T00:00:00Z",
        "end": "2026-10-12T00:00:00Z",
        "cursor": cursor,
        "operation_id": "other-op",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            ROOT + "/tasks/task/operations",
            params=params,
            headers=HEADERS,
        )
        assert response.status_code == 400
        assert app.state.token_diagnostic_queries.reads == []


class BlockingService:
    def __init__(self):
        self.started = asyncio.Event()
        self.released = asyncio.Event()

    async def dashboard(self, *args):
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.released.set()


@pytest.mark.asyncio
async def test_api_timeout_returns_unavailable_and_releases_reader(
    app,
    monkeypatch,
):
    from monitor.app.routers import token_diagnostics_v2

    monkeypatch.setattr(
        token_diagnostics_v2,
        "READ_SECONDS",
        0.01,
        raising=False,
    )
    app.state.token_diagnostic_queries = service = BlockingService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
    ) as client:
        response = await asyncio.wait_for(
            client.get(ROOT + "/dashboard", headers=HEADERS),
            0.2,
        )
        assert response.status_code == 503
        assert response.json()["detail"] == token_diagnostics_v2.UNAVAILABLE
        assert service.released.is_set()


@pytest.mark.asyncio
async def test_api_request_cancellation_propagates_and_releases_reader(app):
    app.state.token_diagnostic_queries = service = BlockingService()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
    ) as client:
        task = asyncio.create_task(
            client.get(ROOT + "/dashboard", headers=HEADERS),
        )
        await asyncio.wait_for(service.started.wait(), 0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.released.is_set()

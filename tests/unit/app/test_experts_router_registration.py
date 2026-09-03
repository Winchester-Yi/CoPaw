# -*- coding: utf-8 -*-
"""Regression coverage for the root Experts API route."""

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from swe.app.routers import _build_router
from swe.app.routers.experts import _repository
from swe.app.routers import experts as experts_router


def test_experts_router_is_registered_in_root_api_router() -> None:
    """Console requests use /api/experts with the selected-Agent header."""
    assert any(route.path == "/experts" for route in _build_router().routes)


@pytest.mark.asyncio
async def test_experts_repository_uses_active_agent_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def resolve_active_agent(_request):
        return (
            SimpleNamespace(
                agent_id="active-agent",
                tenant_id="tenant-1",
                workspace_dir=str(tmp_path),
            ),
            SimpleNamespace(),
        )

    agent_context = importlib.import_module("swe.app.agent_context")
    monkeypatch.setattr(
        agent_context,
        "get_agent_and_config_for_request",
        resolve_active_agent,
    )
    request = Request({"type": "http", "headers": []})

    repository = await _repository(request)

    assert (
        repository._root == tmp_path / "agents"
    )  # pylint: disable=protected-access
    assert (
        repository._owner_scope == "tenant-1/active-agent"
    )  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_experts_list_runs_repository_io_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    class _Repository:
        def list(self):
            return []

    async def run_worker(function, *args, **kwargs):
        calls.append(function)
        return function(*args, **kwargs)

    async def repository(_request):
        return _Repository()

    monkeypatch.setattr(experts_router, "_repository", repository)
    monkeypatch.setattr(experts_router, "run_runtime_state_work", run_worker)

    result = await experts_router.list_experts(
        Request({"type": "http", "headers": []}),
    )

    assert result == []
    assert len(calls) == 1

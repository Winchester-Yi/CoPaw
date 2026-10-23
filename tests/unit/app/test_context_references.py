# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_type_hints

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def test_consume_task_outcome_accepts_future_contract() -> None:
    from swe.app.context_references import _consume_task_outcome

    assert get_type_hints(_consume_task_outcome)["task"] == asyncio.Future[Any]


def test_directory_cache_isolates_scopes_and_expires_categories() -> None:
    from swe.app.context_references import ContextReferenceDirectoryCache

    clock = _Clock()
    cache = ContextReferenceDirectoryCache(clock=clock)
    cache.put(("tenant-a", "default"), "skills", ["a"])
    cache.put(("tenant-b", "default"), "skills", ["b"])

    assert cache.get(("tenant-a", "default"), "skills") == ["a"]
    assert cache.get(("tenant-b", "default"), "skills") == ["b"]

    clock.value = 301.0
    assert cache.get(("tenant-a", "default"), "skills") is None
    assert cache.get(("tenant-b", "default"), "skills") is None


def test_directory_cache_uses_fixed_ttls_removes_expired_entries_and_evicts_lru() -> (
    None
):
    from swe.app.context_references import ContextReferenceDirectoryCache

    clock = _Clock()
    cache = ContextReferenceDirectoryCache(capacity=2, clock=clock)
    cache.put(("tenant", "one"), "skills", ["skill"])
    cache.put(("tenant", "one"), "mcp_tools", ["tool"])
    cache.put(("tenant", "two"), "files", ["file"])
    assert cache.get(("tenant", "one"), "skills") == ["skill"]

    cache.put(("tenant", "three"), "skills", ["new"])
    assert cache.get(("tenant", "two"), "files") is None
    assert cache.get(("tenant", "one"), "mcp_tools") == ["tool"]

    clock.value = 181.0
    assert cache.get(("tenant", "one"), "mcp_tools") is None
    assert cache.get(("tenant", "three"), "skills") == ["new"]


def test_directory_cache_removes_all_expired_categories_for_accessed_scope() -> (
    None
):
    from swe.app.context_references import ContextReferenceDirectoryCache

    clock = _Clock()
    cache = ContextReferenceDirectoryCache(clock=clock)
    scope = ("tenant", "default")
    cache.put(scope, "skills", ["skill"])
    cache.put(scope, "mcp_tools", ["tool"])
    clock.value = 181.0

    assert cache.get(scope, "skills") == ["skill"]
    assert cache._entries[scope] == {"skills": cache._entries[scope]["skills"]}


@pytest.mark.asyncio
async def test_directory_cache_never_serves_a_stale_value_after_refresh_failure() -> (
    None
):
    from swe.app.context_references import ContextReferenceDirectoryCache

    clock = _Clock()
    cache = ContextReferenceDirectoryCache(clock=clock)
    scope = ("tenant", "default")
    cache.put(scope, "skills", ["stale"])
    clock.value = 301.0

    async def failed_refresh():
        raise RuntimeError("unavailable")

    with pytest.raises(RuntimeError, match="unavailable"):
        await cache.get_or_refresh(scope, "skills", failed_refresh)
    assert cache.get(scope, "skills") is None


@pytest.mark.asyncio
async def test_directory_cache_refreshes_each_scope_category_once() -> None:
    from swe.app.context_references import ContextReferenceDirectoryCache

    cache = ContextReferenceDirectoryCache()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def refresh():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return ["skill"]

    first = asyncio.create_task(
        cache.get_or_refresh(("tenant", "default"), "skills", refresh),
    )
    await started.wait()
    second = asyncio.create_task(
        cache.get_or_refresh(("tenant", "default"), "skills", refresh),
    )
    release.set()

    assert await first == ["skill"]
    assert await second == ["skill"]
    assert calls == 1


def test_workspace_file_index_limits_each_root_and_matches_filename_only(
    tmp_path: Path,
) -> None:
    from swe.app.context_references import discover_workspace_files

    media = tmp_path / "media"
    static = tmp_path / "static"
    media.mkdir()
    static.mkdir()
    for index in range(5_001):
        file = media / f"media-{index}.txt"
        file.write_text("ignored", encoding="utf-8")
        os.utime(file, (index, index))
    target = static / "nested" / "report.pdf"
    target.parent.mkdir()
    target.write_text("ignored", encoding="utf-8")

    indexed = discover_workspace_files(tmp_path)

    assert len([item for item in indexed if item.root == "media"]) == 5_000
    assert [
        item.relative_path for item in indexed if item.root == "static"
    ] == [
        "nested/report.pdf",
    ]
    assert [
        item.relative_path for item in indexed if item.matches("report")
    ] == [
        "nested/report.pdf",
    ]
    assert not any(item.matches("nested") for item in indexed)


def test_workspace_file_index_uses_configured_media_dir_and_keeps_same_names(
    tmp_path: Path,
) -> None:
    from swe.app.context_references import discover_workspace_files

    media = tmp_path / "media"
    configured_media = tmp_path / "configured-media"
    static = tmp_path / "static"
    media.mkdir()
    configured_media.mkdir()
    static.mkdir()
    (media / "report.txt").write_text("media")
    (configured_media / "outside.txt").write_text("outside")
    (static / "report.txt").write_text("static")

    indexed = discover_workspace_files(tmp_path, media_dir=configured_media)

    assert [(item.root, item.relative_path, item.id) for item in indexed] == [
        ("media", "report.txt", "workspace_file:media/report.txt"),
        ("static", "report.txt", "workspace_file:static/report.txt"),
    ]


def test_workspace_file_index_rejects_symlinked_roots_outside_workspace(
    tmp_path: Path,
) -> None:
    from swe.app.context_references import discover_workspace_files

    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not a context reference")
    (tmp_path / "media").symlink_to(outside, target_is_directory=True)
    (tmp_path / "static").symlink_to(outside, target_is_directory=True)

    assert discover_workspace_files(tmp_path) == []


@pytest.mark.asyncio
async def test_mcp_reference_ids_keep_slash_containing_names_distinct() -> (
    None
):
    from swe.app.context_references import discover_mcp_tools

    class Client:
        def __init__(self, tool_name: str) -> None:
            self._tool_name = tool_name

        async def list_tools(self):
            return [SimpleNamespace(name=self._tool_name, description="")]

    class Manager:
        def get_client(self, key):
            return Client({"a/b": "c", "a": "b/c"}[key])

    references = await discover_mcp_tools(
        manager=Manager(),
        agent_config=SimpleNamespace(
            mcp=SimpleNamespace(
                clients={
                    "a/b": SimpleNamespace(enabled=True),
                    "a": SimpleNamespace(enabled=True),
                },
            ),
        ),
    )

    assert len({item.id for item in references}) == 2


@pytest.mark.asyncio
async def test_mcp_discovery_keeps_successes_and_omits_failures_and_timeouts() -> (
    None
):
    from swe.app.context_references import discover_mcp_tools

    class HealthyClient:
        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(name="search", description="Find docs"),
                ],
            )

    class FailedClient:
        async def list_tools(self):
            raise RuntimeError("unavailable")

    class SlowClient:
        async def list_tools(self):
            await asyncio.sleep(1)

    config = SimpleNamespace(
        mcp=SimpleNamespace(
            clients={
                "healthy": SimpleNamespace(enabled=True),
                "failed": SimpleNamespace(enabled=True),
                "slow": SimpleNamespace(enabled=True),
                "disabled": SimpleNamespace(enabled=False),
            },
        ),
    )

    class Manager:
        async def get_client(self, key):
            return {
                "healthy": HealthyClient(),
                "failed": FailedClient(),
                "slow": SlowClient(),
            }.get(key)

    tools = await discover_mcp_tools(
        manager=Manager(),
        agent_config=config,
        per_client_timeout=0.01,
        overall_timeout=0.02,
    )

    assert [(item.server, item.name, item.description) for item in tools] == [
        ("healthy", "search", "Find docs"),
    ]


@pytest.mark.asyncio
async def test_mcp_discovery_returns_at_deadline_when_client_ignores_cancellation() -> (
    None
):
    from swe.app.context_references import discover_mcp_tools

    started = asyncio.Event()
    release = asyncio.Event()

    class UncooperativeClient:
        async def list_tools(self):
            started.set()
            while True:
                try:
                    await release.wait()
                    return []
                except asyncio.CancelledError:
                    continue

    class Manager:
        async def get_client(self, _key):
            return UncooperativeClient()

    started_at = asyncio.get_running_loop().time()
    discovery = asyncio.create_task(
        discover_mcp_tools(
            manager=Manager(),
            agent_config=SimpleNamespace(
                mcp=SimpleNamespace(
                    clients={"slow": SimpleNamespace(enabled=True)},
                ),
            ),
            per_client_timeout=0.01,
            overall_timeout=0.02,
        ),
    )
    await started.wait()

    assert await asyncio.wait_for(discovery, timeout=0.2) == []
    assert asyncio.get_running_loop().time() - started_at < 0.2
    release.set()


@pytest.mark.asyncio
async def test_overall_timeout_cancels_and_consumes_provider_operation() -> (
    None
):
    from swe.app.context_references import discover_mcp_tools

    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    class Provider:
        async def get_context_reference_mcp_client(self, _key, _config):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return None

        async def release_context_reference_mcp_client(self, _client):
            raise AssertionError("a client was never created")

    discovery = asyncio.create_task(
        discover_mcp_tools(
            manager=Provider(),
            agent_config=SimpleNamespace(
                mcp=SimpleNamespace(
                    clients={"slow": SimpleNamespace(enabled=True)},
                ),
            ),
            per_client_timeout=1,
            overall_timeout=0.01,
        ),
    )
    await started.wait()

    assert await asyncio.wait_for(discovery, timeout=0.2) == []
    await asyncio.wait_for(cancelled.wait(), timeout=0.1)
    release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_provider_closes_a_client_returned_after_cancellation(
    monkeypatch,
) -> None:
    """A cancellation-uncooperative connect must not leave a client alive."""
    from swe.app.context_references import _AgentRunnerMCPClientProvider
    from swe.app.runner import runner as runner_module

    started = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()

    class LateClient:
        async def close(self) -> None:
            closed.set()

    async def build_clients(_config):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return [LateClient()]

    async def cleanup_clients(clients):
        for client in clients:
            await client.close()

    monkeypatch.setattr(
        runner_module,
        "_build_and_connect_mcp_clients",
        build_clients,
    )
    monkeypatch.setattr(runner_module, "_cleanup_mcp_clients", cleanup_clients)

    provider_task = asyncio.create_task(
        _AgentRunnerMCPClientProvider().get_context_reference_mcp_client(
            "late",
            SimpleNamespace(enabled=True),
        ),
    )
    await started.wait()
    provider_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await provider_task

    release.set()
    await asyncio.wait_for(closed.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_directory_uses_the_workspace_runner_for_active_enabled_mcp_clients(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from swe.app.context_references import ContextReferenceDirectory
    from swe.app.workspace.workspace import Workspace

    monkeypatch.setattr(
        "swe.app.context_references.resolve_effective_skills",
        lambda _workspace, _channel: [],
    )
    calls: list[str] = []
    released: list[object] = []

    async def _list_tools_response():
        return [SimpleNamespace(name="search", description="Find docs")]

    class ActiveClient:
        async def connect(self, *, timeout):
            assert timeout > 0

        async def list_tools(self):
            return await _list_tools_response()

        async def close(self):
            released.append(self)

    from swe.app.runner import runner as runner_module

    async def build_clients(config, **_kwargs):
        calls.extend(config.clients)
        return [ActiveClient()]

    async def cleanup_clients(clients):
        for client in clients:
            await client.close()

    monkeypatch.setattr(
        runner_module,
        "_build_and_connect_mcp_clients",
        build_clients,
    )
    monkeypatch.setattr(runner_module, "_cleanup_mcp_clients", cleanup_clients)

    workspace = Workspace(
        agent_id="default",
        workspace_dir=str(tmp_path),
        tenant_id="tenant-a",
    )
    response = await ContextReferenceDirectory().discover(
        workspace=workspace,
        agent_config=SimpleNamespace(
            mcp=SimpleNamespace(
                clients={
                    "enabled": SimpleNamespace(enabled=True, name="enabled"),
                    "disabled": SimpleNamespace(
                        enabled=False,
                        name="disabled",
                    ),
                },
            ),
        ),
    )

    assert calls == ["enabled"]
    assert [(item.server, item.name) for item in response.mcp_tools] == [
        ("enabled", "search"),
    ]
    await asyncio.sleep(0)
    assert len(released) == 1


def test_context_references_endpoint_groups_and_limits_discovery_results(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from swe.agents import skill_runtime_snapshot
    from swe.app.routers import console as console_router

    app = FastAPI()
    app.include_router(console_router.router)
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        tenant_id="tenant-a",
        agent_id="default",
        mcp_manager=SimpleNamespace(
            get_client=lambda _key: None,
        ),
    )

    async def get_workspace(_request):
        return workspace

    async def get_workspace_and_config(_request):
        return workspace, SimpleNamespace(mcp=None)

    monkeypatch.setattr(console_router, "get_agent_for_request", get_workspace)
    monkeypatch.setattr(
        console_router,
        "get_agent_and_config_for_request",
        get_workspace_and_config,
        raising=False,
    )

    async def get_snapshot(_workspace_dir: Path):
        return SimpleNamespace(
            skills={
                f"skill-{index}": SimpleNamespace(
                    channels={"console"},
                    metadata={"description": f"Description {index}"},
                )
                for index in range(5)
            },
        )

    monkeypatch.setattr(
        skill_runtime_snapshot,
        "get_workspace_skill_snapshot_async",
        get_snapshot,
    )
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "report.txt").write_text("ignored")

    client = TestClient(app)
    response = client.get("/console/context-references?q=report")

    assert response.status_code == 200
    assert response.json() == {
        "skills": [],
        "mcp_tools": [],
        "files": [
            {
                "type": "workspace_file",
                "id": "workspace_file:media/report.txt",
                "root": "media",
                "relative_path": "report.txt",
                "label": "report.txt",
                "description": "media/report.txt",
            },
        ],
    }

    empty_query_response = client.get("/console/context-references")
    assert [
        item["name"] for item in empty_query_response.json()["skills"]
    ] == [f"skill-{index}" for index in range(5)]

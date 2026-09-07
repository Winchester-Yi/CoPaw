# -*- coding: utf-8 -*-
"""Scoped discovery of Skills, callable MCP tools, and workspace files."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Literal, TypeVar

from pydantic import BaseModel, Field

from ..agents.skills_manager import (
    _read_skill_from_dir,
    resolve_effective_skills,
    resolve_workspace_managed_skill_dir,
)

logger = logging.getLogger(__name__)

SKILLS_TTL_SECONDS = 300.0
MCP_AND_FILES_TTL_SECONDS = 180.0
DIRECTORY_CACHE_CAPACITY = 128
MAX_FILES_PER_ROOT = 5_000
MAX_RESULTS_PER_GROUP = 4
MCP_CLIENT_TIMEOUT_SECONDS = 2.0
MCP_DISCOVERY_TIMEOUT_SECONDS = 3.0

ScopeKey = tuple[str | None, str]
Category = Literal["skills", "mcp_tools", "files"]
T = TypeVar("T")


class SkillContextReference(BaseModel):
    type: Literal["skill"] = "skill"
    id: str
    name: str
    label: str
    description: str = ""


class MCPToolContextReference(BaseModel):
    type: Literal["mcp_tool"] = "mcp_tool"
    id: str
    server: str
    name: str
    label: str
    description: str = ""


class WorkspaceFileContextReference(BaseModel):
    type: Literal["workspace_file"] = "workspace_file"
    id: str
    root: Literal["media", "static"]
    relative_path: str
    label: str
    description: str

    def matches(self, query: str) -> bool:
        return query.casefold() in self.label.casefold()


class ContextReferencesResponse(BaseModel):
    skills: list[SkillContextReference] = Field(default_factory=list)
    mcp_tools: list[MCPToolContextReference] = Field(default_factory=list)
    files: list[WorkspaceFileContextReference] = Field(default_factory=list)


@dataclass
class _CachedValue:
    value: Any
    expires_at: float


class ContextReferenceDirectoryCache:
    """Process-local, scope-keyed LRU cache with per-category TTLs."""

    _TTLS: dict[Category, float] = {
        "skills": SKILLS_TTL_SECONDS,
        "mcp_tools": MCP_AND_FILES_TTL_SECONDS,
        "files": MCP_AND_FILES_TTL_SECONDS,
    }

    def __init__(
        self,
        capacity: int = DIRECTORY_CACHE_CAPACITY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capacity = capacity
        self._clock = clock
        self._entries: OrderedDict[ScopeKey, dict[Category, _CachedValue]] = (
            OrderedDict()
        )
        self._refreshes: dict[tuple[ScopeKey, Category], asyncio.Task[Any]] = (
            {}
        )

    @staticmethod
    async def _run_refresh(refresh: Callable[[], Awaitable[T]]) -> T:
        return await refresh()

    def get(self, scope: ScopeKey, category: Category) -> Any | None:
        entries = self._entries.get(scope)
        if entries is None:
            return None
        self._entries.move_to_end(scope)
        now = self._clock()
        for expired_category in [
            name
            for name, cached in entries.items()
            if cached.expires_at <= now
        ]:
            del entries[expired_category]
        if not entries:
            del self._entries[scope]
            return None
        entry = entries.get(category)
        if entry is None:
            return None
        return entry.value

    def put(self, scope: ScopeKey, category: Category, value: Any) -> None:
        entries = self._entries.get(scope)
        if entries is None:
            entries = {}
            self._entries[scope] = entries
        self._entries.move_to_end(scope)
        entries[category] = _CachedValue(
            value=value,
            expires_at=self._clock() + self._TTLS[category],
        )
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)

    async def get_or_refresh(
        self,
        scope: ScopeKey,
        category: Category,
        refresh: Callable[[], Awaitable[T]],
    ) -> T:
        cached = self.get(scope, category)
        if cached is not None:
            return cached
        refresh_key = (scope, category)
        task = self._refreshes.get(refresh_key)
        if task is None:
            task = asyncio.create_task(self._run_refresh(refresh))
            self._refreshes[refresh_key] = task
        try:
            result = await asyncio.shield(task)
        finally:
            if task.done():
                self._refreshes.pop(refresh_key, None)
        if self._refreshes.get(refresh_key) is task:
            self._refreshes.pop(refresh_key, None)
        self.put(scope, category, result)
        return result


def discover_skills(workspace_dir: Path) -> list[SkillContextReference]:
    """Return metadata for skills enabled in the Console channel."""
    references: list[SkillContextReference] = []
    for name in resolve_effective_skills(workspace_dir, "console"):
        skill = _read_skill_from_dir(
            resolve_workspace_managed_skill_dir(
                workspace_dir,
                name,
                enabled=True,
            ),
            "customized",
        )
        if skill is None:
            continue
        references.append(
            SkillContextReference(
                id=f"skill:{name}",
                name=name,
                label=name,
                description=skill.description,
            ),
        )
    return references


def _workspace_files_for_root(
    workspace_dir: Path,
    root_name: Literal["media", "static"],
    root: Path | None = None,
) -> list[WorkspaceFileContextReference]:
    root = root or workspace_dir / root_name
    if not root.is_dir():
        return []
    resolved_root = root.resolve()
    try:
        resolved_root.relative_to(workspace_dir.resolve())
    except (OSError, ValueError):
        # Do not let a workspace directory symlink expose files outside the
        # active agent workspace.
        return []
    candidates: list[tuple[float, Path]] = []
    for path in root.rglob("*"):
        try:
            resolved_path = path.resolve()
            resolved_path.relative_to(resolved_root)
            if not resolved_path.is_file():
                continue
            candidates.append((resolved_path.stat().st_mtime, resolved_path))
        except OSError:
            continue
        except ValueError:
            continue
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [
        WorkspaceFileContextReference(
            id=f"workspace_file:{root_name}/{path.relative_to(resolved_root).as_posix()}",
            root=root_name,
            relative_path=path.relative_to(resolved_root).as_posix(),
            label=path.name,
            description=f"{root_name}/{path.relative_to(resolved_root).as_posix()}",
        )
        for _, path in candidates[:MAX_FILES_PER_ROOT]
    ]


def discover_workspace_files(
    workspace_dir: Path,
    media_dir: Path | None = None,
) -> list[WorkspaceFileContextReference]:
    """Index metadata only; no workspace file content is read."""
    workspace_media_dir = workspace_dir / "media"
    if media_dir is not None:
        try:
            # Console may be configured to store uploads elsewhere, but a
            # context reference is deliberately limited to this workspace.
            if media_dir.resolve() != workspace_media_dir.resolve():
                media_dir = workspace_media_dir
        except OSError:
            media_dir = workspace_media_dir
    return [
        *_workspace_files_for_root(workspace_dir, "media", media_dir),
        *_workspace_files_for_root(workspace_dir, "static"),
    ]


async def _list_client_tools(
    server: str,
    client: Any,
    timeout: float,
) -> list[MCPToolContextReference]:
    result = await _await_with_hard_deadline(client.list_tools(), timeout)
    tools = getattr(result, "tools", result) or []
    return [
        MCPToolContextReference(
            id=build_mcp_tool_reference_id(
                server,
                str(getattr(tool, "name", "")),
            ),
            server=server,
            name=str(getattr(tool, "name", "")),
            label=f"{server} / {getattr(tool, 'name', '')}",
            description=str(getattr(tool, "description", "") or ""),
        )
        for tool in tools
        if getattr(tool, "name", "")
    ]


def build_mcp_tool_reference_id(server: str, name: str) -> str:
    """Encode the two independently user-controlled identity parts safely."""
    return "mcp_tool:" + json.dumps(
        [server, name],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _consume_task_outcome(task: asyncio.Future[Any]) -> None:
    """Retrieve a background task outcome without delaying the response."""

    def consume(completed: asyncio.Future[Any]) -> None:
        try:
            completed.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Background MCP discovery task failed", exc_info=True)

    task.add_done_callback(consume)


async def _await_with_hard_deadline(
    awaitable: Awaitable[T],
    timeout: float,
) -> T:
    """Bound response latency even if the underlying client ignores cancel."""
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except asyncio.CancelledError:
        task.cancel()
        _consume_task_outcome(task)
        raise
    except asyncio.TimeoutError:
        task.cancel()
        _consume_task_outcome(task)
        raise


async def discover_mcp_tools(
    *,
    manager: Any | None,
    agent_config: Any,
    per_client_timeout: float = MCP_CLIENT_TIMEOUT_SECONDS,
    overall_timeout: float = MCP_DISCOVERY_TIMEOUT_SECONDS,
) -> list[MCPToolContextReference]:
    """Discover callable tools from current enabled, already-active clients."""
    if manager is None:
        return []
    configured_clients = (
        getattr(getattr(agent_config, "mcp", None), "clients", {}) or {}
    )

    async def discover_one(
        key: str,
        config: Any,
    ) -> list[MCPToolContextReference]:
        client = None
        try:
            get_client = getattr(
                manager,
                "get_context_reference_mcp_client",
                None,
            )
            if get_client is None:
                get_client = manager.get_client
                client = get_client(key)
            else:
                client = get_client(key, config)
            if inspect.isawaitable(client):
                client = await _await_with_hard_deadline(
                    client,
                    per_client_timeout,
                )
            if client is None:
                return []
            return await _list_client_tools(key, client, per_client_timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "MCP tool discovery failed for %s",
                key,
                exc_info=True,
            )
            return []
        finally:
            release_client = getattr(
                manager,
                "release_context_reference_mcp_client",
                None,
            )
            if client is not None and release_client is not None:
                release = release_client(client)
                if inspect.isawaitable(release):
                    release_task = asyncio.ensure_future(release)
                    _consume_task_outcome(release_task)

    tasks = [
        asyncio.create_task(discover_one(key, config))
        for key, config in configured_clients.items()
        if getattr(config, "enabled", False)
    ]
    if not tasks:
        return []
    done, pending = await asyncio.wait(tasks, timeout=overall_timeout)
    for task in pending:
        task.cancel()
        _consume_task_outcome(task)
    results: list[MCPToolContextReference] = []
    for task in tasks:
        if task in done and not task.cancelled():
            try:
                results.extend(task.result())
            except Exception:
                logger.debug("MCP discovery task failed", exc_info=True)
    return results


class _AgentRunnerMCPClientProvider:
    """Use the same short-lived MCP lifecycle as an AgentRunner request."""

    async def get_context_reference_mcp_client(
        self,
        key: str,
        config: Any,
    ) -> Any | None:
        from .runner.runner import (
            _build_and_connect_mcp_clients,
            _cleanup_mcp_clients,
        )

        build_task = asyncio.create_task(
            _build_and_connect_mcp_clients(
                SimpleNamespace(clients={key: config}),
            ),
        )
        try:
            clients = await asyncio.shield(build_task)
        except asyncio.CancelledError:

            def cleanup_late_clients(task: asyncio.Task[list[Any]]) -> None:
                try:
                    clients = task.result()
                except asyncio.CancelledError:
                    return
                except Exception:
                    logger.debug(
                        "Late MCP client creation failed",
                        exc_info=True,
                    )
                    return
                cleanup_task = asyncio.create_task(
                    _cleanup_mcp_clients(clients),
                )
                _consume_task_outcome(cleanup_task)

            build_task.add_done_callback(cleanup_late_clients)
            raise
        current_task = asyncio.current_task()
        if current_task is not None and current_task.cancelling():
            await _cleanup_mcp_clients(clients)
            raise asyncio.CancelledError
        return clients[0] if clients else None

    async def release_context_reference_mcp_client(self, client: Any) -> None:
        from .runner.runner import _cleanup_mcp_clients

        await _cleanup_mcp_clients([client])


class ContextReferenceDirectory:
    """Cached context-reference discovery service for Console requests."""

    def __init__(
        self,
        cache: ContextReferenceDirectoryCache | None = None,
    ) -> None:
        self._cache = cache or ContextReferenceDirectoryCache()

    async def discover(
        self,
        workspace: Any,
        agent_config: Any,
        query: str = "",
        media_dir: Path | None = None,
    ) -> ContextReferencesResponse:
        workspace_dir = Path(workspace.workspace_dir)
        scope = (getattr(workspace, "tenant_id", None), workspace.agent_id)

        async def refresh_skills() -> list[SkillContextReference]:
            from .agents.skill_runtime_snapshot import (
                get_workspace_skill_snapshot_async,
            )

            snapshot = await get_workspace_skill_snapshot_async(workspace_dir)
            return [
                SkillContextReference(
                    id=f"skill:{name}",
                    name=name,
                    label=name,
                    description=str(
                        skill.metadata.get("description") or "",
                    ),
                )
                for name, skill in snapshot.skills.items()
                if "all" in skill.channels or "console" in skill.channels
            ]

        async def refresh_mcp() -> list[MCPToolContextReference]:
            return await discover_mcp_tools(
                manager=_AgentRunnerMCPClientProvider(),
                agent_config=agent_config,
            )

        async def refresh_files() -> list[WorkspaceFileContextReference]:
            return discover_workspace_files(workspace_dir, media_dir=media_dir)

        async def cached(
            category: Category,
            refresh: Callable[[], Awaitable[list[T]]],
        ) -> list[T]:
            try:
                return await self._cache.get_or_refresh(
                    scope,
                    category,
                    refresh,
                )
            except Exception:
                logger.warning(
                    "Context reference refresh failed for %s",
                    category,
                    exc_info=True,
                )
                return []

        normalized_query = query.strip()
        skills, mcp_tools = await asyncio.gather(
            cached("skills", refresh_skills),
            cached("mcp_tools", refresh_mcp),
        )
        files: list[WorkspaceFileContextReference] = []
        if normalized_query:
            files = await cached("files", refresh_files)
        query_folded = normalized_query.casefold()

        def matches(item: Any) -> bool:
            if not query_folded:
                return True
            return query_folded in item.label.casefold()

        def limit_results(items: list[T]) -> list[T]:
            return items if not query_folded else items[:MAX_RESULTS_PER_GROUP]

        return ContextReferencesResponse(
            skills=limit_results([item for item in skills if matches(item)]),
            mcp_tools=[item for item in mcp_tools if matches(item)][
                :MAX_RESULTS_PER_GROUP
            ],
            files=[item for item in files if item.matches(normalized_query)][
                :MAX_RESULTS_PER_GROUP
            ],
        )


context_reference_directory = ContextReferenceDirectory()

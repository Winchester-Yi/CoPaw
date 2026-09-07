# -*- coding: utf-8 -*-
"""Submit-time, non-sensitive snapshot construction for scenario chats."""

from __future__ import annotations

import asyncio
import logging
import os
import httpx
import json
from copy import deepcopy
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

from .service import ScenarioPresetCatalogService

logger = logging.getLogger(__name__)

_SNAPSHOT_META_KEY = "scenario_preset_snapshot"


async def initialize_scenario_snapshot(
    *,
    service: ScenarioPresetCatalogService,
    source_id: str,
    scenario_id: str,
    agent_id: str | None,
    workspace_dir: Path | None = None,
    agent_config: Any | None = None,
    bbk_id: str | None = None,
    market_client: Any | None = None,
    mcp_tool_discoverer: (
        Callable[[str, dict[str, Any]], Awaitable[list[dict[str, Any]]]] | None
    ) = None,
    session_resource_root: Path | None = None,
) -> dict[str, Any]:
    """Validate current catalog state and create the immutable safe snapshot.

    Only the first submit resolves market resources. Persistent resources are
    preferred; missing resources are staged below the Chat-private directory.
    The metadata snapshot carries IDs, versions, paths and frozen tool schemas,
    never packaged content, MCP configuration, or credentials.
    """
    scenario, bindings, capability = await service.get_submittable_scenario(
        source_id,
        scenario_id,
    )
    skill_names = await asyncio.to_thread(
        _resolve_local_skill_names,
        workspace_dir,
        bindings,
    )
    resources: list[dict[str, Any]] = [
        _resource_snapshot(binding, skill_names, agent_config)
        for binding in bindings
    ]
    await _freeze_persistent_mcp_tools(
        resources=resources,
        agent_config=agent_config,
        mcp_tool_discoverer=mcp_tool_discoverer,
    )
    await _resolve_temporary_skill_resources(
        resources=resources,
        source_id=source_id,
        bbk_id=bbk_id,
        market_client=market_client,
        session_resource_root=session_resource_root,
    )
    await _resolve_temporary_mcp_resources(
        resources=resources,
        source_id=source_id,
        bbk_id=bbk_id,
        market_client=market_client,
        mcp_tool_discoverer=mcp_tool_discoverer,
        session_resource_root=session_resource_root,
    )
    snapshot: dict[str, Any] = {
        "scenario_id": scenario.id,
        "capability_id": capability.id,
        "capability_name": capability.name,
        "agent_id": agent_id,
        "resources": resources,
    }
    logger.info(
        "scenario_preset_initialized source_id=%s scenario_id=%s agent_id=%s resource_outcomes=%s",
        source_id,
        scenario.id,
        agent_id,
        [
            {
                "id": item["id"],
                "type": item["type"],
                "status": item["status"],
            }
            for item in resources
        ],
    )
    return snapshot


async def _resolve_temporary_skill_resources(
    *,
    resources: list[dict[str, Any]],
    source_id: str,
    bbk_id: str | None,
    market_client: Any | None,
    session_resource_root: Path | None,
) -> None:
    if session_resource_root is None:
        return
    client = market_client or HttpMarketScenarioResourceClient()
    for resource in resources:
        if (
            resource.get("type") != "skill"
            or resource.get("status") != "unresolved"
        ):
            continue
        try:
            detail = await client.get_skill_detail(
                source_id=source_id,
                item_id=resource["id"],
                bbk_id=bbk_id,
            )
            payload = await client.download_skill(
                source_id=source_id,
                item_id=resource["id"],
                bbk_id=bbk_id,
            )
            from .resources import stage_temporary_skill_zip

            skill_name, skill_path = stage_temporary_skill_zip(
                payload,
                resource_id=resource["id"],
                session_root=session_resource_root,
            )
            resource.update(
                {
                    "status": "temporary",
                    "version": str(detail.get("version") or ""),
                    "skill_name": skill_name,
                    "skill_path": str(skill_path),
                },
            )
        except Exception as exc:
            resource.update(
                {
                    "status": "unavailable",
                    "unavailable_reason": type(exc).__name__,
                },
            )
            logger.warning(
                "scenario_skill_resolution_failed resource_id=%s error_type=%s",
                resource.get("id"),
                type(exc).__name__,
            )


async def _freeze_persistent_mcp_tools(
    *,
    resources: list[dict[str, Any]],
    agent_config: Any | None,
    mcp_tool_discoverer: (
        Callable[[str, dict[str, Any]], Awaitable[list[dict[str, Any]]]] | None
    ),
) -> None:
    for resource in resources:
        if resource.get("type") != "mcp_service":
            continue
        if resource.get("status") != "persistent":
            continue
        client = _resolve_local_mcp_client(agent_config, resource["id"])
        if client is None:
            continue
        key, config = client
        try:
            if mcp_tool_discoverer is not None:
                tools = await mcp_tool_discoverer(
                    resource["id"],
                    config.model_dump(mode="json"),
                )
            else:
                tools = await _discover_persistent_mcp_tools(key, config)
            resource["tools"] = tools
        except Exception as exc:
            resource["tools"] = []
            resource.update(
                {
                    "status": "unavailable",
                    "unavailable_reason": type(exc).__name__,
                },
            )
            logger.warning(
                "scenario_persistent_mcp_discovery_failed resource_id=%s "
                "error_type=%s",
                resource.get("id"),
                type(exc).__name__,
            )


async def _resolve_temporary_mcp_resources(
    *,
    resources: list[dict[str, Any]],
    source_id: str,
    bbk_id: str | None,
    market_client: Any | None,
    mcp_tool_discoverer: (
        Callable[[str, dict[str, Any]], Awaitable[list[dict[str, Any]]]] | None
    ),
    session_resource_root: Path | None,
) -> None:
    if session_resource_root is None:
        return
    client = market_client or HttpMarketScenarioResourceClient()
    for resource in resources:
        if resource.get("type") != "mcp_service":
            continue
        if resource.get("status") == "persistent":
            continue
        try:
            detail = await client.get_mcp_detail(
                source_id=source_id,
                item_id=resource["id"],
                bbk_id=bbk_id,
            )
            if not isinstance(detail, dict):
                resource.update(
                    {
                        "status": "unavailable",
                        "unavailable_reason": "invalid_market_detail",
                    },
                )
                continue
            from .resources import (
                resolve_temporary_mcp_config,
                sanitize_mcp_config,
                stage_temporary_mcp_config,
            )

            config = sanitize_mcp_config(detail.get("config"))
            resolved_config = resolve_temporary_mcp_config(config)
            if resolved_config is None:
                resource.update(
                    {
                        "status": "unavailable",
                        "unavailable_reason": "tenant_credentials_unavailable",
                    },
                )
                continue
            discover = mcp_tool_discoverer or _discover_snapshot_mcp_tools
            tools = await discover(resource["id"], resolved_config)
            config_path = stage_temporary_mcp_config(
                config,
                resource_id=resource["id"],
                session_root=session_resource_root,
            )
            resource.update(
                {
                    "status": "temporary",
                    "version": str(detail.get("version") or ""),
                    "mcp_client_key": str(
                        detail.get("client_key") or resource["id"],
                    ),
                    "mcp_config_path": str(config_path),
                    "tools": tools,
                },
            )
        except Exception as exc:
            resource.update(
                {
                    "status": "unavailable",
                    "unavailable_reason": type(exc).__name__,
                },
            )
            logger.warning(
                "scenario_mcp_resolution_failed resource_id=%s error_type=%s",
                resource.get("id"),
                type(exc).__name__,
            )


async def _discover_snapshot_mcp_tools(
    resource_id: str,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    from ...config.config import MCPClientConfig, MCPConfig
    from ..runner.runner import _build_lazy_mcp_clients

    client_config = MCPClientConfig(
        name=f"scenario:{resource_id}",
        source=f"marketplace:{resource_id}",
        market_client_key=resource_id,
        **config,
    )
    lazy_clients = _build_lazy_mcp_clients(
        MCPConfig(clients={resource_id: client_config}),
        tenant_id=None,
        user_id=None,
    )
    if not lazy_clients:
        return []
    try:
        tools = await lazy_clients[0].list_tools()
        return _serialize_mcp_tools(tools)
    finally:
        await lazy_clients[0].close()


async def _discover_persistent_mcp_tools(
    client_key: str,
    client_config: Any,
) -> list[dict[str, Any]]:
    from ...config.config import MCPConfig
    from ..runner.runner import _build_lazy_mcp_clients

    clients = _build_lazy_mcp_clients(
        MCPConfig(clients={client_key: client_config}),
        tenant_id=None,
        user_id=None,
    )
    if not clients:
        return []
    try:
        return _serialize_mcp_tools(await clients[0].list_tools())
    finally:
        await clients[0].close()


def _serialize_mcp_tools(tools: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": str(getattr(tool, "name", "")),
            "description": str(getattr(tool, "description", "") or ""),
            "inputSchema": getattr(tool, "inputSchema", {}) or {},
        }
        for tool in tools
        if getattr(tool, "name", None)
    ]


class HttpMarketScenarioResourceClient:
    """Minimal market reader used only at first-message snapshot time."""

    def __init__(self, base_url: str | None = None, timeout: float = 10.0):
        self.base_url = (
            base_url
            or os.getenv("SWE_MARKET_API_BASE_URL")
            or os.getenv("MARKET_API_BASE_URL")
            or "http://127.0.0.1:8091/api"
        ).rstrip("/")
        self.timeout = timeout

    async def get_mcp_detail(
        self,
        *,
        source_id: str,
        item_id: str,
        bbk_id: str | None,
    ) -> dict[str, Any]:
        headers = {"X-Source-Id": source_id}
        if bbk_id:
            headers["X-Bbk-Id"] = bbk_id
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.base_url}/market/mcp/{item_id}",
                headers=headers,
            )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    async def get_skill_detail(
        self,
        *,
        source_id: str,
        item_id: str,
        bbk_id: str | None,
    ) -> dict[str, Any]:
        return await self._get_json(
            f"/market/skills/{item_id}",
            source_id,
            bbk_id,
        )

    async def download_skill(
        self,
        *,
        source_id: str,
        item_id: str,
        bbk_id: str | None,
    ) -> bytes:
        headers = _market_headers(source_id, bbk_id)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.base_url}/market/skills/{item_id}/download",
                headers=headers,
            )
        response.raise_for_status()
        return response.content

    async def _get_json(
        self,
        path: str,
        source_id: str,
        bbk_id: str | None,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.base_url}{path}",
                headers=_market_headers(source_id, bbk_id),
            )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}


def _market_headers(source_id: str, bbk_id: str | None) -> dict[str, str]:
    headers = {"X-Source-Id": source_id}
    if bbk_id:
        headers["X-Bbk-Id"] = bbk_id
    return headers


def _resolve_local_skill_names(
    workspace_dir: Path | None,
    bindings: list[Any],
) -> dict[str, str]:
    if workspace_dir is None:
        return {}
    from ..runner.skill_selection import resolve_scenario_skill_names

    resolved: dict[str, str] = {}
    for binding in bindings:
        if binding.resource_type.value != "skill":
            continue
        names = resolve_scenario_skill_names(
            workspace_dir=workspace_dir,
            channel="console",
            resource_ids=[binding.resource_id],
        )
        if names:
            resolved[binding.resource_id] = names[0]
    return resolved


def _resource_snapshot(
    binding: Any,
    skill_names: dict[str, str],
    agent_config: Any | None,
) -> dict[str, Any]:
    resource = {
        "id": binding.resource_id,
        "type": binding.resource_type.value,
        "status": "unresolved",
    }
    if binding.resource_type.value == "skill":
        matching_name = skill_names.get(binding.resource_id)
        if matching_name is not None:
            resource.update(
                {"status": "persistent", "skill_name": matching_name},
            )
    elif binding.resource_type.value == "mcp_service":
        matching_key = _resolve_local_mcp_client_key(
            agent_config,
            binding.resource_id,
        )
        if matching_key is not None:
            resource.update(
                {
                    "status": "persistent",
                    "mcp_client_key": matching_key,
                },
            )
    return resource


def _resolve_local_mcp_client_key(
    agent_config: Any | None,
    resource_id: str,
) -> str | None:
    resolved = _resolve_local_mcp_client(agent_config, resource_id)
    if resolved is None:
        return None
    key, client = resolved
    market_key = str(getattr(client, "market_client_key", "") or "").strip()
    return market_key or key


def _resolve_local_mcp_client(
    agent_config: Any | None,
    resource_id: str,
) -> tuple[str, Any] | None:
    clients = getattr(getattr(agent_config, "mcp", None), "clients", None)
    if not isinstance(clients, dict):
        return None
    expected_source = f"marketplace:{resource_id}"
    for key, client in clients.items():
        if (
            getattr(client, "enabled", False)
            and getattr(
                client,
                "source",
                "",
            )
            == expected_source
        ):
            return str(key), client
    return None


def get_scenario_snapshot(
    meta: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Read a previously initialized snapshot without re-querying catalog data."""
    snapshot = (meta or {}).get(_SNAPSHOT_META_KEY)
    return deepcopy(snapshot) if isinstance(snapshot, dict) else None


def with_scenario_snapshot(
    meta: dict[str, Any] | None,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Return a ChatSpec metadata merge that preserves unrelated fields."""
    return {**(meta or {}), _SNAPSHOT_META_KEY: snapshot}


def scenario_snapshot_skill_names(
    snapshot: dict[str, Any] | None,
) -> list[str]:
    """Return only validated skill names captured in an immutable chat snapshot."""
    result: list[str] = []
    for resource in (snapshot or {}).get("resources", []):
        if not isinstance(resource, dict) or resource.get("type") != "skill":
            continue
        name = resource.get("skill_name")
        if isinstance(name, str) and name.strip():
            result.append(name.strip())
    return result


def scenario_snapshot_skill_directives(
    snapshot: dict[str, Any] | None,
    *,
    workspace_dir: Path,
    chat_id: str,
) -> list[Any]:
    """Build trusted Skill directives for staged, Chat-private packages."""
    from ..runner.skill_selection import SkillUseDirective
    import frontmatter

    session_root = _chat_session_root(workspace_dir, chat_id)
    directives: list[Any] = []
    for resource in (snapshot or {}).get("resources", []):
        if not isinstance(resource, dict) or resource.get("type") != "skill":
            continue
        if resource.get("status") != "temporary":
            continue
        name = resource.get("skill_name")
        raw_path = resource.get("skill_path")
        if not isinstance(name, str) or not isinstance(raw_path, str):
            continue
        try:
            path = Path(raw_path)
            _reject_symlink_path(path, session_root)
            path = path.resolve()
            path.relative_to(session_root)
            content = path.read_text(encoding="utf-8")
        except (OSError, ValueError, UnicodeError):
            _mark_resource_unavailable(resource, "skill_path_unavailable")
            continue
        try:
            description = str(
                frontmatter.loads(content).get("description") or "",
            )
        except (ValueError, TypeError):
            description = ""
        directives.append(
            SkillUseDirective(
                name=name.strip(),
                description=description,
                path=path,
            ),
        )
    return [directive for directive in directives if directive.name]


def scenario_snapshot_mcp_configs(
    snapshot: dict[str, Any] | None,
    *,
    workspace_dir: Path,
    chat_id: str,
) -> list[dict[str, Any]]:
    """Return trusted session MCP configs captured in a Chat snapshot.

    The snapshot is server-created and may contain only safe config fields;
    callers still validate each entry before constructing a runtime client.
    """
    result: list[dict[str, Any]] = []
    session_root = _chat_session_root(workspace_dir, chat_id)
    for resource in (snapshot or {}).get("resources", []):
        if not isinstance(resource, dict):
            continue
        if resource.get("type") != "mcp_service":
            continue
        if resource.get("status") != "temporary":
            continue
        config = _read_temporary_mcp_config(
            resource.get("mcp_config_path"),
            session_root,
        )
        if isinstance(config, dict):
            result.append(
                {
                    "resource_id": str(resource.get("id") or ""),
                    "client_key": str(
                        resource.get("mcp_client_key")
                        or resource.get("id")
                        or "",
                    ),
                    "config": dict(config),
                    "tools": resource.get("tools", []),
                },
            )
        else:
            _mark_resource_unavailable(resource, "mcp_config_unavailable")
    return [
        item for item in result if item["resource_id"] and item["client_key"]
    ]


def _mark_resource_unavailable(resource: dict[str, Any], reason: str) -> None:
    resource["status"] = "unavailable"
    resource["unavailable_reason"] = reason


def _read_temporary_mcp_config(
    raw_path: object,
    session_root: Path,
) -> dict[str, Any] | None:
    import json

    if not isinstance(raw_path, str):
        return None
    try:
        path = Path(raw_path)
        _reject_symlink_path(path, session_root)
        path = path.resolve()
        path.relative_to(session_root)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _chat_session_root(workspace_dir: Path, chat_id: str) -> Path:
    from uuid import UUID

    UUID(str(chat_id))
    return (workspace_dir / ".scenario_sessions" / str(chat_id)).resolve()


def _reject_symlink_path(path: Path, boundary: Path) -> None:
    """Reject symlink components before resolving a Chat-private path."""
    try:
        relative = path.relative_to(boundary)
    except ValueError as exc:
        raise ValueError(
            "scenario resource path escapes Chat boundary",
        ) from exc
    current = boundary
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise ValueError("scenario resource path contains symlink")

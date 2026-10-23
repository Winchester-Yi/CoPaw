# -*- coding: utf-8 -*-
"""API routes for MCP (Model Context Protocol) clients management."""

from __future__ import annotations

from pathlib import Path as FilePath
from typing import Any, Dict, List, Optional, Literal

from fastapi import (
    APIRouter,
    Body,
    HTTPException,
    Path as FastAPIPath,
    Request,
)
from pydantic import BaseModel, Field

from ..utils import schedule_agent_reload
from ...config.config import (
    MCPClientConfig,
    MCPConfig,
    load_agent_config,
    save_agent_config,
)
from ...config.context import resolve_request_effective_tenant_id
from ...config.utils import (
    get_tenant_request_working_dir,
    list_logical_tenant_ids,
)
from ..workspace.tenant_initializer import TenantInitializer

router = APIRouter(prefix="/mcp", tags=["mcp"])


class MCPClientInfo(BaseModel):
    """MCP client information for API responses."""

    key: str = Field(..., description="Unique client key identifier")
    name: str = Field(..., description="Client display name")
    description: str = Field(default="", description="Client description")
    enabled: bool = Field(..., description="Whether the client is enabled")
    transport: Literal["stdio", "streamable_http", "sse"] = Field(
        ...,
        description="MCP transport type",
    )
    url: str = Field(
        default="",
        description="Remote MCP endpoint URL (for HTTP/SSE transports)",
    )
    headers: Dict[str, str] = Field(
        default_factory=dict,
        description="HTTP headers for remote transport",
    )
    command: str = Field(
        default="",
        description="Command to launch the MCP server",
    )
    args: List[str] = Field(
        default_factory=list,
        description="Command-line arguments",
    )
    env: Dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables",
    )
    cwd: str = Field(
        default="",
        description="Working directory for stdio MCP command",
    )


class MCPClientCreateRequest(BaseModel):
    """Request body for creating/updating an MCP client."""

    name: str = Field(..., description="Client display name")
    description: str = Field(default="", description="Client description")
    enabled: bool = Field(
        default=True,
        description="Whether to enable the client",
    )
    transport: Literal["stdio", "streamable_http", "sse"] = Field(
        default="stdio",
        description="MCP transport type",
    )
    url: str = Field(
        default="",
        description="Remote MCP endpoint URL (for HTTP/SSE transports)",
    )
    headers: Dict[str, str] = Field(
        default_factory=dict,
        description="HTTP headers for remote transport",
    )
    command: str = Field(
        default="",
        description="Command to launch the MCP server",
    )
    args: List[str] = Field(
        default_factory=list,
        description="Command-line arguments",
    )
    env: Dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables",
    )
    cwd: str = Field(
        default="",
        description="Working directory for stdio MCP command",
    )


class MCPClientUpdateRequest(BaseModel):
    """Request body for updating an MCP client (all fields optional)."""

    name: Optional[str] = Field(None, description="Client display name")
    description: Optional[str] = Field(None, description="Client description")
    enabled: Optional[bool] = Field(
        None,
        description="Whether to enable the client",
    )
    transport: Optional[Literal["stdio", "streamable_http", "sse"]] = Field(
        None,
        description="MCP transport type",
    )
    url: Optional[str] = Field(
        None,
        description="Remote MCP endpoint URL (for HTTP/SSE transports)",
    )
    headers: Optional[Dict[str, str]] = Field(
        None,
        description="HTTP headers for remote transport",
    )
    command: Optional[str] = Field(
        None,
        description="Command to launch the MCP server",
    )
    args: Optional[List[str]] = Field(
        None,
        description="Command-line arguments",
    )
    env: Optional[Dict[str, str]] = Field(
        None,
        description="Environment variables",
    )
    cwd: Optional[str] = Field(
        None,
        description="Working directory for stdio MCP command",
    )


class MCPDistributionRequest(BaseModel):
    """Request body for distributing MCP clients to target tenants."""

    client_keys: List[str] = Field(
        default_factory=list,
        description="Selected source MCP client keys",
    )
    target_tenant_ids: List[str] = Field(
        default_factory=list,
        description="Target tenant IDs to update",
    )
    overwrite: bool = Field(
        default=False,
        description="Must be true for MCP distribution",
    )


class MCPDistributionTenantResult(BaseModel):
    """Per-tenant MCP distribution result."""

    tenant_id: str = Field(..., description="Target tenant ID")
    success: bool = Field(..., description="Whether distribution succeeded")
    bootstrapped: bool = Field(
        default=False,
        description="Whether the target tenant was bootstrapped during write",
    )
    default_agent_updated: List[str] = Field(
        default_factory=list,
        description="Selected client keys written to target default agent",
    )
    error: str = Field(default="", description="Failure details if any")


class MCPDistributionResponse(BaseModel):
    """Response payload for MCP distribution requests."""

    source_agent_id: str = Field(..., description="Source agent ID")
    results: List[MCPDistributionTenantResult] = Field(
        default_factory=list,
        description="Per-tenant distribution results",
    )


class MCPDistributionTenantListResponse(BaseModel):
    """Response for MCP distribution tenant list."""

    tenant_ids: List[str] = Field(default_factory=list)


def _restore_original_values(
    incoming: Dict[str, str],
    existing: Dict[str, str],
) -> Dict[str, str]:
    """Preserve original values when incoming matches their masked form."""
    restored: Dict[str, str] = {}
    for k, v in incoming.items():
        if k in existing and v == _mask_env_value(existing[k]):
            restored[k] = existing[k]
        else:
            restored[k] = v
    return restored


def _request_tenant_id(request: Request) -> str | None:
    return getattr(request.state, "tenant_id", None)


def _request_source_id(request: Request) -> str | None:
    return getattr(request.state, "source_id", None)


def _request_effective_tenant_id(request: Request) -> str | None:
    return resolve_request_effective_tenant_id(
        _request_tenant_id(request),
        _request_source_id(request),
        getattr(request.state, "scope_id", None),
    )


def _request_tenant_working_dir(request: Request) -> FilePath:
    return get_tenant_request_working_dir(_request_tenant_id(request))


def _get_multi_agent_manager(request: Request) -> Any:
    manager = getattr(request.app.state, "multi_agent_manager", None)
    if manager is None:
        raise RuntimeError("MultiAgentManager not initialized")
    return manager


def _validate_target_tenant_id(tenant_id: str) -> str:
    tenant_id = str(tenant_id or "").strip()
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if len(tenant_id) > 256:
        raise ValueError(f"Invalid tenant ID format: {tenant_id}")
    if ".." in tenant_id or "/" in tenant_id or "\\" in tenant_id:
        raise ValueError(f"Invalid tenant ID format: {tenant_id}")
    if any(ord(c) < 32 for c in tenant_id):
        raise ValueError(f"Invalid tenant ID format: {tenant_id}")
    return tenant_id


def _clone_mcp_client_config(client: MCPClientConfig) -> MCPClientConfig:
    return MCPClientConfig.model_validate(client.model_dump(mode="json"))


async def _resolve_source_mcp_clients(
    request: Request,
    client_keys: List[str],
) -> tuple[Any, Dict[str, MCPClientConfig]]:
    from ..agent_context import get_agent_for_request

    source_agent = await get_agent_for_request(request)
    source_config = load_agent_config(
        source_agent.agent_id,
        tenant_id=source_agent.tenant_id,
    )
    source_clients_by_key = (
        source_config.mcp.clients
        if source_config.mcp is not None and source_config.mcp.clients
        else {}
    )

    source_clients: Dict[str, MCPClientConfig] = {}
    missing_client_keys: List[str] = []
    for client_key in client_keys:
        client = source_clients_by_key.get(client_key)
        if client is None:
            missing_client_keys.append(client_key)
            continue
        source_clients[client_key] = _clone_mcp_client_config(client)

    if missing_client_keys:
        missing = ", ".join(missing_client_keys)
        raise HTTPException(
            status_code=400,
            detail=f"Source MCP client(s) not found: {missing}",
        )

    return source_agent, source_clients


async def _distribute_mcp_clients_to_tenant(
    request: Request,
    *,
    target_tenant_id: str,
    source_clients: Dict[str, MCPClientConfig],
) -> MCPDistributionTenantResult:
    initializer = TenantInitializer(
        _request_tenant_working_dir(request).parent,
        target_tenant_id,
        source_id=_request_source_id(request),
    )
    was_bootstrapped = initializer.has_seeded_bootstrap()
    if not was_bootstrapped:
        pool = getattr(
            getattr(getattr(request, "app", None), "state", request.state),
            "tenant_workspace_pool",
            None,
        )
        if pool is None:
            raise HTTPException(
                status_code=503,
                detail="Tenant pool not available",
            )
        await pool.ensure_bootstrap(
            target_tenant_id,
            source_id=_request_source_id(request),
        )

    effective_target_tenant_id = getattr(
        initializer,
        "effective_tenant_id",
        target_tenant_id,
    )
    target_config = load_agent_config(
        "default",
        tenant_id=effective_target_tenant_id,
    )
    original_target_config = target_config.model_copy(deep=True)
    if target_config.mcp is None:
        target_config.mcp = MCPConfig(clients={})

    for client_key, source_client in source_clients.items():
        target_config.mcp.clients[client_key] = _clone_mcp_client_config(
            source_client,
        )

    manager = _get_multi_agent_manager(request)
    try:
        save_agent_config(
            "default",
            target_config,
            tenant_id=effective_target_tenant_id,
        )
        await manager.reload_agent(
            "default",
            tenant_id=effective_target_tenant_id,
        )
    except Exception as exc:
        rollback_errors: List[str] = []
        try:
            save_agent_config(
                "default",
                original_target_config,
                tenant_id=effective_target_tenant_id,
            )
        except Exception as rollback_save_exc:
            rollback_errors.append(
                f"rollback save failed: {rollback_save_exc}",
            )
        else:
            try:
                await manager.reload_agent(
                    "default",
                    tenant_id=effective_target_tenant_id,
                )
            except Exception as rollback_reload_exc:
                rollback_errors.append(
                    f"rollback reload failed: {rollback_reload_exc}",
                )
        if rollback_errors:
            raise RuntimeError(
                f"{exc}; {'; '.join(rollback_errors)}",
            ) from exc
        raise

    return MCPDistributionTenantResult(
        tenant_id=target_tenant_id,
        success=True,
        bootstrapped=not was_bootstrapped,
        default_agent_updated=list(source_clients.keys()),
    )


def _mask_env_value(value: str) -> str:
    """
    Mask environment variable value showing first 2-3 chars and last 4 chars.

    Examples:
        sk-proj-1234567890abcdefghij1234 -> sk-****************************1234
        abc123456789xyz -> ab***********xyz (if no dash)
        my-api-key-value -> my-************lue
        short123 -> ******** (8 chars or less, fully masked)
    """
    if not value:
        return value

    length = len(value)
    if length <= 8:
        # For short values, just mask everything
        return "*" * length

    # Show first 2-3 characters (3 if there's a dash at position 2)
    prefix_len = 3 if length > 2 and value[2] == "-" else 2
    prefix = value[:prefix_len]

    # Show last 4 characters
    suffix = value[-4:]

    # Calculate masked section length (at least 4 asterisks)
    masked_len = max(length - prefix_len - 4, 4)

    return f"{prefix}{'*' * masked_len}{suffix}"


def _build_client_info(key: str, client: MCPClientConfig) -> MCPClientInfo:
    """Build MCPClientInfo from config with masked env values."""
    # Mask environment variable values for security
    masked_env = (
        {k: _mask_env_value(v) for k, v in client.env.items()}
        if client.env
        else {}
    )
    masked_headers = (
        {k: _mask_env_value(v) for k, v in client.headers.items()}
        if client.headers
        else {}
    )

    return MCPClientInfo(
        key=key,
        name=client.name,
        description=client.description,
        enabled=client.enabled,
        transport=client.transport,
        url=client.url,
        headers=masked_headers,
        command=client.command,
        args=client.args,
        env=masked_env,
        cwd=client.cwd,
    )


@router.get(
    "",
    response_model=List[MCPClientInfo],
    summary="List all MCP clients",
)
async def list_mcp_clients(request: Request) -> List[MCPClientInfo]:
    """Get list of all configured MCP clients."""
    from ..agent_context import get_agent_and_config_for_request

    _, agent_config = await get_agent_and_config_for_request(request)
    mcp_config = agent_config.mcp
    if mcp_config is None or not mcp_config.clients:
        return []

    return [
        _build_client_info(key, client)
        for key, client in mcp_config.clients.items()
    ]


@router.get(
    "/{client_key}",
    response_model=MCPClientInfo,
    summary="Get MCP client details",
)
async def get_mcp_client(
    request: Request,
    client_key: str = FastAPIPath(...),
) -> MCPClientInfo:
    """Get details of a specific MCP client."""
    from ..agent_context import get_agent_and_config_for_request

    _, agent_config = await get_agent_and_config_for_request(request)
    mcp_config = agent_config.mcp
    if mcp_config is None:
        raise HTTPException(404, detail=f"MCP client '{client_key}' not found")

    client = mcp_config.clients.get(client_key)
    if client is None:
        raise HTTPException(404, detail=f"MCP client '{client_key}' not found")
    return _build_client_info(client_key, client)


@router.get(
    "/distribution/tenants",
    response_model=MCPDistributionTenantListResponse,
    summary="List tenants for MCP client distribution",
)
async def list_mcp_distribution_tenants(
    request: Request,
) -> MCPDistributionTenantListResponse:
    """Return all tenants belonging to the current source_id."""
    return MCPDistributionTenantListResponse(
        tenant_ids=await list_logical_tenant_ids(
            _request_source_id(request),
            source_filter=True,
            include_templates=True,
        ),
    )


@router.post(
    "/distribute/default-agents",
    response_model=MCPDistributionResponse,
    summary="Distribute selected MCP clients to target tenant default agents",
)
async def distribute_mcp_clients_to_default_agents(
    request: Request,
    body: MCPDistributionRequest = Body(...),
) -> MCPDistributionResponse:
    """Copy selected source-agent MCP clients into target default agents."""
    if not body.overwrite:
        raise HTTPException(
            status_code=400,
            detail="overwrite=true is required for MCP distribution",
        )
    if not body.client_keys:
        raise HTTPException(
            status_code=400,
            detail="No MCP client keys provided",
        )
    if not body.target_tenant_ids:
        raise HTTPException(
            status_code=400,
            detail="No target tenant IDs provided",
        )

    source_agent, source_clients = await _resolve_source_mcp_clients(
        request,
        body.client_keys,
    )

    results: List[MCPDistributionTenantResult] = []
    for tenant_id in body.target_tenant_ids:
        try:
            validated_tenant_id = _validate_target_tenant_id(tenant_id)
            results.append(
                await _distribute_mcp_clients_to_tenant(
                    request,
                    target_tenant_id=validated_tenant_id,
                    source_clients=source_clients,
                ),
            )
        except Exception as exc:
            results.append(
                MCPDistributionTenantResult(
                    tenant_id=str(tenant_id),
                    success=False,
                    error=str(exc),
                ),
            )

    return MCPDistributionResponse(
        source_agent_id=source_agent.agent_id,
        results=results,
    )


@router.post(
    "",
    response_model=MCPClientInfo,
    summary="Create a new MCP client",
    status_code=201,
)
async def create_mcp_client(
    request: Request,
    client_key: str = Body(..., embed=True),
    client: MCPClientCreateRequest = Body(..., embed=True),
) -> MCPClientInfo:
    """Create a new MCP client configuration."""
    from ..agent_context import agent_config_update_scope

    # Create new client config
    new_client = MCPClientConfig(
        name=client.name,
        description=client.description,
        enabled=client.enabled,
        transport=client.transport,
        url=client.url,
        headers=client.headers,
        command=client.command,
        args=client.args,
        env=client.env,
        cwd=client.cwd,
    )

    async with agent_config_update_scope(request) as (workspace, agent_config):
        if agent_config.mcp is None:
            agent_config.mcp = MCPConfig(clients={})
        if client_key in agent_config.mcp.clients:
            raise HTTPException(
                400,
                detail=f"MCP client '{client_key}' already exists. Use PUT to "
                f"update.",
            )
        agent_config.mcp.clients[client_key] = new_client
        save_agent_config(
            workspace.agent_id,
            agent_config,
            tenant_id=workspace.tenant_id,
        )

    # Hot reload config (async, non-blocking)
    schedule_agent_reload(
        request,
        workspace.agent_id,
        tenant_id=workspace.tenant_id,
    )

    return _build_client_info(client_key, new_client)


@router.put(
    "/{client_key}",
    response_model=MCPClientInfo,
    summary="Update an MCP client",
)
async def update_mcp_client(
    request: Request,
    client_key: str = FastAPIPath(...),
    updates: MCPClientUpdateRequest = Body(...),
) -> MCPClientInfo:
    """Update an existing MCP client configuration."""
    from ..agent_context import agent_config_update_scope

    async with agent_config_update_scope(request) as (workspace, agent_config):
        if (
            agent_config.mcp is None
            or client_key not in agent_config.mcp.clients
        ):
            raise HTTPException(
                404,
                detail=f"MCP client '{client_key}' not found",
            )

        existing = agent_config.mcp.clients[client_key]
        update_data = updates.model_dump(exclude_unset=True)
        if "env" in update_data and update_data["env"] is not None:
            update_data["env"] = _restore_original_values(
                update_data["env"],
                existing.env or {},
            )
        if "headers" in update_data and update_data["headers"] is not None:
            update_data["headers"] = _restore_original_values(
                update_data["headers"],
                existing.headers or {},
            )

        merged_data = existing.model_dump(mode="json")
        merged_data.update(update_data)
        updated_client = MCPClientConfig.model_validate(merged_data)
        agent_config.mcp.clients[client_key] = updated_client
        save_agent_config(
            workspace.agent_id,
            agent_config,
            tenant_id=workspace.tenant_id,
        )

    # Hot reload config (async, non-blocking)
    schedule_agent_reload(
        request,
        workspace.agent_id,
        tenant_id=workspace.tenant_id,
    )

    return _build_client_info(client_key, updated_client)


@router.patch(
    "/{client_key}/toggle",
    response_model=MCPClientInfo,
    summary="Toggle MCP client enabled status",
)
async def toggle_mcp_client(
    request: Request,
    client_key: str = FastAPIPath(...),
) -> MCPClientInfo:
    """Toggle the enabled status of an MCP client."""
    from ..agent_context import agent_config_update_scope

    async with agent_config_update_scope(request) as (workspace, agent_config):
        if (
            agent_config.mcp is None
            or client_key not in agent_config.mcp.clients
        ):
            raise HTTPException(
                404,
                detail=f"MCP client '{client_key}' not found",
            )
        client = agent_config.mcp.clients[client_key]
        client.enabled = not client.enabled
        save_agent_config(
            workspace.agent_id,
            agent_config,
            tenant_id=workspace.tenant_id,
        )

    # Hot reload config (async, non-blocking)
    schedule_agent_reload(
        request,
        workspace.agent_id,
        tenant_id=workspace.tenant_id,
    )

    return _build_client_info(client_key, client)


@router.delete(
    "/{client_key}",
    response_model=Dict[str, str],
    summary="Delete an MCP client",
)
async def delete_mcp_client(
    request: Request,
    client_key: str = FastAPIPath(...),
) -> Dict[str, str]:
    """Delete an MCP client configuration."""
    from ..agent_context import agent_config_update_scope

    async with agent_config_update_scope(request) as (workspace, agent_config):
        if (
            agent_config.mcp is None
            or client_key not in agent_config.mcp.clients
        ):
            raise HTTPException(
                404,
                detail=f"MCP client '{client_key}' not found",
            )
        del agent_config.mcp.clients[client_key]
        save_agent_config(
            workspace.agent_id,
            agent_config,
            tenant_id=workspace.tenant_id,
        )

    # Hot reload config (async, non-blocking)
    schedule_agent_reload(
        request,
        workspace.agent_id,
        tenant_id=workspace.tenant_id,
    )

    return {"message": f"MCP client '{client_key}' deleted successfully"}

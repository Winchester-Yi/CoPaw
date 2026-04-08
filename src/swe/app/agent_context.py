# -*- coding: utf-8 -*-
"""Agent context utilities for multi-agent and multi-tenant support.

Provides utilities to get the correct agent instance for each request,
with tenant-first resolution order.
"""
from contextvars import ContextVar
from typing import Optional, TYPE_CHECKING
from fastapi import Request

from ..config.utils import load_config, get_tenant_config_path
from ..config.context import (
    get_current_tenant_id,
    get_current_user_id,
    TenantContextError,
)
from .middleware.tenant_workspace import TenantWorkspaceContext

if TYPE_CHECKING:
    from .workspace import Workspace

# Context variable to store current agent ID across async calls
_current_agent_id: ContextVar[Optional[str]] = ContextVar(
    "current_agent_id",
    default=None,
)


def _resolve_tenant_id(request: Request) -> Optional[str]:
    """Resolve tenant ID from context or request state."""
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        tenant_id = getattr(request.state, "tenant_id", None)
    return tenant_id


def _resolve_user_id(request: Request) -> Optional[str]:
    """Resolve user ID from context or request state."""
    user_id = get_current_user_id()
    if user_id is None:
        user_id = getattr(request.state, "user_id", None)
    return user_id


def _resolve_target_agent_id(
    request: Request,
    agent_id: Optional[str] = None,
) -> tuple[Optional[str], bool]:
    """Resolve target agent ID from various sources.

    Returns:
        Tuple of (target_agent_id, explicit_agent_requested)
    """
    target_agent_id = agent_id
    explicit = target_agent_id is not None

    if not target_agent_id and hasattr(request.state, "agent_id"):
        target_agent_id = request.state.agent_id
        explicit = target_agent_id is not None

    if not target_agent_id:
        target_agent_id = request.headers.get("X-Agent-Id")
        explicit = target_agent_id is not None

    return target_agent_id, explicit


def _get_cached_workspace(
    request: Request,
    target_agent_id: Optional[str],
    explicit_requested: bool,
) -> Optional["Workspace"]:
    """Get cached workspace if valid and matches target agent."""
    workspace = getattr(request.state, "workspace", None)
    if workspace is None or isinstance(workspace, TenantWorkspaceContext):
        return None
    if explicit_requested:
        return None
    if (
        target_agent_id
        and getattr(workspace, "agent_id", None) != target_agent_id
    ):
        return None
    return workspace


def _get_tenant_aware_config(tenant_id: Optional[str] = None):
    """Get config with tenant awareness.

    When tenant_id is provided, loads config from tenant workspace.
    Otherwise falls back to global config.

    Args:
        tenant_id: Optional tenant ID for tenant-scoped config.

    Returns:
        Configuration object.
    """
    if tenant_id is None:
        tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return load_config()
    return load_config(get_tenant_config_path(tenant_id))


async def get_agent_for_request(
    request: Request,
    agent_id: Optional[str] = None,
) -> "Workspace":
    """Get agent workspace for current request with tenant-first resolution.

    Resolution order (tenant-first):
    1. Resolve tenant from X-Tenant-Id header or context
    2. Resolve user from X-User-Id header or context
    3. Resolve agent from context, header, or tenant-local config

    Args:
        request: FastAPI request object
        agent_id: Agent ID override (highest priority)

    Returns:
        Workspace for the specified or active agent

    Raises:
        HTTPException: If agent not found or tenant context missing
    """
    from fastapi import HTTPException

    # Resolve contexts and target agent
    tenant_id = _resolve_tenant_id(request)
    user_id = _resolve_user_id(request)
    target_agent_id, explicit_agent_requested = _resolve_target_agent_id(
        request,
        agent_id,
    )

    # Check for cached workspace
    workspace = _get_cached_workspace(
        request,
        target_agent_id,
        explicit_agent_requested,
    )
    if workspace:
        return workspace

    # Load config and determine target agent
    config = _get_tenant_aware_config(tenant_id)
    if not target_agent_id:
        target_agent_id = config.agents.active_agent or "default"

    # Check if agent exists and is enabled
    if target_agent_id not in config.agents.profiles:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{target_agent_id}' not found",
        )

    agent_ref = config.agents.profiles[target_agent_id]
    if not getattr(agent_ref, "enabled", True):
        raise HTTPException(
            status_code=403,
            detail=f"Agent '{target_agent_id}' is disabled",
        )

    # Get MultiAgentManager (tenant-aware resolution)
    if not hasattr(request.app.state, "multi_agent_manager"):
        raise HTTPException(
            status_code=500,
            detail="MultiAgentManager not initialized",
        )

    # Store tenant/user context in request state for downstream use
    if tenant_id:
        request.state.tenant_id = tenant_id
    if user_id:
        request.state.user_id = user_id

    manager = request.app.state.multi_agent_manager

    try:
        workspace = await manager.get_agent(
            target_agent_id,
            tenant_id=tenant_id,
        )
        if not workspace:
            raise HTTPException(
                status_code=404,
                detail=f"Agent '{target_agent_id}' not found",
            )
        return workspace
    except ValueError as e:
        raise HTTPException(
            status_code=404,
            detail=str(e),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get agent: {str(e)}",
        ) from e


def get_active_agent_id(tenant_id: Optional[str] = None) -> str:
    """Get current active agent ID from config.

    Args:
        tenant_id: Optional tenant ID for tenant-scoped lookup.

    Returns:
        Active agent ID, defaults to "default"
    """
    try:
        config = _get_tenant_aware_config(tenant_id)
        return config.agents.active_agent or "default"
    except Exception:
        return "default"


def set_current_agent_id(agent_id: str) -> None:
    """Set current agent ID in context.

    Args:
        agent_id: Agent ID to set
    """
    _current_agent_id.set(agent_id)


def get_current_agent_id(tenant_id: Optional[str] = None) -> str:
    """Get current agent ID from context or config fallback.

    Args:
        tenant_id: Optional tenant ID for tenant-scoped lookup.

    Returns:
        Current agent ID, defaults to active agent or "default"
    """
    agent_id = _current_agent_id.get()
    if agent_id:
        return agent_id
    return get_active_agent_id(tenant_id)


def get_tenant_workspace(request: Request) -> Optional["Workspace"]:
    """Get tenant workspace from request state.

    Args:
        request: FastAPI request object

    Returns:
        Workspace if tenant workspace is bound, None otherwise
    """
    return getattr(request.state, "workspace", None)


def get_tenant_workspace_strict(request: Request) -> "Workspace":
    """Get tenant workspace from request state, raising if not set.

    Args:
        request: FastAPI request object

    Returns:
        Workspace for the current tenant

    Raises:
        TenantContextError: If workspace is not set in request state
    """
    workspace = getattr(request.state, "workspace", None)
    if workspace is None:
        raise TenantContextError(
            "Tenant workspace not bound to request. "
            "Ensure tenant workspace middleware is installed.",
        )
    return workspace


__all__ = [
    "get_agent_for_request",
    "get_active_agent_id",
    "set_current_agent_id",
    "get_current_agent_id",
    "get_tenant_workspace",
    "get_tenant_workspace_strict",
]

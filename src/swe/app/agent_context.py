# -*- coding: utf-8 -*-
"""Agent context utilities for multi-agent and multi-tenant support.

Provides utilities to get the correct agent instance for each request,
with tenant-first resolution order.
"""

from contextvars import ContextVar
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
import asyncio
import threading
from typing import AsyncIterator, Optional, TYPE_CHECKING
from fastapi import HTTPException, Request

from ..config.utils import (
    load_config,
    get_tenant_config_path,
    get_tenant_storage_config_path,
)
from ..config.context import (
    canonicalize_scope_id,
    get_current_scope_id,
    get_current_tenant_id,
    get_current_user_id,
    get_current_source_id,
    TenantContextError,
    resolve_request_effective_tenant_id,
    resolve_runtime_tenant_id,
    resolve_storage_tenant_id,
)
from ..constant import WORKING_DIR
from ..runtime_workers import run_runtime_state_work
from .middleware.tenant_workspace import TenantWorkspaceContext

if TYPE_CHECKING:
    from ..config.config import AgentProfileConfig
    from .workspace import Workspace

# Context variable to store current agent ID across async calls
_current_agent_id: ContextVar[Optional[str]] = ContextVar(
    "current_agent_id",
    default=None,
)
_AGENT_CONFIG_UPDATE_LOCKS: dict[
    tuple[asyncio.AbstractEventLoop, str, str],
    asyncio.Lock,
] = {}
_AGENT_CONFIG_UPDATE_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class FileManagerSourceScopeLocation:
    """Trusted external root location for one tenant file-manager request."""

    base_dir: Path
    component: str


def _resolve_tenant_id(request: Request) -> Optional[str]:
    """Resolve tenant ID from context or request state."""
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        tenant_id = getattr(request.state, "tenant_id", None)
    return tenant_id


def _resolve_source_id(request: Request) -> Optional[str]:
    """Resolve source ID from context or request state."""
    source_id = get_current_source_id()
    if source_id is None:
        source_id = getattr(request.state, "source_id", None)
    return source_id


def _resolve_scope_id(request: Request | None = None) -> Optional[str]:
    """Resolve runtime scope ID from context or request state."""
    scope_id = get_current_scope_id()
    if scope_id is None and request is not None:
        scope_id = getattr(request.state, "scope_id", None)
    if scope_id is None:
        return None
    return canonicalize_scope_id(scope_id)


def _resolve_effective_tenant_id(
    tenant_id: Optional[str],
    source_id: Optional[str] = None,
    scope_id: Optional[str] = None,
) -> Optional[str]:
    """Resolve request-effective tenant ID for scoped workspace/config access."""
    return resolve_request_effective_tenant_id(
        tenant_id,
        source_id,
        scope_id or get_current_scope_id(),
    )


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


def _get_tenant_aware_config(
    tenant_id: Optional[str] = None,
    source_id: Optional[str] = None,
    scope_id: Optional[str] = None,
):
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
    if source_id is None:
        source_id = get_current_source_id()
    if scope_id is None:
        scope_id = get_current_scope_id()
    storage_tenant_id = resolve_storage_tenant_id(
        tenant_id,
        source_id,
        scope_id=scope_id,
    )
    if storage_tenant_id is None:
        return load_config()
    return load_config(get_tenant_storage_config_path(storage_tenant_id))


async def resolve_file_manager_workspace_dir(request: Request) -> Path:
    """Resolve a verified Agent directory without starting its runtime."""

    from fastapi import HTTPException

    tenant_id = _resolve_tenant_id(request)
    source_id = _resolve_source_id(request)
    scope_id = _resolve_scope_id(request)
    effective_tenant_id = _resolve_effective_tenant_id(
        tenant_id,
        source_id,
        scope_id,
    )
    tenant_workspace = getattr(request.state, "workspace", None)
    if not isinstance(tenant_workspace, TenantWorkspaceContext):
        raise HTTPException(
            status_code=503,
            detail="Tenant workspace is unavailable",
        )
    if effective_tenant_id is None:
        raise HTTPException(
            status_code=400,
            detail="Tenant context required",
        )

    config = _get_tenant_aware_config(
        tenant_id,
        source_id=source_id,
        scope_id=scope_id,
    )
    requested_agent_id, _ = _resolve_target_agent_id(request)
    agent_id = requested_agent_id or config.agents.active_agent or "default"
    profile = config.agents.profiles.get(agent_id)
    if profile is None:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{agent_id}' not found",
        )
    if not getattr(profile, "enabled", True):
        raise HTTPException(
            status_code=403,
            detail=f"Agent '{agent_id}' is disabled",
        )

    workspace_dir = Path(profile.workspace_dir).expanduser().resolve()
    allowed_root = (
        Path(tenant_workspace.workspace_dir).resolve() / "workspaces"
    )
    try:
        workspace_dir.relative_to(allowed_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=403,
            detail="Agent workspace is unavailable",
        ) from exc
    if not workspace_dir.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{agent_id}' not found",
        )
    return workspace_dir


def resolve_file_manager_source_scope_location(
    request: Request,
) -> FileManagerSourceScopeLocation:
    """Return the external root location bound to the request tenant scope."""

    tenant_workspace = getattr(request.state, "workspace", None)
    if not isinstance(tenant_workspace, TenantWorkspaceContext):
        raise HTTPException(
            status_code=503,
            detail="Tenant workspace is unavailable",
        )
    component = tenant_workspace.workspace_dir.name
    if component in {"", ".", ".."} or "/" in component:
        raise HTTPException(
            status_code=503,
            detail="Tenant workspace is unavailable",
        )
    return FileManagerSourceScopeLocation(
        base_dir=WORKING_DIR,
        component=component,
    )


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
    source_id = _resolve_source_id(request)
    scope_id = _resolve_scope_id(request)
    effective_tenant_id = _resolve_effective_tenant_id(
        tenant_id,
        source_id,
        scope_id=scope_id,
    )
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
    config = _get_tenant_aware_config(
        tenant_id,
        source_id=source_id,
        scope_id=scope_id,
    )
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
    if effective_tenant_id:
        request.state.effective_tenant_id = effective_tenant_id
    if source_id:
        request.state.source_id = source_id
    if user_id:
        request.state.user_id = user_id
    request.state.agent_id = target_agent_id

    manager = request.app.state.multi_agent_manager

    try:
        workspace = await manager.get_agent(
            target_agent_id,
            tenant_id=effective_tenant_id,
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


async def get_agent_and_config_for_request(
    request: Request,
    agent_id: Optional[str] = None,
) -> tuple["Workspace", "AgentProfileConfig"]:
    """Resolve the request workspace and authoritative tenant-scoped config."""
    from ..config.config import load_agent_config
    from ..runtime_workers import run_runtime_state_work

    workspace = await get_agent_for_request(request, agent_id=agent_id)
    agent_config = await run_runtime_state_work(
        load_agent_config,
        workspace.agent_id,
        tenant_id=getattr(workspace, "tenant_id", None),
    )
    return workspace, agent_config


def _agent_config_update_lock(
    tenant_id: Optional[str],
    agent_id: str,
) -> asyncio.Lock:
    """Return the current loop's lock for one tenant Agent config."""
    key = (asyncio.get_running_loop(), tenant_id or "", agent_id)
    with _AGENT_CONFIG_UPDATE_LOCKS_GUARD:
        lock = _AGENT_CONFIG_UPDATE_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _AGENT_CONFIG_UPDATE_LOCKS[key] = lock
        return lock


@asynccontextmanager
async def agent_config_write_scope(
    tenant_id: Optional[str],
    agent_id: str,
) -> AsyncIterator[None]:
    """Serialize one tenant Agent configuration mutation."""
    lock = _agent_config_update_lock(tenant_id, agent_id)
    async with lock:
        yield


@asynccontextmanager
async def agent_config_update_scope(
    request: Request,
    agent_id: Optional[str] = None,
) -> AsyncIterator[tuple["Workspace", "AgentProfileConfig"]]:
    """Serialize one Agent configuration read-modify-save transaction."""
    from ..config.config import load_agent_config

    workspace = await get_agent_for_request(request, agent_id=agent_id)
    tenant_id = getattr(workspace, "tenant_id", None)
    async with agent_config_write_scope(tenant_id, workspace.agent_id):
        agent_config = await run_runtime_state_work(
            load_agent_config,
            workspace.agent_id,
            tenant_id=tenant_id,
        )
        yield workspace, agent_config


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
    "FileManagerSourceScopeLocation",
    "resolve_file_manager_source_scope_location",
    "resolve_file_manager_workspace_dir",
    "get_agent_for_request",
    "get_agent_and_config_for_request",
    "get_active_agent_id",
    "set_current_agent_id",
    "get_current_agent_id",
    "get_tenant_workspace",
    "get_tenant_workspace_strict",
]

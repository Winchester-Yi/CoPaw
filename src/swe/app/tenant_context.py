# -*- coding: utf-8 -*-
"""Tenant context binding utilities for HTTP, cron, and channel callbacks.

This module provides shared helpers and context managers for binding
tenant/workspace context in various entry points (HTTP requests,
cron jobs, channel callbacks).
"""
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from swe.config.context import (
    TenantContextError,
)


@contextmanager
def bind_tenant_context(
    tenant_id: str | None = None,
    user_id: str | None = None,
    workspace_dir: Path | None = None,
) -> Generator[None, None, None]:
    """Bind tenant context for the duration of the context manager.

    This is the primary entry point for non-HTTP code paths (cron jobs,
    background tasks, channel callbacks) to establish tenant context.

    Args:
        tenant_id: The tenant ID to bind. Required for tenant-scoped operations.
        user_id: The user ID to bind. Optional.
        workspace_dir: The workspace directory to bind. Required for
            file operations.

    Yields:
        None

    Example:
        # In a cron job executor
        with bind_tenant_context(
            tenant_id=job.tenant_id,
            user_id=job.user_id,
            workspace_dir=workspace.path,
        ):
            result = execute_job(job)
    """
    from swe.config.context import (
        set_current_tenant_id,
        set_current_user_id,
        set_current_workspace_dir,
        reset_current_tenant_id,
        reset_current_user_id,
        reset_current_workspace_dir,
    )

    tokens = []
    try:
        if tenant_id is not None:
            tokens.append(("tenant", set_current_tenant_id(tenant_id)))
        if user_id is not None:
            tokens.append(("user", set_current_user_id(user_id)))
        if workspace_dir is not None:
            tokens.append(
                ("workspace", set_current_workspace_dir(workspace_dir)),
            )
        yield
    finally:
        # Reset in reverse order to restore state correctly
        for name, token in reversed(tokens):
            if name == "tenant":
                reset_current_tenant_id(token)
            elif name == "user":
                reset_current_user_id(token)
            elif name == "workspace":
                reset_current_workspace_dir(token)


def get_tenant_context() -> dict:
    """Get the current tenant context as a dictionary.

    Returns:
        Dictionary containing 'tenant_id', 'user_id', and 'workspace_dir'
        (or None for each if not set).
    """
    from swe.config.context import (
        get_current_tenant_id,
        get_current_user_id,
        get_current_workspace_dir,
    )

    return {
        "tenant_id": get_current_tenant_id(),
        "user_id": get_current_user_id(),
        "workspace_dir": get_current_workspace_dir(),
    }


def require_tenant_context() -> tuple[str, Path]:
    """Require that tenant context is set, returning tenant_id and workspace_dir.

    Returns:
        Tuple of (tenant_id, workspace_dir).

    Raises:
        TenantContextError: If tenant_id or workspace_dir is not set.
    """
    from swe.config.context import (
        get_current_tenant_id_strict,
        get_current_workspace_dir_strict,
    )

    tenant_id = get_current_tenant_id_strict()
    workspace_dir = get_current_workspace_dir_strict()
    return tenant_id, workspace_dir


def require_full_context() -> tuple[str, str, Path]:
    """Require that full tenant context is set, returning all three values.

    Returns:
        Tuple of (tenant_id, user_id, workspace_dir).

    Raises:
        TenantContextError: If any of tenant_id, user_id, or workspace_dir
            is not set.
    """
    from swe.config.context import (
        get_current_tenant_id_strict,
        get_current_user_id_strict,
        get_current_workspace_dir_strict,
    )

    tenant_id = get_current_tenant_id_strict()
    user_id = get_current_user_id_strict()
    workspace_dir = get_current_workspace_dir_strict()
    return tenant_id, user_id, workspace_dir


@contextmanager
def bind_request_context(
    request,
) -> Generator[None, None, None]:
    """Bind tenant context from a FastAPI request object.

    Extracts tenant_id and user_id from request headers, and workspace
    from request.state.workspace.

    Args:
        request: The FastAPI request object.

    Yields:
        None

    Example:
        @app.middleware("http")
        async def tenant_middleware(request: Request, call_next):
            with bind_request_context(request):
                return await call_next(request)
    """
    tenant_id = request.headers.get("X-Tenant-Id")
    user_id = request.headers.get("X-User-Id")
    workspace = getattr(request.state, "workspace", None)
    # Support both workspace_dir (standard) and path (legacy) attributes
    if workspace is not None:
        workspace_dir = getattr(workspace, "workspace_dir", None)
        if workspace_dir is None:
            workspace_dir = getattr(workspace, "path", None)
    else:
        workspace_dir = None

    with bind_tenant_context(
        tenant_id=tenant_id,
        user_id=user_id,
        workspace_dir=workspace_dir,
    ):
        yield


__all__ = [
    "bind_tenant_context",
    "get_tenant_context",
    "require_tenant_context",
    "require_full_context",
    "bind_request_context",
    "TenantContextError",
]

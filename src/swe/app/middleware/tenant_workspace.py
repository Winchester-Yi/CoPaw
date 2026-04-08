# -*- coding: utf-8 -*-
# pylint: disable=unused-import,too-many-branches
"""Tenant workspace middleware for multi-tenant isolation.

Loads tenant workspace from TenantWorkspacePool, stores it in request.state,
and binds workspace context for the duration of the request.

Middleware ordering: Must come after TenantIdentityMiddleware and before
AgentContextMiddleware.
"""
import logging
from pathlib import Path
from typing import Callable, Awaitable, Optional

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp

from swe.config.context import (
    set_current_workspace_dir,
    reset_current_workspace_dir,
)

logger = logging.getLogger(__name__)


class TenantWorkspaceContext:
    """Lightweight context for tenant workspace.

    This class provides a minimal workspace context without requiring
    the full Workspace runtime to be started. It holds the tenant_id
    and workspace_dir for request-scoped context binding.

    Attributes:
        tenant_id: The tenant identifier.
        workspace_dir: Path to the tenant's workspace directory.
    """

    def __init__(self, tenant_id: str, workspace_dir: Path):
        self.tenant_id = tenant_id
        self.workspace_dir = Path(workspace_dir).expanduser().resolve()

    def __repr__(self) -> str:
        return (
            f"TenantWorkspaceContext(tenant_id={self.tenant_id},"
            f"workspace_dir={self.workspace_dir})"
        )


class TenantWorkspaceMiddleware(BaseHTTPMiddleware):
    """Middleware to load and bind tenant workspace for requests.

    Expects tenant_id to be set in request.state by TenantIdentityMiddleware.
    Loads the corresponding workspace from TenantWorkspacePool and binds
    workspace context for file operations.

    Middleware ordering:
    1. TenantIdentityMiddleware (sets tenant_id in request.state)
    2. TenantWorkspaceMiddleware (this middleware - loads workspace)
    3. AgentContextMiddleware (resolves agent within tenant context)
    """

    def __init__(
        self,
        app: ASGIApp,
        require_workspace: bool = True,
    ):
        """Initialize tenant workspace middleware.

        Args:
            app: The ASGI application.
            require_workspace: If True, require workspace for non-exempt routes.
        """
        super().__init__(app)
        self._require_workspace = require_workspace

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Load tenant workspace and bind context.

        Args:
            request: The incoming request.
            call_next: The next middleware/endpoint to call.

        Returns:
            The response from the next handler.

        Raises:
            HTTPException: If workspace is required but cannot be loaded.
        """
        # Get tenant_id from request state (set by TenantIdentityMiddleware)
        tenant_id = getattr(request.state, "tenant_id", None)
        workspace = None
        workspace_token = None

        try:
            # Load workspace if tenant_id is available
            if tenant_id:
                workspace = await self._get_workspace(request, tenant_id)

                if workspace:
                    # Store workspace in request state
                    request.state.workspace = workspace
                    request.state.tenant_workspace = workspace

                    # Bind workspace directory context
                    workspace_token = set_current_workspace_dir(
                        workspace.workspace_dir,
                    )

                    logger.debug(
                        f"TenantWorkspaceMiddleware: loaded workspace for "
                        f"tenant={tenant_id}, path={workspace.workspace_dir}",
                    )

                    # Note: Tenant model configuration is now managed entirely
                    # by ProviderManager. The old TenantModelContext loading
                    # has been removed as part of active model source unification.
                    # ProviderManager handles its own lazy initialization and
                    # legacy migration from tenant_models.json when needed.
                elif self._require_workspace:
                    # Workspace required but not found
                    logger.warning(
                        f"Workspace not found for tenant: {tenant_id}",
                    )
                    raise HTTPException(
                        status_code=503,
                        detail=f"Workspace not available for tenant '{tenant_id}'",
                    )
            elif self._require_workspace:
                # No tenant context but workspace required
                # Check if this is an exempt route
                if not self._is_workspace_exempt(request.url.path):
                    logger.warning(
                        f"No tenant context for request: {request.url.path}",
                    )
                    raise HTTPException(
                        status_code=400,
                        detail="Tenant context required for this endpoint",
                    )

            # Call next handler
            response = await call_next(request)

            # Add workspace info to response headers (for debugging)
            if workspace and tenant_id:
                response.headers["X-Tenant-Workspace-Loaded"] = "true"

            return response

        finally:
            # Reset workspace context if set
            if workspace_token:
                try:
                    reset_current_workspace_dir(workspace_token)
                except Exception as e:  # noqa: BLE001
                    logger.error("Failed to reset workspace context: %s", e)

    async def _get_workspace(
        self,
        request: Request,
        tenant_id: str,
    ):
        """Get workspace context for tenant.

        Args:
            request: The FastAPI request object.
            tenant_id: The tenant ID to get workspace for.

        Returns:
            TenantWorkspaceContext instance or None if not available.
            Note: This returns a lightweight context, not a full Workspace runtime.
            The Workspace runtime is started on-demand via MultiAgentManager.get_agent().
        """
        # Get tenant workspace pool from app state
        pool = getattr(request.app.state, "tenant_workspace_pool", None)
        if pool is None:
            logger.warning("TenantWorkspacePool not available in app.state")
            return None

        try:
            # Ensure tenant is bootstrapped (minimal - directories only)
            await pool.ensure_bootstrap(tenant_id)

            # Create lightweight context without starting workspace runtime
            # The full Workspace runtime is lazy-loaded via MultiAgentManager.get_agent()
            workspace_dir = pool.get_tenant_workspace_dir(tenant_id)
            context = TenantWorkspaceContext(
                tenant_id=tenant_id,
                workspace_dir=workspace_dir,
            )
            logger.debug(
                f"Created TenantWorkspaceContext for tenant={tenant_id}, "
                f"dir={workspace_dir}",
            )
            return context
        except Exception as e:
            logger.error(
                f"Error bootstrapping tenant {tenant_id}: {e}",
            )
            return None

    def _is_workspace_exempt(self, path: str) -> bool:
        """Check if a route is exempt from workspace requirements.

        Args:
            path: The request path to check.

        Returns:
            True if the route is exempt, False otherwise.
        """
        # Same exemptions as tenant identity for consistency
        exempt_paths = frozenset(
            [
                "/health",
                "/healthz",
                "/ready",
                "/readyz",
                "/alive",
                "/api/version",
                "/docs",
                "/redoc",
                "/openapi.json",
                "/api/auth/login",
                "/api/auth/register",
                "/api/auth/refresh",
                "/api/auth/logout",
                "/logo.png",
                "/dark-logo.png",
                "/swe-symbol.svg",
                "/swe-dark.png",
            ],
        )

        if path in exempt_paths:
            return True

        # Prefix match for certain routes
        exempt_prefixes = (
            "/assets/",
            "/console/",
        )
        if any(path.startswith(prefix) for prefix in exempt_prefixes):
            return True

        return False


def get_workspace_from_request(request: Request):
    """Get tenant workspace from request state.

    Args:
        request: The FastAPI request object.

    Returns:
        Workspace instance if available, None otherwise.
    """
    return getattr(request.state, "workspace", None)


def get_workspace_from_request_strict(request: Request):
    """Get tenant workspace from request state, raising if not available.

    Args:
        request: The FastAPI request object.

    Returns:
        Workspace instance.

    Raises:
        HTTPException: If workspace is not available in request state.
    """
    workspace = getattr(request.state, "workspace", None)
    if workspace is None:
        raise HTTPException(
            status_code=503,
            detail="Tenant workspace not available",
        )
    return workspace


__all__ = [
    "TenantWorkspaceMiddleware",
    "get_workspace_from_request",
    "get_workspace_from_request_strict",
]

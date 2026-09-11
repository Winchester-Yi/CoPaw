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
import time
from typing import TYPE_CHECKING, Callable, Awaitable

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from swe.config.context import (
    canonicalize_scope_id,
    resolve_storage_tenant_id,
    set_current_workspace_dir,
    reset_current_workspace_dir,
)
from swe.app.middleware.tenant_identity import (
    PUBLIC_ROUTE_EXEMPT_PREFIXES,
)
from swe.app.middleware.provider_models_timing import (
    is_provider_models_list_request,
    log_provider_models_middleware_before_next,
    log_provider_models_middleware_done,
    log_provider_models_middleware_error,
)
from swe.app.identity_resolver import resolve_user_identity

if TYPE_CHECKING:
    from swe.app.workspace.bootstrap_state import (
        TenantBootstrapUnavailable as _TenantBootstrapUnavailable,
    )
else:
    try:
        from swe.app.workspace.bootstrap_state import (
            TenantBootstrapUnavailable as _TenantBootstrapUnavailable,
        )
    except (
        ModuleNotFoundError
    ):  # pragma: no cover - isolated router module tests

        class _TenantBootstrapUnavailable(RuntimeError):
            """Compatibility placeholder when workspace package is stubbed."""

            retry_after_seconds = 2


logger = logging.getLogger(__name__)


def _get_effective_request_tenant_id(request: Request) -> str | None:
    """Return the storage tenant used for workspace and config isolation."""
    tenant_id = getattr(request.state, "tenant_id", None)
    source_id = getattr(request.state, "source_id", None)
    scope_id = getattr(request.state, "scope_id", None)
    if not isinstance(tenant_id, str) or not tenant_id:
        return None
    return resolve_storage_tenant_id(
        tenant_id,
        source_id if isinstance(source_id, str) else None,
        scope_id=scope_id if isinstance(scope_id, str) else None,
    )


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
        # Use effective tenant_id for source-scoped default tenants.
        is_timing = is_provider_models_list_request(request)
        started_at = time.perf_counter()
        tenant_id = _get_effective_request_tenant_id(request)
        workspace = None
        workspace_token = None
        workspace_ms = 0
        before_next_at = None

        try:
            if request.method == "OPTIONS" or self._is_workspace_exempt(
                request.url.path,
            ):
                return await call_next(request)

            # Load workspace if tenant_id is available
            if tenant_id:
                workspace_started_at = time.perf_counter()
                workspace = await self._get_workspace(request, tenant_id)
                workspace_ms = int(
                    (time.perf_counter() - workspace_started_at) * 1000,
                )

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
            if is_timing:
                before_next_at = log_provider_models_middleware_before_next(
                    logger,
                    "TenantWorkspaceMiddleware",
                    request,
                    started_at,
                    effective_tenant_id=tenant_id,
                    workspace_loaded=workspace is not None,
                    workspace_ms=workspace_ms,
                    require_workspace=self._require_workspace,
                )
            response = await call_next(request)

            # Add workspace info to response headers (for debugging)
            if workspace and tenant_id:
                response.headers["X-Tenant-Workspace-Loaded"] = "true"

            if is_timing and before_next_at is not None:
                log_provider_models_middleware_done(
                    logger,
                    "TenantWorkspaceMiddleware",
                    request,
                    started_at,
                    before_next_at,
                    response,
                    effective_tenant_id=tenant_id,
                    workspace_loaded=workspace is not None,
                    workspace_ms=workspace_ms,
                    require_workspace=self._require_workspace,
                )
            return response

        except _TenantBootstrapUnavailable as exc:
            return JSONResponse(
                status_code=503,
                content={"detail": "Tenant bootstrap unavailable"},
                headers={"Retry-After": str(exc.retry_after_seconds)},
            )
        except Exception:
            if is_timing:
                log_provider_models_middleware_error(
                    logger,
                    "TenantWorkspaceMiddleware",
                    request,
                    started_at,
                    before_next_at,
                    effective_tenant_id=tenant_id,
                    workspace_loaded=workspace is not None,
                    workspace_ms=workspace_ms,
                    require_workspace=self._require_workspace,
                )
            raise
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
            is_timing = is_provider_models_list_request(request)
            started_at = time.perf_counter()
            # Get source_id from request state (set by TenantIdentityMiddleware)
            source_id = getattr(request.state, "source_id", None)
            raw_scope_id = getattr(request.state, "scope_id", None)
            scope_id = (
                raw_scope_id
                if isinstance(raw_scope_id, str) and raw_scope_id
                else None
            )
            # Get user_name and bbk_id from request state for database record
            user_name = getattr(request.state, "user_name", None)
            bbk_id = getattr(request.state, "bbk_id", None)
            resolve_identity_ms = 0
            if not user_name or not bbk_id:
                resolve_identity_started_at = time.perf_counter()
                resolved_identity = await resolve_user_identity(
                    tenant_id=getattr(request.state, "tenant_id", None)
                    or tenant_id,
                    source_id=source_id,
                    user_name=user_name,
                    bbk_id=bbk_id,
                    headers={
                        key: value
                        for key, value in {
                            "Content-Type": "application/json",
                            "Authorization": request.headers.get(
                                "Authorization",
                            ),
                        }.items()
                        if value
                    },
                    allow_remote_lookup=True,
                )
                resolve_identity_ms = int(
                    (time.perf_counter() - resolve_identity_started_at) * 1000,
                )
                user_name = resolved_identity.user_name
                bbk_id = resolved_identity.bbk_id
                request.state.user_name = user_name
                request.state.bbk_id = bbk_id

            # Ensure tenant is bootstrapped (minimal - directories only)
            bootstrap_started_at = time.perf_counter()
            await pool.ensure_bootstrap(
                getattr(request.state, "tenant_id", None) or tenant_id,
                source_id=source_id,
                scope_id=scope_id,
                tenant_name=user_name,
                bbk_id=bbk_id,
            )
            bootstrap_duration_ms = int(
                (time.perf_counter() - bootstrap_started_at) * 1000,
            )
            logger.debug(
                "ensure_bootstrap duration_ms=%d tenant_id=%s source_id=%s "
                "scope_id=%s",
                bootstrap_duration_ms,
                tenant_id,
                source_id,
                scope_id,
            )

            # Create lightweight context without starting workspace runtime
            # The full Workspace runtime is lazy-loaded via MultiAgentManager.get_agent()
            context_started_at = time.perf_counter()
            workspace_dir = pool.get_tenant_workspace_dir(tenant_id)
            context = TenantWorkspaceContext(
                tenant_id=tenant_id,
                workspace_dir=workspace_dir,
            )
            context_ms = int(
                (time.perf_counter() - context_started_at) * 1000,
            )
            if is_timing:
                logger.info(
                    "provider_models_workspace_get_done tenant_id=%s "
                    "source_id=%s scope_id=%s total_ms=%d "
                    "resolve_identity_ms=%d ensure_bootstrap_ms=%d "
                    "context_ms=%d workspace_dir=%s resolved_bbk_id=%s "
                    "resolved_user_name_present=%s",
                    tenant_id,
                    source_id,
                    scope_id,
                    int((time.perf_counter() - started_at) * 1000),
                    resolve_identity_ms,
                    bootstrap_duration_ms,
                    context_ms,
                    workspace_dir,
                    bool(bbk_id),
                    bool(user_name),
                )
            logger.debug(
                f"Created TenantWorkspaceContext for tenant={tenant_id}, "
                f"dir={workspace_dir}",
            )
            return context
        except _TenantBootstrapUnavailable:
            logger.warning(
                "tenant_bootstrap_unavailable tenant_id=%s",
                tenant_id,
            )
            raise
        except Exception as e:
            if is_provider_models_list_request(request):
                logger.exception(
                    "provider_models_workspace_get_error tenant_id=%s "
                    "duration_ms=%d",
                    tenant_id,
                    int((time.perf_counter() - started_at) * 1000),
                )
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
                "/api/health/health",
                "/ready",
                "/readyz",
                "/alive",
                "/api/version",
                "/api/runtime/memory-diagnostic",
                "/api/runtime/memory-type-holders",
                "/api/runtime/inotify-diagnostic",
                # Read-only jobs list; identity/auth checks still apply.
                "/api/external/cron/jobs",
                "/api/external/cron/jobs/",
                "/docs",
                "/redoc",
                "/openapi.json",
                "/api/auth/login",
                "/api/auth/register",
                "/api/auth/refresh",
                "/api/auth/logout",
                "/api/zhaohu/callback",
                "/logo.png",
                "/dark-logo.png",
                "/swe-symbol.svg",
                "/swe-dark.png",
            ],
        )

        if path in exempt_paths:
            return True

        if any(
            path.startswith(prefix) for prefix in PUBLIC_ROUTE_EXEMPT_PREFIXES
        ):
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

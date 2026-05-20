# -*- coding: utf-8 -*-
"""Tenant identity middleware for multi-tenant isolation.

Parses and validates X-Tenant-Id and X-User-Id headers, enforces
tenant identity requirements on stateful routes, and binds tenant/user
context for the duration of the request.
"""

import logging
from typing import Callable, Awaitable
from urllib.parse import unquote

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response, JSONResponse
from starlette.types import ASGIApp

from swe.config.context import (
    is_valid_identity_value,
    resolve_runtime_tenant_id,
    resolve_scope_id,
    set_current_tenant_id,
    set_current_user_id,
    set_current_source_id,
    set_current_scope_id,
    reset_current_tenant_id,
    reset_current_user_id,
    reset_current_source_id,
    reset_current_scope_id,
)

logger = logging.getLogger(__name__)

# Routes that are explicitly exempt from tenant identity requirements
# These are either truly stateless or system-level endpoints
TENANT_EXEMPT_ROUTES = frozenset(
    [
        # Health check endpoints
        "/health",
        "/healthz",
        "/api/health/health",
        "/ready",
        "/readyz",
        "/alive",
        # Version endpoint
        "/api/version",
        # OpenAPI docs (if enabled)
        "/docs",
        "/redoc",
        "/openapi.json",
        # Auth endpoints
        "/api/auth/login",
        "/api/auth/register",
        "/api/auth/refresh",
        "/api/auth/logout",
        # Static assets
        "/assets",
        "/logo.png",
        "/dark-logo.png",
        "/swe-symbol.svg",
        "/swe-dark.png",
        # Console SPA routes (static files)
        "/console",
        "/console/",
    ],
)

SOURCE_EXEMPT_ROUTES = frozenset(
    [
        # Health and docs endpoints never enter tenant-scoped runtime
        "/health",
        "/healthz",
        "/api/health/health",
        "/ready",
        "/readyz",
        "/alive",
        "/api/version",
        "/docs",
        "/redoc",
        "/openapi.json",
        # Auth endpoints remain source-agnostic
        "/api/auth/login",
        "/api/auth/register",
        "/api/auth/refresh",
        "/api/auth/logout",
        # Static assets
        "/assets",
        "/logo.png",
        "/dark-logo.png",
        "/swe-symbol.svg",
        "/swe-dark.png",
        "/console",
        "/console/",
    ],
)

PUBLIC_ROUTE_EXEMPT_PREFIXES = (
    "/assets/",
    "/static/",
    "/console/",
    "/api/assets/text/",
)


def is_tenant_exempt(path: str) -> bool:
    """Check if a route is exempt from tenant identity requirements.

    Args:
        path: The request path to check.

    Returns:
        True if the route is exempt, False otherwise.
    """
    # Exact match
    if path in TENANT_EXEMPT_ROUTES:
        return True

    # Prefix match for certain routes
    if any(path.startswith(prefix) for prefix in PUBLIC_ROUTE_EXEMPT_PREFIXES):
        return True

    return False


def is_source_exempt(path: str) -> bool:
    """Check if a route is exempt from source identity requirements."""
    if path in SOURCE_EXEMPT_ROUTES:
        return True

    if any(path.startswith(prefix) for prefix in PUBLIC_ROUTE_EXEMPT_PREFIXES):
        return True

    return False


class TenantIdentityMiddleware(BaseHTTPMiddleware):
    """Middleware to extract and validate tenant identity from headers.

        Reads X-Tenant-Id and X-User-Id headers, validates them, and binds
    the tenant/user context for the duration of the request. Stateful
        routes require a valid tenant ID; exempt routes skip validation.

        Middleware ordering: Should be placed early in the middleware stack,
        before TenantWorkspaceMiddleware and AgentContextMiddleware.
    """

    def __init__(
        self,
        app: ASGIApp,
        require_tenant: bool = True,
        default_tenant_id: str | None = None,
    ):
        """Initialize tenant identity middleware.

        Args:
            app: The ASGI application.
            require_tenant: If True, require tenant ID on non-exempt routes.
            default_tenant_id: Default tenant ID to use if not provided
                and require_tenant is False. Set to None to enforce strict
                tenant isolation with no fallback.
        """
        super().__init__(app)
        self._require_tenant = require_tenant
        self._default_tenant_id = default_tenant_id

    def _resolve_request_identity(
        self,
        request: Request,
    ) -> tuple[
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
        bool,
    ]:
        """Resolve request identity and enforce ingress contracts."""
        path = request.url.path
        is_tenant_optional = request.method == "OPTIONS" or is_tenant_exempt(
            path,
        )
        is_source_optional = request.method == "OPTIONS" or is_source_exempt(
            path,
        )
        tenant_id = request.headers.get("X-Tenant-Id")
        user_id = request.headers.get("X-User-Id")
        source_id = request.headers.get("X-Source-Id")
        user_name = request.headers.get("X-User-Name")
        bbk_id = request.headers.get("X-Bbk-Id")

        # 对 header 中的 user_name 进行 URI 解码
        if user_name:
            user_name = unquote(user_name)

        if not is_tenant_optional:
            tenant_id = self._validate_tenant_id(path, tenant_id)
        if not is_source_optional:
            source_id = self._validate_source_id(path, source_id)

        scope_id = resolve_scope_id(tenant_id, source_id)

        return (
            tenant_id,
            user_id,
            source_id,
            scope_id,
            user_name,
            bbk_id,
            is_tenant_optional and is_source_optional,
        )

    def _validate_tenant_id(
        self,
        path: str,
        tenant_id: str | None,
    ) -> str | None:
        """Validate tenant ID for non-exempt routes."""
        if not tenant_id:
            if self._require_tenant:
                logger.warning(
                    f"Missing X-Tenant-Id header for {path}",
                )
                raise HTTPException(
                    status_code=400,
                    detail="X-Tenant-Id header is required",
                )
            return self._default_tenant_id

        if not self._is_valid_tenant_id(tenant_id):
            logger.warning(f"Invalid tenant ID format: {tenant_id}")
            raise HTTPException(
                status_code=400,
                detail="Invalid X-Tenant-Id format",
            )
        return tenant_id

    def _validate_source_id(
        self,
        path: str,
        source_id: str | None,
    ) -> str:
        """Validate source ID for scoped routes."""
        if not source_id:
            logger.warning(
                f"Missing X-Source-Id header for {path}",
            )
            raise HTTPException(
                status_code=400,
                detail="X-Source-Id header is required",
            )

        if not self._is_valid_source_id(source_id):
            logger.warning(f"Invalid source ID format: {source_id}")
            raise HTTPException(
                status_code=400,
                detail="Invalid X-Source-Id format",
            )
        return source_id

    def _store_request_state(
        self,
        request: Request,
        tenant_id: str | None,
        user_id: str | None,
        source_id: str | None,
        scope_id: str | None,
        user_name: str | None,
        bbk_id: str | None,
    ) -> None:
        """Store identity in request state for downstream use."""
        effective_tenant_id = scope_id or resolve_runtime_tenant_id(
            tenant_id,
            source_id,
        )
        if tenant_id:
            request.state.tenant_id = tenant_id
        if effective_tenant_id:
            request.state.effective_tenant_id = effective_tenant_id
        if user_id:
            request.state.user_id = user_id
        if source_id:
            request.state.source_id = source_id
        if scope_id:
            request.state.scope_id = scope_id
        if user_name:
            request.state.user_name = user_name
        if bbk_id:
            request.state.bbk_id = bbk_id

    def _bind_context(
        self,
        tenant_id: str | None,
        user_id: str | None,
        source_id: str | None,
        scope_id: str | None,
    ) -> list[tuple[str, object]]:
        """Bind identity to context variables, return tokens for reset."""
        tokens = []
        if tenant_id:
            tokens.append(("tenant", set_current_tenant_id(tenant_id)))
        if user_id:
            tokens.append(("user", set_current_user_id(user_id)))
        if source_id:
            tokens.append(("source", set_current_source_id(source_id)))
        if scope_id:
            tokens.append(("scope", set_current_scope_id(scope_id)))
        return tokens

    def _reset_context(self, tokens: list[tuple[str, object]]) -> None:
        """Reset context variables using tokens."""
        reset_map = {
            "tenant": reset_current_tenant_id,
            "user": reset_current_user_id,
            "source": reset_current_source_id,
            "scope": reset_current_scope_id,
        }
        for name, token in reversed(tokens):
            if name in reset_map:
                reset_map[name](token)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Extract tenant identity and bind context.

        Args:
            request: The incoming request.
            call_next: The next middleware/endpoint to call.

        Returns:
            The response from the next handler.

        Raises:
            HTTPException: If tenant ID is required but missing/invalid.
        """
        tokens = []

        try:
            (
                tenant_id,
                user_id,
                source_id,
                scope_id,
                user_name,
                bbk_id,
                is_exempt,
            ) = self._resolve_request_identity(request)

            self._store_request_state(
                request,
                tenant_id,
                user_id,
                source_id,
                scope_id,
                user_name,
                bbk_id,
            )
            tokens = self._bind_context(
                tenant_id,
                user_id,
                source_id,
                scope_id,
            )

            logger.debug(
                f"TenantIdentityMiddleware: tenant_id={tenant_id}, "
                f"user_id={user_id}, bbk_id={bbk_id}, source_id={source_id}, "
                f"path={request.url.path}, exempt={is_exempt}",
            )

            response = await call_next(request)

            if tenant_id:
                response.headers["X-Tenant-Id-Resolved"] = tenant_id
            if scope_id:
                response.headers["X-Scope-Id-Resolved"] = scope_id

            return response
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
            )
        finally:
            self._reset_context(tokens)

    def _is_valid_tenant_id(self, tenant_id: str) -> bool:
        """Validate tenant ID format."""
        return is_valid_identity_value(tenant_id)

    def _is_valid_source_id(self, source_id: str) -> bool:
        """Validate source ID format."""
        return is_valid_identity_value(source_id)


def get_tenant_id_from_request(request: Request) -> str | None:
    """Get tenant ID from request state.

    Args:
        request: The FastAPI request object.

    Returns:
        The tenant ID if set, None otherwise.
    """
    return getattr(request.state, "tenant_id", None)


def get_user_id_from_request(request: Request) -> str | None:
    """Get user ID from request state.

    Args:
        request: The FastAPI request object.

    Returns:
        The user ID if set, None otherwise.
    """
    return getattr(request.state, "user_id", None)


def require_tenant_id(request: Request) -> str:
    """Require tenant ID from request, raising if not set.

    Args:
        request: The FastAPI request object.

    Returns:
        The tenant ID.

    Raises:
        HTTPException: If tenant ID is not set in request state.
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(
            status_code=400,
            detail="Tenant context not available",
        )
    return tenant_id


def require_user_id(request: Request) -> str:
    """Require user ID from request, raising if not set.

    Args:
        request: The FastAPI request object.

    Returns:
        The user ID.

    Raises:
        HTTPException: If user ID is not set in request state.
    """
    user_id = getattr(request.state, "user_id", None)
    if user_id is None:
        raise HTTPException(
            status_code=400,
            detail="User context not available",
        )
    return user_id


def get_source_id_from_request(request: Request) -> str | None:
    """Get source ID from request state.

    Args:
        request: The FastAPI request object.

    Returns:
        The source ID if set, None otherwise.
    """
    return getattr(request.state, "source_id", None)


def get_user_name_from_request(request: Request) -> str | None:
    """Get user name from request state.

    Args:
        request: The FastAPI request object.

    Returns:
        The user name if set, None otherwise.
    """
    return getattr(request.state, "user_name", None)


def get_bbk_id_from_request(request: Request) -> str | None:
    """Get bbk_id from request state.

    Args:
        request: The FastAPI request object.

    Returns:
        The bbk_id if set, None otherwise.
    """
    return getattr(request.state, "bbk_id", None)


__all__ = [
    "TenantIdentityMiddleware",
    "is_tenant_exempt",
    "TENANT_EXEMPT_ROUTES",
    "get_tenant_id_from_request",
    "get_user_id_from_request",
    "require_tenant_id",
    "require_user_id",
    "get_source_id_from_request",
    "get_user_name_from_request",
    "get_bbk_id_from_request",
]

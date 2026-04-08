# -*- coding: utf-8 -*-
"""Header passthrough middleware for MCP server requests.

Extracts x-header-* prefixed HTTP headers and stores them in context
for later injection into MCP client HTTP requests.
"""
import logging
from typing import Callable, Awaitable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp

from swe.config.context import (
    set_current_passthrough_headers,
    reset_current_passthrough_headers,
)

logger = logging.getLogger(__name__)


class HeaderPassthroughMiddleware(BaseHTTPMiddleware):
    """Middleware to extract x-header-* headers for MCP passthrough.

    This middleware extracts HTTP headers prefixed with 'x-header-' and
    stores them in request context for later use when creating MCP client
    connections. The prefix is stripped when passing to MCP servers.

    Example:
        x-header-cookie: session_id=abc123
        → becomes 'cookie: session_id=abc123' in MCP request

    Headers are stored in Python contextvars, ensuring proper isolation
    across concurrent async requests.
    """

    HEADER_PREFIX = "x-header-"

    def __init__(self, app: ASGIApp) -> None:
        """Initialize header passthrough middleware.

        Args:
            app: The ASGI application.
        """
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Extract x-header-* headers and bind to context.

        Args:
            request: The incoming request.
            call_next: The next middleware/endpoint to call.

        Returns:
            The response from the next handler.
        """
        passthrough_headers = self._extract_passthrough_headers(request)

        token = None
        if passthrough_headers:
            token = set_current_passthrough_headers(passthrough_headers)
            request.state.passthrough_headers = passthrough_headers
            logger.debug(
                f"HeaderPassthroughMiddleware: extracted headers "
                f"{list(passthrough_headers.keys())}",
            )

        try:
            response = await call_next(request)
            return response
        finally:
            if token:
                reset_current_passthrough_headers(token)

    def _extract_passthrough_headers(self, request: Request) -> dict[str, str]:
        """Extract x-header-* headers from request.

        Strips the 'x-header-' prefix from header names.

        Args:
            request: The FastAPI request object.

        Returns:
            Dictionary of headers with prefix stripped.
        """
        headers = {}
        for name, value in request.headers.items():
            name_lower = name.lower()
            if name_lower.startswith(self.HEADER_PREFIX):
                # Strip prefix: x-header-cookie → cookie
                mcp_name = name_lower[len(self.HEADER_PREFIX):]
                headers[mcp_name] = value
        return headers


__all__ = [
    "HeaderPassthroughMiddleware",
]
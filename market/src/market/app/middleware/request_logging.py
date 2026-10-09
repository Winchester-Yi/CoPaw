# -*- coding: utf-8 -*-
"""Request logging middleware for market service.

Logs request parameters, headers, body, response status, and processing time.
"""

from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

logger = logging.getLogger(__name__)

MAX_BODY_LOG_SIZE = 1024 * 1024

BINARY_PREFIXES = (
    "application/octet-stream",
    "image/",
    "audio/",
    "video/",
    "application/pdf",
)

EXEMPT_PATHS = frozenset(
    {
        "/health",
        "/healthz",
        "/api/health/health",
        "/ready",
        "/readyz",
        "/alive",
        "/docs",
        "/redoc",
        "/openapi.json",
    },
)


def _is_exempt(path: str) -> bool:
    return path in EXEMPT_PATHS


def _is_binary(content_type: str | None) -> bool:
    if not content_type:
        return False
    ct = content_type.lower()
    if ct.startswith("multipart/form-data"):
        return True
    return any(ct.startswith(p) for p in BINARY_PREFIXES)


def _body_summary(body: bytes, content_type: str | None) -> str:
    if not body:
        return ""
    if _is_binary(content_type):
        return f"[binary content, {len(body)} bytes]"
    limit = MAX_BODY_LOG_SIZE
    text = body[:limit].decode("utf-8", errors="replace")
    if len(body) > limit:
        text += "...[truncated]"
    return text


def _log_start(
    method: str,
    path: str,
    query: str,
    source_id: str | None,
    user_id: str | None,
    body_str: str,
) -> None:
    logger.info(
        "request_start method=%s path=%s query=%s source_id=%s user_id=%s body=%s",
        method,
        path,
        query or "-",
        source_id or "-",
        user_id or "-",
        body_str or "-",
    )


def _log_done(
    method: str,
    path: str,
    status_code: int,
    duration_ms: int,
    source_id: str | None,
    user_id: str | None,
) -> None:
    logger.info(
        "request_done method=%s path=%s status_code=%s duration_ms=%d source_id=%s user_id=%s",
        method,
        path,
        status_code,
        duration_ms,
        source_id or "-",
        user_id or "-",
    )


def _log_error(
    method: str,
    path: str,
    duration_ms: int,
    source_id: str | None,
    user_id: str | None,
    error: str,
) -> None:
    logger.exception(
        "request_error method=%s path=%s duration_ms=%d source_id=%s user_id=%s error=%s",
        method,
        path,
        duration_ms,
        source_id or "-",
        user_id or "-",
        error,
    )


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log request parameters and response details."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if _is_exempt(request.url.path):
            return await call_next(request)

        started_at = time.perf_counter()
        method = request.method
        path = request.url.path
        query = request.url.query
        source_id = request.headers.get("X-Source-Id")
        user_id = request.headers.get("X-User-Id")
        content_type = request.headers.get("content-type")

        body_str = ""
        if method in ("POST", "PUT", "PATCH"):
            body = await request.body()
            body_str = _body_summary(body, content_type)

        _log_start(method, path, query, source_id, user_id, body_str)

        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception as e:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            _log_error(method, path, duration_ms, source_id, user_id, str(e))
            raise

        duration_ms = int((time.perf_counter() - started_at) * 1000)
        _log_done(method, path, status_code, duration_ms, source_id, user_id)
        return response

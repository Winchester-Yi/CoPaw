# -*- coding: utf-8 -*-
"""Shared primitives for canonical structured tool failures."""

from __future__ import annotations

import asyncio
from collections.abc import (
    AsyncGenerator,
    Callable,
    Generator,
    MutableMapping,
)
from functools import wraps
import inspect
from typing import Any

from agentscope.tool import ToolResponse
from anyio import ClosedResourceError

try:
    from mcp.shared.exceptions import McpError
except ImportError:  # pragma: no cover - optional dependency surface
    McpError = ()  # type: ignore[assignment]

_DEFAULT_ERROR_DETAIL = "Tool error"
TOOL_GOVERNANCE_BLOCK_FIELD = "_swe_tool_governance"
TOOL_GOVERNANCE_MESSAGE_METADATA_FIELD = "_swe_tool_governance_by_call_id"
_TOOL_GOVERNANCE_STATUSES = frozenset({"pending", "rejected", "blocked"})
_FALLBACK_ERROR_DETAILS = {
    "approval_required": "Tool execution requires approval.",
    "hook_denied": "Hook denied tool execution.",
    "invalid_arguments": "Tool arguments are invalid.",
    "mcp_tool_error": "MCP tool error.",
    "mcp_transport_error": "MCP transport error.",
    "not_found": "Requested resource was not found.",
    "permission_denied": "Permission denied.",
    "process_limit_exceeded": "Process limit exceeded.",
    "shell_command_failed": "Shell command failed.",
    "shell_concurrency_limit_exceeded": "Shell concurrency limit exceeded.",
    "tool_timeout": "Tool timed out.",
    "tool_guard_denied": "Tool execution was denied.",
    "unexpected_tool_error": "Unexpected tool error.",
}


def make_failure_text_block(text: str) -> dict[str, str]:
    """Build the canonical text content block for tool failures."""
    return {"type": "text", "text": text}


def _fallback_detail(error_type: str) -> str:
    return _FALLBACK_ERROR_DETAILS.get(error_type, _DEFAULT_ERROR_DETAIL)


def _normalize_failure_content(
    *,
    error_type: str,
    detail: str | None,
    content: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if content:
        return [dict(block) for block in content]
    return [make_failure_text_block(detail or _fallback_detail(error_type))]


class ToolExecutionError(Exception):
    """Explicit tool failure contract for Swe-owned tool execution paths."""

    def __init__(
        self,
        *,
        error_type: str,
        detail: str | None = None,
        content: list[dict[str, Any]] | None = None,
    ) -> None:
        self.error_type = error_type
        self.detail = detail or _fallback_detail(error_type)
        self.content = _normalize_failure_content(
            error_type=error_type,
            detail=self.detail,
            content=content,
        )
        super().__init__(self.detail)


def build_structured_failure_output(
    *,
    error_type: str,
    detail: str | None = None,
    content: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the canonical structured failed tool_result payload."""
    return {
        "isError": True,
        "error_type": error_type,
        "content": _normalize_failure_content(
            error_type=error_type,
            detail=detail,
            content=content,
        ),
    }


def build_failed_tool_result_block(
    *,
    tool_call_id: str,
    tool_name: str,
    error_type: str,
    detail: str | None = None,
    content: list[dict[str, Any]] | None = None,
    governance_status: str | None = None,
) -> dict[str, Any]:
    """Build a full canonical failed tool_result block."""
    block: dict[str, Any] = {
        "type": "tool_result",
        "id": tool_call_id,
        "name": tool_name,
        "output": build_structured_failure_output(
            error_type=error_type,
            detail=detail,
            content=content,
        ),
    }
    if governance_status:
        block[TOOL_GOVERNANCE_BLOCK_FIELD] = governance_status
    return block


def attach_tool_governance_message_metadata(
    message: Any,
    *,
    tool_call_id: str,
    governance_status: str,
) -> Any:
    """Carry trusted governance through adapters that rebuild tool blocks."""
    if not tool_call_id or governance_status not in _TOOL_GOVERNANCE_STATUSES:
        return message
    current = getattr(message, "metadata", None)
    metadata = dict(current) if isinstance(current, MutableMapping) else {}
    current_by_call = metadata.get(TOOL_GOVERNANCE_MESSAGE_METADATA_FIELD)
    by_call = (
        dict(current_by_call)
        if isinstance(current_by_call, MutableMapping)
        else {}
    )
    by_call[tool_call_id] = governance_status
    metadata[TOOL_GOVERNANCE_MESSAGE_METADATA_FIELD] = by_call
    message.metadata = metadata
    return message


def build_failed_tool_response(
    *,
    error_type: str,
    detail: str | None = None,
    content: list[dict[str, Any]] | None = None,
    stream: bool = False,
    is_last: bool = True,
) -> ToolResponse:
    """Build a ToolResponse carrying the canonical structured failure."""
    return ToolResponse(
        content=build_structured_failure_output(
            error_type=error_type,
            detail=detail,
            content=content,
        ),
        stream=stream,
        is_last=is_last,
    )


def map_exception_to_error_type(exc: Exception) -> str:
    """Map known exception families to canonical tool failure types."""
    if isinstance(exc, ToolExecutionError):
        return exc.error_type
    if isinstance(exc, asyncio.TimeoutError):
        return "tool_timeout"
    if isinstance(exc, ClosedResourceError):
        return "mcp_transport_error"
    if McpError and isinstance(exc, McpError):
        return "mcp_tool_error"
    return "unexpected_tool_error"


def build_failed_tool_response_from_exception(
    exc: Exception,
    *,
    stream: bool = False,
    is_last: bool = True,
) -> ToolResponse:
    """Convert a tool exception into the canonical structured failure."""
    if isinstance(exc, ToolExecutionError):
        return build_failed_tool_response(
            error_type=exc.error_type,
            detail=exc.detail,
            content=exc.content,
            stream=stream,
            is_last=is_last,
        )

    return build_failed_tool_response(
        error_type=map_exception_to_error_type(exc),
        detail=str(exc) or None,
        stream=stream,
        is_last=is_last,
    )


def _wrap_async_generator(
    result: AsyncGenerator[ToolResponse, None],
) -> AsyncGenerator[ToolResponse, None]:
    async def _wrapped() -> AsyncGenerator[ToolResponse, None]:
        try:
            async for chunk in result:
                yield chunk
        except Exception as exc:  # pylint: disable=broad-except
            yield build_failed_tool_response_from_exception(
                exc,
                stream=True,
                is_last=True,
            )

    return _wrapped()


def _wrap_sync_generator(
    result: Generator[ToolResponse, None, None],
) -> Generator[ToolResponse, None, None]:
    def _wrapped() -> Generator[ToolResponse, None, None]:
        try:
            yield from result
        except Exception as exc:  # pylint: disable=broad-except
            yield build_failed_tool_response_from_exception(
                exc,
                stream=True,
                is_last=True,
            )

    return _wrapped()


def _normalize_tool_result(result: Any) -> Any:
    if isinstance(result, AsyncGenerator):
        return _wrap_async_generator(result)
    if isinstance(result, Generator):
        return _wrap_sync_generator(result)
    return result


def normalize_tool_function_errors(
    tool_func: Callable[..., Any],
) -> Callable[..., Any]:
    """Wrap a tool function so failures become canonical structured results."""
    if getattr(tool_func, "__swe_tool_failure_normalized__", False):
        return tool_func

    @wraps(tool_func)
    async def _wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            result = tool_func(*args, **kwargs)
            if inspect.isawaitable(result) and not isinstance(
                result,
                AsyncGenerator,
            ):
                result = await result
            return _normalize_tool_result(result)
        except Exception as exc:  # pylint: disable=broad-except
            return build_failed_tool_response_from_exception(exc)

    _wrapped.__swe_tool_failure_normalized__ = True
    _wrapped.__swe_tool_failure_original__ = tool_func
    return _wrapped

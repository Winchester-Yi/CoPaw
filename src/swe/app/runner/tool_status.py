# -*- coding: utf-8 -*-
"""Pure helpers for tool status presentation fields."""

from __future__ import annotations

from collections.abc import MutableMapping
import json
from typing import Any

TOOL_STATUS_RUNNING = "running"
TOOL_STATUS_SUCCESS = "success"
TOOL_STATUS_FAILED = "failed"

# Tool Guard governance statuses.  These describe why a tool did NOT run
# (waiting for approval, rejected by the user, intercepted by policy) and
# must never be conflated with a real execution failure.
TOOL_STATUS_PENDING = "pending"
TOOL_STATUS_REJECTED = "rejected"
TOOL_STATUS_BLOCKED = "blocked"

TOOL_STATUS_FIELD = "tool_status"
TOOL_ERROR_FIELD = "tool_error"
GOVERNANCE_FIELD = "tool_governance"
TOOL_ERROR_SUMMARY_LIMIT = 500

_DEFAULT_TOOL_ERROR = "Tool error"
_UNSET = object()

_GOVERNANCE_STATUSES = frozenset(
    {TOOL_STATUS_PENDING, TOOL_STATUS_REJECTED, TOOL_STATUS_BLOCKED},
)


def resolve_governance_status(value: Any) -> str | None:
    """仅接受 Tool Guard 内部写入的规范治理状态。"""
    if isinstance(value, str) and value in _GOVERNANCE_STATUSES:
        return value
    return None


def apply_governance_tool_status(
    data: MutableMapping[str, Any],
    governance_status: Any,
) -> str | None:
    """附加来自可信内部标记的治理状态。

    Tool Guard 治理结果与执行结果相互独立。治理命中时清除展示层的
    执行状态和错误字段，避免旧的失败投影覆盖治理状态。
    """
    governance = resolve_governance_status(governance_status)
    if governance is None:
        return None
    data[GOVERNANCE_FIELD] = governance
    data.pop(TOOL_STATUS_FIELD, None)
    data.pop(TOOL_ERROR_FIELD, None)
    return governance


def apply_running_tool_status(
    data: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Mark a tool call-start payload as running."""
    data[TOOL_STATUS_FIELD] = TOOL_STATUS_RUNNING
    data.pop(TOOL_ERROR_FIELD, None)
    return data


def apply_terminal_tool_status(
    data: MutableMapping[str, Any],
    *,
    raw_output: Any = _UNSET,
) -> MutableMapping[str, Any]:
    """Attach terminal tool status and bounded error summary."""
    tool_output = data.get("output") if raw_output is _UNSET else raw_output
    error_text = _extract_error_text(tool_output, data=data)
    if error_text is None:
        if data.get(TOOL_STATUS_FIELD) == TOOL_STATUS_FAILED:
            data[TOOL_STATUS_FIELD] = TOOL_STATUS_FAILED
            data[TOOL_ERROR_FIELD] = (
                _stringify_error(data.get(TOOL_ERROR_FIELD))
                or _DEFAULT_TOOL_ERROR
            )
            return data
        data[TOOL_STATUS_FIELD] = TOOL_STATUS_SUCCESS
        data[TOOL_ERROR_FIELD] = None
        return data

    data[TOOL_STATUS_FIELD] = TOOL_STATUS_FAILED
    data[TOOL_ERROR_FIELD] = _bound_error_text(error_text)
    return data


def _extract_error_text(
    tool_output: Any,
    *,
    data: MutableMapping[str, Any],
) -> str | None:
    explicit_error = _stringify_error(data.get("error"))
    if explicit_error:
        return explicit_error

    top_level_is_error = data.get("isError")
    if isinstance(top_level_is_error, bool) and top_level_is_error:
        return (
            _extract_content_error(data.get("content")) or _DEFAULT_TOOL_ERROR
        )

    if isinstance(tool_output, str):
        stripped_output = tool_output.strip()
        if stripped_output.startswith(("{", "[")):
            try:
                tool_output = json.loads(stripped_output)
            except json.JSONDecodeError:
                pass

    if isinstance(tool_output, MutableMapping):
        nested_error = _stringify_error(tool_output.get("error"))
        if nested_error:
            return nested_error

        is_error = tool_output.get("isError")
        if isinstance(is_error, bool) and is_error:
            return (
                _extract_content_error(tool_output.get("content"))
                or _DEFAULT_TOOL_ERROR
            )

    result_is_error = getattr(tool_output, "isError", None)
    if isinstance(result_is_error, bool) and result_is_error:
        return (
            _extract_content_error(getattr(tool_output, "content", None))
            or _DEFAULT_TOOL_ERROR
        )

    return None


def _extract_content_error(content: Any) -> str | None:
    if isinstance(content, str):
        return content.strip() or None

    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            text = _extract_block_text(block)
            if text:
                parts.append(text)
        if parts:
            return "\n".join(parts)
        return None

    return _stringify_error(content)


def _extract_block_text(block: Any) -> str | None:
    if isinstance(block, MutableMapping):
        return _stringify_error(block.get("text"))
    return _stringify_error(getattr(block, "text", None))


def _stringify_error(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bound_error_text(text: str) -> str:
    return text[:TOOL_ERROR_SUMMARY_LIMIT]

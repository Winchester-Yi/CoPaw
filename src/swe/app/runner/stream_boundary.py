# -*- coding: utf-8 -*-
"""Helpers for normalizing stream boundary events."""

from __future__ import annotations

import copy
from typing import Any, AsyncGenerator, AsyncIterator, Iterable

from agentscope_runtime.engine.schemas.agent_schemas import (
    ContentType,
    Event,
    Message,
    MessageType,
    RunStatus,
)

from ...agents.utils.tool_summary import (
    generate_tool_call_summary,
    generate_tool_output_summary,
)
from ...agents.tool_failure import (
    TOOL_GOVERNANCE_BLOCK_FIELD,
    TOOL_GOVERNANCE_MESSAGE_METADATA_FIELD,
)
from .operation_group import attach_operation_group
from .tool_status import (
    apply_governance_tool_status,
    apply_running_tool_status,
    apply_terminal_tool_status,
)

# 不在聊天流中展示进度的工具名称集合
_SILENT_TOOL_NAMES: frozenset[str] = frozenset({"update_task_progress"})


def _is_silent_tool_event(event: Event) -> bool:
    """检查事件是否属于不应在聊天流中展示的工具。"""
    if not isinstance(event, Message):
        return False
    if event.type not in (
        MessageType.FUNCTION_CALL,
        MessageType.PLUGIN_CALL,
        MessageType.FUNCTION_CALL_OUTPUT,
        MessageType.PLUGIN_CALL_OUTPUT,
        MessageType.MCP_TOOL_CALL,
        MessageType.MCP_TOOL_CALL_OUTPUT,
    ):
        return False
    for content in event.content or []:
        data = getattr(content, "data", None) or {}
        if isinstance(data, dict) and data.get("name") in _SILENT_TOOL_NAMES:
            return True
    return False


def _is_empty_reasoning_boundary_message(event: Event) -> bool:
    """Return True when *event* is the empty assistant-message boundary."""
    if not isinstance(event, Message):
        return False
    if event.object != "message" or event.type != MessageType.MESSAGE:
        return False
    if event.status != RunStatus.InProgress:
        return False
    return not event.content


def _consume_tool_governance_metadata(
    event: Message,
    data: dict,
) -> Any:
    """Read trusted governance metadata and strip it from the UI event."""
    governance_status = data.get(TOOL_GOVERNANCE_BLOCK_FIELD)
    metadata = getattr(event, "metadata", None)
    if governance_status is None and isinstance(metadata, dict):
        by_call = metadata.get(TOOL_GOVERNANCE_MESSAGE_METADATA_FIELD)
        call_id = data.get("call_id")
        if isinstance(by_call, dict) and isinstance(call_id, str):
            governance_status = by_call.get(call_id)
    if isinstance(metadata, dict) and (
        TOOL_GOVERNANCE_MESSAGE_METADATA_FIELD in metadata
    ):
        event.metadata = {
            key: value
            for key, value in metadata.items()
            if key != TOOL_GOVERNANCE_MESSAGE_METADATA_FIELD
        }
    return governance_status


def _normalize_reasoning_boundary_events(
    events: Iterable[Event],
):
    """Replace empty assistant boundaries with reasoning completed events."""
    current_reasoning: Message | None = None

    for event in events:
        if isinstance(event, Message):
            if (
                event.object == "message"
                and event.type == MessageType.REASONING
                and event.status == RunStatus.InProgress
            ):
                current_reasoning = event
                yield event
                continue

            if (
                current_reasoning is not None
                and event.object == "message"
                and event.type == MessageType.REASONING
                and event.id == current_reasoning.id
                and event.status != RunStatus.InProgress
            ):
                current_reasoning = None
                yield event
                continue

            if (
                current_reasoning is not None
                and _is_empty_reasoning_boundary_message(event)
            ):
                completed_reasoning = copy.deepcopy(current_reasoning)
                completed_reasoning.completed()
                current_reasoning = None
                yield completed_reasoning
                yield event
                continue

        yield event


async def _enrich_tool_message(event: Message) -> None:
    """Attach summaries to tool events."""
    contents = event.content or []

    if event.type in (
        MessageType.FUNCTION_CALL,
        MessageType.PLUGIN_CALL,
        MessageType.MCP_TOOL_CALL,
    ):
        for content in contents:
            if getattr(content, "type", None) != ContentType.DATA:
                continue
            data = getattr(content, "data", None)
            if not isinstance(data, dict):
                data = {}
                content.data = data
            tool_name = data.get("name", "")
            arguments = data.get("arguments", "{}")
            server_label = data.get("server_label")
            # Strip the display-only operation_group key before summary
            # generation and before the payload reaches the console.
            attach_operation_group(data, arguments)
            fallback = generate_tool_call_summary(
                tool_name=tool_name,
                arguments=data.get("arguments", "{}"),
                server_label=server_label,
            )
            data["summary"] = fallback
            apply_running_tool_status(data)

    elif event.type in (
        MessageType.FUNCTION_CALL_OUTPUT,
        MessageType.PLUGIN_CALL_OUTPUT,
        MessageType.MCP_TOOL_CALL_OUTPUT,
    ):
        for content in contents:
            if getattr(content, "type", None) != ContentType.DATA:
                continue
            data = getattr(content, "data", None)
            if not isinstance(data, dict):
                data = {}
                content.data = data
            tool_name = data.get("name", "")
            output = data.get("output", "")
            arguments = data.get("arguments")
            governance_status = _consume_tool_governance_metadata(event, data)
            fallback = generate_tool_output_summary(
                tool_name=tool_name,
                output=output,
                governance_status=governance_status,
            )
            data["output_summary"] = fallback
            apply_terminal_tool_status(data)
            apply_governance_tool_status(
                data,
                governance_status,
            )
            data.pop(TOOL_GOVERNANCE_BLOCK_FIELD, None)


async def normalize_reasoning_boundary_stream(
    source_stream: AsyncIterator[Event],
) -> AsyncGenerator[Event, None]:
    """Async wrapper for reasoning boundary normalization."""
    current_reasoning: Message | None = None

    async for event in source_stream:
        if isinstance(event, Message):
            if (
                event.object == "message"
                and event.type == MessageType.REASONING
                and event.status == RunStatus.InProgress
            ):
                current_reasoning = event
                yield event
                continue

            if (
                current_reasoning is not None
                and event.object == "message"
                and event.type == MessageType.REASONING
                and event.id == current_reasoning.id
                and event.status != RunStatus.InProgress
            ):
                current_reasoning = None
                yield event
                continue

            if (
                current_reasoning is not None
                and _is_empty_reasoning_boundary_message(event)
            ):
                completed_reasoning = copy.deepcopy(current_reasoning)
                completed_reasoning.completed()
                current_reasoning = None
                yield completed_reasoning
                yield event
                continue

        if _is_silent_tool_event(event):
            continue

        if isinstance(event, Message):
            await _enrich_tool_message(event)

        yield event

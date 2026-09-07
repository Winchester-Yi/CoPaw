# -*- coding: utf-8 -*-
"""Persisted Main Agent context occupancy snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from agentscope.agent._react_agent import _MemoryMark
from pydantic import BaseModel, ConfigDict, Field

from ...agents.utils import get_swe_token_counter

CONTEXT_USAGE_STATE_KEY = "context_usage"
CONTEXT_USAGE_INVALID_STATE_KEY = "context_usage_invalid"
CONTEXT_USAGE_SCHEMA_VERSION = 1


class ContextUsageStatus(str, Enum):
    """Runtime-aligned presentation stage for context occupancy."""

    NORMAL = "normal"
    GOVERNANCE = "governance"
    ACTIVE = "active"
    EMERGENCY = "emergency"
    OVERFLOW = "overflow"


class ContextUsageSnapshot(BaseModel):
    """Numeric-only snapshot committed beside one cleaned Agent state."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = CONTEXT_USAGE_SCHEMA_VERSION
    used_tokens: int = Field(ge=0)
    max_tokens: int = Field(gt=0)
    remaining_tokens: int = Field(ge=0)
    usage_ratio: float = Field(ge=0)
    system_context_tokens: int = Field(ge=0)
    tool_definition_tokens: int = Field(ge=0)
    conversation_tokens: int = Field(ge=0)
    governance_threshold_ratio: float = Field(ge=0, le=1)
    active_threshold_ratio: float = Field(ge=0, le=1)
    emergency_threshold_ratio: float = Field(ge=0, le=1)
    status: ContextUsageStatus
    estimated: bool = True
    as_of: datetime


class ContextUsageAvailable(ContextUsageSnapshot):
    """Available API response enriched with request-time staleness."""

    available: Literal[True] = True
    stale: bool = False


class ContextUsageUnavailable(BaseModel):
    """Stable API response for chats without a committed snapshot."""

    model_config = ConfigDict(extra="forbid")

    available: Literal[False] = False


ContextUsageResponse = ContextUsageAvailable | ContextUsageUnavailable


def _is_compressed_mark(mark: Any) -> bool:
    return (
        mark == _MemoryMark.COMPRESSED or mark == _MemoryMark.COMPRESSED.value
    )


def _cleaned_online_messages(agent_state: dict[str, Any]) -> list[dict]:
    """Extract effective online messages from the detached cleaned state."""
    memory_state = agent_state.get("memory")
    if not isinstance(memory_state, dict):
        return []
    content = memory_state.get("content")
    if not isinstance(content, list):
        return []

    messages: list[dict] = []
    for entry in content:
        payload: Any = entry
        marks: Any = []
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            payload, marks = entry
        if isinstance(marks, list) and any(
            _is_compressed_mark(mark) for mark in marks
        ):
            continue
        if isinstance(payload, dict):
            messages.append(payload)
    return messages


async def _prefixed_memory_messages(memory: Any) -> list[dict]:
    """Return only summary/long-term messages prepended by the runtime."""
    with_prefix = await memory.get_memory(prepend_summary=True)
    online = await memory.get_memory(prepend_summary=False)
    prefix_count = max(len(with_prefix) - len(online), 0)
    return [message.to_dict() for message in with_prefix[:prefix_count]]


def _status_for_ratio(
    ratio: float,
    *,
    governance_ratio: float,
    active_ratio: float,
    emergency_ratio: float,
) -> ContextUsageStatus:
    if ratio >= 1:
        return ContextUsageStatus.OVERFLOW
    if ratio >= emergency_ratio:
        return ContextUsageStatus.EMERGENCY
    if ratio >= active_ratio:
        return ContextUsageStatus.ACTIVE
    if ratio >= governance_ratio:
        return ContextUsageStatus.GOVERNANCE
    return ContextUsageStatus.NORMAL


async def capture_context_usage(
    agent: Any,
    cleaned_agent_state: dict[str, Any],
    *,
    sampled_at: datetime | None = None,
) -> ContextUsageSnapshot:
    """Estimate categorized persisted context without calling the model."""
    agent_config = agent._agent_config  # pylint: disable=protected-access
    running = agent_config.running
    compact = running.context_compact
    max_tokens = int(running.max_input_length)
    counter = get_swe_token_counter(agent_config)

    sys_prompt = agent.sys_prompt or ""
    system_context_tokens = await counter.count(messages=[], text=sys_prompt)
    prefix_messages = await _prefixed_memory_messages(agent.memory)
    if prefix_messages:
        system_context_tokens += await counter.count(
            messages=prefix_messages,
        )

    schemas = agent.toolkit.get_json_schemas()
    tool_definition_tokens = (
        await counter.count(messages=[], tools=schemas) if schemas else 0
    )

    online_messages = _cleaned_online_messages(cleaned_agent_state)
    conversation_tokens = (
        await counter.count(messages=online_messages) if online_messages else 0
    )
    used_tokens = (
        system_context_tokens + tool_definition_tokens + conversation_tokens
    )
    usage_ratio = used_tokens / max_tokens
    governance_ratio = float(compact.lightweight_governance_ratio)
    active_ratio = float(compact.memory_compact_ratio)
    emergency_ratio = float(compact.emergency_compact_ratio)

    return ContextUsageSnapshot(
        used_tokens=used_tokens,
        max_tokens=max_tokens,
        remaining_tokens=max(max_tokens - used_tokens, 0),
        usage_ratio=usage_ratio,
        system_context_tokens=system_context_tokens,
        tool_definition_tokens=tool_definition_tokens,
        conversation_tokens=conversation_tokens,
        governance_threshold_ratio=governance_ratio,
        active_threshold_ratio=active_ratio,
        emergency_threshold_ratio=emergency_ratio,
        status=_status_for_ratio(
            usage_ratio,
            governance_ratio=governance_ratio,
            active_ratio=active_ratio,
            emergency_ratio=emergency_ratio,
        ),
        as_of=sampled_at or datetime.now(timezone.utc),
    )

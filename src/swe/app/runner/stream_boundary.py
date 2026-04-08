# -*- coding: utf-8 -*-
"""Helpers for normalizing stream boundary events."""

from __future__ import annotations

import copy
from typing import AsyncGenerator, AsyncIterator, Iterable

from agentscope_runtime.engine.schemas.agent_schemas import (
    Event,
    Message,
    MessageType,
    RunStatus,
)


def _is_empty_reasoning_boundary_message(event: Event) -> bool:
    """Return True when *event* is the empty assistant-message boundary."""
    if not isinstance(event, Message):
        return False
    if event.object != "message" or event.type != MessageType.MESSAGE:
        return False
    if event.status != RunStatus.InProgress:
        return False
    return not event.content


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

        yield event

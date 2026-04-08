# -*- coding: utf-8 -*-
from __future__ import annotations

from agentscope_runtime.engine.schemas.agent_schemas import (
    Message,
    MessageType,
    Role,
    RunStatus,
    TextContent,
)

from swe.app.runner.stream_boundary import (
    _normalize_reasoning_boundary_events,
)


def _message(
    *,
    msg_id: str,
    msg_type: str,
    status: str,
    text: str | None = None,
) -> Message:
    content = None
    if text is not None:
        content = [TextContent(text=text, delta=False, index=0)]
    return Message(
        id=msg_id,
        type=msg_type,
        role=Role.ASSISTANT,
        status=status,
        content=content,
    )


def test_reasoning_empty_boundary_becomes_completed_reasoning_event() -> None:
    reasoning = _message(
        msg_id="reason-1",
        msg_type=MessageType.REASONING,
        status=RunStatus.InProgress,
        text="thinking",
    )
    boundary = _message(
        msg_id="boundary-1",
        msg_type=MessageType.MESSAGE,
        status=RunStatus.InProgress,
    )

    events = list(_normalize_reasoning_boundary_events([reasoning, boundary]))

    assert events[0] is reasoning
    assert events[1].id == "reason-1"
    assert events[1].type == MessageType.REASONING
    assert events[1].status == RunStatus.Completed


def test_non_reasoning_message_boundary_is_preserved() -> None:
    message = _message(
        msg_id="msg-1",
        msg_type=MessageType.MESSAGE,
        status=RunStatus.InProgress,
        text="hello",
    )
    next_message = _message(
        msg_id="msg-2",
        msg_type=MessageType.MESSAGE,
        status=RunStatus.InProgress,
    )

    events = list(
        _normalize_reasoning_boundary_events([message, next_message]),
    )

    assert events == [message, next_message]


def test_reasoning_boundary_keeps_following_assistant_message_start() -> None:
    reasoning = _message(
        msg_id="reason-1",
        msg_type=MessageType.REASONING,
        status=RunStatus.InProgress,
        text="thinking",
    )
    answer_start = _message(
        msg_id="answer-1",
        msg_type=MessageType.MESSAGE,
        status=RunStatus.InProgress,
    )

    events = list(
        _normalize_reasoning_boundary_events([reasoning, answer_start]),
    )

    assert len(events) == 3
    assert events[0] is reasoning
    assert events[1].id == "reason-1"
    assert events[1].type == MessageType.REASONING
    assert events[1].status == RunStatus.Completed
    assert events[2] is answer_start

# -*- coding: utf-8 -*-
from __future__ import annotations

import json

import pytest
from agentscope.message import Msg, ToolResultBlock
from agentscope_runtime.adapters.agentscope.stream import (
    adapt_agentscope_message_stream,
)
from agentscope_runtime.engine.schemas.agent_schemas import (
    DataContent,
    Message,
    MessageType,
    Role,
    RunStatus,
    TextContent,
)

from swe.app.runner.stream_boundary import (
    _normalize_reasoning_boundary_events,
    normalize_reasoning_boundary_stream,
)
from swe.agents.tool_failure import (
    TOOL_GOVERNANCE_MESSAGE_METADATA_FIELD,
    build_failed_tool_result_block,
)
from swe.agents.utils import tool_summary


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


@pytest.mark.asyncio
async def test_stream_tool_call_uses_rule_summary_without_model(
    monkeypatch,
) -> None:
    event = Message(
        id="tool-1",
        type=MessageType.FUNCTION_CALL,
        role=Role.ASSISTANT,
        status=RunStatus.InProgress,
        content=[
            DataContent(
                data={
                    "name": "grep_search",
                    "arguments": '{"pattern": "tenant"}',
                },
                delta=False,
                index=0,
            ),
        ],
    )

    async def source():
        yield event

    called = False

    async def unexpected_model_summary(_prompt):
        nonlocal called
        called = True
        return "模型生成的摘要"

    monkeypatch.setattr(
        tool_summary,
        "_run_summary_model",
        unexpected_model_summary,
    )

    events = [
        item async for item in normalize_reasoning_boundary_stream(source())
    ]

    assert called is False
    assert events[0].content[0].data["summary"] == "正在搜索 tenant"
    assert events[0].content[0].data["tool_status"] == "running"
    assert "tool_error" not in events[0].content[0].data


@pytest.mark.asyncio
async def test_stream_tool_output_uses_rule_summary_without_model(
    monkeypatch,
) -> None:
    event = Message(
        id="tool-2",
        type=MessageType.FUNCTION_CALL_OUTPUT,
        role=Role.ASSISTANT,
        status=RunStatus.InProgress,
        content=[
            DataContent(
                data={
                    "name": "grep_search",
                    "output": '["a.py:1", "b.py:2"]',
                },
                delta=False,
                index=0,
            ),
        ],
    )

    async def source():
        yield event

    called = False

    async def unexpected_model_summary(_prompt):
        nonlocal called
        called = True
        return "模型生成的摘要"

    monkeypatch.setattr(
        tool_summary,
        "_run_summary_model",
        unexpected_model_summary,
    )

    events = [
        item async for item in normalize_reasoning_boundary_stream(source())
    ]

    assert called is False
    assert events[0].content[0].data["output_summary"] == "共找到 2 项内容"
    assert events[0].content[0].data["tool_status"] == "success"
    assert events[0].content[0].data["tool_error"] is None


@pytest.mark.asyncio
async def test_stream_tool_output_marks_failed_status() -> None:
    event = Message(
        id="tool-4",
        type=MessageType.FUNCTION_CALL_OUTPUT,
        role=Role.ASSISTANT,
        status=RunStatus.InProgress,
        content=[
            DataContent(
                data={
                    "name": "grep_search",
                    "output": {"error": "permission denied"},
                },
                delta=False,
                index=0,
            ),
        ],
    )

    async def source():
        yield event

    events = [
        item async for item in normalize_reasoning_boundary_stream(source())
    ]

    assert events[0].content[0].data["tool_status"] == "failed"
    assert events[0].content[0].data["tool_error"] == "permission denied"
    assert events[0].content[0].data["output_summary"] == "内容搜索未成功完成"


@pytest.mark.asyncio
async def test_stream_tool_output_marks_structured_json_failure() -> None:
    event = Message(
        id="tool-4b",
        type=MessageType.FUNCTION_CALL_OUTPUT,
        role=Role.ASSISTANT,
        status=RunStatus.InProgress,
        content=[
            DataContent(
                data={
                    "name": "grep_search",
                    "output": (
                        '{"isError": true, "error_type": "permission_denied", '
                        '"content": [{"type": "text", '
                        '"text": "permission denied"}]}'
                    ),
                },
                delta=False,
                index=0,
            ),
        ],
    )

    async def source():
        yield event

    events = [
        item async for item in normalize_reasoning_boundary_stream(source())
    ]

    assert events[0].content[0].data["tool_status"] == "failed"
    assert events[0].content[0].data["tool_error"] == "permission denied"
    assert events[0].content[0].data["output_summary"] == "内容搜索已完成"


@pytest.mark.asyncio
async def test_stream_silent_tool_event_is_filtered_before_status_enrichment() -> (
    None
):
    event = Message(
        id="tool-5",
        type=MessageType.FUNCTION_CALL,
        role=Role.ASSISTANT,
        status=RunStatus.InProgress,
        content=[
            DataContent(
                data={
                    "name": "update_task_progress",
                    "arguments": '{"done": true}',
                },
                delta=False,
                index=0,
            ),
        ],
    )

    async def source():
        yield event

    events = [
        item async for item in normalize_reasoning_boundary_stream(source())
    ]

    assert events == []


async def _collect_events(stream) -> list[Message]:
    return [item async for item in stream]


@pytest.mark.asyncio
async def test_stream_tool_call_attaches_operation_group() -> None:
    event = Message(
        id="tool-group-1",
        type=MessageType.FUNCTION_CALL,
        role=Role.ASSISTANT,
        status=RunStatus.InProgress,
        content=[
            DataContent(
                data={
                    "name": "read_file",
                    "arguments": (
                        '{"file_path": "/tmp/demo.txt", "__swe_operation_group": '
                        '{"id": "inspect", "name": "检查图片、识别文字"}}'
                    ),
                },
                delta=False,
                index=0,
            ),
        ],
    )

    async def source():
        yield event

    events = [
        item async for item in normalize_reasoning_boundary_stream(source())
    ]

    data = events[0].content[0].data
    assert data["operation_group"] == {
        "id": "inspect",
        "title": "检查图片、识别文字",
    }
    assert "__swe_operation_group" not in data["arguments"]


@pytest.mark.asyncio
async def test_stream_tool_call_uses_safe_title_for_unsafe_group_name() -> None:
    event = Message(
        id="tool-group-2",
        type=MessageType.FUNCTION_CALL,
        role=Role.ASSISTANT,
        status=RunStatus.InProgress,
        content=[
            DataContent(
                data={
                    "name": "execute_shell_command",
                    "arguments": (
                        '{"command": "pwd", "__swe_operation_group": '
                        '{"id": "shell", "name": "/tmp/secret"}}'
                    ),
                },
                delta=False,
                index=0,
            ),
        ],
    )

    async def source():
        yield event

    events = [
        item async for item in normalize_reasoning_boundary_stream(source())
    ]

    data = events[0].content[0].data
    assert data["operation_group"]["title"] == "任务操作"
    assert "/tmp/secret" not in json.dumps(data)


@pytest.mark.asyncio
async def test_stream_tool_output_marks_pending_governance() -> None:
    event = Message(
        id="tool-group-3",
        type=MessageType.FUNCTION_CALL_OUTPUT,
        role=Role.ASSISTANT,
        status=RunStatus.InProgress,
        content=[
            DataContent(
                data={
                    "name": "execute_shell_command",
                    "_swe_tool_governance": "pending",
                    "output": {
                        "isError": True,
                        "error_type": "approval_required",
                        "content": [
                            {"type": "text", "text": "risk detected"},
                        ],
                    },
                },
                delta=False,
                index=0,
            ),
        ],
    )

    async def source():
        yield event

    events = [
        item async for item in normalize_reasoning_boundary_stream(source())
    ]

    data = events[0].content[0].data
    assert data["tool_governance"] == "pending"
    assert "_swe_tool_governance" not in data
    assert "tool_status" not in data
    assert "tool_error" not in data
    assert data["output_summary"] == "操作等待审批"


@pytest.mark.asyncio
async def test_agentscope_live_adapter_preserves_pending_governance() -> None:
    async def source():
        block = build_failed_tool_result_block(
            tool_call_id="tool-live-1",
            tool_name="execute_shell_command",
            error_type="approval_required",
            detail="risk detected",
            governance_status="pending",
        )
        msg = Msg(
            "system",
            [ToolResultBlock(**block)],
            "system",
        )
        msg.metadata = {
            TOOL_GOVERNANCE_MESSAGE_METADATA_FIELD: {
                "tool-live-1": "pending",
            },
        }
        yield msg, True

    events = [
        item
        async for item in normalize_reasoning_boundary_stream(
            adapt_agentscope_message_stream(source()),
        )
    ]
    output_event = next(
        item
        for item in events
        if isinstance(item, Message)
        and item.type == MessageType.PLUGIN_CALL_OUTPUT
        and item.content
    )
    data = output_event.content[0].data

    assert data["tool_governance"] == "pending"
    assert "tool_status" not in data
    assert "tool_error" not in data
    assert data["output_summary"] == "操作等待审批"
    assert TOOL_GOVERNANCE_MESSAGE_METADATA_FIELD not in (
        output_event.metadata or {}
    )


@pytest.mark.asyncio
async def test_stream_tool_output_marks_blocked_governance() -> None:
    event = Message(
        id="tool-group-4",
        type=MessageType.FUNCTION_CALL_OUTPUT,
        role=Role.ASSISTANT,
        status=RunStatus.InProgress,
        content=[
            DataContent(
                data={
                    "name": "execute_shell_command",
                    "_swe_tool_governance": "blocked",
                    "output": {
                        "isError": True,
                        "error_type": "tool_guard_denied",
                        "content": [
                            {"type": "text", "text": "denied list"},
                        ],
                    },
                },
                delta=False,
                index=0,
            ),
        ],
    )

    async def source():
        yield event

    events = [
        item async for item in normalize_reasoning_boundary_stream(source())
    ]

    data = events[0].content[0].data
    assert data["tool_governance"] == "blocked"
    assert "_swe_tool_governance" not in data
    assert "tool_status" not in data
    assert "tool_error" not in data
    assert data["output_summary"] == "操作已拦截"

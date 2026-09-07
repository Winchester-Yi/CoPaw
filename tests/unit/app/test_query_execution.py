# -*- coding: utf-8 -*-
"""Contract tests for the deep QueryExecution facade seam."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import Msg
from agentscope_runtime.engine.schemas.agent_schemas import (
    AgentRequest,
    Message,
)

from swe.app.runner.query_execution import (
    QueryExecution,
    QueryFrame,
    QueryInvocation,
)
from swe.app.runner.runner import AgentRunner, _QueryPreflight
from swe.app.answer_turn.models import TurnIdentity


def test_query_handler_context_prepares_trace_identity_metadata(
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    context = runner._prepare_query_handler_context(
        [Msg(name="user", role="user", content="hello")],
        SimpleNamespace(
            session_id="session-1",
            user_id="user-1",
            channel_meta={},
        ),
    )
    assert context.query == "hello"
    assert context.session_id == "session-1"
    assert context.trace_fields is None


class _RecordingAdapter:
    def __init__(self) -> None:
        self.invocations: list[QueryInvocation] = []

    async def stream(self, invocation: QueryInvocation):
        self.invocations.append(invocation)
        yield QueryFrame(
            message=Msg(name="Friday", role="assistant", content="first"),
            last=False,
        )
        yield QueryFrame(
            message=Msg(name="Friday", role="assistant", content="final"),
            last=True,
        )


@pytest.mark.asyncio
async def test_query_handler_preserves_query_execution_frame_order(
    tmp_path,
) -> None:
    """The facade forwards one immutable invocation without reordering frames."""
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    adapter = _RecordingAdapter()
    runner._query_execution = QueryExecution(adapter)
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    messages = [Msg(name="user", role="user", content="hello")]

    frames = [
        frame
        async for frame in runner.query_handler(messages, request=request)
    ]

    assert adapter.invocations == [
        QueryInvocation(request=request, msgs=tuple(messages)),
    ]
    assert [(msg.get_text_content(), last) for msg, last in frames] == [
        ("first", False),
        ("final", True),
    ]


@pytest.mark.asyncio
async def test_live_adapter_runs_admission_without_runner_entry(
    tmp_path,
) -> None:
    """The live Adapter owns admission instead of calling the legacy entry."""
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    runner._prepare_query_preflight = AsyncMock(
        return_value=_QueryPreflight(
            response=Msg(name="Friday", role="assistant", content="blocked"),
        ),
    )

    async def unexpected_legacy_entry(*_args, **_kwargs):
        if _args:
            raise AssertionError("live adapter called runner entry")
        yield Msg(name="Friday", role="assistant", content="unexpected"), True

    runner._stream_query_entry = unexpected_legacy_entry

    frames = [
        frame
        async for frame in runner.query_handler(
            [Msg(name="user", role="user", content="hello")],
            request=request,
        )
    ]

    assert [(msg.get_text_content(), last) for msg, last in frames] == [
        ("blocked", True),
    ]


@pytest.mark.asyncio
async def test_stream_query_settles_after_terminal_response_is_yielded(
    tmp_path,
    monkeypatch,
) -> None:
    order: list[str] = []

    class RecordingCoordinator:
        async def settle(self, _outcome):
            order.append("settle")

    async def fake_stream_frames(self, _msgs, _request, _context):
        order.append("message")
        yield Msg(name="Friday", role="assistant", content="done"), True

    runner = AgentRunner(
        agent_id="test-agent",
        workspace_dir=tmp_path,
        answer_turn_coordinator=RecordingCoordinator(),
    )
    runner._health = True
    monkeypatch.setattr(
        AgentRunner,
        "_stream_query_handler_frames",
        fake_stream_frames,
    )
    identity = TurnIdentity.create(chat_id="chat-1", msgid="msg-1")
    request = AgentRequest(
        input=[
            Message(
                role="user",
                content=[{"type": "text", "text": "hello"}],
            ),
        ],
    )
    request.session_id = "session-1"
    request.user_id = "user-1"
    request.channel = "console"
    request.channel_meta = {"answer_turn_identity": identity}

    events = []
    async for event in runner.stream_query(request):
        if event.object == "response" and event.status == "completed":
            order.append("response")
        events.append(event)

    assert events[-1].object == "response"
    assert events[-1].status == "completed"
    assert order == ["message", "response", "settle"]


@pytest.mark.asyncio
async def test_stream_query_settles_failed_when_wrapper_processing_raises(
    tmp_path,
    monkeypatch,
) -> None:
    outcomes: list[str] = []

    class RecordingCoordinator:
        async def settle(self, outcome):
            outcomes.append(outcome.status.value)

    async def fake_stream_frames(self, _msgs, _request, _context):
        yield Msg(name="Friday", role="assistant", content="done"), True

    runner = AgentRunner(
        agent_id="test-agent",
        workspace_dir=tmp_path,
        answer_turn_coordinator=RecordingCoordinator(),
    )
    runner._health = True
    monkeypatch.setattr(
        AgentRunner,
        "_stream_query_handler_frames",
        fake_stream_frames,
    )
    monkeypatch.setattr(
        runner,
        "_attach_trace_id_to_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("trace attachment failed"),
        ),
    )
    identity = TurnIdentity.create(chat_id="chat-1", msgid="msg-1")
    request = AgentRequest(
        input=[
            Message(
                role="user",
                content=[{"type": "text", "text": "hello"}],
            ),
        ],
    )
    request.session_id = "session-1"
    request.user_id = "user-1"
    request.channel = "console"
    request.channel_meta = {"answer_turn_identity": identity}

    with pytest.raises(RuntimeError, match="trace attachment failed"):
        async for _event in runner.stream_query(request):
            pass

    assert outcomes == ["failed"]

# -*- coding: utf-8 -*-
"""Runtime contract tests for independent Tool execution spans."""

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from swe.agents.tool_guard_mixin import ToolGuardMixin
from swe.observability.config import EmitterConfig
from swe.observability.context import bind_context
from swe.observability.emitter import JsonlEmitter
from swe.observability.models import ObservabilityContext, Scope


class _ToolExecutor:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    async def _acting(self, tool_call):
        self.calls.append(tool_call)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if callable(self.outcome):
            return await self.outcome(tool_call)
        return self.outcome


class _ObservedToolAgent(ToolGuardMixin, _ToolExecutor):
    def _resolve_mcp_server(self, _tool_name):
        return getattr(self, "mcp_server", None)

    def _tool_has_specific_timeout(self, _tool_name):
        return True


def _events(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def _assert_tool_asset(events: list[dict], *, span_id: str, tool_name: str):
    assets = [event for event in events if event["event_name"] == "asset.used"]
    assert len(assets) == 1
    assert assets[0]["parent_obs_span_id"] == span_id
    assert assets[0]["asset"] == {
        "asset_type": "tool",
        "asset_id": tool_name,
        "asset_version": "unknown",
        "asset_source": "runtime_registered",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "span_status", "technical_status", "has_error"),
    [
        (
            {"content": "safe result"},
            "succeeded",
            "OK",
            False,
        ),
        (
            {"isError": True, "error_type": "permission_denied"},
            "failed",
            "OK",
            False,
        ),
    ],
)
async def test_tool_execution_preserves_business_result_and_projects_outcome(
    monkeypatch,
    outcome,
    span_status,
    technical_status,
    has_error,
):
    from swe.observability import instrumentation

    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(instrumentation, "default_emitter", lambda: emitter)
    agent = _ObservedToolAgent(outcome)
    agent._observability_step_index = 4
    tool_call = {"id": "call-1", "name": "read_file", "input": {}}
    context = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("swe", "source-a", "tenant-a", "agent-a"),
    ).new_attempt()

    with bind_context(context):
        result = await agent._run_tool_call_with_hard_timeout_impl(
            tool_call,
            "read_file",
            {},
        )
    emitter.close()
    events = _events(stream)

    assert result is outcome
    assert agent.calls == [tool_call]
    span_events = [
        event for event in events if event["event_name"].startswith("span.")
    ]
    assert [event["event_name"] for event in span_events] == [
        "span.started",
        "span.ended",
    ]
    assert span_events[0]["tool"]["tool_key"] == "read_file"
    assert span_events[0]["tool"]["tool_call_id"] == "call-1"
    assert span_events[0]["step_index"] == 4
    assert span_events[1]["status"] == span_status
    assert span_events[1]["technical_status"] == technical_status
    assert span_events[1]["has_error"] is has_error
    assert span_events[1].get("error_type") == outcome.get("error_type")
    _assert_tool_asset(
        events,
        span_id=span_events[0]["obs_span_id"],
        tool_name="read_file",
    )
    assert "private tool input" not in json.dumps(events)


@pytest.mark.asyncio
async def test_mcp_tool_assets_capture_snapshot_identity_without_configuration(
    monkeypatch,
):
    from swe.observability import instrumentation

    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(instrumentation, "default_emitter", lambda: emitter)
    agent = _ObservedToolAgent({"content": "private tool output"})
    agent.mcp_server = "runtime-mcp-name"
    agent._request_context = {
        "subagent_mcp_server_keys": {"runtime-mcp-name": "search_provider"},
        "mcp_config": {"headers": {"Authorization": "secret-value"}},
    }
    context = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("swe", "source-a", "tenant-a", "agent-a"),
    ).new_attempt()

    with bind_context(context):
        await agent._run_tool_call_with_hard_timeout_impl(
            {
                "id": "call-mcp",
                "name": "search",
                "input": {"query": "private tool input"},
            },
            "search",
            {"query": "private tool input"},
        )
    emitter.close()
    events = _events(stream)
    span_id = next(
        event["obs_span_id"]
        for event in events
        if event["event_name"] == "span.started"
    )
    assets = [event for event in events if event["event_name"] == "asset.used"]

    assert [event["asset"] for event in assets] == [
        {
            "asset_type": "tool",
            "asset_id": "search",
            "asset_version": "unknown",
            "asset_source": "runtime_registered",
        },
        {
            "asset_type": "mcp_server",
            "asset_id": "search_provider",
            "asset_version": "unknown",
            "asset_source": "runtime_snapshot",
        },
    ]
    assert all(event["parent_obs_span_id"] == span_id for event in assets)
    event_text = json.dumps(events)
    for private_value in (
        "private tool input",
        "private tool output",
        "Authorization",
        "secret-value",
        "mcp_config",
    ):
        assert private_value not in event_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status", "technical_status"),
    [
        (RuntimeError("private tool error"), "failed", "ERROR"),
        (asyncio.CancelledError(), "cancelled", None),
    ],
)
async def test_tool_execution_records_exception_and_preserves_propagation(
    monkeypatch,
    error,
    expected_status,
    technical_status,
):
    from swe.observability import instrumentation

    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(instrumentation, "default_emitter", lambda: emitter)
    agent = _ObservedToolAgent(error)
    context = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("swe", "source-a", "tenant-a", "agent-a"),
    ).new_attempt()

    with bind_context(context):
        with pytest.raises(type(error)):
            await agent._run_tool_call_with_hard_timeout_impl(
                {"id": "call-2", "name": "read_file", "input": {}},
                "read_file",
                {},
            )
    emitter.close()
    events = _events(stream)

    span_events = [
        event for event in events if event["event_name"].startswith("span.")
    ]
    assert [event["event_name"] for event in span_events] == [
        "span.started",
        "span.ended",
    ]
    assert span_events[1]["status"] == expected_status
    assert span_events[1].get("technical_status") == technical_status
    if technical_status is not None:
        assert span_events[1]["error_type"] == type(error).__name__
    _assert_tool_asset(
        events,
        span_id=span_events[0]["obs_span_id"],
        tool_name="read_file",
    )
    assert "private tool error" not in stream.getvalue()


@pytest.mark.asyncio
async def test_parallel_tool_executions_keep_distinct_span_identities(
    monkeypatch,
):
    from swe.observability import instrumentation

    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(instrumentation, "default_emitter", lambda: emitter)

    class ConcurrentToolExecutor(_ObservedToolAgent):
        def __init__(self):
            self.ready = asyncio.Event()
            super().__init__(self._result)

        async def _result(self, tool_call):
            if len(self.calls) == 2:
                self.ready.set()
            await self.ready.wait()
            return {"content": tool_call["id"]}

    agent = ConcurrentToolExecutor()
    context = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("swe", "source-a", "tenant-a", "agent-a"),
    ).new_attempt()
    calls = [
        {"id": "parallel-call-a", "name": "read_file", "input": {}},
        {"id": "parallel-call-b", "name": "write_file", "input": {}},
    ]

    with bind_context(context):
        results = await asyncio.gather(
            *(
                agent._run_tool_call_with_hard_timeout_impl(
                    tool_call,
                    tool_call["name"],
                    {},
                )
                for tool_call in calls
            ),
        )
    assert emitter.close(timeout=1) == 0
    events = _events(stream)
    started = {
        event["tool"]["tool_call_id"]: event
        for event in events
        if event["event_name"] == "span.started"
    }
    ended = {
        event["tool"]["tool_call_id"]: event
        for event in events
        if event["event_name"] == "span.ended"
    }

    assert results == [
        {"content": "parallel-call-a"},
        {"content": "parallel-call-b"},
    ]
    assert len(agent.calls) == 2
    assert set(started) == {"parallel-call-a", "parallel-call-b"}
    assert set(ended) == set(started)
    execution_ids = {
        event["tool"]["tool_execution_id"] for event in started.values()
    }
    assert len(execution_ids) == 2
    assert all(event["status"] == "succeeded" for event in ended.values())


@pytest.mark.asyncio
async def test_tool_hard_timeout_records_controlled_failure(monkeypatch):
    from swe.agents import tool_guard_mixin
    from swe.observability import instrumentation

    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(instrumentation, "default_emitter", lambda: emitter)
    monkeypatch.setattr(
        tool_guard_mixin,
        "LOCAL_TOOL_EXECUTION_HARD_TIMEOUT",
        0.01,
    )

    async def slow_result(_tool_call):
        await asyncio.sleep(0.05)
        return {"content": "too late"}

    class GenericTimeoutAgent(_ObservedToolAgent):
        def _tool_has_specific_timeout(self, _tool_name):
            return False

        async def _persist_local_tool_timeout_result(
            self,
            _tool_call_id,
            _tool_name,
            _timeout_text,
        ):
            return None

        def _extract_current_tool_response(
            self,
            _tool_call_id,
            *,
            include_structured_failure=False,
        ):
            assert include_structured_failure
            return {"isError": True, "error_type": "tool_timeout"}

    agent = GenericTimeoutAgent(slow_result)
    context = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("swe", "source-a", "tenant-a", "agent-a"),
    ).new_attempt()
    tool_call = {"id": "timeout-call", "name": "local_tool", "input": {}}

    with bind_context(context):
        result = await agent._run_tool_call_with_hard_timeout_impl(
            tool_call,
            "local_tool",
            {},
        )
    assert emitter.close(timeout=1) == 0
    events = _events(stream)

    assert result is None
    assert len(agent.calls) == 1
    span_events = [
        event for event in events if event["event_name"].startswith("span.")
    ]
    assert [event["event_name"] for event in span_events] == [
        "span.started",
        "span.ended",
    ]
    assert span_events[-1]["status"] == "timed_out"
    assert span_events[-1]["technical_status"] == "ERROR"
    assert span_events[-1]["error_type"] == "tool_timeout"
    _assert_tool_asset(
        events,
        span_id=span_events[0]["obs_span_id"],
        tool_name="local_tool",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected_status"),
    [
        ("auto_denied", "rejected"),
        ("needs_approval", "waiting_approval"),
    ],
)
async def test_guard_short_circuit_emits_guard_span_without_tool_execution(
    monkeypatch,
    decision,
    expected_status,
):
    from swe.observability import instrumentation

    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(instrumentation, "default_emitter", lambda: emitter)

    class DenyingAgent(_ObservedToolAgent):
        def __init__(self):
            super().__init__({"content": "must not execute"})
            self._tool_guard_lock = asyncio.Lock()
            self.executed_guard_action = False

        def _ensure_tool_guard(self):
            return None

        async def _acting_shell_installation_denial(self, *_args):
            return False

        async def _acting_policy_denial(self, *_args, **_kwargs):
            return False

        async def _apply_pre_tool_hook(self, tool_call, tool_name, tool_input):
            return tool_call, tool_input, False, None

        async def _notify_skill_detector_tool_call(self, *_args):
            return None

        async def _decide_guard_action(self, tool_call):
            return SimpleNamespace(
                kind=decision,
                tool_name=tool_call["name"],
                tool_input=tool_call["input"],
                guard_result=None,
            )

        async def _execute_guard_action(self, _action, _tool_call):
            self.executed_guard_action = True
            return {"isError": True, "error_type": "tool_guard_denied"}

        async def _emit_tool_trace_start(self, *_args):
            return ""

        async def _emit_tool_trace_end(self, *_args, **_kwargs):
            return None

    agent = DenyingAgent()
    context = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("swe", "source-a", "tenant-a", "agent-a"),
    ).new_attempt()

    with bind_context(context):
        result = await agent._acting_impl(
            {"id": "call-denied", "name": "read_file", "input": {}},
        )
    emitter.close()
    events = _events(stream)

    assert result["error_type"] == "tool_guard_denied"
    assert not agent.calls
    assert agent.executed_guard_action
    assert [event["event_name"] for event in events] == [
        "span.started",
        "span.ended",
    ]
    assert all(event["span_type"] == "Guard" for event in events)
    assert events[1]["status"] == expected_status
    assert events[1]["technical_status"] == "OK"
    assert events[1]["has_error"] is False
    from swe.observability.projector import _validate_payload

    for event in events:
        _validate_payload(json.dumps(event))


@pytest.mark.asyncio
async def test_tool_span_is_bound_only_during_tool_execution(monkeypatch):
    from swe.observability import instrumentation

    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(instrumentation, "default_emitter", lambda: emitter)

    async def record_evidence(tool_call):
        assert instrumentation.emit_tool_output_evidence(
            chat_id="chat-1",
            message_id="tool-message-1",
            occurrence_id=0,
        )
        return tool_call

    agent = _ObservedToolAgent(record_evidence)
    context = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("swe", "source-a", "tenant-a", "agent-a"),
    ).new_attempt()

    with bind_context(context):
        result = await agent._run_tool_call_with_hard_timeout_impl(
            {"id": "call-evidence", "name": "read_file", "input": {}},
            "read_file",
            {},
        )
        assert not instrumentation.emit_tool_output_evidence(
            chat_id="chat-1",
            message_id="outside-tool-call",
        )
    emitter.close()
    events = _events(stream)
    tool_span = next(
        event for event in events if event["event_name"] == "span.started"
    )
    evidence = next(
        event
        for event in events
        if event.get("event_type") == "evidence.linked"
    )

    assert result["id"] == "call-evidence"
    assert evidence["obs_span_id"] == tool_span["obs_span_id"]
    assert evidence["chat_id"] == "chat-1"
    assert evidence["message_id"] == "tool-message-1"
    assert evidence["occurrence_id"] == 0
    assert evidence["content_type"] == "tool_output"
    assert evidence["saved_state"] == "available"


class _PolicyDeniedAgent(_ObservedToolAgent):
    def __init__(self):
        super().__init__({"content": "must not execute"})
        self._tool_guard_lock = asyncio.Lock()
        self._request_context = {}

    def _ensure_tool_guard(self):
        return None

    async def _acting_shell_installation_denial(self, *_args):
        return False

    async def _acting_policy_denial(self, *_args, **_kwargs):
        return True


@pytest.mark.asyncio
async def test_policy_short_circuit_records_guard_reason(monkeypatch):
    """策略短路也要留下 Guard 事实与 reason_code。"""
    from swe.observability import instrumentation

    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(instrumentation, "default_emitter", lambda: emitter)
    agent = _PolicyDeniedAgent()
    context = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("swe", "source-a", "tenant-a", "agent-a"),
    ).new_attempt()

    with bind_context(context):
        result = await agent._acting_impl(
            {"id": "call-policy", "name": "read_file", "input": {}},
        )
    emitter.close()
    events = _events(stream)

    assert result is None
    assert [event["event_name"] for event in events] == [
        "span.started",
        "span.ended",
    ]
    assert events[0]["span_type"] == "Guard"
    assert events[1]["status"] == "rejected"
    assert events[1]["reason_code"] == "policy_denied"


@pytest.mark.asyncio
async def test_subagent_needs_approval_records_rejected(monkeypatch):
    """SubAgent 的 needs_approval 会被自动拒绝，Guard 不能记成等待审批。"""
    from swe.observability import instrumentation

    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(instrumentation, "default_emitter", lambda: emitter)

    class _SubagentAgent(_ObservedToolAgent):
        def __init__(self):
            super().__init__({"content": "must not execute"})
            self._tool_guard_lock = asyncio.Lock()
            self._request_context = {"agent_role": "subagent"}

        def _ensure_tool_guard(self):
            return None

        async def _acting_shell_installation_denial(self, *_args):
            return False

        async def _acting_policy_denial(self, *_args, **_kwargs):
            return False

        async def _apply_pre_tool_hook(
            self,
            tool_call,
            tool_name,
            tool_input,
        ):
            return tool_call, tool_input, False, None

        async def _notify_skill_detector_tool_call(self, *_args):
            return None

        async def _decide_guard_action(self, tool_call):
            return SimpleNamespace(
                kind="needs_approval",
                tool_name=tool_call["name"],
                tool_input=tool_call["input"],
                guard_result=None,
            )

        async def _execute_guard_action(self, _action, _tool_call):
            return {"isError": True, "error_type": "tool_guard_denied"}

        async def _emit_tool_trace_start(self, *_args):
            return ""

        async def _emit_tool_trace_end(self, *_args, **_kwargs):
            return None

    agent = _SubagentAgent()
    context = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("swe", "source-a", "tenant-a", "agent-a"),
    ).new_attempt()

    with bind_context(context):
        await agent._acting_impl(
            {"id": "call-subagent", "name": "read_file", "input": {}},
        )
    emitter.close()
    events = _events(stream)

    assert events[0]["span_type"] == "Guard"
    assert events[1]["status"] == "rejected"


@pytest.mark.asyncio
async def test_unregistered_tool_is_marked_without_asset(monkeypatch):
    """模型编造的工具名标为 unregistered，且不能声称已注册资产。"""
    from swe.observability import instrumentation

    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(instrumentation, "default_emitter", lambda: emitter)
    agent = _ObservedToolAgent({"content": "ok"})
    agent.toolkit = SimpleNamespace(tools={"read_file": object()})
    context = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("swe", "source-a", "tenant-a", "agent-a"),
    ).new_attempt()

    with bind_context(context):
        await agent._run_tool_call_with_hard_timeout_impl(
            {"id": "call-ghost", "name": "ghost_tool", "input": {}},
            "ghost_tool",
            {},
        )
    emitter.close()
    events = _events(stream)

    assert events[0]["tool"]["executor_kind"] == "unregistered"
    assert not [
        event for event in events if event["event_name"] == "asset.used"
    ]

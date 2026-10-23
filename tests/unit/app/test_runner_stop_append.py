# -*- coding: utf-8 -*-
"""Post-approval append delivery and its durable Stop boundary."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from agentscope.message import Msg
from agentscope.memory import InMemoryMemory
from agentscope_runtime.adapters.agentscope.stream import (
    adapt_agentscope_message_stream,
)
from agentscope_runtime.engine.schemas.agent_schemas import (
    Message,
    MessageType,
    RunStatus,
)

from swe.agents.hook_runtime.models import (
    HookConfig,
    HookDecision,
    HookSessionOverlay,
    MergedHookResult,
    StopAppendContribution,
)
from swe.app.answer_turn.coordinator import AnswerTurnCoordinator
from swe.app.answer_turn.in_memory import (
    InMemoryApproval,
    InMemoryExecution,
    InMemoryGoal,
    InMemorySession,
    InMemoryStream,
    InMemorySubagent,
)
from swe.app.answer_turn.models import TurnIdentity, TurnOutcome, TurnStatus
from swe.app.runner.runner import AgentRunner, _QueryTurnOutcome
from swe.app.runner.query_attempt import _stream_retry_attempt
from swe.app.runner.session import SafeJSONSession
from swe.tracing.models import TraceStatus
from swe.app.runner.turn_lifecycle import (
    _stream_goal_finalization,
    _stream_standard_completion_lifecycle,
    _resolve_stop_gate,
    _resolve_goal_finalization_stop_gate,
)


async def _setup(tmp_path):
    coordinator = AnswerTurnCoordinator(
        stream=InMemoryStream(),
        execution=InMemoryExecution(),
        session=InMemorySession(),
        goal=InMemoryGoal(),
        subagent=InMemorySubagent(),
        approval=InMemoryApproval(),
    )
    lease = await coordinator.start_or_attach(
        "chat",
        {},
        AsyncMock(),
        msgid="request",
    )
    owner = object.__new__(AgentRunner)
    owner._answer_turn_coordinator = coordinator
    owner.workspace_dir = tmp_path
    owner.tenant_id = "default"
    owner.agent_id = "test"
    memory = InMemoryMemory()
    agent = SimpleNamespace(memory=memory)
    execution = SimpleNamespace(state={})
    runtime = SimpleNamespace(
        agent=agent,
        session_execution=execution,
        session_id="s",
        user_id="u",
        skip_history=False,
        tenant_hooks=HookConfig(enabled=True),
        agent_config=SimpleNamespace(),
        hook_overlay=HookSessionOverlay(),
    )
    request = SimpleNamespace(
        channel="console",
        session_id="s",
        user_id="u",
        trace_id=None,
        channel_meta={"answer_turn_identity": lease.identity},
    )
    owner.save_job_session_state = AsyncMock(return_value=True)
    outcome = _QueryTurnOutcome(assistant_response="original")
    return owner, runtime, request, outcome, lease.identity


def _contributions():
    return [
        StopAppendContribution(
            source="tenant",
            group_id="g",
            handler_id="h",
            order=0,
            text="  **exact**\n\nMarkdown  ",
        ),
    ]


@pytest.mark.asyncio
async def test_coordinator_commit_serializes_with_stop(tmp_path):
    owner, _, _, _, identity = await _setup(tmp_path)
    coordinator = owner._answer_turn_coordinator
    commit_message = getattr(coordinator, "commit_message", None)
    assert callable(
        commit_message,
    ), "coordinator needs atomic message admission"
    entered, release = asyncio.Event(), asyncio.Event()

    async def persist():
        entered.set()
        await release.wait()

    committing = asyncio.create_task(commit_message(identity, persist))
    await entered.wait()
    stopping = asyncio.create_task(
        coordinator.claim_stop(identity, msgid="request"),
    )
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    assert await committing is True
    assert (await stopping).accepted
    callback = AsyncMock()
    assert await commit_message(identity, callback) is False
    callback.assert_not_called()
    await coordinator.settle(TurnOutcome.cancelled(identity))


@pytest.mark.asyncio
@pytest.mark.parametrize("goal", [False, True])
@pytest.mark.parametrize("buffered", [False, True])
async def test_final_body_precedes_append_only_after_allow(
    tmp_path,
    goal,
    buffered,
):
    owner, runtime, request, outcome, _ = await _setup(tmp_path)
    plan = SimpleNamespace(original_user_message="question", turn_msgs=[])
    owner._resolve_max_stop_turns = lambda _: 2
    owner._requires_stop_output_buffer = lambda **_: buffered
    owner._should_stop_follow_up = lambda _: True
    owner._build_stop_follow_up_msg = lambda _: Msg("Friday", "retry", "user")
    gate_calls = 0

    async def gate(**kwargs):
        nonlocal gate_calls
        gate_calls += 1
        kwargs["outcome"].stop_hook_active = True
        return MergedHookResult(
            decision=(
                HookDecision.BLOCK if gate_calls == 1 else HookDecision.ALLOW
            ),
            reason="retry" if gate_calls == 1 else "ok",
        )

    async def turns(**_):
        outcome.assistant_response = "original"
        msg = Msg("Friday", "original", "assistant")
        if buffered and not goal:
            outcome.buffered_assistant_messages[:] = [msg]
        else:
            yield msg, True

    events = []

    async def append(**_):
        assert gate_calls == 2
        assert events[-1] == "original"
        events.append("append called")
        yield Msg("Friday", "append", "assistant"), True

    owner._emit_stop_hook_if_needed = gate
    owner._stream_agent_turns = turns
    owner._stream_goal_finalization_turn = turns
    owner._stream_stop_appended_messages = append
    stream = (
        _stream_goal_finalization
        if goal
        else _stream_standard_completion_lifecycle
    )
    kwargs = {
        "request": request,
        "runtime": runtime,
        "plan": plan,
        "outcome": outcome,
    }
    if goal:
        kwargs["settled"] = SimpleNamespace()
    async for msg, _ in stream(owner, **kwargs):
        events.append(msg.get_text_content())
    assert events[-2:] == ["append called", "append"]
    assert outcome.assistant_response == "original"


@pytest.mark.asyncio
async def test_append_commits_before_publish_and_enters_real_memory(
    tmp_path,
    monkeypatch,
):
    owner, runtime, request, outcome, identity = await _setup(tmp_path)
    method = getattr(owner, "_stream_stop_appended_messages", None)
    assert callable(method), "runner needs durable append phase"
    outcome.stop_append_approved = True
    emitter = AsyncMock(return_value=_contributions())
    monkeypatch.setattr(
        "swe.app.runner.runner._build_stop_hook_runtime",
        lambda **_: SimpleNamespace(emit_stop_append=emitter),
    )
    plan = SimpleNamespace(original_user_message="question")
    result = [
        item
        async for item in method(
            request=request,
            runtime=runtime,
            plan=plan,
            outcome=outcome,
        )
    ]
    owner.save_job_session_state.assert_awaited_once()
    msg, last = result[0]
    assert str(UUID(msg.id)) == msg.id
    assert last is True
    assert msg.get_text_content() == _contributions()[0].text
    assert msg.role == "assistant"
    marker = msg.metadata["stop_append"]
    assert marker == {
        "message_id": msg.id,
        "chat_id": identity.chat_id,
        "msgid": identity.msgid,
        "turn_id": identity.turn_id,
        "source": "tenant",
        "group_id": "g",
        "handler_id": "h",
        "order": 0,
    }
    assert (await runtime.agent.memory.get_memory())[-1].id == msg.id
    assert outcome.assistant_response == "original"
    assert [
        item
        async for item in method(
            request=request,
            runtime=runtime,
            plan=plan,
            outcome=outcome,
        )
    ] == []
    assert emitter.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_second_save", [False, True])
async def test_live_adapter_publishes_each_append_before_next_save(
    tmp_path,
    monkeypatch,
    fail_second_save,
):
    owner, runtime, request, outcome, _ = await _setup(tmp_path)
    outcome.stop_append_approved = True
    contributions = _contributions() + [
        _contributions()[0].model_copy(
            update={"handler_id": "second", "order": 1, "text": "第二条补充"},
        ),
    ]
    monkeypatch.setattr(
        "swe.app.runner.runner._build_stop_hook_runtime",
        lambda **_: SimpleNamespace(
            emit_stop_append=AsyncMock(return_value=contributions),
        ),
    )
    owner.save_job_session_state.side_effect = [True, not fail_second_save]
    source = owner._stream_stop_appended_messages(
        request=request,
        runtime=runtime,
        plan=SimpleNamespace(original_user_message="q"),
        outcome=outcome,
    )
    delivered = []
    save_counts = []
    expected_error = (
        pytest.raises(RuntimeError, match="Stop append persistence failed")
        if fail_second_save
        else nullcontext()
    )
    with expected_error:
        async for event in adapt_agentscope_message_stream(source):
            if (
                isinstance(event, Message)
                and event.type == MessageType.MESSAGE
                and event.status == RunStatus.Completed
            ):
                delivered.append(event)
                save_counts.append(owner.save_job_session_state.await_count)
    expected = contributions[:1] if fail_second_save else contributions
    assert [event.content[0].text for event in delivered] == [
        item.text for item in expected
    ]
    assert save_counts == list(range(1, len(expected) + 1))
    assert [
        event.metadata["stop_append"]["handler_id"] for event in delivered
    ] == [item.handler_id for item in expected]
    persisted = await runtime.agent.memory.get_memory()
    assert [
        event.metadata["stop_append"]["message_id"] for event in delivered
    ] == [msg.id for msg in persisted]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, TimeoutError(), ConnectionError()])
async def test_failed_storage_rolls_back_only_unsaved_append(
    tmp_path,
    monkeypatch,
    failure,
):
    owner, runtime, request, outcome, _ = await _setup(tmp_path)
    method = getattr(owner, "_stream_stop_appended_messages", None)
    assert callable(method), "runner needs durable append phase"
    outcome.stop_append_approved = True
    original = Msg("Friday", "original", "assistant")
    await runtime.agent.memory.add(original)
    emitter = AsyncMock(return_value=_contributions())
    monkeypatch.setattr(
        "swe.app.runner.runner._build_stop_hook_runtime",
        lambda **_: SimpleNamespace(emit_stop_append=emitter),
    )
    owner.save_job_session_state = AsyncMock(
        return_value=failure if failure is False else True,
        side_effect=failure if failure is not False else None,
    )
    with pytest.raises(RuntimeError, match="Stop append persistence failed"):
        async for _ in method(
            request=request,
            runtime=runtime,
            plan=SimpleNamespace(original_user_message="q"),
            outcome=outcome,
        ):
            pytest.fail("unsaved append was published")
    assert [m.id for m in await runtime.agent.memory.get_memory()] == [
        original.id,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("secret append"),
        ConnectionError("secret append"),
        RuntimeError("secret append"),
    ],
)
@pytest.mark.parametrize("trace_failure", [False, True])
async def test_any_exception_after_append_phase_cannot_retry_or_dump_body(
    exc,
    trace_failure,
):
    async def no_items(**_):
        for item in ():
            yield item

    outcome = _QueryTurnOutcome()

    async def single_attempt(**_):
        outcome.stop_append_started = True
        yield Msg("Friday", "append", "assistant"), True
        raise exc

    owner = SimpleNamespace(
        _stream_retry_backoff_notice=no_items,
        _stream_single_query_attempt=single_attempt,
        _should_retry=lambda *_: True,
        _raise_console_model_call_failed_if_needed=AsyncMock(),
        _handle_query_error=AsyncMock(),
        _end_trace_if_needed=AsyncMock(
            side_effect=(
                RuntimeError("secret trace body") if trace_failure else None
            ),
        ),
        _stream_retryable_query_error=no_items,
    )
    with pytest.raises(
        RuntimeError,
        match="Stop append finalization failed",
    ) as caught:
        async for _ in _stream_retry_attempt(
            owner,
            retry_attempt=0,
            max_retry_attempts=3,
            max_retries=2,
            retry_state=SimpleNamespace(),
            attempt_state=SimpleNamespace(runtime=None),
            attempt_input=None,
            outcome=outcome,
            trace_id="trace-append",
            request=SimpleNamespace(),
            session_execution=None,
            session_id="s",
            backoff_base=0,
            backoff_cap=0,
        ):
            pass
    assert "secret append" not in str(caught.value)
    assert "secret trace body" not in str(caught.value)
    owner._end_trace_if_needed.assert_awaited_once_with(
        "trace-append",
        TraceStatus.ERROR,
        error="Stop append finalization failed",
    )
    owner._raise_console_model_call_failed_if_needed.assert_not_called()
    owner._handle_query_error.assert_not_called()


@pytest.mark.asyncio
async def test_real_trace_closure_failure_logs_category_without_private_body(
    monkeypatch,
):
    owner = object.__new__(AgentRunner)
    backend = SimpleNamespace(
        end_trace=AsyncMock(side_effect=RuntimeError("private append body")),
    )
    records = []
    monkeypatch.setattr(
        "swe.app.runner.runner.has_trace_manager",
        lambda: True,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.get_trace_manager",
        lambda: backend,
    )
    monkeypatch.setattr("swe.tracing.get_current_trace", lambda: None)
    monkeypatch.setattr(
        "swe.app.runner.runner.logger.warning",
        lambda message, *args: records.append(message % args),
    )
    await owner._end_trace_if_needed(
        "trace-append",
        TraceStatus.ERROR,
        error="Stop append finalization failed",
    )
    backend.end_trace.assert_awaited_once_with(
        "trace-append",
        status=TraceStatus.ERROR,
        error="Stop append finalization failed",
    )
    diagnostic = "\n".join(records)
    assert "RuntimeError" in diagnostic
    assert "private append body" not in diagnostic


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "skip",
    [
        "channel",
        "empty",
        "blocked",
        "plan",
        "pretool",
        "fallback",
        "disabled",
        "identity",
        "coordinator",
        "execution",
        "stopping",
    ],
)
async def test_ineligible_paths_never_execute_append_scripts(
    tmp_path,
    monkeypatch,
    skip,
):
    owner, runtime, request, outcome, identity = await _setup(tmp_path)
    outcome.stop_append_approved = True
    if skip == "channel":
        request.channel = "dingtalk"
    elif skip == "empty":
        outcome.assistant_response = "  "
    elif skip == "disabled":
        outcome.stop_append_approved = False
    elif skip == "identity":
        request.channel_meta = {}
    elif skip == "coordinator":
        owner._answer_turn_coordinator = None
    elif skip == "execution":
        runtime.session_execution = None
    elif skip == "stopping":
        await owner._answer_turn_coordinator.claim_stop(
            identity,
            msgid="request",
        )
    else:
        field = {
            "blocked": "completion_blocked",
            "plan": "plan_interaction_turn_boundary",
            "pretool": "pre_tool_terminal_stop",
            "fallback": "goal_finalization_fallback",
        }[skip]
        setattr(outcome, field, True)
    builder = AsyncMock()
    monkeypatch.setattr(
        "swe.app.runner.runner._build_stop_hook_runtime",
        builder,
    )
    assert [
        item
        async for item in owner._stream_stop_appended_messages(
            request=request,
            runtime=runtime,
            plan=SimpleNamespace(original_user_message="q"),
            outcome=outcome,
        )
    ] == []
    builder.assert_not_called()
    if skip == "stopping":
        await owner._answer_turn_coordinator.settle(
            TurnOutcome.cancelled(identity),
        )


@pytest.mark.asyncio
async def test_stop_during_collection_discards_uncommitted_results(
    tmp_path,
    monkeypatch,
):
    owner, runtime, request, outcome, identity = await _setup(tmp_path)
    outcome.stop_append_approved = True

    async def collect(*_, stop_requested, **__):
        assert await stop_requested() is False
        await owner._answer_turn_coordinator.claim_stop(
            identity,
            msgid="request",
        )
        assert await stop_requested() is True
        return _contributions()

    monkeypatch.setattr(
        "swe.app.runner.runner._build_stop_hook_runtime",
        lambda **_: SimpleNamespace(emit_stop_append=collect),
    )
    assert [
        item
        async for item in owner._stream_stop_appended_messages(
            request=request,
            runtime=runtime,
            plan=SimpleNamespace(original_user_message="q"),
            outcome=outcome,
        )
    ] == []
    owner.save_job_session_state.assert_not_called()
    await owner._answer_turn_coordinator.settle(
        TurnOutcome.cancelled(identity),
    )


@pytest.mark.asyncio
async def test_stop_after_commit_keeps_first_message_and_discards_remaining(
    tmp_path,
    monkeypatch,
):
    owner, runtime, request, outcome, identity = await _setup(tmp_path)
    outcome.stop_append_approved = True
    contributions = _contributions() + [
        _contributions()[0].model_copy(
            update={"handler_id": "second", "order": 1},
        ),
    ]
    monkeypatch.setattr(
        "swe.app.runner.runner._build_stop_hook_runtime",
        lambda **_: SimpleNamespace(
            emit_stop_append=AsyncMock(return_value=contributions),
        ),
    )
    stream = owner._stream_stop_appended_messages(
        request=request,
        runtime=runtime,
        plan=SimpleNamespace(original_user_message="q"),
        outcome=outcome,
    )
    first, _ = await stream.__anext__()
    await owner._answer_turn_coordinator.claim_stop(identity, msgid="request")
    assert [item async for item in stream] == []
    assert [msg.id for msg in await runtime.agent.memory.get_memory()] == [
        first.id,
    ]
    owner.save_job_session_state.assert_awaited_once()
    await owner._answer_turn_coordinator.settle(
        TurnOutcome.cancelled(identity),
    )


@pytest.mark.asyncio
async def test_empty_contributions_create_no_message_and_do_not_rerun(
    tmp_path,
    monkeypatch,
):
    owner, runtime, request, outcome, _ = await _setup(tmp_path)
    outcome.stop_append_approved = True
    emitter = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "swe.app.runner.runner._build_stop_hook_runtime",
        lambda **_: SimpleNamespace(emit_stop_append=emitter),
    )
    for _ in range(2):
        assert [
            item
            async for item in owner._stream_stop_appended_messages(
                request=request,
                runtime=runtime,
                plan=SimpleNamespace(original_user_message="q"),
                outcome=outcome,
            )
        ] == []
    assert emitter.await_count == 1
    assert await runtime.agent.memory.get_memory() == []
    owner.save_job_session_state.assert_not_called()


@pytest.mark.asyncio
async def test_disconnect_does_not_interrupt_collection(tmp_path, monkeypatch):
    owner, runtime, request, outcome, identity = await _setup(tmp_path)
    outcome.stop_append_approved = True
    runtime.agent.interrupt = AsyncMock()
    # A transport flag does not claim Stop or change the RUNNING state.
    owner._answer_turn_coordinator._turns[identity.chat_id].transport_ended = (
        True
    )
    emitter = AsyncMock(return_value=_contributions())
    monkeypatch.setattr(
        "swe.app.runner.runner._build_stop_hook_runtime",
        lambda **_: SimpleNamespace(emit_stop_append=emitter),
    )
    assert (
        len(
            [
                item
                async for item in owner._stream_stop_appended_messages(
                    request=request,
                    runtime=runtime,
                    plan=SimpleNamespace(original_user_message="q"),
                    outcome=outcome,
                )
            ],
        )
        == 1
    )
    runtime.agent.interrupt.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("published", [False, True])
async def test_storage_exception_retains_only_messages_visible_in_committed_state(
    tmp_path,
    monkeypatch,
    published,
):
    from swe.app.runner.session import PublishedLedgerCommitError

    owner, runtime, request, outcome, _ = await _setup(tmp_path)
    outcome.stop_append_approved = True
    emitter = AsyncMock(return_value=_contributions())
    monkeypatch.setattr(
        "swe.app.runner.runner._build_stop_hook_runtime",
        lambda **_: SimpleNamespace(emit_stop_append=emitter),
    )

    async def fail(*_, **__):
        if published:
            runtime.session_execution.state = {
                "agent": {"memory": runtime.agent.memory.state_dict()},
            }
            raise PublishedLedgerCommitError(runtime.session_execution.state)
        raise OSError("secret append must not appear in exception")

    owner.save_job_session_state = fail
    with pytest.raises(
        RuntimeError,
        match="Stop append persistence failed",
    ) as caught:
        async for _ in owner._stream_stop_appended_messages(
            request=request,
            runtime=runtime,
            plan=SimpleNamespace(original_user_message="q"),
            outcome=outcome,
        ):
            pytest.fail("failed commit may not publish a bubble")
    assert "secret append" not in str(caught.value)
    assert len(await runtime.agent.memory.get_memory()) == int(published)


@pytest.mark.asyncio
async def test_diagnostic_backend_failure_cannot_replace_storage_error(
    tmp_path,
    monkeypatch,
):
    owner, runtime, request, outcome, _ = await _setup(tmp_path)
    outcome.stop_append_approved = True
    monkeypatch.setattr(
        "swe.app.runner.runner._build_stop_hook_runtime",
        lambda **_: SimpleNamespace(
            emit_stop_append=AsyncMock(return_value=_contributions()),
        ),
    )
    owner.save_job_session_state = AsyncMock(
        side_effect=TimeoutError("private"),
    )

    def broken_log(*_, **__):
        raise ValueError("logging backend unavailable")

    monkeypatch.setattr("logging.Logger.info", broken_log)
    monkeypatch.setattr("logging.Logger.warning", broken_log)
    with pytest.raises(RuntimeError, match="Stop append persistence failed"):
        async for _ in owner._stream_stop_appended_messages(
            request=request,
            runtime=runtime,
            plan=SimpleNamespace(original_user_message="q"),
            outcome=outcome,
        ):
            pass


@pytest.mark.asyncio
async def test_append_persistence_uses_existing_cleanup_timeout(
    tmp_path,
    monkeypatch,
):
    owner, runtime, request, outcome, _ = await _setup(tmp_path)
    outcome.stop_append_approved = True
    monkeypatch.setattr(
        "swe.app.runner.stop_append.QUERY_CLEANUP_TIMEOUT",
        0.01,
        raising=False,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._build_stop_hook_runtime",
        lambda **_: SimpleNamespace(
            emit_stop_append=AsyncMock(return_value=_contributions()),
        ),
    )

    async def stalled(*_, **__):
        await asyncio.Event().wait()

    owner.save_job_session_state = stalled
    stream = owner._stream_stop_appended_messages(
        request=request,
        runtime=runtime,
        plan=SimpleNamespace(original_user_message="q"),
        outcome=outcome,
    )
    with pytest.raises(RuntimeError, match="Stop append persistence failed"):
        await asyncio.wait_for(stream.__anext__(), timeout=0.2)
    assert await runtime.agent.memory.get_memory() == []


@pytest.mark.asyncio
async def test_monitor_indexes_approved_body_after_append(
    tmp_path,
    monkeypatch,
):
    owner, runtime, _, outcome, _ = await _setup(tmp_path)
    outcome.stop_append_started = True
    await runtime.agent.memory.add(Msg("Friday", "original", "assistant"))
    await runtime.agent.memory.add(
        Msg("Friday", "script supplement", "assistant"),
    )
    owner._generate_backend_suggestions_if_needed = AsyncMock()
    owner._end_trace_if_needed = AsyncMock()
    index = AsyncMock()
    monkeypatch.setattr(
        "swe.app.runner.runner._index_model_output_to_monitor",
        index,
    )
    await owner._complete_successful_query_attempt(
        runtime=runtime,
        plan=SimpleNamespace(),
        outcome=outcome,
        trace_id="trace",
    )
    index.assert_awaited_once_with("trace", "original")


@pytest.mark.asyncio
async def test_qa_extraction_keeps_approved_body_after_append(
    tmp_path,
    monkeypatch,
):
    from swe.config.config import SuggestionMode

    owner, runtime, _, outcome, _ = await _setup(tmp_path)
    outcome.stop_append_started = True
    runtime.chat = SimpleNamespace(id="chat")
    runtime.agent_config.running = SimpleNamespace(
        suggestions=SimpleNamespace(
            enabled=True,
            mode=SuggestionMode.QA_EXTRACTION_ONLY,
            user_message_max_length=100,
            qa_content_total_max_length=1000,
            assistant_response_max_length=500,
        ),
    )
    await runtime.agent.memory.add(
        Msg("Friday", "script supplement", "assistant"),
    )
    store = AsyncMock()
    monkeypatch.setattr("swe.app.suggestions.store.store_qa_content", store)
    await owner._store_qa_content_if_needed(
        runtime=runtime,
        query="question",
        outcome=outcome,
    )
    assert store.await_args.kwargs["assistant_response"] == "original"


@pytest.mark.asyncio
@pytest.mark.parametrize("goal", [False, True])
async def test_rejected_gate_clears_previous_append_approval(tmp_path, goal):
    owner, runtime, request, outcome, _ = await _setup(tmp_path)
    outcome.stop_append_approved = True
    outcome.max_stop_turns = 2
    owner._emit_stop_hook_if_needed = AsyncMock(
        return_value=MergedHookResult(
            decision=HookDecision.BLOCK,
            reason="rejected",
        ),
    )
    owner._should_stop_follow_up = lambda _: True
    owner._build_stop_follow_up_msg = lambda _: Msg("Friday", "retry", "user")
    gate = _resolve_goal_finalization_stop_gate if goal else _resolve_stop_gate
    await gate(
        owner,
        request=request,
        runtime=runtime,
        plan=SimpleNamespace(turn_msgs=[]),
        outcome=outcome,
    )
    assert outcome.stop_append_approved is False


@pytest.mark.asyncio
async def test_successful_commit_diagnostics_contain_identity_but_no_body(
    tmp_path,
    monkeypatch,
):
    owner, runtime, request, outcome, identity = await _setup(tmp_path)
    outcome.stop_append_approved = True
    monkeypatch.setattr(
        "swe.app.runner.runner._build_stop_hook_runtime",
        lambda **_: SimpleNamespace(
            emit_stop_append=AsyncMock(return_value=_contributions()),
        ),
    )
    records = []
    monkeypatch.setattr(
        "swe.agents.hook_runtime.append.logger.info",
        lambda message, *args: records.append(message % args),
    )
    messages = [
        item
        async for item in owner._stream_stop_appended_messages(
            request=request,
            runtime=runtime,
            plan=SimpleNamespace(original_user_message="q"),
            outcome=outcome,
        )
    ]
    diagnostic = "\n".join(records)
    assert messages[0][0].id in diagnostic
    assert identity.turn_id in diagnostic
    assert "bytes=" in diagnostic and "sha256=" in diagnostic
    assert "commit_seconds=" in diagnostic
    assert _contributions()[0].text not in diagnostic


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "version,cancel,published_failure",
    [
        (2, False, False),
        (3, False, False),
        (2, True, False),
        (3, True, False),
        (3, True, "await"),
        (3, True, "result"),
    ],
)
# The writer, cancellation and reload assertions share one transaction boundary.
async def test_real_session_commit_preserves_appends_and_other_state(  # pylint: disable=too-many-statements
    tmp_path,
    monkeypatch,
    version,
    cancel,
    published_failure,
):
    import threading
    from swe.app.runner import session as session_module

    owner, runtime, request, outcome, identity = await _setup(tmp_path)
    outcome.stop_append_approved = True
    memory = runtime.agent.memory
    original = Msg("Friday", "original", "assistant")
    await memory.add(original)
    runtime.agent.state_dict = lambda: {"memory": memory.state_dict()}
    owner.save_job_session_state = AgentRunner.save_job_session_state.__get__(
        owner,
    )
    owner.session = SafeJSONSession(str(tmp_path / "sessions"))
    await owner.session.save_merged_state(
        "s",
        user_id="u",
        state={
            "other_module": {"keep": True},
            "turn_states": {"request": {"status": "running"}},
        },
    )
    if version == 3:
        await owner.session.migrate_terminal_turns(
            "s",
            user_id="u",
            dry_run=False,
        )
    emitter = AsyncMock(return_value=_contributions())
    monkeypatch.setattr(
        "swe.app.runner.runner._build_stop_hook_runtime",
        lambda **_: SimpleNamespace(emit_stop_append=emitter),
    )
    entered, release = threading.Event(), threading.Event()
    writer_name = (
        "_write_json_state_sync" if version == 2 else "_commit_ledger_sync"
    )
    writer = getattr(session_module, writer_name)

    def blocked_writer(*args):
        # v2 returns None; v3 returns the published ledger used on failure.
        result = writer(*args)  # pylint: disable=assignment-from-no-return
        entered.set()
        assert release.wait(5)
        if published_failure:
            raise session_module.PublishedLedgerCommitError(result)
        return result

    shield = asyncio.shield
    shield_calls = 0

    async def cancellation_after_result(task):
        """Make the actual writer finish before cancellation reaches its owner."""
        nonlocal shield_calls
        shield_calls += 1
        try:
            return await shield(task)
        except asyncio.CancelledError:
            if shield_calls == 1:
                try:
                    await shield(task)
                except session_module.PublishedLedgerCommitError:
                    pass
            raise

    async with owner.session.execution("s", user_id="u") as execution:
        runtime.session_execution = execution
        if cancel:
            monkeypatch.setattr(session_module, writer_name, blocked_writer)
        if published_failure == "result":
            monkeypatch.setattr(
                session_module.asyncio,
                "shield",
                cancellation_after_result,
            )
        stream = owner._stream_stop_appended_messages(
            request=request,
            runtime=runtime,
            plan=SimpleNamespace(original_user_message="q"),
            outcome=outcome,
        )
        delivery = asyncio.create_task(stream.__anext__())
        if cancel:
            assert await asyncio.to_thread(entered.wait, 5)
            delivery.cancel()
            await asyncio.sleep(0)
            release.set()
            expected_error = (
                RuntimeError if published_failure else asyncio.CancelledError
            )
            with pytest.raises(expected_error):
                await delivery
        else:
            msg, _ = await delivery
            assert (
                execution.state["agent"]["memory"]["content"][-1][0]["id"]
                == msg.id
            )
            await stream.aclose()
        messages = await memory.get_memory()
        assert len(messages) == 2
        marker = messages[-1].metadata["stop_append"]
        assert marker["turn_id"] == identity.turn_id
        assert (
            execution.state["agent"]["memory"]["content"][-1][0]["metadata"][
                "stop_append"
            ]
            == marker
        )
        durable = json.loads((tmp_path / "sessions" / "u_s.json").read_text())
        assert execution.state["revision"] == durable["revision"]
        assert execution.state["other_module"] == {"keep": True}
        assert execution.state["schema_version"] == version
        # Subsequent cleanup uses the same memory, retaining the durable append.
        monkeypatch.setattr(session_module, writer_name, writer)
        monkeypatch.setattr(session_module.asyncio, "shield", shield)
        await owner.save_job_session_state(
            runtime.agent,
            "s",
            False,
            "u",
            runtime.hook_overlay,
            session_execution=execution,
        )
    reloaded = await owner.session.get_session_state_dict("s", user_id="u")
    raw = reloaded["agent"]["memory"]["content"][-1][0]
    assert raw["id"] == marker["message_id"]
    assert raw["content"] == [
        {"type": "text", "text": _contributions()[0].text},
    ]
    restored = InMemoryMemory()
    restored.load_state_dict(reloaded["agent"]["memory"])
    assert (await restored.get_memory())[
        -1
    ].get_text_content() == _contributions()[0].text

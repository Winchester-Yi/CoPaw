# -*- coding: utf-8 -*-
"""Runner-level Goal stream lifecycle regression tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from agentscope.message import Msg

from swe.agents.hook_runtime.models import HookDecision, MergedHookResult
from swe.agents.react_agent import SWEAgent as RealSWEAgent
from swe.app.goals.models import (
    CompletionCriterion,
    GoalContract,
    GoalScope,
    GoalState,
)
from swe.app.goals.service import GoalService, InMemoryGoalStore
from swe.app.runner.runner import (
    AgentRunner,
    _QueryTurnOutcome,
    _TurnPlan,
    _append_goal_tool_observations,
    _build_goal_contract_context,
    _goal_matches_runtime_scope,
)


def _contract() -> GoalContract:
    return GoalContract(
        objective="Finish the change",
        completion_criteria=[
            CompletionCriterion(
                requirement="Tests pass",
                observable_assertion="pytest succeeds",
                verification_method="Run the focused test suite",
                expected_outcome="The suite reports all tests passing",
            ),
        ],
        constraints={"must_preserve": [], "must_not_do": []},
        autonomy_boundary="No deployment",
    )


def test_completion_judge_package_excludes_background_subagent_details() -> (
    None
):
    observations: list[dict[str, str]] = []

    _append_goal_tool_observations(
        observations,
        Msg(
            name="Friday",
            role="assistant",
            content=[
                {
                    "type": "tool_result",
                    "id": "subagent-1",
                    "name": "get_subagent",
                    "output": {"details": "raw background transcript"},
                },
                {
                    "type": "tool_result",
                    "id": "read-1",
                    "name": "read_file",
                    "output": "bounded source evidence",
                },
            ],
        ),
    )

    assert observations == [
        {
            "tool_call_id": "read-1",
            "tool_name": "read_file",
            "output": '"bounded source evidence"',
        },
    ]


async def _goal_service() -> tuple[GoalService, str]:
    service = GoalService(InMemoryGoalStore())
    goal = await service.create_goal(
        scope=GoalScope(
            tenant_id="tenant-1",
            source_id="source-1",
            agent_profile_id="agent-1",
            chat_id="chat-1",
            effective_model="model-1",
        ),
        contract=_contract(),
    )
    return service, goal.goal_id


@pytest.mark.asyncio
async def test_stop_interrupts_the_matching_waiting_goal_turn() -> None:
    service, goal_id = await _goal_service()
    await service.begin_turn(goal_id, msgid="msg-current")
    await service.settle_turn(goal_id, decision="wait")

    interrupted = await service.interrupt_turn_if_matches(
        goal_id,
        "msg-current",
        "Chat Stop interrupted the active Goal turn",
    )

    assert interrupted is not None
    assert interrupted.state == GoalState.INTERRUPTED
    assert interrupted.turn_active is False
    assert not await service.active_turn_matches(goal_id, "msg-current")


@pytest.mark.asyncio
async def test_stop_does_not_overwrite_a_cancelled_goal_turn() -> None:
    service, goal_id = await _goal_service()
    await service.begin_turn(goal_id, msgid="msg-current")
    cancelled = await service.get(goal_id)
    cancelled.turn_active = False
    cancelled.state = GoalState.CANCELLED
    await service.persist(cancelled)

    interrupted = await service.interrupt_turn_if_matches(
        goal_id,
        "msg-current",
        "Chat Stop interrupted the active Goal turn",
    )

    assert interrupted is None
    assert (await service.get(goal_id)).state == GoalState.CANCELLED


@pytest.mark.asyncio
async def test_goal_stream_keeps_intermediate_turns_open_and_wakes_from_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, goal_id = await _goal_service()
    monkeypatch.setattr(
        "swe.app.goals.registry.get_goal_service",
        lambda: service,
    )
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    resolutions = [
        {
            "decision": "wait",
            "summary": "Waiting for user input",
            "wake_conditions": ["user steering"],
        },
        {
            "decision": "propose_completion",
            "summary": "Completed after wake",
            "completion_proposal": "Verify the completed change",
        },
    ]
    agent = SimpleNamespace(
        _request_context={
            "goal_verifier": lambda _goal: {"criterion-1": (True, "passed")},
            "source_id": "source-1",
        },
    )
    runtime = SimpleNamespace(agent=agent, chat=SimpleNamespace(id="chat-1"))
    calls = 0

    async def fake_stream_agent_turns(**_kwargs):
        nonlocal calls
        agent._request_context["goal_turn_resolution"] = resolutions[calls]
        calls += 1
        yield Msg(
            name="Friday",
            role="assistant",
            content=f"turn-{calls}",
        ), True

    async def fake_wait_for_goal_wake(waiting_goal_id: str) -> None:
        assert waiting_goal_id == goal_id
        await service.wake(goal_id, "User steering received")

    async def fake_finalization_turn(**_kwargs):
        yield Msg(
            name="Friday",
            role="assistant",
            content="Formal completion delivery from the Main Agent.",
        ), True

    monkeypatch.setattr(runner, "_stream_agent_turns", fake_stream_agent_turns)
    monkeypatch.setattr(
        "swe.app.goals.wakeup.wait_for_goal_wake",
        fake_wait_for_goal_wake,
    )
    monkeypatch.setattr(
        runner,
        "_stream_goal_finalization_turn",
        fake_finalization_turn,
    )
    stop_calls: list[dict[str, Any]] = []

    async def fake_emit_stop_hook_if_needed(**kwargs):
        stop_calls.append(kwargs)
        return MergedHookResult(decision=HookDecision.ALLOW)

    monkeypatch.setattr(
        runner,
        "_emit_stop_hook_if_needed",
        fake_emit_stop_hook_if_needed,
    )
    monkeypatch.setattr(
        runner,
        "_requires_stop_output_buffer",
        lambda **_kwargs: False,
    )

    async def fake_completion_review(**_kwargs):
        return {"criterion-1": (True, "passed")}

    monkeypatch.setattr(
        runner,
        "_run_goal_completion_review",
        fake_completion_review,
    )

    events = [
        event
        async for event in runner._stream_completion_lifecycle(
            request=SimpleNamespace(channel_meta={"goal_id": goal_id}),
            runtime=runtime,
            plan=_TurnPlan(original_user_message="Start Goal", turn_msgs=[]),
            outcome=_QueryTurnOutcome(),
        )
    ]

    assert calls == 2
    assert [last for _, last in events] == [False, False, True]
    assert (
        events[-1][0].content
        == "Formal completion delivery from the Main Agent."
    )
    assert len(stop_calls) == 1
    assert stop_calls[0]["outcome"].assistant_response == (
        "Formal completion delivery from the Main Agent."
    )


@pytest.mark.asyncio
async def test_goal_finalization_buffers_candidate_chunks_until_stop_approves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, goal_id = await _goal_service()
    monkeypatch.setattr(
        "swe.app.goals.registry.get_goal_service",
        lambda: service,
    )
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    agent = SimpleNamespace(_request_context={"source_id": "source-1"})
    runtime = SimpleNamespace(agent=agent, chat=SimpleNamespace(id="chat-1"))

    async def fake_stream_agent_turns(**_kwargs):
        agent._request_context["goal_turn_resolution"] = {
            "decision": "propose_completion",
            "summary": "Ready for completion",
            "completion_proposal": "Deliver the completed work",
        }
        yield Msg(name="Friday", role="assistant", content="main turn"), True

    async def fake_finalization_turn(**_kwargs):
        yield Msg(
            name="Friday",
            role="assistant",
            content="candidate chunk",
        ), False
        yield Msg(
            name="Friday",
            role="assistant",
            content="approved final",
        ), True

    async def fake_completion_review(**_kwargs):
        return {"criterion-1": (True, "passed")}

    async def fake_emit_stop_hook_if_needed(**_kwargs):
        return MergedHookResult(decision=HookDecision.ALLOW)

    monkeypatch.setattr(runner, "_stream_agent_turns", fake_stream_agent_turns)
    monkeypatch.setattr(
        runner,
        "_stream_goal_finalization_turn",
        fake_finalization_turn,
    )
    monkeypatch.setattr(
        runner,
        "_run_goal_completion_review",
        fake_completion_review,
    )
    monkeypatch.setattr(
        runner,
        "_emit_stop_hook_if_needed",
        fake_emit_stop_hook_if_needed,
    )
    monkeypatch.setattr(
        runner,
        "_requires_stop_output_buffer",
        lambda **_kwargs: True,
    )

    events = [
        event
        async for event in runner._stream_completion_lifecycle(
            request=SimpleNamespace(channel_meta={"goal_id": goal_id}),
            runtime=runtime,
            plan=_TurnPlan(original_user_message="Start Goal", turn_msgs=[]),
            outcome=_QueryTurnOutcome(),
        )
    ]

    assert [message.content for message, _ in events] == [
        "main turn",
        "approved final",
    ]


@pytest.mark.asyncio
async def test_goal_finalization_retry_receives_stop_rejection_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, goal_id = await _goal_service()
    monkeypatch.setattr(
        "swe.app.goals.registry.get_goal_service",
        lambda: service,
    )
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    agent = SimpleNamespace(_request_context={"source_id": "source-1"})
    runtime = SimpleNamespace(agent=agent, chat=SimpleNamespace(id="chat-1"))
    observed_reasons: list[str | None] = []

    async def fake_stream_agent_turns(**_kwargs):
        agent._request_context["goal_turn_resolution"] = {
            "decision": "propose_completion",
            "summary": "Ready for completion",
            "completion_proposal": "Deliver the completed work",
        }
        yield Msg(name="Friday", role="assistant", content="main turn"), True

    async def fake_finalization_turn(
        *,
        stop_rejection_reason: str | None = None,
        **_kwargs,
    ):
        observed_reasons.append(stop_rejection_reason)
        yield Msg(
            name="Friday",
            role="assistant",
            content=f"delivery {len(observed_reasons)}",
        ), True

    async def fake_completion_review(**_kwargs):
        return {"criterion-1": (True, "passed")}

    stop_calls = 0

    async def fake_emit_stop_hook_if_needed(**_kwargs):
        nonlocal stop_calls
        stop_calls += 1
        if stop_calls == 1:
            return MergedHookResult(
                decision=HookDecision.BLOCK,
                reason="Include the mandatory acknowledgement",
            )
        return MergedHookResult(decision=HookDecision.ALLOW)

    monkeypatch.setattr(runner, "_stream_agent_turns", fake_stream_agent_turns)
    monkeypatch.setattr(
        runner,
        "_stream_goal_finalization_turn",
        fake_finalization_turn,
    )
    monkeypatch.setattr(
        runner,
        "_run_goal_completion_review",
        fake_completion_review,
    )
    monkeypatch.setattr(
        runner,
        "_emit_stop_hook_if_needed",
        fake_emit_stop_hook_if_needed,
    )
    monkeypatch.setattr(
        runner,
        "_requires_stop_output_buffer",
        lambda **_kwargs: False,
    )

    events = [
        event
        async for event in runner._stream_completion_lifecycle(
            request=SimpleNamespace(channel_meta={"goal_id": goal_id}),
            runtime=runtime,
            plan=_TurnPlan(original_user_message="Start Goal", turn_msgs=[]),
            outcome=_QueryTurnOutcome(),
        )
    ]

    assert observed_reasons == [
        None,
        "Include the mandatory acknowledgement",
    ]
    assert events[-1][0].content == "delivery 2"


@pytest.mark.asyncio
async def test_goal_finalization_falls_back_when_model_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, goal_id = await _goal_service()
    monkeypatch.setattr(
        "swe.app.goals.registry.get_goal_service",
        lambda: service,
    )
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    goal = await service.get(goal_id)
    goal.state = GoalState.COMPLETE
    await service.persist(goal)

    async def fail_to_create_agent(**_kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(
        runner,
        "_create_goal_finalization_agent_async",
        fail_to_create_agent,
    )
    events = [
        event
        async for event in runner._stream_goal_finalization_turn(
            runtime=SimpleNamespace(
                agent=SimpleNamespace(_request_context={}),
                agent_config=None,
                session_id="session-1",
                chat=SimpleNamespace(id="chat-1"),
            ),
            goal=goal,
        )
    ]
    assert len(events) == 1
    assert events[0][1] is True
    assert "Completion Judge accepted" in events[0][0].content


@pytest.mark.asyncio
async def test_explicit_goal_request_reports_when_goal_cannot_start_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, goal_id = await _goal_service()
    goal = await service.get(goal_id)
    goal.state = GoalState.PAUSED
    await service.persist(goal)
    monkeypatch.setattr(
        "swe.app.goals.registry.get_goal_service",
        lambda: service,
    )
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    agent = SimpleNamespace(
        _request_context={"source_id": "source-1"},
    )
    runtime = SimpleNamespace(agent=agent, chat=SimpleNamespace(id="chat-1"))

    async def unexpected_turn(**_kwargs):
        raise AssertionError("a paused Goal must not begin a Main Agent turn")
        yield

    monkeypatch.setattr(runner, "_stream_agent_turns", unexpected_turn)
    events = [
        event
        async for event in runner._stream_completion_lifecycle(
            request=SimpleNamespace(channel_meta={"goal_id": goal_id}),
            runtime=runtime,
            plan=_TurnPlan(
                original_user_message="Continue Goal",
                turn_msgs=[],
            ),
            outcome=_QueryTurnOutcome(),
        )
    ]

    assert [last for _, last in events] == [True]
    assert "PAUSED" in events[0][0].content
    assert "resume" in events[0][0].content.lower()
    assert (await service.get(goal_id)).state == GoalState.PAUSED


@pytest.mark.asyncio
async def test_active_goal_request_without_goal_id_is_queued_as_steering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, goal_id = await _goal_service()
    monkeypatch.setattr(
        "swe.app.goals.registry.get_goal_service",
        lambda: service,
    )
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    runtime = SimpleNamespace(
        agent=SimpleNamespace(_request_context={"source_id": "source-1"}),
        chat=SimpleNamespace(id="chat-1"),
    )

    async def unexpected_turn(**_kwargs):
        raise AssertionError("active Goal input must not start ordinary Chat")
        yield

    monkeypatch.setattr(runner, "_stream_agent_turns", unexpected_turn)
    events = [
        event
        async for event in runner._stream_completion_lifecycle(
            request=SimpleNamespace(channel_meta={}),
            runtime=runtime,
            plan=_TurnPlan(
                original_user_message="Please cover the edge cases",
                turn_msgs=[],
            ),
            outcome=_QueryTurnOutcome(),
        )
    ]

    goal = await service.get(goal_id)
    assert [item.content for item in goal.steering] == [
        "Please cover the edge cases",
    ]
    assert [last for _, last in events] == [True]
    assert "steering" in events[0][0].content.lower()


@pytest.mark.asyncio
async def test_implicit_steering_rejects_a_goal_outside_the_runtime_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, goal_id = await _goal_service()
    goal = await service.get(goal_id)
    goal.scope.agent_profile_id = "another-agent"
    await service.persist(goal)
    monkeypatch.setattr(
        "swe.app.goals.registry.get_goal_service",
        lambda: service,
    )
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    runtime = SimpleNamespace(
        agent=SimpleNamespace(_request_context={"source_id": "source-1"}),
        chat=SimpleNamespace(id="chat-1"),
    )

    events = [
        event
        async for event in runner._stream_completion_lifecycle(
            request=SimpleNamespace(channel_meta={}),
            runtime=runtime,
            plan=_TurnPlan(
                original_user_message="Please cover the edge cases",
                turn_msgs=[],
            ),
            outcome=_QueryTurnOutcome(),
        )
    ]

    assert (await service.get(goal_id)).steering == []
    assert [last for _, last in events] == [True]
    assert "not available" in events[0][0].content


@pytest.mark.asyncio
async def test_goal_finalization_persists_the_final_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, goal_id = await _goal_service()
    goal = await service.get(goal_id)
    goal.state = GoalState.COMPLETE
    await service.persist(goal)
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    persisted: list[Msg] = []

    class Memory:
        async def add(self, msg: Msg) -> None:
            persisted.append(msg)

    class FinalizationAgent:
        async def __call__(self, _msgs):
            return None

    async def fake_stream_printing_messages(**kwargs):
        kwargs["coroutine_task"].close()
        yield Msg(
            name="Friday",
            role="assistant",
            content="Final delivery",
        ), True

    async def fake_enforce(stream, **_kwargs):
        async for event in stream:
            yield event

    monkeypatch.setattr(
        runner,
        "_create_goal_finalization_agent_async",
        AsyncMock(return_value=FinalizationAgent()),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.stream_printing_messages",
        fake_stream_printing_messages,
    )
    monkeypatch.setattr(runner, "_enforce_query_timeout", fake_enforce)

    events = [
        event
        async for event in runner._stream_goal_finalization_turn(
            runtime=SimpleNamespace(
                agent=SimpleNamespace(_request_context={}, memory=Memory()),
                agent_config=None,
                session_id="session-1",
                chat=SimpleNamespace(id="chat-1"),
            ),
            goal=goal,
        )
    ]

    assert [msg.content for msg in persisted] == ["Final delivery"]
    assert events[-1][1] is True


@pytest.mark.asyncio
async def test_interrupted_goal_after_wake_does_not_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, goal_id = await _goal_service()
    monkeypatch.setattr(
        "swe.app.goals.registry.get_goal_service",
        lambda: service,
    )
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    agent = SimpleNamespace(
        _request_context={
            "goal_verifier": lambda _goal: {},
            "source_id": "source-1",
        },
    )
    runtime = SimpleNamespace(agent=agent, chat=SimpleNamespace(id="chat-1"))

    async def fake_stream_agent_turns(**_kwargs):
        agent._request_context["goal_turn_resolution"] = {
            "decision": "wait",
            "summary": "Waiting",
            "wake_conditions": ["manual wake"],
        }
        yield Msg(name="Friday", role="assistant", content="waiting"), True

    async def fake_wait_for_goal_wake(_goal_id: str) -> None:
        goal = await service.get(goal_id)
        goal.state = GoalState.INTERRUPTED
        await service.persist(goal)

    async def unexpected_finalization(**_kwargs):
        raise AssertionError("INTERRUPTED must not start Finalization Turn")
        yield

    monkeypatch.setattr(runner, "_stream_agent_turns", fake_stream_agent_turns)
    monkeypatch.setattr(
        "swe.app.goals.wakeup.wait_for_goal_wake",
        fake_wait_for_goal_wake,
    )
    monkeypatch.setattr(
        runner,
        "_stream_goal_finalization_turn",
        unexpected_finalization,
    )

    events = [
        event
        async for event in runner._stream_completion_lifecycle(
            request=SimpleNamespace(channel_meta={"goal_id": goal_id}),
            runtime=runtime,
            plan=_TurnPlan(original_user_message="Start Goal", turn_msgs=[]),
            outcome=_QueryTurnOutcome(),
        )
    ]

    assert [last for _, last in events] == [False]


@pytest.mark.asyncio
async def test_cancelling_a_waiting_goal_stream_marks_the_goal_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, goal_id = await _goal_service()
    monkeypatch.setattr(
        "swe.app.goals.registry.get_goal_service",
        lambda: service,
    )
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    agent = SimpleNamespace(
        _request_context={"source_id": "source-1", "msgid": "msg-current"},
    )
    runtime = SimpleNamespace(agent=agent, chat=SimpleNamespace(id="chat-1"))
    waiting = asyncio.Event()

    async def fake_stream_agent_turns(**_kwargs):
        agent._request_context["goal_turn_resolution"] = {
            "decision": "wait",
            "summary": "Waiting",
            "wake_conditions": ["manual wake"],
        }
        yield Msg(name="Friday", role="assistant", content="waiting"), True

    async def fake_wait_for_goal_wake(_goal_id: str) -> None:
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "_stream_agent_turns", fake_stream_agent_turns)
    monkeypatch.setattr(
        "swe.app.goals.wakeup.wait_for_goal_wake",
        fake_wait_for_goal_wake,
    )

    stream = runner._stream_completion_lifecycle(
        request=SimpleNamespace(channel_meta={"goal_id": goal_id}),
        runtime=runtime,
        plan=_TurnPlan(original_user_message="Start Goal", turn_msgs=[]),
        outcome=_QueryTurnOutcome(),
    )
    await anext(stream)
    waiting_turn = asyncio.create_task(anext(stream))
    await waiting.wait()
    waiting_turn.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiting_turn

    assert (await service.get(goal_id)).state == GoalState.INTERRUPTED


def test_goal_finalization_agent_is_tool_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    captured: dict[str, Any] = {}

    class FakeAgent:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("swe.app.runner.runner.SWEAgent", FakeAgent)
    runtime = SimpleNamespace(
        agent=SimpleNamespace(
            _request_context={
                "session_id": "session-1",
                "user_id": "user-1",
                "channel": "console",
                "chat_id": "chat-1",
                "turn_id": "turn-1",
            },
        ),
        agent_config="agent-config",
    )
    created = runner._create_goal_finalization_agent(
        runtime=runtime,
        goal=SimpleNamespace(),
    )

    assert isinstance(created, FakeAgent)
    assert captured["enable_memory_manager"] is False
    assert captured["mcp_clients"] == []
    assert captured["source_tool_versions"] == ()
    assert captured["request_context"]["goal_finalization"] is True
    assert (
        "only the final concise user-facing response"
        in captured["system_prompt_override"]
    )


def test_goal_completion_judge_agent_is_restricted_and_model_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    captured: dict[str, Any] = {}
    frozen_provider = object()

    class FakeAgent:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    class FakeProviderManager:
        def get_provider(self, provider_id: str) -> object:
            assert provider_id == "provider-1"
            return frozen_provider

    monkeypatch.setattr("swe.app.runner.runner.SWEAgent", FakeAgent)
    monkeypatch.setattr(
        "swe.providers.provider_manager.ProviderManager.get_instance",
        lambda tenant_id: (
            FakeProviderManager()
            if tenant_id == "tenant-1"
            else pytest.fail("Judge must use the runner tenant")
        ),
    )
    runtime = SimpleNamespace(
        agent=SimpleNamespace(
            _request_context={
                "session_id": "session-1",
                "user_id": "user-1",
                "channel": "console",
                "chat_id": "chat-1",
                "turn_id": "turn-1",
                "agent_id": "agent-1",
                "tenant_id": "tenant-1",
                "source_id": "source-1",
                "trace_id": "trace-1",
                "goal_id": "goal-1",
                "goal_verifier": object(),
                "selected_expert_id": "expert-1",
                "plan_mode_enabled": True,
            },
            _resolved_model_slot={
                "provider_id": "provider-1",
                "model": "model-1",
            },
        ),
        agent_config="agent-config",
    )

    created = runner._create_goal_completion_judge_agent(
        runtime=runtime,
        goal=SimpleNamespace(),
    )

    assert isinstance(created, FakeAgent)
    assert captured["request_context"] == {
        "session_id": "session-1",
        "user_id": "user-1",
        "channel": "console",
        "chat_id": "chat-1",
        "turn_id": "turn-1",
        "agent_id": "agent-1",
        "tenant_id": "tenant-1",
        "source_id": "source-1",
        "trace_id": "trace-1",
        "goal_id": "goal-1",
        "agent_role": "completion_judge",
    }
    assert captured["enable_memory_manager"] is False
    assert captured["memory_manager"] is None
    assert captured["mcp_clients"] == []
    assert captured["enable_workspace_skills"] is False
    assert captured["task_tracker"] is None
    assert captured["source_tool_versions"] == ()
    assert captured["model_slot_override"].provider_id == "provider-1"
    assert captured["model_slot_override"].model == "model-1"
    assert captured["model_provider_override"] is frozen_provider
    prompt = captured["system_prompt_override"]
    assert "completion review" in prompt.lower()
    assert (
        '{"reviews":[{"criterion_id":...,"decision":"accept"|"reject",'
        '"reason":...,"evidence_refs":[...]}]}' in prompt
    )
    assert "one entry for every supplied criterion" in prompt.lower()
    assert "missing evidence" in prompt.lower()

    judge = object.__new__(RealSWEAgent)
    judge._agent_config = SimpleNamespace()
    judge._request_context = captured["request_context"]
    judge._workspace_dir = None
    assert set(judge._create_toolkit().tools) == {
        "read_file",
        "grep_search",
        "glob_search",
        "get_current_time",
    }


@pytest.mark.asyncio
async def test_goal_completion_judge_uses_the_goal_frozen_model_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    captured: dict[str, Any] = {}

    class FakeAgent:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    class FakeProviderManager:
        def get_provider(self, provider_id: str) -> object:
            assert provider_id == "frozen-provider"
            return object()

    monkeypatch.setattr("swe.app.runner.runner.SWEAgent", FakeAgent)
    monkeypatch.setattr(
        "swe.providers.provider_manager.ProviderManager.get_instance",
        lambda _tenant_id: FakeProviderManager(),
    )
    service, goal_id = await _goal_service()
    stored_goal = await service.get(goal_id)
    stored_goal.scope.effective_model_provider_id = "frozen-provider"
    stored_goal.scope.effective_model = "frozen-model"
    await service.persist(stored_goal)
    goal = await service.get(goal_id)
    runtime = SimpleNamespace(
        agent=SimpleNamespace(
            _request_context={},
            _resolved_model_slot={
                "provider_id": "new-provider",
                "model": "new-model",
            },
        ),
        agent_config="agent-config",
    )

    runner._create_goal_completion_judge_agent(runtime=runtime, goal=goal)

    assert captured["model_slot_override"].provider_id == "frozen-provider"
    assert captured["model_slot_override"].model == "frozen-model"


def test_goal_scope_rejects_a_different_resolved_provider() -> None:
    goal = SimpleNamespace(
        scope=SimpleNamespace(
            chat_id="chat-1",
            tenant_id="tenant-1",
            agent_profile_id="agent-1",
            source_id="source-1",
            effective_model_provider_id="frozen-provider",
            effective_model="model-1",
        ),
    )
    runtime = SimpleNamespace(
        chat=SimpleNamespace(id="chat-1"),
        agent=SimpleNamespace(
            _request_context={"source_id": "source-1"},
            _resolved_model_slot={
                "provider_id": "other-provider",
                "model": "model-1",
            },
        ),
    )

    assert not _goal_matches_runtime_scope(
        goal,
        runtime,
        tenant_id="tenant-1",
        agent_id="agent-1",
    )


def test_goal_completion_judge_agent_requires_frozen_resolved_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    created = False

    class FakeAgent:
        def __init__(self, **_kwargs) -> None:
            nonlocal created
            created = True

    monkeypatch.setattr("swe.app.runner.runner.SWEAgent", FakeAgent)
    runtime = SimpleNamespace(
        agent=SimpleNamespace(
            _request_context={},
            _resolved_model_slot={"provider_id": "provider-1"},
        ),
        agent_config="agent-config",
    )

    with pytest.raises(RuntimeError, match="frozen model is unavailable"):
        runner._create_goal_completion_judge_agent(
            runtime=runtime,
            goal=SimpleNamespace(),
        )

    assert created is False


@pytest.mark.asyncio
async def test_goal_completion_reviewer_parses_hidden_judge_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, goal_id = await _goal_service()
    goal = await service.get(goal_id)
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    captured: dict[str, object] = {}

    class FakeJudge:
        def __call__(self, messages):
            captured["messages"] = messages

            async def run() -> None:
                return None

            return run()

    async def fake_stream_printing_messages(**kwargs):
        kwargs["coroutine_task"].close()
        yield Msg(
            name="Judge",
            role="assistant",
            content=(
                '{"reviews":[{"criterion_id":"criterion-1",'
                '"decision":"accept","reason":"Observed output",'
                '"evidence_refs":["tool-1"]}]}'
            ),
        ), True

    async def fake_enforce(stream, **_kwargs):
        async for item in stream:
            yield item

    monkeypatch.setattr(
        runner,
        "_create_goal_completion_judge_agent_async",
        AsyncMock(return_value=FakeJudge()),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.stream_printing_messages",
        fake_stream_printing_messages,
    )
    monkeypatch.setattr(runner, "_enforce_query_timeout", fake_enforce)
    runtime = SimpleNamespace(
        agent=SimpleNamespace(
            _request_context={
                "_goal_turn_tool_observations": [
                    {"tool_name": "read_file", "output": "observed"},
                ],
            },
        ),
        session_id="session-1",
        chat=SimpleNamespace(id="chat-1"),
    )
    resolution = SimpleNamespace(
        completion_proposal="The result is ready",
        evidence_refs=["main-agent-evidence"],
    )

    reviewer = runner._create_goal_completion_reviewer(
        runtime=runtime,
        resolution=resolution,
    )
    result = await reviewer(goal)

    assert result == {"criterion-1": (True, "tool-1")}
    package = captured["messages"][0].content
    assert "The result is ready" in package
    assert "main-agent-evidence" in package
    assert "read_file" in package


@pytest.mark.asyncio
async def test_goal_completion_reviewer_ignores_legacy_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    runtime = SimpleNamespace(
        agent=SimpleNamespace(
            _request_context={
                "goal_verifier": lambda _: {"criterion-1": (True, "bypass")},
            },
        ),
    )
    resolution = SimpleNamespace(
        completion_proposal="Ready",
        evidence_refs=[],
    )
    calls = 0

    async def fake_review(**_kwargs):
        nonlocal calls
        calls += 1
        return {"criterion-1": (False, "Judge rejected")}

    monkeypatch.setattr(runner, "_run_goal_completion_review", fake_review)

    result = await runner._create_goal_completion_reviewer(
        runtime=runtime,
        resolution=resolution,
    )(SimpleNamespace())

    assert result == {"criterion-1": (False, "Judge rejected")}
    assert calls == 1


@pytest.mark.asyncio
async def test_goal_completion_reviewer_maps_denied_approval_to_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, goal_id = await _goal_service()
    goal = await service.get(goal_id)
    goal.criteria[0].verification_request_id = "approval-1"
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")

    class FakeApprovals:
        async def get_request_status(self, request_id: str):
            assert request_id == "approval-1"
            return {"status": "denied"}

    monkeypatch.setattr(
        "swe.app.approvals.get_approval_service",
        lambda: FakeApprovals(),
    )

    result = await runner._completion_review_approval_result(goal)

    assert result == {
        "criterion-1": (False, "Completion Judge approval denied"),
    }


@pytest.mark.asyncio
async def test_goal_completion_reviewer_keeps_submitted_approval_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, goal_id = await _goal_service()
    goal = await service.get(goal_id)
    goal.criteria[0].verification_request_id = "approval-1"
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")

    class FakeApprovals:
        async def get_request_status(self, request_id: str):
            assert request_id == "approval-1"
            return {"status": "submitted"}

    monkeypatch.setattr(
        "swe.app.approvals.get_approval_service",
        lambda: FakeApprovals(),
    )

    result = await runner._completion_review_approval_result(goal)

    assert result is not None
    assert result["criterion-1"].request_id == "approval-1"


@pytest.mark.asyncio
async def test_approved_review_without_replay_payload_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, goal_id = await _goal_service()
    goal = await service.get(goal_id)
    goal.criteria[0].verification_request_id = "approval-1"
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")

    class FakeApprovals:
        async def get_request_status(self, _request_id: str):
            return {"status": "approved"}

        async def get_request(self, _request_id: str):
            return SimpleNamespace(extra={})

    monkeypatch.setattr(
        "swe.app.approvals.get_approval_service",
        lambda: FakeApprovals(),
    )
    monkeypatch.setattr(
        runner,
        "_create_goal_completion_judge_agent_async",
        AsyncMock(
            side_effect=AssertionError("Judge must not start without replay"),
        ),
    )

    result = await runner._run_goal_completion_review(
        runtime=SimpleNamespace(
            agent=SimpleNamespace(_request_context={}),
            session_id="session-1",
            chat=SimpleNamespace(id="chat-1"),
        ),
        review_goal=goal,
        completion_proposal="Ready",
        evidence_refs=[],
        tool_observations=[],
    )

    assert result == {
        "criterion-1": (
            False,
            "Completion Judge approved tool replay is unavailable",
        ),
    }


@pytest.mark.asyncio
async def test_approved_review_replay_preserves_hook_approval_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, goal_id = await _goal_service()
    goal = await service.get(goal_id)
    goal.criteria[0].verification_request_id = "approval-1"
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    record = SimpleNamespace(
        request_id="approval-1",
        tool_name="read_file",
        extra={
            "tool_call": {"id": "call-1", "name": "read_file", "input": {}},
            "sibling_tool_calls": [{"id": "call-2"}],
            "remaining_queue": [{"id": "call-3"}],
            "thinking_blocks": [{"type": "thinking"}],
            "approval_kind": "hook_pre_tool_use",
            "hook_ask_handler_ids": ["hook-1"],
        },
    )

    class FakeApprovals:
        async def get_request(self, request_id: str):
            assert request_id == "approval-1"
            return record

    monkeypatch.setattr(
        "swe.app.approvals.get_approval_service",
        lambda: FakeApprovals(),
    )

    replay = await runner._completion_review_approved_tool_call(goal)

    assert replay is not None
    assert replay["_sibling_tool_calls"] == [{"id": "call-2"}]
    assert replay["_remaining_queue"] == [{"id": "call-3"}]
    assert replay["_thinking_blocks"] == [{"type": "thinking"}]
    assert replay["_approval_replay"]["request_id"] == "approval-1"


def test_completion_judge_skips_bootstrap_hook(
    tmp_path,
) -> None:
    (tmp_path / "BOOTSTRAP.md").write_text("bootstrap", encoding="utf-8")
    judge = object.__new__(RealSWEAgent)
    judge._request_context = {"agent_role": "completion_judge"}
    judge._workspace_dir = tmp_path
    judge._language = "en"
    judge._enable_memory_manager = False
    judge.memory_manager = None
    registered_hooks: list[dict[str, object]] = []

    def capture_hook(**kwargs) -> None:
        registered_hooks.append(kwargs)

    judge.register_instance_hook = capture_hook

    judge._register_hooks()

    assert registered_hooks == []
    assert not (tmp_path / ".bootstrap_completed").exists()


def test_completion_judge_keeps_profile_disabled_read_tools_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = {
        "execute_shell_command": SimpleNamespace(
            enabled=True,
            async_execution=False,
        ),
        "read_file": SimpleNamespace(enabled=False),
        "grep_search": SimpleNamespace(enabled=True),
        "glob_search": SimpleNamespace(enabled=True),
        "get_current_time": SimpleNamespace(enabled=True),
    }
    monkeypatch.setattr(
        "swe.config.config._default_builtin_tools",
        lambda: defaults,
    )
    judge = object.__new__(RealSWEAgent)
    judge._agent_config = SimpleNamespace(tools=None)
    judge._request_context = {"agent_role": "completion_judge"}
    judge._workspace_dir = None

    enabled_tools, _ = judge._tool_settings(judge._request_context, False)

    assert enabled_tools["read_file"] is False
    assert "read_file" not in judge._create_toolkit().tools


def test_completion_judge_sparse_profile_defaults_unlisted_tools_to_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "swe.config.config._default_builtin_tools",
        lambda: {
            "execute_shell_command": SimpleNamespace(
                enabled=False,
                async_execution=False,
            ),
            "read_file": SimpleNamespace(enabled=True),
        },
    )
    judge = object.__new__(RealSWEAgent)
    judge._agent_config = SimpleNamespace(tools=None)
    judge._request_context = {"agent_role": "completion_judge"}
    judge._workspace_dir = None

    assert set(judge._create_toolkit().tools) == {"read_file"}


@pytest.mark.asyncio
async def test_goal_stream_rejects_a_goal_from_another_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, goal_id = await _goal_service()
    monkeypatch.setattr(
        "swe.app.goals.registry.get_goal_service",
        lambda: service,
    )
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    agent = SimpleNamespace(
        _request_context={
            "goal_verifier": lambda _goal: {"criterion-1": (True, "passed")},
            "source_id": "source-1",
        },
    )
    runtime = SimpleNamespace(
        agent=agent,
        chat=SimpleNamespace(id="other-chat"),
    )

    async def unexpected_turn(**_kwargs):
        raise AssertionError("foreign Goal must not begin a Main Agent turn")
        yield

    monkeypatch.setattr(runner, "_stream_agent_turns", unexpected_turn)
    events = [
        event
        async for event in runner._stream_completion_lifecycle(
            request=SimpleNamespace(channel_meta={"goal_id": goal_id}),
            runtime=runtime,
            plan=_TurnPlan(original_user_message="Start Goal", turn_msgs=[]),
            outcome=_QueryTurnOutcome(),
        )
    ]

    assert [last for _, last in events] == [True]
    assert "not available" in events[0][0].content


@pytest.mark.asyncio
async def test_goal_continuation_context_includes_the_full_active_contract() -> (
    None
):
    service, goal_id = await _goal_service()
    goal = await service.get(goal_id)

    context = _build_goal_contract_context(goal)

    assert "Requirement: Tests pass" in context
    assert "Observable assertion: pytest succeeds" in context
    assert "Verification method: Run the focused test suite" in context
    assert "Expected outcome: The suite reports all tests passing" in context
    assert "must_preserve: none" in context
    assert "must_not_do: none" in context
    assert "Autonomy boundary: No deployment" in context


@pytest.mark.asyncio
async def test_goal_stream_retries_approved_completion_review_before_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, goal_id = await _goal_service()
    monkeypatch.setattr(
        "swe.app.goals.registry.get_goal_service",
        lambda: service,
    )
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    verifier_calls = 0

    async def verifier(_goal):
        nonlocal verifier_calls
        verifier_calls += 1
        if verifier_calls == 1:
            from swe.app.goals.runtime import CompletionReviewPending

            return {
                "criterion-1": CompletionReviewPending(
                    request_id="approval-1",
                    reason="Completion Judge tool approval is pending",
                ),
            }
        return {"criterion-1": (True, "approved completion review passed")}

    agent = SimpleNamespace(
        _request_context={"goal_verifier": verifier, "source_id": "source-1"},
    )
    runtime = SimpleNamespace(agent=agent, chat=SimpleNamespace(id="chat-1"))
    turns = 0

    async def fake_stream_agent_turns(**_kwargs):
        nonlocal turns
        turns += 1
        agent._request_context["goal_turn_resolution"] = {
            "decision": "propose_completion",
            "summary": "Ready for completion review",
            "completion_proposal": "Review the result",
        }
        yield Msg(name="Friday", role="assistant", content="turn"), True

    async def fake_wait_for_goal_wake(waiting_goal_id: str) -> None:
        assert waiting_goal_id == goal_id
        await service.wake(goal_id, "Tool approval approved")

    monkeypatch.setattr(runner, "_stream_agent_turns", fake_stream_agent_turns)
    monkeypatch.setattr(
        "swe.app.goals.wakeup.wait_for_goal_wake",
        fake_wait_for_goal_wake,
    )

    async def fake_completion_review(**_kwargs):
        return await verifier(None)

    monkeypatch.setattr(
        runner,
        "_run_goal_completion_review",
        fake_completion_review,
    )

    events = [
        event
        async for event in runner._stream_completion_lifecycle(
            request=SimpleNamespace(channel_meta={"goal_id": goal_id}),
            runtime=runtime,
            plan=_TurnPlan(original_user_message="Start Goal", turn_msgs=[]),
            outcome=_QueryTurnOutcome(),
        )
    ]

    assert turns == 1
    assert verifier_calls == 2
    assert events[-1][0].content.startswith("Goal Completion Judge accepted")


@pytest.mark.asyncio
async def test_steering_during_pending_completion_review_keeps_waiting_for_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, goal_id = await _goal_service()
    monkeypatch.setattr(
        "swe.app.goals.registry.get_goal_service",
        lambda: service,
    )
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    review_calls = 0
    wait_calls = 0
    agent = SimpleNamespace(_request_context={"source_id": "source-1"})
    runtime = SimpleNamespace(agent=agent, chat=SimpleNamespace(id="chat-1"))

    async def fake_stream_agent_turns(**_kwargs):
        agent._request_context["goal_turn_resolution"] = {
            "decision": "propose_completion",
            "summary": "Ready for review",
            "completion_proposal": "Review the completed work",
        }
        yield Msg(name="Friday", role="assistant", content="turn"), True

    async def fake_completion_review(**_kwargs):
        nonlocal review_calls
        review_calls += 1
        if review_calls < 3:
            from swe.app.goals.runtime import CompletionReviewPending

            return {
                "criterion-1": CompletionReviewPending(
                    request_id="approval-1",
                    reason="Judge tool approval is pending",
                ),
            }
        return {"criterion-1": (True, "Judge observed the test output")}

    async def fake_wait_for_goal_wake(waiting_goal_id: str) -> None:
        nonlocal wait_calls
        assert waiting_goal_id == goal_id
        wait_calls += 1
        if wait_calls == 1:
            await service.enqueue_steering(
                goal_id,
                "Please prioritize edge cases",
            )
        else:
            await service.wake(goal_id, "Judge tool approval approved")

    monkeypatch.setattr(runner, "_stream_agent_turns", fake_stream_agent_turns)
    monkeypatch.setattr(
        runner,
        "_run_goal_completion_review",
        fake_completion_review,
    )
    monkeypatch.setattr(
        "swe.app.goals.wakeup.wait_for_goal_wake",
        fake_wait_for_goal_wake,
    )

    events = [
        event
        async for event in runner._stream_completion_lifecycle(
            request=SimpleNamespace(channel_meta={"goal_id": goal_id}),
            runtime=runtime,
            plan=_TurnPlan(original_user_message="Start Goal", turn_msgs=[]),
            outcome=_QueryTurnOutcome(),
        )
    ]

    assert wait_calls == 2
    assert review_calls == 3
    assert (await service.get(goal_id)).state == GoalState.COMPLETE
    assert events[-1][0].content.startswith("Goal Completion Judge accepted")


@pytest.mark.asyncio
async def test_interrupted_goal_does_not_emit_a_finalization_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, goal_id = await _goal_service()
    monkeypatch.setattr(
        "swe.app.goals.registry.get_goal_service",
        lambda: service,
    )
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    agent = SimpleNamespace(
        _request_context={
            "goal_verifier": lambda _: {},
            "source_id": "source-1",
        },
    )
    runtime = SimpleNamespace(agent=agent, chat=SimpleNamespace(id="chat-1"))
    outcome = _QueryTurnOutcome(pre_tool_terminal_stop=True)

    async def fake_stream_agent_turns(**_kwargs):
        yield Msg(name="Friday", role="assistant", content="partial"), True

    monkeypatch.setattr(runner, "_stream_agent_turns", fake_stream_agent_turns)
    events = [
        event
        async for event in runner._stream_completion_lifecycle(
            request=SimpleNamespace(channel_meta={"goal_id": goal_id}),
            runtime=runtime,
            plan=_TurnPlan(original_user_message="Start Goal", turn_msgs=[]),
            outcome=outcome,
        )
    ]

    assert [event[0].content for event in events] == ["partial"]
    assert (await service.get(goal_id)).state.value == "INTERRUPTED"


@pytest.mark.asyncio
async def test_cancelled_goal_turn_is_marked_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, goal_id = await _goal_service()
    monkeypatch.setattr(
        "swe.app.goals.registry.get_goal_service",
        lambda: service,
    )
    runner = AgentRunner(agent_id="agent-1", tenant_id="tenant-1")
    runtime = SimpleNamespace(
        agent=SimpleNamespace(
            _request_context={"source_id": "source-1"},
        ),
        chat=SimpleNamespace(id="chat-1"),
    )

    async def cancelled_turn(**_kwargs):
        raise asyncio.CancelledError()
        yield

    monkeypatch.setattr(runner, "_stream_agent_turns", cancelled_turn)

    with pytest.raises(asyncio.CancelledError):
        async for _event in runner._stream_completion_lifecycle(
            request=SimpleNamespace(channel_meta={"goal_id": goal_id}),
            runtime=runtime,
            plan=_TurnPlan(original_user_message="Start Goal", turn_msgs=[]),
            outcome=_QueryTurnOutcome(),
        ):
            pass

    assert (await service.get(goal_id)).state == GoalState.INTERRUPTED

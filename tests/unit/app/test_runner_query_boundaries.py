# -*- coding: utf-8 -*-
"""Characterize externally visible boundaries in the AgentRunner query path."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from agentscope.message import Msg

from swe.agents.hook_runtime.models import (
    HookConfig,
    HookDecision,
    HookEventName,
    HookMatcherGroupConfig,
    CommandHookHandlerConfig,
    HookOverlayEntry,
    LoadedSkillHookSource,
    HookSessionOverlay,
    MergedHookResult,
)
from swe.app.runner.runner import (
    AgentRunner,
    _QueryPreflight,
    _QueryRuntime,
    _QueryRuntimeInputs,
)
from swe.app.runner.query_contracts import _RuntimeStartResult
from swe.app.runner.query_runtime import _drop_invalid_workspace_skill_hooks
from swe.agents.skill_runtime_snapshot import (
    ManifestStat,
    WorkspaceSkillSnapshot,
)


def _request(**overrides):
    payload = {
        "session_id": "session-1",
        "user_id": "user-1",
        "channel": "console",
        "channel_meta": {},
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _agent_config() -> SimpleNamespace:
    return SimpleNamespace(
        id="test-agent",
        mcp=None,
        hooks=HookConfig(),
        running=SimpleNamespace(),
    )


def _blocked_msg(text: str) -> Msg:
    return Msg(name="Friday", role="assistant", content=text)


def test_final_snapshot_validation_removes_invalid_skill_hooks() -> None:
    source = LoadedSkillHookSource(
        source_id="skill:stale",
        skill_name="stale",
        skill_root="/workspace/skills/stale",
        source_path="/workspace/skills/stale/hooks/hooks.json",
        hook_config=HookConfig(
            enabled=True,
            events={
                HookEventName.POST_TOOL_USE: [
                    HookMatcherGroupConfig(
                        id="skill:stale:post",
                        hooks=[
                            CommandHookHandlerConfig(
                                id="skill:stale:handler",
                                command="echo ok",
                            ),
                        ],
                    ),
                ],
            },
        ),
    )
    overlay = HookSessionOverlay(
        loaded_skill_sources=[source],
        entries=[
            HookOverlayEntry(hook_id="skill:stale:handler"),
            HookOverlayEntry(hook_id="tenant:always", enabled=True),
        ],
    )

    filtered = _drop_invalid_workspace_skill_hooks(overlay, {"stale"})

    assert filtered.loaded_skill_sources == []
    assert [entry.hook_id for entry in filtered.entries] == ["tenant:always"]


@pytest.mark.asyncio
async def test_final_snapshot_validation_failure_keeps_query_without_skills(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A transient final-check error must not abort an ordinary query."""
    from swe.app.runner import query_runtime

    snapshot = WorkspaceSkillSnapshot(
        workspace_dir=tmp_path,
        generation=1,
        manifest_stat=ManifestStat(1, 1, 1),
        skills={"stale": SimpleNamespace()},
    )
    inputs = _QueryRuntimeInputs(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        skip_history=False,
        agent_config=_agent_config(),
        tenant_hooks=HookConfig(),
        hook_overlay=HookSessionOverlay(),
        env_context="",
        selected_context_directives=["use stale"],
        selected_skill_directives=[
            SimpleNamespace(name="stale", render=lambda: "use stale"),
        ],
        auth_token=None,
        passthrough_headers={},
        workspace_skill_snapshot=snapshot,
    )

    async def fail_validation(_snapshot):
        raise PermissionError("workspace temporarily unreadable")

    monkeypatch.setattr(
        "swe.agents.skill_runtime_snapshot.validate_workspace_skill_snapshot",
        fail_validation,
    )
    captured: dict[str, Any] = {}

    class _Agent:
        async def register_mcp_clients(self):
            return None

        def set_console_output_enabled(self, *, enabled):
            del enabled

    owner = SimpleNamespace(
        workspace_dir=tmp_path,
        tenant_id=None,
        agent_id="test-agent",
    )

    def create_agent(**kwargs):
        captured.update(kwargs)
        return _Agent()

    owner._create_agent_for_query = create_agent
    owner._attach_session_skill_detector = lambda **_kwargs: None
    runtime = await query_runtime.finalize_query_runtime(
        owner,
        request=_request(),
        query="hello",
        msgs=[],
        preflight=_QueryPreflight(),
        inputs=inputs,
        resources=SimpleNamespace(
            chat=None,
            turn_id="turn-1",
            env_context="",
        ),
        mcp_clients=[],
        get_last_user_text=lambda _msgs: "",
        debug_log=lambda *_args: None,
    )

    assert runtime.agent is not None
    assert captured["workspace_skill_snapshot"].skills == {}
    assert inputs.selected_skill_directives == []
    assert inputs.selected_context_directives == []


@pytest.mark.asyncio
async def test_query_boundary_facades_delegate_to_collaborators(
    monkeypatch,
    tmp_path,
) -> None:
    from swe.app.runner import query_preflight, query_runtime

    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    preflight = _QueryPreflight()
    runtime_start = SimpleNamespace()
    prepare_preflight = AsyncMock(return_value=preflight)
    prepare_runtime = AsyncMock(return_value=runtime_start)
    monkeypatch.setattr(
        query_preflight,
        "prepare_query_preflight",
        prepare_preflight,
    )
    monkeypatch.setattr(
        query_runtime,
        "prepare_query_runtime",
        prepare_runtime,
    )
    request = _request()
    msgs = [Msg(name="user", role="user", content="hello")]

    assert (
        await runner._prepare_query_preflight(
            session_id="session-1",
            user_id="user-1",
            query="hello",
            request=request,
        )
        is preflight
    )
    assert (
        await runner._prepare_query_runtime(
            request=request,
            msgs=msgs,
            query="hello",
            preflight=preflight,
        )
        is runtime_start
    )
    prepare_preflight.assert_awaited_once_with(
        runner,
        session_id="session-1",
        user_id="user-1",
        query="hello",
        request=request,
        session_execution=None,
    )
    prepare_runtime.assert_awaited_once_with(
        runner,
        request=request,
        msgs=msgs,
        query="hello",
        preflight=preflight,
        session_execution=None,
    )


@pytest.mark.asyncio
async def test_query_attempt_and_turn_lifecycle_facades_delegate(
    monkeypatch,
    tmp_path,
) -> None:
    from swe.app.runner import query_attempt, turn_lifecycle
    from swe.app.runner.runner import _QueryTurnOutcome, _TurnPlan

    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    request = _request()
    plan = _TurnPlan(original_user_message="hello", turn_msgs=[])
    outcome = _QueryTurnOutcome()
    attempt_calls: list[Any] = []
    turn_calls: list[Any] = []

    async def stream_query_attempt(owner, **kwargs):
        attempt_calls.extend([owner, kwargs])
        yield _blocked_msg("attempt facade"), True

    async def stream_turn_lifecycle(owner, **kwargs):
        turn_calls.extend([owner, kwargs])
        yield _blocked_msg("turn facade"), True

    monkeypatch.setattr(
        query_attempt,
        "stream_query_after_preflight",
        stream_query_attempt,
    )
    monkeypatch.setattr(
        turn_lifecycle,
        "stream_completion_lifecycle",
        stream_turn_lifecycle,
    )

    attempt_events = [
        event
        async for event in runner._stream_query_after_preflight(
            [],
            request=request,
            query="hello",
            session_id="session-1",
            preflight=_QueryPreflight(),
        )
    ]
    turn_events = [
        event
        async for event in runner._stream_completion_lifecycle(
            request=request,
            runtime=SimpleNamespace(),
            plan=plan,
            outcome=outcome,
        )
    ]

    assert [
        (msg.get_text_content(), last) for msg, last in attempt_events
    ] == [
        ("attempt facade", True),
    ]
    assert [(msg.get_text_content(), last) for msg, last in turn_events] == [
        ("turn facade", True),
    ]
    assert attempt_calls == [
        runner,
        {
            "msgs": [],
            "request": request,
            "query": "hello",
            "session_id": "session-1",
            "preflight": _QueryPreflight(),
            "session_execution": None,
        },
    ]
    assert turn_calls == [
        runner,
        {
            "request": request,
            "runtime": turn_calls[1]["runtime"],
            "plan": plan,
            "outcome": outcome,
        },
    ]


@pytest.mark.asyncio
async def test_session_lifecycle_facades_preserve_restore_snapshot_save_order(
    monkeypatch,
    tmp_path,
) -> None:
    from swe.app.runner import session_lifecycle
    from swe.app.runner.runner import _SkillFreshnessRefreshResult

    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runtime = SimpleNamespace()
    events: list[str] = []

    async def restore(owner, *, runtime):
        assert owner is runner
        assert runtime is not None
        events.append("restore")

    async def build_snapshot(owner, *, runtime, refresh_result):
        assert owner is runner
        assert runtime is not None
        assert refresh_result is not None
        events.append("snapshot")
        return {"skill": {"version": "v1"}}

    async def save(
        owner,
        agent,
        session_id,
        skip_history,
        user_id,
        hook_overlay,
    ):
        assert owner is runner
        assert agent is not None
        assert (session_id, skip_history, user_id, hook_overlay) == (
            "session-1",
            False,
            "user-1",
            None,
        )
        events.append("save")

    monkeypatch.setattr(
        session_lifecycle,
        "restore_confirmed_session_skill_context",
        restore,
    )
    monkeypatch.setattr(
        session_lifecycle,
        "build_skill_snapshot_to_persist",
        build_snapshot,
    )
    monkeypatch.setattr(session_lifecycle, "save_job_session_state", save)

    await runner._restore_confirmed_session_skill_context(runtime=runtime)
    snapshot = await runner._build_skill_snapshot_to_persist(
        runtime=runtime,
        refresh_result=_SkillFreshnessRefreshResult(),
    )
    await runner.save_job_session_state(
        SimpleNamespace(),
        "session-1",
        False,
        "user-1",
    )

    assert snapshot == {"skill": {"version": "v1"}}
    assert events == ["restore", "snapshot", "save"]


@pytest.mark.asyncio
async def test_cleanup_collaborator_waits_for_every_task_then_raises_first_error(
    monkeypatch,
) -> None:
    from swe.app.runner import query_cleanup

    completed: list[str] = []
    gather_calls: list[dict[str, Any]] = []
    original_gather = asyncio.gather

    async def gather_with_contract(*awaitables, **kwargs):
        gather_calls.append(kwargs)
        return await original_gather(*awaitables, **kwargs)

    async def cleanup(name: str, error: BaseException | None = None) -> None:
        await asyncio.sleep(0)
        completed.append(name)
        if error is not None:
            raise error

    first_error = RuntimeError("save failed")
    owner = SimpleNamespace(
        _save_state_during_cleanup=lambda **_kwargs: cleanup(
            "save",
            first_error,
        ),
        _update_chat_during_cleanup=lambda _runtime: cleanup("chat"),
        _cleanup_mcp_during_cleanup=lambda _runtime: cleanup(
            "mcp",
            RuntimeError("mcp failed"),
        ),
        _end_skill_detector_during_cleanup=lambda _runtime: cleanup(
            "detector",
        ),
    )
    monkeypatch.setattr(
        query_cleanup.asyncio,
        "gather",
        gather_with_contract,
    )

    with pytest.raises(RuntimeError, match="save failed") as error:
        await query_cleanup.cleanup_query_resources(
            owner,
            runtime=None,
            session_state_loaded=False,
            session_id="session-1",
        )

    assert error.value is first_error
    assert set(completed) == {"save", "chat", "mcp", "detector"}
    assert gather_calls == [{"return_exceptions": True}]


@pytest.mark.asyncio
async def test_cleanup_module_saves_state_with_enabled_hook_overlay() -> None:
    from swe.app.runner import query_cleanup

    saved: list[tuple[Any, str, bool, str, Any]] = []

    async def save_state(
        agent,
        session_id,
        skip_history,
        user_id,
        *,
        hook_overlay,
    ) -> None:
        saved.append((agent, session_id, skip_history, user_id, hook_overlay))

    overlay = SimpleNamespace()
    runtime = SimpleNamespace(
        tenant_hooks=SimpleNamespace(enabled=True),
        agent_config=SimpleNamespace(),
        hook_overlay=overlay,
        agent="agent-1",
        session_id="session-1",
        skip_history=False,
        user_id="user-1",
    )
    owner = SimpleNamespace(save_job_session_state=save_state)

    await query_cleanup.save_state_during_cleanup(
        owner,
        runtime=runtime,
        session_state_loaded=True,
        cleanup_timeout=1.0,
        hook_config_enabled=lambda *_args: True,
    )

    assert saved == [("agent-1", "session-1", False, "user-1", overlay)]


@pytest.mark.asyncio
async def test_runtime_input_builder_keeps_request_context_and_headers(
    monkeypatch,
    tmp_path,
) -> None:
    from swe.app.runner import query_runtime

    owner = SimpleNamespace(
        agent_id="agent-1",
        tenant_id="tenant-1",
        workspace_dir=tmp_path,
    )
    request = _request(
        source_id="source-1",
        user_name="Ada",
        cookie="session=cookie",
        passthrough_headers={"x-request-id": "request-1"},
        system_prompt_injections=["request instruction"],
    )
    preflight = _QueryPreflight(
        hook_additional_context="hook instruction",
    )
    monkeypatch.setattr(
        query_runtime,
        "get_system_prompt_injections",
        lambda: ["global instruction"],
    )

    inputs = await query_runtime.build_query_runtime_inputs(
        owner,
        request=request,
        msgs=[Msg(name="user", role="user", content="hello")],
        preflight=preflight,
        build_environment_context=lambda **kwargs: (
            f"context:{kwargs['source_id']}:{kwargs['user_name']}"
        ),
        request_source_id=lambda value: value.source_id,
        request_user_name=lambda value: value.user_name,
        request_passthrough_headers=lambda value: value.passthrough_headers,
        with_hook_context=lambda context, hook: f"{context}|{hook}",
        merge_system_prompt_injections=lambda *parts: [
            item for part in parts for item in part
        ],
        with_system_prompt_injections=lambda context, injections: (
            f"{context}|{'|'.join(injections)}"
        ),
        request_system_prompt_injections=lambda value: (
            value.system_prompt_injections
        ),
        load_tenant_hooks=lambda _tenant_id: HookConfig(),
        load_agent_configuration=lambda *_args, **_kwargs: SimpleNamespace(
            id="agent-1",
        ),
        current_passthrough_headers=lambda: {},
    )

    assert inputs.env_context == (
        "context:source-1:Ada|hook instruction|global instruction|"
        "request instruction"
    )
    assert inputs.passthrough_headers == {
        "x-request-id": "request-1",
        "cookie": "session=cookie",
    }


@pytest.mark.asyncio
async def test_runtime_finalizer_registers_mcp_before_attaching_detector() -> (
    None
):
    from swe.app.runner import query_runtime

    events: list[str] = []

    class FakeAgent:
        async def register_mcp_clients(self) -> None:
            events.append("register")

        def set_console_output_enabled(self, *, enabled: bool) -> None:
            assert enabled is False
            events.append("console")

    agent = FakeAgent()
    owner = SimpleNamespace(
        agent_id="agent-1",
        tenant_id="tenant-1",
        _create_agent_for_query=lambda **_kwargs: agent,
        _attach_session_skill_detector=lambda *, runtime, request: (
            events.append("detector")
        ),
    )
    inputs = _QueryRuntimeInputs(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        skip_history=False,
        agent_config=SimpleNamespace(),
        tenant_hooks=HookConfig(),
        hook_overlay=HookSessionOverlay(),
        env_context="base",
        selected_context_directives=["context"],
        auth_token="token-1",
        passthrough_headers={},
    )

    runtime = await query_runtime.finalize_query_runtime(
        owner,
        request=_request(),
        query="hello",
        msgs=[Msg(name="user", role="user", content="hello")],
        preflight=_QueryPreflight(),
        inputs=inputs,
        resources=SimpleNamespace(
            chat="chat-1",
            turn_id="turn-1",
            env_context="base",
        ),
        mcp_clients=["mcp-1"],
        get_last_user_text=lambda _msgs: "fallback",
        debug_log=lambda *_args: None,
    )

    assert runtime.agent is agent
    assert runtime.selected_context_directives == ["context"]
    assert events == ["register", "console", "detector"]


@pytest.mark.asyncio
async def test_runtime_module_loads_selected_skill_hooks_after_start(
    monkeypatch,
    tmp_path,
) -> None:
    from swe.app.runner import query_runtime

    loaded: list[str] = []
    overlay = HookSessionOverlay()
    inputs = SimpleNamespace(
        hook_overlay=overlay,
        selected_skill_directives=[
            SimpleNamespace(name="skill-1", path=tmp_path / "skill-1.md"),
        ],
    )

    def load_hooks(**kwargs):
        loaded.append(kwargs["skill_name"])
        return kwargs["session_state"]

    monkeypatch.setattr(
        query_runtime,
        "load_skill_hooks_for_session",
        load_hooks,
        raising=False,
    )

    result = await query_runtime.load_selected_skill_hooks(
        inputs=inputs,
        workspace_dir=tmp_path,
        tenant_id="tenant-1",
        approved_http_urls=set(),
    )

    assert result == overlay
    assert loaded == ["skill-1"]


def test_runtime_module_builds_mcp_clients_with_request_scope() -> None:
    from swe.app.runner import query_runtime

    calls: list[dict[str, object]] = []
    clients: list[object] = []

    def build_clients(mcp_config, **kwargs):
        assert mcp_config == "config"
        calls.append(kwargs)
        return ["mcp-1"]

    query_runtime.build_runtime_mcp_clients(
        clients,
        agent_config=SimpleNamespace(mcp="config"),
        tenant_id="tenant-1",
        user_id="user-1",
        passthrough_headers={"x-request-id": "request-1"},
        session_id="session-1",
        chat_id="chat-1",
        trace_id="trace-1",
        frozen_tools_by_key={"mcp-1": []},
        build_lazy_clients=build_clients,
    )

    assert clients == ["mcp-1"]
    assert calls == [
        {
            "tenant_id": "tenant-1",
            "user_id": "user-1",
            "passthrough_headers": {"x-request-id": "request-1"},
            "session_id": "session-1",
            "chat_id": "chat-1",
            "trace_id": "trace-1",
            "frozen_tools_by_key": {"mcp-1": []},
        },
    ]


@pytest.mark.asyncio
async def test_blocked_runtime_cleanup_updates_chat_before_closing_mcp(
    monkeypatch,
) -> None:
    from swe.app.runner import query_cleanup

    events: list[str] = []

    async def update_chat(chat):
        assert chat == "chat-1"
        events.append("chat")

    async def close_mcp(clients):
        assert clients == ["mcp-1"]
        events.append("mcp")

    monkeypatch.setattr(query_cleanup, "cleanup_mcp_clients", close_mcp)
    owner = SimpleNamespace(
        _chat_manager=SimpleNamespace(update_chat=update_chat),
    )
    runtime_start = SimpleNamespace(
        block_response=Msg(name="Friday", role="assistant", content="blocked"),
        runtime=None,
        blocked_session_id="session-1",
        blocked_chat="chat-1",
        blocked_mcp_clients=["mcp-1"],
    )

    await query_cleanup.cleanup_blocked_runtime_start(
        owner,
        runtime_start,
        cleanup_timeout=1.0,
    )

    assert events == ["chat", "mcp"]


@pytest.mark.asyncio
async def test_retry_backoff_precedes_terminal_error_trace_after_exhaustion(
    monkeypatch,
    tmp_path,
) -> None:
    from swe.app.runner import query_attempt

    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    events: list[str] = []
    terminal_error = RuntimeError("rate limited")
    terminal_error.status_code = 429
    attempts = 0

    async def failed_attempt(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        events.append(f"attempt-{attempts}")
        if attempts == -1:
            yield _blocked_msg("unreachable"), True
        raise terminal_error

    async def sleep(_delay: float) -> None:
        events.append("backoff")

    async def terminal_trace(**_kwargs) -> None:
        events.append("terminal trace")

    async def terminal_error_handler(**_kwargs) -> None:
        events.append("terminal error handler")

    runner._start_query_trace = AsyncMock(return_value="trace-1")
    runner._load_query_retry_settings = lambda *_args: (2, 1, 1.0, 1.0)
    runner._stream_single_query_attempt = failed_attempt
    runner._raise_console_model_call_failed_if_needed = terminal_trace
    runner._handle_query_error = terminal_error_handler
    runner._cleanup_query_resources = AsyncMock()
    runner._cleanup_blocked_runtime_start = AsyncMock()
    runner._store_qa_content_if_needed = AsyncMock()
    monkeypatch.setattr(query_attempt.asyncio, "sleep", sleep)

    with pytest.raises(RuntimeError, match="rate limited"):
        async for _msg, _last in runner._stream_query_after_preflight(
            [Msg(name="user", role="user", content="hello")],
            request=_request(),
            query="hello",
            session_id="session-1",
            preflight=_QueryPreflight(),
        ):
            pass

    assert events == [
        "attempt-1",
        "backoff",
        "attempt-2",
        "terminal trace",
        "terminal error handler",
    ]


def test_runner_keeps_only_collaborator_lifecycle_facades() -> None:
    legacy_methods = {
        "_stream_completion_lifecycle_legacy",
        "_stream_single_query_attempt_legacy",
        "_stream_query_after_preflight_legacy",
    }

    assert not legacy_methods.intersection(vars(AgentRunner))


def test_turn_lifecycle_delegates_goal_and_stop_orchestration() -> None:
    from swe.app.runner import turn_lifecycle

    helpers = {
        "_resolve_implicit_goal_steering",
        "_begin_goal_turn",
        "_settle_goal_turn",
        "_wait_for_goal_wake",
        "_stream_goal_completion_lifecycle",
        "_stream_standard_completion_lifecycle",
        "_resolve_stop_gate",
    }

    assert helpers.issubset(vars(turn_lifecycle))


def test_session_lifecycle_does_not_expose_swe_agent_at_runtime() -> None:
    from swe.app.runner import session_lifecycle

    assert "SWEAgent" not in vars(session_lifecycle)


@pytest.mark.asyncio
async def test_query_runtime_collaborator_preserves_assembly_order(
    monkeypatch,
) -> None:
    from swe.app.runner import query_runtime

    events: list[str] = []

    async def refresh_if_due() -> None:
        events.append("refresh")

    manager = SimpleNamespace(refresh_if_due=refresh_if_due)

    async def get_or_create_instance(tenant_id: str) -> object:
        assert tenant_id == "tenant-1"
        events.append("provider")
        return manager

    async def build_inputs(**_kwargs) -> object:
        events.append("inputs")
        return SimpleNamespace()

    async def start_resources(**kwargs) -> tuple[object, None]:
        kwargs["mcp_clients"].append(object())
        events.extend(
            [
                "resources",
                "lazy MCP",
                "SESSION_START",
                "selected-skill hooks",
            ],
        )
        return SimpleNamespace(), None

    async def finalize_runtime(**_kwargs) -> object:
        events.extend(["agent build", "register_mcp_clients"])
        return SimpleNamespace()

    owner = SimpleNamespace(
        tenant_id="tenant-1",
        _build_query_runtime_inputs=build_inputs,
        _start_query_runtime_resources=start_resources,
        _finalize_query_runtime=finalize_runtime,
        _cleanup_query_runtime_mcp_clients=AsyncMock(),
    )
    monkeypatch.setattr(
        query_runtime.ProviderManager,
        "get_or_create_instance",
        get_or_create_instance,
    )

    result = await query_runtime.prepare_query_runtime(
        owner,
        request=_request(),
        msgs=[Msg(name="user", role="user", content="hello")],
        query="hello",
        preflight=_QueryPreflight(),
    )

    assert result.runtime is not None
    assert events == [
        "provider",
        "refresh",
        "inputs",
        "resources",
        "lazy MCP",
        "SESSION_START",
        "selected-skill hooks",
        "agent build",
        "register_mcp_clients",
    ]
    owner._cleanup_query_runtime_mcp_clients.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_runtime_collaborator_returns_session_start_block(
    monkeypatch,
) -> None:
    from swe.app.runner import query_runtime

    events: list[str] = []
    block_result = _RuntimeStartResult(block_response=_blocked_msg("blocked"))

    async def refresh_if_due() -> None:
        events.append("refresh")

    async def get_or_create_instance(_tenant_id: str) -> object:
        events.append("provider")
        return SimpleNamespace(refresh_if_due=refresh_if_due)

    async def build_inputs(**_kwargs) -> object:
        events.append("inputs")
        return SimpleNamespace()

    async def start_resources(**kwargs) -> tuple[object, _RuntimeStartResult]:
        kwargs["mcp_clients"].append(object())
        events.extend(["resources", "lazy MCP", "SESSION_START"])
        return SimpleNamespace(), block_result

    owner = SimpleNamespace(
        tenant_id="tenant-1",
        _build_query_runtime_inputs=build_inputs,
        _start_query_runtime_resources=start_resources,
        _finalize_query_runtime=AsyncMock(),
        _cleanup_query_runtime_mcp_clients=AsyncMock(),
    )
    monkeypatch.setattr(
        query_runtime.ProviderManager,
        "get_or_create_instance",
        get_or_create_instance,
    )

    result = await query_runtime.prepare_query_runtime(
        owner,
        request=_request(),
        msgs=[],
        query=None,
        preflight=_QueryPreflight(),
    )

    assert result is block_result
    assert events == [
        "provider",
        "refresh",
        "inputs",
        "resources",
        "lazy MCP",
        "SESSION_START",
    ]
    owner._finalize_query_runtime.assert_not_awaited()
    owner._cleanup_query_runtime_mcp_clients.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_runtime_collaborator_cleans_mcp_after_assembly_error(
    monkeypatch,
) -> None:
    from swe.app.runner import query_runtime

    events: list[str] = []
    mcp_client = object()

    async def refresh_if_due() -> None:
        events.append("refresh")

    async def get_or_create_instance(_tenant_id: str) -> object:
        events.append("provider")
        return SimpleNamespace(refresh_if_due=refresh_if_due)

    async def build_inputs(**_kwargs) -> object:
        events.append("inputs")
        return SimpleNamespace()

    async def start_resources(**kwargs) -> tuple[object, None]:
        kwargs["mcp_clients"].append(mcp_client)
        events.extend(["resources", "lazy MCP", "SESSION_START"])
        return SimpleNamespace(), None

    async def finalize_runtime(**_kwargs) -> object:
        events.append("agent build")
        raise RuntimeError("agent build failed")

    async def cleanup_mcp_clients(clients: list[object]) -> None:
        assert clients == [mcp_client]
        events.append("cleanup")

    owner = SimpleNamespace(
        tenant_id="tenant-1",
        _build_query_runtime_inputs=build_inputs,
        _start_query_runtime_resources=start_resources,
        _finalize_query_runtime=finalize_runtime,
        _cleanup_query_runtime_mcp_clients=cleanup_mcp_clients,
    )
    monkeypatch.setattr(
        query_runtime.ProviderManager,
        "get_or_create_instance",
        get_or_create_instance,
    )

    with pytest.raises(RuntimeError, match="agent build failed"):
        await query_runtime.prepare_query_runtime(
            owner,
            request=_request(),
            msgs=[],
            query=None,
            preflight=_QueryPreflight(),
        )

    assert events == [
        "provider",
        "refresh",
        "inputs",
        "resources",
        "lazy MCP",
        "SESSION_START",
        "agent build",
        "cleanup",
    ]


@pytest.mark.asyncio
async def test_user_prompt_block_terminates_before_runtime_preparation(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SimpleNamespace(
        get_session_state_dict=AsyncMock(return_value={}),
        mutate_session_state=AsyncMock(return_value={}),
    )
    runner._chat_manager = None
    tenant_hooks = HookConfig(
        enabled=True,
        events={
            HookEventName.USER_PROMPT_SUBMIT: [
                HookMatcherGroupConfig(
                    hooks=[
                        CommandHookHandlerConfig(
                            id="blocker",
                            command="unused",
                        ),
                    ],
                ),
            ],
        },
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *_args, **_kwargs: _agent_config(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *_args, **_kwargs: tenant_hooks,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        AsyncMock(
            return_value=MergedHookResult(
                decision=HookDecision.BLOCK,
                reason="prompt blocked",
            ),
        ),
    )
    prepare_runtime = AsyncMock()
    runner._prepare_query_runtime = prepare_runtime

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="hello")],
            request=_request(),
        )
    ]

    assert [(msg.get_text_content(), last) for msg, last in outputs] == [
        ("prompt blocked", True),
    ]
    prepare_runtime.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("approval_consumed", [False, True])
async def test_command_dispatch_requires_unconsumed_approval(
    monkeypatch,
    tmp_path,
    approval_consumed: bool,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner._prepare_query_preflight = AsyncMock(
        return_value=_QueryPreflight(
            approval_consumed=approval_consumed,
        ),
    )
    command_calls: list[str] = []

    async def command_path(*_args):
        command_calls.append("command")
        yield _blocked_msg("command response"), True

    async def normal_path(*_args, **_kwargs):
        yield _blocked_msg("normal response"), True

    monkeypatch.setattr(
        "swe.app.runner.query_execution.admission.run_command_path",
        command_path,
    )
    runner._stream_query_after_preflight = normal_path
    runner._start_query_trace = AsyncMock(return_value=None)
    runner._end_trace_if_needed = AsyncMock()

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="/history")],
            request=_request(),
        )
    ]

    expected = "normal response" if approval_consumed else "command response"
    assert outputs[-1][0].get_text_content() == expected
    assert command_calls == ([] if approval_consumed else ["command"])


@pytest.mark.asyncio
async def test_runtime_resources_resolve_chat_before_mcp_setup(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    chat = SimpleNamespace(id="chat-1")
    events: list[object] = []

    async def get_or_create_chat(*_args, **_kwargs):
        events.append("chat")
        return chat

    def build_clients(*_args, **kwargs):
        events.append(("mcp", kwargs["chat_id"]))
        return []

    runner._chat_manager = SimpleNamespace(
        get_or_create_chat=get_or_create_chat,
    )
    runner._emit_session_start_hook = AsyncMock(return_value=("base", None))
    monkeypatch.setattr(
        "swe.app.runner.runner._build_lazy_mcp_clients",
        build_clients,
    )
    monkeypatch.setattr(
        "swe.app.runner.context_references.build_context_reference_directives",
        AsyncMock(return_value=[]),
    )

    inputs = _QueryRuntimeInputs(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        skip_history=False,
        agent_config=_agent_config(),
        tenant_hooks=HookConfig(),
        hook_overlay=HookSessionOverlay(),
        env_context="base",
        selected_context_directives=[],
        auth_token=None,
        passthrough_headers={},
    )
    clients: list[object] = []

    resources, block_result = await runner._start_query_runtime_resources(
        request=_request(),
        msgs=[Msg(name="user", role="user", content="hello")],
        inputs=inputs,
        mcp_clients=clients,
    )

    assert resources.chat is chat
    assert block_result is None
    assert events == ["chat", ("mcp", "chat-1")]


@pytest.mark.asyncio
async def test_session_start_block_cleans_previously_created_chat_and_mcp(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner._prepare_query_preflight = AsyncMock(return_value=_QueryPreflight())
    chat = SimpleNamespace(id="chat-1", meta={})
    mcp_client = object()
    events: list[object] = []

    async def get_or_create_chat(*_args, **_kwargs):
        events.append(("chat created", chat))
        return chat

    async def update_chat(updated_chat):
        events.append(("chat cleaned", updated_chat))

    async def cleanup_mcp(clients):
        events.append(("mcp cleaned", clients))

    runner._chat_manager = SimpleNamespace(
        get_or_create_chat=get_or_create_chat,
        update_chat=update_chat,
    )
    monkeypatch.setattr(
        "swe.app.runner.query_cleanup.cleanup_mcp_clients",
        cleanup_mcp,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._build_lazy_mcp_clients",
        lambda *_args, **_kwargs: [mcp_client],
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.build_env_context",
        lambda **_kwargs: "base",
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *_args, **_kwargs: SimpleNamespace(
            id="test-agent",
            mcp=None,
            hooks=HookConfig(enabled=True),
            running=SimpleNamespace(),
        ),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *_args, **_kwargs: HookConfig(enabled=True),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._resolve_active_model_label",
        lambda *_args, **_kwargs: "openai/gpt-test",
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        AsyncMock(
            return_value=MergedHookResult(
                decision=HookDecision.BLOCK,
                reason="session start blocked",
            ),
        ),
    )
    runner._start_query_trace = AsyncMock(return_value=None)
    runner._end_trace_if_needed = AsyncMock()
    runner._load_query_retry_settings = lambda *_args: (1, 0, 0.0, 0.0)
    runner._store_qa_content_if_needed = AsyncMock()

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="hello")],
            request=_request(),
        )
    ]

    assert outputs[-1][0].get_text_content() == "session start blocked"
    assert events == [
        ("chat created", chat),
        ("chat cleaned", chat),
        ("mcp cleaned", [mcp_client]),
    ]


@pytest.mark.asyncio
async def test_retry_notice_is_streamed_before_the_next_attempt(
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner._start_query_trace = AsyncMock(return_value=None)
    runner._end_trace_if_needed = AsyncMock()
    runner._load_query_retry_settings = lambda *_args: (2, 1, 0.0, 0.0)
    runner._cleanup_query_resources = AsyncMock()
    runner._cleanup_blocked_runtime_start = AsyncMock()
    runner._store_qa_content_if_needed = AsyncMock()
    attempts: list[str] = []

    async def attempt(*_args, **_kwargs):
        attempts.append(f"attempt-{len(attempts) + 1}")
        if len(attempts) == 1:
            error = RuntimeError("rate limiter unavailable")
            error.status_code = 429
            raise error
        yield _blocked_msg("second attempt response"), True

    runner._stream_single_query_attempt = attempt

    stream = runner._stream_query_after_preflight(
        [Msg(name="user", role="user", content="hello")],
        request=_request(),
        query="hello",
        session_id="session-1",
        preflight=_QueryPreflight(),
    )

    try:
        first_notice, first_last = await anext(stream)
        assert (
            first_notice.get_text_content()
            == "请求频率超限，正在重试 (1/1)..."
        )
        assert first_last is False
        assert attempts == ["attempt-1"]

        retry_notice, retry_last = await anext(stream)
        assert retry_notice.get_text_content() == "正在重试 (1/1)..."
        assert retry_last is False
        assert attempts == ["attempt-1"]

        response, response_last = await anext(stream)
        assert response.get_text_content() == "second attempt response"
        assert response_last is True
        assert attempts == ["attempt-1", "attempt-2"]
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_finally_cleans_each_query_resource(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    events: list[str] = []

    async def save_job_session_state(*_args, **_kwargs):
        events.append("session save")

    async def update_chat(*_args, **_kwargs):
        events.append("chat update")

    async def cleanup_mcp(*_args, **_kwargs):
        events.append("mcp close")

    async def detector_shutdown():
        events.append("detector shutdown")

    runner.save_job_session_state = save_job_session_state
    runner._chat_manager = SimpleNamespace(update_chat=update_chat)
    monkeypatch.setattr(
        "swe.app.runner.query_cleanup.cleanup_mcp_clients",
        cleanup_mcp,
    )
    runtime = _QueryRuntime(
        agent=SimpleNamespace(),
        agent_config=_agent_config(),
        tenant_hooks=HookConfig(),
        hook_overlay=HookSessionOverlay(),
        chat=SimpleNamespace(id="chat-1"),
        session_skill_detector=SimpleNamespace(
            on_reasoning_end=detector_shutdown,
        ),
        mcp_clients=[object()],
        session_id="session-1",
        user_id="user-1",
        channel="console",
        skip_history=False,
        pending_confirmed_skill_snapshots={},
    )

    async def successful_attempt(*_args, attempt_state, **_kwargs):
        attempt_state.runtime = runtime
        attempt_state.session_state_loaded = True
        attempt_state.succeeded = True
        if attempt_state.should_return:
            yield _blocked_msg("unreachable"), True

    runner._start_query_trace = AsyncMock(return_value=None)
    runner._load_query_retry_settings = lambda *_args: (1, 0, 0.0, 0.0)
    runner._stream_single_query_attempt = successful_attempt
    runner._cleanup_blocked_runtime_start = AsyncMock()
    runner._store_qa_content_if_needed = AsyncMock()

    outputs = [
        item
        async for item in runner._stream_query_after_preflight(
            [Msg(name="user", role="user", content="hello")],
            request=_request(),
            query="hello",
            session_id="session-1",
            preflight=_QueryPreflight(),
        )
    ]

    assert outputs == []
    assert set(events) == {
        "session save",
        "chat update",
        "mcp close",
        "detector shutdown",
    }
    assert len(events) == 4


@pytest.mark.asyncio
async def test_cancelled_attempt_cancels_trace_then_runs_finally_cleanup_and_resets_context(
    monkeypatch,
    tmp_path,
) -> None:
    from agentscope_runtime.engine.schemas.exception import AgentException

    from swe.app.runner import query_attempt
    from swe.tracing.models import TraceStatus

    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    events: list[str] = []
    agent = SimpleNamespace(interrupt=AsyncMock())
    runtime = SimpleNamespace(agent=agent)

    class ClaimsContext:
        def __enter__(self) -> None:
            events.append("claims enter")

        def __exit__(self, *_args) -> None:
            events.append("claims reset")

    async def cancelled_attempt(*_args, attempt_state, **_kwargs):
        attempt_state.runtime = runtime
        attempt_state.session_state_loaded = True
        if attempt_state.should_return:
            yield _blocked_msg("unreachable"), True
        raise asyncio.CancelledError()

    runner._request_file_url_network = lambda _request: None
    runner._start_query_trace = AsyncMock(return_value="trace-1")
    runner._end_trace_if_needed = AsyncMock()
    runner._load_query_retry_settings = lambda *_args: (1, 0, 0.0, 0.0)
    runner._stream_single_query_attempt = cancelled_attempt
    runner._cleanup_query_resources = AsyncMock(
        side_effect=lambda **_kwargs: events.append("resource cleanup"),
    )
    runner._cleanup_blocked_runtime_start = AsyncMock(
        side_effect=lambda _runtime_start: events.append("blocked cleanup"),
    )
    runner._store_qa_content_if_needed = AsyncMock(
        side_effect=lambda **_kwargs: events.append("qa cleanup"),
    )
    monkeypatch.setattr(
        query_attempt,
        "runtime_invocation_claims_context",
        lambda **_kwargs: ClaimsContext(),
    )
    monkeypatch.setattr(
        query_attempt,
        "set_current_file_url_network",
        lambda _value: "network-token",
    )
    monkeypatch.setattr(
        query_attempt,
        "reset_current_file_url_network",
        lambda _token: events.append("network reset"),
    )

    with pytest.raises(AgentException, match="Task has been cancelled"):
        async for _msg, _last in runner._stream_query_after_preflight(
            [Msg(name="user", role="user", content="hello")],
            request=_request(),
            query="hello",
            session_id="session-1",
            preflight=_QueryPreflight(),
        ):
            pass

    runner._end_trace_if_needed.assert_awaited_once_with(
        "trace-1",
        TraceStatus.CANCELLED,
    )
    agent.interrupt.assert_awaited_once_with()
    runner._cleanup_query_resources.assert_awaited_once_with(
        runtime=runtime,
        session_state_loaded=True,
        session_id="session-1",
    )
    runner._cleanup_blocked_runtime_start.assert_awaited_once_with(None)
    runner._store_qa_content_if_needed.assert_awaited_once()
    assert events == [
        "claims enter",
        "claims reset",
        "network reset",
        "resource cleanup",
        "blocked cleanup",
        "qa cleanup",
    ]

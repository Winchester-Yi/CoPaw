# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import Msg

from swe.agents.hook_runtime.models import (
    CommandHookHandlerConfig,
    LoadedSkillHookSource,
    HookConfig,
    HookDecision,
    HookEventName,
    HookMatcherGroupConfig,
    HookSessionState,
    HookSessionOverlay,
    AdditionalContext,
    MergedHookResult,
)
from swe.app.runner.runner import (
    AgentRunner,
    _create_session_skill_detector,
    _hook_config_enabled,
    _QueryRuntime,
    _TurnPlan,
    _QueryTurnOutcome,
)
from swe.app.runner.session import SafeJSONSession
from swe.config.config import SuggestionMode


def _agent_config(hooks: HookConfig | None = None):
    return SimpleNamespace(
        id="test-agent",
        hooks=hooks or HookConfig(),
        mcp=None,
        running=SimpleNamespace(
            suggestions=SimpleNamespace(
                enabled=False,
                mode=SuggestionMode.DISABLED,
            ),
            post_turn_validation=SimpleNamespace(enabled=False),
        ),
    )


class _FakeAgent:
    last_env_context = ""

    def __init__(self, **kwargs):
        self.memory = _FakeMemory()
        self.env_context = kwargs.get("env_context", "")
        _FakeAgent.last_env_context = self.env_context

    async def register_mcp_clients(self):
        return

    def set_console_output_enabled(self, enabled=False):
        del enabled

    def rebuild_sys_prompt(self):
        return

    async def __call__(self, turn_msgs):
        for msg in turn_msgs:
            self.memory.content.append((msg, []))
        reply = Msg(name="Friday", role="assistant", content="agent reply")
        self.memory.content.append((reply, []))
        return [reply]

    def state_dict(self):
        return {
            "memory": {
                "content": [
                    [msg.to_dict(), marks]
                    for msg, marks in self.memory.content
                ],
            },
        }


class _FakeMemory:
    def __init__(self):
        self.content = []

    async def add(self, msg, marks=None):
        if marks is None:
            normalized_marks = []
        elif isinstance(marks, list):
            normalized_marks = marks
        else:
            normalized_marks = [marks]
        self.content.append((msg, normalized_marks))


async def _fake_stream_printing_messages(*, agents, coroutine_task):
    del agents
    turn_msgs = await coroutine_task
    for msg in turn_msgs:
        yield msg, True


def _patch_normal_agent_path(monkeypatch):
    monkeypatch.setattr(
        "swe.app.runner.runner._build_and_connect_mcp_clients",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr("swe.app.runner.runner.SWEAgent", _FakeAgent)
    monkeypatch.setattr(
        "swe.app.runner.runner.stream_printing_messages",
        _fake_stream_printing_messages,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._cleanup_mcp_clients",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.build_env_context",
        lambda **kwargs: "base context",
    )


def test_hook_config_enabled_accepts_loaded_skill_sources() -> None:
    state = HookSessionState(
        loaded_skill_sources=[
            LoadedSkillHookSource(
                source_id="skill:xlsx",
                skill_name="xlsx",
                skill_root="/workspace/skills/xlsx",
                source_path="/workspace/skills/xlsx/hooks/hooks.json",
                hook_config=HookConfig(
                    enabled=True,
                    events={
                        HookEventName.STOP: [
                            HookMatcherGroupConfig(
                                id="skill:xlsx:stop",
                                hooks=[
                                    CommandHookHandlerConfig(
                                        id="skill:xlsx:stop-hook",
                                        command="echo {}",
                                    ),
                                ],
                            ),
                        ],
                    },
                ),
            ),
        ],
    )

    assert _hook_config_enabled(HookConfig(), _agent_config(), state)


@pytest.mark.asyncio
async def test_create_session_skill_detector_loads_skill_hooks(
    tmp_path,
) -> None:
    skill_root = tmp_path / "skills" / "xlsx"
    (skill_root / "hooks").mkdir(parents=True)
    (skill_root / "scripts").mkdir()
    (skill_root / "scripts" / "check.py").write_text(
        "print('{}')\n",
        encoding="utf-8",
    )
    (skill_root / "hooks" / "hooks.json").write_text(
        """
        {
          "enabled": true,
          "events": {
            "Stop": [
              {
                "hooks": [
                  {
                    "id": "stop",
                    "type": "command",
                    "argv": ["python", "scripts/check.py"]
                  }
                ]
              }
            ]
          }
        }
        """,
        encoding="utf-8",
    )
    state = HookSessionState()

    def get_state() -> HookSessionState:
        return state

    def set_state(new_state: HookSessionState) -> None:
        nonlocal state
        state = new_state

    detector = _create_session_skill_detector(
        workspace_dir=tmp_path,
        tenant_id="tenant-a",
        user_id="user-1",
        session_id="session-1",
        channel="console",
        source_id="source-1",
        enabled_skills=["xlsx"],
        get_hook_state=get_state,
        set_hook_state=set_state,
        approved_http_urls=set(),
    )

    await detector.start_skill(
        "xlsx",
        trigger_tool="user_message",
        trigger_reason="declared",
    )

    assert state.loaded_skill_sources[0].source_id == "skill:xlsx"
    handler = (
        state.loaded_skill_sources[0]
        .hook_config.events[HookEventName.STOP][0]
        .hooks[0]
    )
    assert handler.id == "skill:xlsx:stop"


@pytest.mark.asyncio
async def test_create_session_skill_detector_loads_http_skill_hooks_without_approvals(
    tmp_path,
) -> None:
    skill_root = tmp_path / "skills" / "xlsx"
    (skill_root / "hooks").mkdir(parents=True)
    (skill_root / "scripts").mkdir()
    (skill_root / "hooks" / "hooks.json").write_text(
        """
        {
          "enabled": true,
          "events": {
            "Stop": [
              {
                "hooks": [
                  {
                    "id": "notify",
                    "type": "http",
                    "url": "https://hooks.example.test/skill"
                  }
                ]
              }
            ]
          }
        }
        """,
        encoding="utf-8",
    )
    state = HookSessionState()

    def get_state() -> HookSessionState:
        return state

    def set_state(new_state: HookSessionState) -> None:
        nonlocal state
        state = new_state

    detector = _create_session_skill_detector(
        workspace_dir=tmp_path,
        tenant_id="tenant-a",
        user_id="user-1",
        session_id="session-1",
        channel="console",
        source_id="source-1",
        enabled_skills=["xlsx"],
        get_hook_state=get_state,
        set_hook_state=set_state,
        approved_http_urls=set(),
    )

    await detector.start_skill(
        "xlsx",
        trigger_tool="user_message",
        trigger_reason="declared",
    )

    handler = (
        state.loaded_skill_sources[0]
        .hook_config.events[HookEventName.STOP][0]
        .hooks[0]
    )
    assert handler.id == "skill:xlsx:notify"
    assert handler.url == "https://hooks.example.test/skill"


@pytest.mark.asyncio
async def test_query_handler_user_prompt_hook_blocks_before_command_dispatch(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SimpleNamespace(
        get_session_state_dict=AsyncMock(return_value={}),
    )
    setattr(runner, "_chat_manager", None)
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
        lambda *args, **kwargs: _agent_config(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: tenant_hooks,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        AsyncMock(
            return_value=MergedHookResult(
                decision=HookDecision.BLOCK,
                reason="blocked prompt",
            ),
        ),
    )
    command_path = AsyncMock()
    monkeypatch.setattr("swe.app.runner.runner.run_command_path", command_path)

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="/history")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert outputs[-1][1] is True
    assert "blocked prompt" in outputs[-1][0].get_text_content()
    command_path.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_handler_no_config_does_not_emit_hook(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SimpleNamespace(
        get_session_state_dict=AsyncMock(return_value={}),
    )
    setattr(runner, "_chat_manager", None)

    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(),
    )
    emit_hook = AsyncMock(return_value=MergedHookResult())
    monkeypatch.setattr("swe.app.runner.runner._emit_runner_hook", emit_hook)

    async def fake_run_command_path(request, msgs, runner):
        yield Msg(name="Friday", role="assistant", content="command"), True

    monkeypatch.setattr(
        "swe.app.runner.runner.run_command_path",
        fake_run_command_path,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="/history")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert outputs[-1][0].get_text_content() == "command"
    emit_hook.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_handler_loads_session_skill_hooks_for_media_message(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._resolve_active_model_label",
        lambda *args, **kwargs: "openai/gpt-test",
    )
    emit_hook = AsyncMock(return_value=MergedHookResult())
    monkeypatch.setattr("swe.app.runner.runner._emit_runner_hook", emit_hook)

    persisted_overlay = HookSessionState(
        loaded_skill_sources=[
            LoadedSkillHookSource(
                source_id="skill:xlsx",
                skill_name="xlsx",
                skill_root=str(tmp_path / "skills" / "xlsx"),
                source_path=str(
                    tmp_path / "skills" / "xlsx" / "hooks" / "hooks.json",
                ),
                hook_config=HookConfig(
                    enabled=True,
                    events={
                        HookEventName.STOP: [
                            HookMatcherGroupConfig(
                                hooks=[
                                    CommandHookHandlerConfig(
                                        id="skill:xlsx:stop",
                                        command="unused",
                                    ),
                                ],
                            ),
                        ],
                    },
                ),
            ),
        ],
    )
    await runner.session.save_merged_state(
        session_id="session-1",
        user_id="user-1",
        state={
            "hook_overlay": persisted_overlay.model_dump(
                mode="json",
                by_alias=True,
            ),
        },
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [
        Msg(
            name="user",
            role="user",
            content=[{"type": "image", "url": "file:///tmp/image.png"}],
        ),
    ]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    emitted_events = [call.args[0] for call in emit_hook.await_args_list]
    assert HookEventName.USER_PROMPT_SUBMIT not in emitted_events
    assert emitted_events == [
        HookEventName.SESSION_START,
        HookEventName.BEFORE_STOP,
        HookEventName.STOP,
    ]


@pytest.mark.asyncio
async def test_query_handler_injects_prompt_additional_context(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._resolve_active_model_label",
        lambda *args, **kwargs: "openai/gpt-test",
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        AsyncMock(
            side_effect=[
                MergedHookResult(
                    session_title="Hooked",
                    additional_context=[
                        AdditionalContext(
                            handler_id="prompt",
                            context="prompt context",
                        ),
                    ],
                ),
                MergedHookResult(
                    additional_context=[
                        AdditionalContext(
                            handler_id="start",
                            context="start context",
                        ),
                    ],
                ),
                MergedHookResult(),
                MergedHookResult(),
            ],
        ),
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    assert request.channel_meta["session_title"] == "Hooked"
    assert "prompt context" in _FakeAgent.last_env_context
    assert "start context" in _FakeAgent.last_env_context


@pytest.mark.asyncio
async def test_query_handler_session_start_block_yields_before_cleanup(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    chat = SimpleNamespace(id="chat-1")
    chat_manager = SimpleNamespace(
        get_or_create_chat=AsyncMock(return_value=chat),
        update_chat=AsyncMock(return_value=chat),
    )
    setattr(runner, "_chat_manager", chat_manager)

    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def slow_cleanup(clients):
        assert clients == ["mcp-client"]
        cleanup_started.set()
        await cleanup_release.wait()

    monkeypatch.setattr(
        "swe.app.runner.runner._build_and_connect_mcp_clients",
        AsyncMock(return_value=["mcp-client"]),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._cleanup_mcp_clients",
        slow_cleanup,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.build_env_context",
        lambda **kwargs: "base context",
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._resolve_active_model_label",
        lambda *args, **kwargs: "openai/gpt-test",
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        AsyncMock(
            side_effect=[
                MergedHookResult(),
                MergedHookResult(
                    decision=HookDecision.BLOCK,
                    reason="session start blocked",
                ),
            ],
        ),
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]
    stream = runner.query_handler(msgs, request=request)
    next_item = asyncio.create_task(anext(stream))

    try:
        done, _pending = await asyncio.wait({next_item}, timeout=0.05)
        assert next_item in done
        msg, last = next_item.result()
        assert last is True
        assert msg.get_text_content() == "session start blocked"
        assert not cleanup_started.is_set()

        close_task = asyncio.create_task(stream.aclose())
        await asyncio.wait_for(cleanup_started.wait(), timeout=0.5)
        chat_manager.update_chat.assert_awaited_once_with(chat)
        cleanup_release.set()
        await asyncio.wait_for(close_task, timeout=0.5)
    finally:
        cleanup_release.set()
        if not next_item.done():
            next_item.cancel()
            await asyncio.gather(next_item, return_exceptions=True)


@pytest.mark.asyncio
async def test_query_handler_before_stop_allow_emits_stop_and_completes(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    emit_hook = AsyncMock()

    async def fake_emit_runner_hook(event_name, **kwargs):
        await emit_hook(event_name, **kwargs)
        if event_name == HookEventName.BEFORE_STOP:
            assert kwargs["assistant_response"] == "agent reply"
            return MergedHookResult(
                decision=HookDecision.ALLOW,
                reason="completion approved",
            )
        return MergedHookResult()

    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        fake_emit_runner_hook,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert [item[0].get_text_content() for item in outputs] == [
        "agent reply",
    ]
    assert [call.args[0] for call in emit_hook.await_args_list] == [
        HookEventName.USER_PROMPT_SUBMIT,
        HookEventName.SESSION_START,
        HookEventName.BEFORE_STOP,
        HookEventName.STOP,
    ]


@pytest.mark.asyncio
async def test_query_handler_before_stop_block_continues_without_stop(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    before_stop_calls = 0
    stop_calls = 0

    async def fake_emit_runner_hook(event_name, **kwargs):
        nonlocal before_stop_calls, stop_calls
        if event_name == HookEventName.BEFORE_STOP:
            before_stop_calls += 1
            if before_stop_calls == 1:
                return MergedHookResult(
                    decision=HookDecision.BLOCK,
                    reason="test tests before stopping",
                )
            return MergedHookResult(
                decision=HookDecision.ALLOW,
                reason="completion approved",
            )
        if event_name == HookEventName.STOP:
            stop_calls += 1
        return MergedHookResult()

    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        fake_emit_runner_hook,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert [item[0].get_text_content() for item in outputs] == [
        "agent reply",
        "agent reply",
    ]
    assert before_stop_calls == 2
    assert stop_calls == 1


@pytest.mark.asyncio
async def test_query_handler_before_stop_block_exhausts_default_budget(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    before_stop_calls = 0

    async def fake_emit_runner_hook(event_name, **kwargs):
        nonlocal before_stop_calls
        if event_name == HookEventName.BEFORE_STOP:
            before_stop_calls += 1
            return MergedHookResult(
                decision=HookDecision.BLOCK,
                reason=f"reason-{before_stop_calls}",
            )
        return MergedHookResult()

    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        fake_emit_runner_hook,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]
    output_texts = [item[0].get_text_content() for item in outputs]

    assert output_texts[:3] == ["agent reply", "agent reply", "agent reply"]
    assert "任务未完成" in output_texts[-1]
    assert "reason-3" in output_texts[-1]
    assert before_stop_calls == 3


@pytest.mark.asyncio
async def test_query_handler_before_stop_budget_exhaustion_finalizes_trace(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    runner._store_pending_validation_if_needed = AsyncMock()
    runner._generate_backend_suggestions_if_needed = AsyncMock()
    runner._index_model_output_if_needed = AsyncMock()
    runner._end_trace_if_needed = AsyncMock()

    async def fake_emit_runner_hook(event_name, **kwargs):
        if event_name == HookEventName.BEFORE_STOP:
            return MergedHookResult(
                decision=HookDecision.BLOCK,
                reason="still incomplete",
            )
        return MergedHookResult()

    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        fake_emit_runner_hook,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert "任务未完成" in outputs[-1][0].get_text_content()
    runner._store_pending_validation_if_needed.assert_not_awaited()
    runner._generate_backend_suggestions_if_needed.assert_not_awaited()
    runner._index_model_output_if_needed.assert_awaited_once()
    runner._end_trace_if_needed.assert_awaited_once()


@pytest.mark.asyncio
async def test_query_handler_before_stop_budget_exhaustion_persists_notice(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )

    async def fake_emit_runner_hook(event_name, **kwargs):
        if event_name == HookEventName.BEFORE_STOP:
            return MergedHookResult(
                decision=HookDecision.BLOCK,
                reason="still incomplete",
            )
        return MergedHookResult()

    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        fake_emit_runner_hook,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]
    notice_text = outputs[-1][0].get_text_content()
    stored_state = await runner.session.get_session_state_dict(
        session_id="session-1",
        user_id="user-1",
    )
    stored_content = stored_state["agent"]["memory"]["content"]
    stored_texts = [entry[0]["content"] for entry in stored_content]

    assert "任务未完成" in notice_text
    assert stored_texts[-1] == notice_text


@pytest.mark.asyncio
async def test_query_handler_before_stop_defers_completion_side_effects(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    runner._store_pending_validation_if_needed = AsyncMock()
    runner._generate_backend_suggestions_if_needed = AsyncMock()
    runner._index_model_output_if_needed = AsyncMock()
    runner._end_trace_if_needed = AsyncMock()
    before_stop_calls = 0

    async def fake_emit_runner_hook(event_name, **kwargs):
        nonlocal before_stop_calls
        if event_name == HookEventName.BEFORE_STOP:
            before_stop_calls += 1
            if before_stop_calls == 1:
                runner._store_pending_validation_if_needed.assert_not_awaited()
                runner._generate_backend_suggestions_if_needed.assert_not_awaited()
                runner._index_model_output_if_needed.assert_not_awaited()
                runner._end_trace_if_needed.assert_not_awaited()
                return MergedHookResult(
                    decision=HookDecision.BLOCK,
                    reason="run checks first",
                )
            return MergedHookResult(decision=HookDecision.ALLOW, reason="ok")
        return MergedHookResult()

    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        fake_emit_runner_hook,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert [item[0].get_text_content() for item in outputs] == [
        "agent reply",
        "agent reply",
    ]
    runner._store_pending_validation_if_needed.assert_awaited_once()
    runner._generate_backend_suggestions_if_needed.assert_awaited_once()
    runner._index_model_output_if_needed.assert_awaited_once()
    runner._end_trace_if_needed.assert_awaited_once()


@pytest.mark.asyncio
async def test_query_handler_aggregate_budget_counts_validation_and_before_stop(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    agent_config = _agent_config(HookConfig(enabled=True))
    agent_config.running.post_turn_validation = SimpleNamespace(
        enabled=True,
        max_auto_turns=1,
        timeout_seconds=5.0,
        user_message_max_length=300,
        assistant_response_max_length=1200,
    )
    agent_config.running.max_before_stop_turns = 2
    agent_config.running.max_automatic_follow_up_turns = 2
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: agent_config,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.validate_task_completion",
        AsyncMock(
            side_effect=[
                SimpleNamespace(
                    completed=False,
                    reason="validation wants more",
                    follow_up_prompt="继续完成剩余步骤。",
                ),
                SimpleNamespace(
                    completed=True,
                    reason="validation ok",
                    follow_up_prompt="",
                ),
                SimpleNamespace(
                    completed=True,
                    reason="validation ok",
                    follow_up_prompt="",
                ),
            ],
        ),
    )
    before_stop_calls = 0

    async def fake_emit_runner_hook(event_name, **kwargs):
        nonlocal before_stop_calls
        if event_name == HookEventName.BEFORE_STOP:
            before_stop_calls += 1
            return MergedHookResult(
                decision=HookDecision.BLOCK,
                reason=f"gate-{before_stop_calls}",
            )
        return MergedHookResult()

    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        fake_emit_runner_hook,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]
    output_texts = [item[0].get_text_content() for item in outputs]

    assert output_texts == [
        "agent reply",
        "agent reply",
        "agent reply",
        output_texts[-1],
    ]
    assert "任务未完成" in output_texts[-1]
    assert "gate-2" in output_texts[-1]
    assert before_stop_calls == 2


@pytest.mark.asyncio
async def test_emit_before_stop_hook_respects_active_guard(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    runtime = _QueryRuntime(
        agent=_FakeAgent(),
        agent_config=_agent_config(
            HookConfig(
                enabled=True,
                events={
                    HookEventName.BEFORE_STOP: [
                        HookMatcherGroupConfig(
                            hooks=[
                                CommandHookHandlerConfig(
                                    id="policy",
                                    command="unused",
                                ),
                            ],
                        ),
                    ],
                },
            ),
        ),
        tenant_hooks=HookConfig(enabled=True),
        hook_overlay=HookSessionOverlay(),
        chat=SimpleNamespace(id="chat-1"),
        session_skill_detector=None,
        mcp_clients=[],
        session_id="session-1",
        user_id="user-1",
        channel="console",
        turn_id="turn-1",
        skip_history=False,
    )
    plan = _TurnPlan(
        original_user_message="hello",
        confirmed_turn_index=0,
        turn_msgs=[],
        validation_config=None,
    )
    outcome = _QueryTurnOutcome(
        assistant_response="agent reply",
        stop_hook_active=True,
    )
    emit_hook = AsyncMock()
    monkeypatch.setattr("swe.app.runner.runner._emit_runner_hook", emit_hook)

    result = await runner._emit_before_stop_hook_if_needed(
        request=SimpleNamespace(
            session_id="session-1",
            user_id="user-1",
            channel="console",
            channel_meta={},
        ),
        runtime=runtime,
        plan=plan,
        outcome=outcome,
    )

    assert result is None
    emit_hook.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_handler_stop_hook_blocks_completion(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )
    emit_hook = AsyncMock(
        side_effect=[
            MergedHookResult(),
            MergedHookResult(),
            MergedHookResult(),
            MergedHookResult(
                decision=HookDecision.BLOCK,
                reason="stop blocked",
            ),
        ],
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        emit_hook,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert [item[0].get_text_content() for item in outputs] == [
        "agent reply",
        "stop blocked",
    ]
    stop_call = emit_hook.await_args_list[-1]
    assert stop_call.args[0] == HookEventName.STOP
    assert stop_call.kwargs["assistant_response"] == "agent reply"


@pytest.mark.asyncio
async def test_query_handler_persists_mutated_hook_overlay(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(HookConfig(enabled=True)),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(enabled=True),
    )

    async def fake_emit_runner_hook(*args, **kwargs):
        kwargs["overlay"].once_executed[
            "default:user-1:session-1:PreToolUse:once"
        ] = True
        return MergedHookResult()

    monkeypatch.setattr(
        "swe.app.runner.runner._emit_runner_hook",
        fake_emit_runner_hook,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="hello")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]
    state = await runner.session.get_session_state_dict(
        session_id="session-1",
        user_id="user-1",
    )

    assert outputs[-1][0].get_text_content() == "agent reply"
    assert state["hook_overlay"]["once_executed"] == {
        "default:user-1:session-1:PreToolUse:once": True,
    }


@pytest.mark.asyncio
async def test_query_handler_ends_request_skill_detector_in_finally(
    monkeypatch,
    tmp_path,
) -> None:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    _patch_normal_agent_path(monkeypatch)
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: HookConfig(),
    )

    detector = SimpleNamespace(
        detect_from_user_message=lambda _message: ("xlsx", 0.9),
        start_skill=AsyncMock(),
        on_reasoning_end=AsyncMock(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._create_session_skill_detector",
        lambda **kwargs: detector,
    )

    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )
    msgs = [Msg(name="user", role="user", content="use xlsx")]

    outputs = [
        item async for item in runner.query_handler(msgs, request=request)
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    detector.start_skill.assert_awaited_once()
    detector.on_reasoning_end.assert_awaited_once()

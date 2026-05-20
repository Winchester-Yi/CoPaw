# -*- coding: utf-8 -*-
"""Runner background suggestion scope regression tests."""

from unittest.mock import AsyncMock

import pytest
from agentscope.message import Msg
from swe.config.config import SuggestionMode

from swe.app.runner import runner as runner_module


class _SuggestionConfig:
    enabled = True
    mode = SuggestionMode.BACKEND_GENERATE
    max_suggestions = 3
    timeout_seconds = 10
    user_message_max_length = 200
    assistant_response_max_length = 400


class _BackendSuggestionConfig:
    enabled = True
    mode = SuggestionMode.BACKEND_GENERATE
    max_suggestions = 3
    timeout_seconds = 10
    user_message_max_length = 200
    assistant_response_max_length = 400


class _DisabledBackendSuggestionConfig(_BackendSuggestionConfig):
    enabled = False


class _QaOnlyBackendSuggestionConfig(_BackendSuggestionConfig):
    mode = SuggestionMode.QA_EXTRACTION_ONLY


def _build_source_config(
    enabled: bool,
    prompt_template: str | None,
    timeout_seconds: float = 5.0,
):
    return type(
        "SourceConfig",
        (),
        {
            "enabled": enabled,
            "prompt_template": prompt_template,
            "timeout_seconds": timeout_seconds,
        },
    )()


class _QAOnlySuggestionConfig:
    enabled = True
    mode = SuggestionMode.QA_EXTRACTION_ONLY
    user_message_max_length = 200
    assistant_response_max_length = 400
    qa_content_total_max_length = 800


class _FakeBackgroundTask:
    def __init__(self) -> None:
        self.callbacks = []

    def add_done_callback(self, callback) -> None:
        self.callbacks.append(callback)


def _patch_create_task(monkeypatch):
    scheduled = []

    def _create_task(coro):
        scheduled.append(coro)
        coro.close()
        return _FakeBackgroundTask()

    monkeypatch.setattr(runner_module.asyncio, "create_task", _create_task)
    return scheduled


@pytest.mark.asyncio
async def test_generate_and_store_suggestions_passes_scope_tenant(
    monkeypatch,
) -> None:
    generate = AsyncMock(return_value=["next question"])
    store = AsyncMock(return_value=None)
    monkeypatch.setattr(runner_module, "generate_suggestions", generate)
    monkeypatch.setattr(runner_module, "store_suggestions", store)

    await runner_module._generate_and_store_suggestions(
        "session-a",
        "user says hi",
        "assistant replies",
        _SuggestionConfig(),
        tenant_id="scope.v1.dGVuYW50LWE.c291cmNlLWE",
    )
    generate.assert_awaited_once_with(
        user_message="user says hi",
        assistant_response="assistant replies",
        max_suggestions=3,
        timeout_seconds=10,
        user_message_max_length=200,
        assistant_response_max_length=400,
        prompt_template=None,
    )

    store.assert_awaited_once_with(
        "session-a",
        ["next question"],
        tenant_id="dGVuYW50LWE.c291cmNlLWE",
        turn_id=None,
    )


@pytest.mark.asyncio
async def test_generate_and_store_suggestions_passes_prompt_template(
    monkeypatch,
) -> None:
    generate = AsyncMock(return_value=["next question"])
    store = AsyncMock(return_value=None)
    monkeypatch.setattr(runner_module, "generate_suggestions", generate)
    monkeypatch.setattr(runner_module, "store_suggestions", store)

    await runner_module._generate_and_store_suggestions(
        "session-a",
        "user says hi",
        "assistant replies",
        _SuggestionConfig(),
        tenant_id="scope.v1.dGVuYW50LWE.c291cmNlLWE",
        prompt_template="source prompt",
    )

    generate.assert_awaited_once_with(
        user_message="user says hi",
        assistant_response="assistant replies",
        max_suggestions=3,
        timeout_seconds=10,
        user_message_max_length=200,
        assistant_response_max_length=400,
        prompt_template="source prompt",
    )
    store.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_and_store_suggestions_prefers_override_timeout(
    monkeypatch,
) -> None:
    generate = AsyncMock(return_value=["next question"])
    store = AsyncMock(return_value=None)
    monkeypatch.setattr(runner_module, "generate_suggestions", generate)
    monkeypatch.setattr(runner_module, "store_suggestions", store)

    await runner_module._generate_and_store_suggestions(
        "session-a",
        "user says hi",
        "assistant replies",
        _SuggestionConfig(),
        timeout_seconds=6.5,
    )

    generate.assert_awaited_once_with(
        user_message="user says hi",
        assistant_response="assistant replies",
        max_suggestions=3,
        timeout_seconds=6.5,
        user_message_max_length=200,
        assistant_response_max_length=400,
        prompt_template=None,
    )
    store.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_and_store_suggestions_passes_turn_id(
    monkeypatch,
) -> None:
    generate = AsyncMock(return_value=["next question"])
    store = AsyncMock(return_value=None)
    monkeypatch.setattr(runner_module, "generate_suggestions", generate)
    monkeypatch.setattr(runner_module, "store_suggestions", store)

    await runner_module._generate_and_store_suggestions(
        "session-a",
        "user says hi",
        "assistant replies",
        _SuggestionConfig(),
        turn_id="response-1",
    )

    store.assert_awaited_once_with(
        "session-a",
        ["next question"],
        tenant_id=None,
        turn_id="response-1",
    )


@pytest.mark.asyncio
async def test_store_pending_continuation_passes_scope_tenant(
    monkeypatch,
) -> None:
    store = AsyncMock(return_value={"id": "validation-1"})
    monkeypatch.setattr(runner_module, "store_pending_continuation", store)

    runner = runner_module.AgentRunner(
        agent_id="agent-a",
        tenant_id="scope.v1.dGVuYW50LWE.c291cmNlLWE",
    )
    runtime = type("Runtime", (), {"session_id": "session-a", "agent": None})()
    plan = type(
        "Plan",
        (),
        {
            "original_user_message": "帮我继续",
            "confirmed_turn_index": 0,
            "validation_config": type(
                "ValidationConfig",
                (),
                {"max_confirmed_turns": 2},
            )(),
        },
    )()
    outcome = type(
        "Outcome",
        (),
        {
            "task_completed": False,
            "last_validation_result": type(
                "ValidationResult",
                (),
                {
                    "reason": "still work left",
                    "follow_up_prompt": "继续处理剩余步骤",
                },
            )(),
            "auto_follow_up_turns": 0,
            "max_auto_turns": 2,
        },
    )()

    await runner._store_pending_validation_if_needed(
        runtime=runtime,
        plan=plan,
        outcome=outcome,
    )

    store.assert_awaited_once_with(
        session_id="session-a",
        user_message="帮我继续",
        assistant_response="",
        reason="still work left",
        follow_up_prompt="继续处理剩余步骤",
        tenant_id="dGVuYW50LWE.c291cmNlLWE",
        confirmed_turn_index=0,
    )


@pytest.mark.asyncio
async def test_store_qa_content_passes_scope_tenant(monkeypatch) -> None:
    store = AsyncMock(return_value=None)
    monkeypatch.setattr("swe.app.suggestions.store.store_qa_content", store)

    runner = runner_module.AgentRunner(
        agent_id="agent-a",
        tenant_id="scope.v1.dGVuYW50LWE.c291cmNlLWE",
    )
    runtime = type(
        "Runtime",
        (),
        {
            "chat": type("Chat", (), {"id": "chat-a"})(),
            "agent": type(
                "Agent",
                (),
                {
                    "memory": type(
                        "Memory",
                        (),
                        {
                            "content": [
                                (
                                    Msg(
                                        name="Friday",
                                        role="assistant",
                                        content="整理后的答案",
                                    ),
                                    [],
                                ),
                            ],
                        },
                    )(),
                },
            )(),
            "agent_config": type(
                "AgentConfig",
                (),
                {
                    "running": type(
                        "Running",
                        (),
                        {"suggestions": _QAOnlySuggestionConfig()},
                    )(),
                },
            )(),
        },
    )()
    outcome = type("Outcome", (), {"task_completed": True})()

    await runner._store_qa_content_if_needed(
        runtime=runtime,
        query="请帮我整理一下",
        outcome=outcome,
    )

    store.assert_awaited_once_with(
        chat_id="chat-a",
        user_message="请帮我整理一下",
        assistant_response="整理后的答案",
        tenant_id="dGVuYW50LWE.c291cmNlLWE",
    )


@pytest.mark.asyncio
async def test_backend_suggestions_run_after_completed_turn(
    monkeypatch,
) -> None:
    scheduled = _patch_create_task(monkeypatch)
    run_task = AsyncMock(return_value=None)
    monkeypatch.setattr(
        runner_module,
        "_generate_and_store_suggestions",
        run_task,
    )
    source_config = _build_source_config(True, None)

    def _get_source_config():
        return source_config

    monkeypatch.setattr(
        runner_module,
        "get_follow_up_suggestions_config",
        _get_source_config,
    )

    runner = runner_module.AgentRunner(
        agent_id="agent-a",
        tenant_id="scope.v1.dGVuYW50LWE.c291cmNlLWE",
    )
    suggestions_config = _BackendSuggestionConfig()
    runtime = type(
        "Runtime",
        (),
        {
            "session_id": "session-a",
            "turn_id": "response-1",
            "agent": None,
            "agent_config": type(
                "AgentConfig",
                (),
                {
                    "running": type(
                        "Running",
                        (),
                        {"suggestions": suggestions_config},
                    )(),
                },
            )(),
        },
    )()
    plan = type("Plan", (), {"original_user_message": "用户问题"})()
    outcome = type(
        "Outcome",
        (),
        {"task_completed": True, "assistant_response": "助手回答"},
    )()

    await runner._generate_backend_suggestions_if_needed(
        runtime=runtime,
        plan=plan,
        outcome=outcome,
    )

    assert len(scheduled) == 1
    run_task.assert_called_once_with(
        "session-a",
        "用户问题",
        "助手回答",
        suggestions_config,
        tenant_id="dGVuYW50LWE.c291cmNlLWE",
        prompt_template=None,
        timeout_seconds=5.0,
        turn_id="response-1",
    )
    assert run_task.await_count == 0


@pytest.mark.asyncio
async def test_backend_suggestions_respect_source_switch(monkeypatch) -> None:
    scheduled = _patch_create_task(monkeypatch)
    run_task = AsyncMock(return_value=None)
    monkeypatch.setattr(
        runner_module,
        "_generate_and_store_suggestions",
        run_task,
    )
    source_config = _build_source_config(False, None)

    def _get_source_config():
        return source_config

    monkeypatch.setattr(
        runner_module,
        "get_follow_up_suggestions_config",
        _get_source_config,
    )

    runner = runner_module.AgentRunner(
        agent_id="agent-a",
        tenant_id="tenant-a",
    )
    runtime = type(
        "Runtime",
        (),
        {
            "session_id": "session-a",
            "agent": None,
            "agent_config": type(
                "AgentConfig",
                (),
                {
                    "running": type(
                        "Running",
                        (),
                        {"suggestions": _BackendSuggestionConfig()},
                    )(),
                },
            )(),
        },
    )()
    plan = type("Plan", (), {"original_user_message": "用户问题"})()
    outcome = type(
        "Outcome",
        (),
        {"task_completed": True, "assistant_response": "助手回答"},
    )()

    await runner._generate_backend_suggestions_if_needed(
        runtime=runtime,
        plan=plan,
        outcome=outcome,
    )

    assert not scheduled
    run_task.assert_not_called()


@pytest.mark.asyncio
async def test_backend_suggestions_pass_custom_source_prompt(
    monkeypatch,
) -> None:
    scheduled = _patch_create_task(monkeypatch)
    run_task = AsyncMock(return_value=None)
    monkeypatch.setattr(
        runner_module,
        "_generate_and_store_suggestions",
        run_task,
    )
    source_config = _build_source_config(
        True,
        "source prompt {user_message}",
    )

    def _get_source_config():
        return source_config

    monkeypatch.setattr(
        runner_module,
        "get_follow_up_suggestions_config",
        _get_source_config,
    )

    runner = runner_module.AgentRunner(
        agent_id="agent-a",
        tenant_id="tenant-a",
    )
    suggestions_config = _BackendSuggestionConfig()
    runtime = type(
        "Runtime",
        (),
        {
            "session_id": "session-a",
            "agent": None,
            "agent_config": type(
                "AgentConfig",
                (),
                {
                    "running": type(
                        "Running",
                        (),
                        {"suggestions": suggestions_config},
                    )(),
                },
            )(),
        },
    )()
    plan = type("Plan", (), {"original_user_message": "用户问题"})()
    outcome = type(
        "Outcome",
        (),
        {"task_completed": True, "assistant_response": "助手回答"},
    )()

    await runner._generate_backend_suggestions_if_needed(
        runtime=runtime,
        plan=plan,
        outcome=outcome,
    )

    assert len(scheduled) == 1
    run_task.assert_called_once_with(
        "session-a",
        "用户问题",
        "助手回答",
        suggestions_config,
        tenant_id="tenant-a",
        prompt_template="source prompt {user_message}",
        timeout_seconds=5.0,
        turn_id=None,
    )
    assert run_task.await_count == 0


@pytest.mark.asyncio
async def test_backend_suggestions_pass_source_timeout(
    monkeypatch,
) -> None:
    scheduled = _patch_create_task(monkeypatch)
    run_task = AsyncMock(return_value=None)
    monkeypatch.setattr(
        runner_module,
        "_generate_and_store_suggestions",
        run_task,
    )
    source_config = _build_source_config(
        True,
        None,
        timeout_seconds=6.5,
    )

    def _get_source_config():
        return source_config

    monkeypatch.setattr(
        runner_module,
        "get_follow_up_suggestions_config",
        _get_source_config,
    )

    runner = runner_module.AgentRunner(
        agent_id="agent-a",
        tenant_id="tenant-a",
    )
    suggestions_config = _BackendSuggestionConfig()
    runtime = type(
        "Runtime",
        (),
        {
            "session_id": "session-a",
            "agent": None,
            "agent_config": type(
                "AgentConfig",
                (),
                {
                    "running": type(
                        "Running",
                        (),
                        {"suggestions": suggestions_config},
                    )(),
                },
            )(),
        },
    )()
    plan = type("Plan", (), {"original_user_message": "用户问题"})()
    outcome = type(
        "Outcome",
        (),
        {"task_completed": True, "assistant_response": "助手回答"},
    )()

    await runner._generate_backend_suggestions_if_needed(
        runtime=runtime,
        plan=plan,
        outcome=outcome,
    )

    assert len(scheduled) == 1
    run_task.assert_called_once_with(
        "session-a",
        "用户问题",
        "助手回答",
        suggestions_config,
        tenant_id="tenant-a",
        prompt_template=None,
        timeout_seconds=6.5,
        turn_id=None,
    )
    assert run_task.await_count == 0


@pytest.mark.asyncio
async def test_backend_suggestions_skip_when_task_not_completed(
    monkeypatch,
) -> None:
    scheduled = _patch_create_task(monkeypatch)
    run_task = AsyncMock(return_value=None)
    monkeypatch.setattr(
        runner_module,
        "_generate_and_store_suggestions",
        run_task,
    )

    runner = runner_module.AgentRunner(
        agent_id="agent-a",
        tenant_id="tenant-a",
    )
    runtime = type(
        "Runtime",
        (),
        {
            "session_id": "session-a",
            "agent": None,
            "agent_config": type(
                "AgentConfig",
                (),
                {
                    "running": type(
                        "Running",
                        (),
                        {"suggestions": _BackendSuggestionConfig()},
                    )(),
                },
            )(),
        },
    )()
    plan = type("Plan", (), {"original_user_message": "用户问题"})()
    outcome = type(
        "Outcome",
        (),
        {"task_completed": False, "assistant_response": "助手回答"},
    )()

    await runner._generate_backend_suggestions_if_needed(
        runtime=runtime,
        plan=plan,
        outcome=outcome,
    )

    assert not scheduled
    run_task.assert_not_called()


@pytest.mark.asyncio
async def test_backend_suggestions_skip_when_suggestions_disabled(
    monkeypatch,
) -> None:
    scheduled = _patch_create_task(monkeypatch)
    run_task = AsyncMock(return_value=None)
    monkeypatch.setattr(
        runner_module,
        "_generate_and_store_suggestions",
        run_task,
    )

    runner = runner_module.AgentRunner(
        agent_id="agent-a",
        tenant_id="tenant-a",
    )
    runtime = type(
        "Runtime",
        (),
        {
            "session_id": "session-a",
            "agent": None,
            "agent_config": type(
                "AgentConfig",
                (),
                {
                    "running": type(
                        "Running",
                        (),
                        {"suggestions": _DisabledBackendSuggestionConfig()},
                    )(),
                },
            )(),
        },
    )()
    plan = type("Plan", (), {"original_user_message": "用户问题"})()
    outcome = type(
        "Outcome",
        (),
        {"task_completed": True, "assistant_response": "助手回答"},
    )()

    await runner._generate_backend_suggestions_if_needed(
        runtime=runtime,
        plan=plan,
        outcome=outcome,
    )

    assert not scheduled
    run_task.assert_not_called()


@pytest.mark.asyncio
async def test_backend_suggestions_skip_when_mode_not_backend_generate(
    monkeypatch,
) -> None:
    scheduled = _patch_create_task(monkeypatch)
    run_task = AsyncMock(return_value=None)
    monkeypatch.setattr(
        runner_module,
        "_generate_and_store_suggestions",
        run_task,
    )

    runner = runner_module.AgentRunner(
        agent_id="agent-a",
        tenant_id="tenant-a",
    )
    runtime = type(
        "Runtime",
        (),
        {
            "session_id": "session-a",
            "agent": None,
            "agent_config": type(
                "AgentConfig",
                (),
                {
                    "running": type(
                        "Running",
                        (),
                        {"suggestions": _QaOnlyBackendSuggestionConfig()},
                    )(),
                },
            )(),
        },
    )()
    plan = type("Plan", (), {"original_user_message": "用户问题"})()
    outcome = type(
        "Outcome",
        (),
        {"task_completed": True, "assistant_response": "助手回答"},
    )()

    await runner._generate_backend_suggestions_if_needed(
        runtime=runtime,
        plan=plan,
        outcome=outcome,
    )

    assert not scheduled
    run_task.assert_not_called()


@pytest.mark.asyncio
async def test_backend_suggestions_skip_when_user_message_missing(
    monkeypatch,
) -> None:
    scheduled = _patch_create_task(monkeypatch)
    run_task = AsyncMock(return_value=None)
    monkeypatch.setattr(
        runner_module,
        "_generate_and_store_suggestions",
        run_task,
    )

    runner = runner_module.AgentRunner(
        agent_id="agent-a",
        tenant_id="tenant-a",
    )
    runtime = type(
        "Runtime",
        (),
        {
            "session_id": "session-a",
            "agent": None,
            "agent_config": type(
                "AgentConfig",
                (),
                {
                    "running": type(
                        "Running",
                        (),
                        {"suggestions": _BackendSuggestionConfig()},
                    )(),
                },
            )(),
        },
    )()
    plan = type("Plan", (), {"original_user_message": ""})()
    outcome = type(
        "Outcome",
        (),
        {"task_completed": True, "assistant_response": "助手回答"},
    )()

    await runner._generate_backend_suggestions_if_needed(
        runtime=runtime,
        plan=plan,
        outcome=outcome,
    )

    assert not scheduled
    run_task.assert_not_called()


@pytest.mark.asyncio
async def test_backend_suggestions_skip_when_assistant_response_missing(
    monkeypatch,
) -> None:
    scheduled = _patch_create_task(monkeypatch)
    run_task = AsyncMock(return_value=None)
    monkeypatch.setattr(
        runner_module,
        "_generate_and_store_suggestions",
        run_task,
    )

    runner = runner_module.AgentRunner(
        agent_id="agent-a",
        tenant_id="tenant-a",
    )
    runtime = type(
        "Runtime",
        (),
        {
            "session_id": "session-a",
            "agent": type(
                "Agent",
                (),
                {"memory": type("Memory", (), {"content": []})()},
            )(),
            "agent_config": type(
                "AgentConfig",
                (),
                {
                    "running": type(
                        "Running",
                        (),
                        {"suggestions": _BackendSuggestionConfig()},
                    )(),
                },
            )(),
        },
    )()
    plan = type("Plan", (), {"original_user_message": "用户问题"})()
    outcome = type(
        "Outcome",
        (),
        {"task_completed": True, "assistant_response": ""},
    )()

    await runner._generate_backend_suggestions_if_needed(
        runtime=runtime,
        plan=plan,
        outcome=outcome,
    )

    assert not scheduled
    run_task.assert_not_called()

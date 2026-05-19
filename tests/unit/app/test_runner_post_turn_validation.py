# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from agentscope.message import Msg

from swe.app.post_turn_continuation_store import (
    clear_pending_continuations,
    peek_latest_pending_continuation,
    store_pending_continuation,
)
from swe.app.runner.runner import AgentRunner
from swe.app.runner.session import SafeJSONSession
from swe.config.config import SuggestionMode


def _fake_agent_config():
    return SimpleNamespace(
        mcp=None,
        running=SimpleNamespace(
            suggestions=SimpleNamespace(
                enabled=True,
                mode=SuggestionMode.BACKEND_GENERATE,
                max_suggestions=3,
                timeout_seconds=5.0,
                user_message_max_length=200,
                assistant_response_max_length=500,
            ),
            post_turn_validation=SimpleNamespace(
                enabled=True,
                max_confirmed_turns=None,
                max_auto_turns=2,
                timeout_seconds=5.0,
                user_message_max_length=300,
                assistant_response_max_length=1200,
            ),
        ),
    )


class FakeAgent:
    def __init__(self, **kwargs):
        self.memory = SimpleNamespace(content=[])

    async def register_mcp_clients(self):
        return

    def set_console_output_enabled(self, enabled=False):
        del enabled

    def rebuild_sys_prompt(self):
        return

    async def __call__(self, turn_msgs):
        for msg in turn_msgs:
            self.memory.content.append((msg, []))
        return list(turn_msgs)

    def state_dict(self):
        return {
            "memory": {
                "content": [
                    [msg.to_dict(), marks]
                    for msg, marks in self.memory.content
                ],
            },
        }


async def fake_stream_printing_messages(*, agents, coroutine_task):
    turn_msgs = await coroutine_task
    first_text = turn_msgs[0].get_text_content()
    reply = Msg(
        name="Friday",
        role="assistant",
        content="最终答案" if "内部续跑指令" in first_text else "我先继续处理",
    )
    agents[0].memory.content.append((reply, []))
    yield reply, True


class _FakeBackgroundTask:
    def add_done_callback(self, callback):  # noqa: ANN001
        del callback


def _patch_create_task(monkeypatch):
    scheduled = []

    def _create_task(coro):
        scheduled.append(coro)
        coro.close()
        return _FakeBackgroundTask()

    monkeypatch.setattr(
        "swe.app.runner.runner.asyncio.create_task",
        _create_task,
    )
    return scheduled


@pytest_asyncio.fixture(autouse=True)
async def _clear_store():
    await clear_pending_continuations()
    yield
    await clear_pending_continuations()


def _patch_runner(monkeypatch, validation_result, generate_suggestions):
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _fake_agent_config(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._build_and_connect_mcp_clients",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr("swe.app.runner.runner.SWEAgent", FakeAgent)
    monkeypatch.setattr(
        "swe.app.runner.runner.stream_printing_messages",
        fake_stream_printing_messages,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.build_env_context",
        lambda **kwargs: kwargs,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._cleanup_mcp_clients",
        AsyncMock(),
    )
    validate_mock = (
        AsyncMock(side_effect=validation_result)
        if isinstance(validation_result, list)
        else AsyncMock(return_value=validation_result)
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.validate_task_completion",
        validate_mock,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._generate_and_store_suggestions",
        generate_suggestions,
    )


def _runner(tmp_path: Path) -> AgentRunner:
    runner = AgentRunner(agent_id="test-agent")
    runner.session = SafeJSONSession(save_dir=str(tmp_path))
    setattr(runner, "_chat_manager", None)
    return runner


@pytest.mark.asyncio
async def test_query_handler_auto_runs_then_stores_pending_continuation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    scheduled = _patch_create_task(monkeypatch)
    generate_suggestions = AsyncMock()
    incomplete_result = SimpleNamespace(
        completed=False,
        reason="still work left",
        follow_up_prompt="继续把剩余步骤做完并给出最终结果。",
    )
    _patch_runner(
        monkeypatch,
        [incomplete_result, incomplete_result, incomplete_result],
        generate_suggestions,
    )

    runner = _runner(tmp_path)
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
            content="帮我把任务做完",
        ),
    ]

    outputs = []
    async for item in runner.query_handler(msgs, request=request):
        outputs.append(item)

    assert [item[0].content for item in outputs] == [
        "我先继续处理",
        "最终答案",
        "最终答案",
    ]
    assert not scheduled
    assert generate_suggestions.call_count == 0
    assert generate_suggestions.await_count == 0
    pending = await peek_latest_pending_continuation(
        session_id="session-1",
        tenant_id=None,
    )
    assert pending is not None
    assert pending["status"] == "needs_confirmation"
    assert pending["reason"] == "still work left"


@pytest.mark.asyncio
async def test_query_handler_auto_run_completion_generates_backend_suggestions(
    monkeypatch,
    tmp_path: Path,
) -> None:
    scheduled = _patch_create_task(monkeypatch)
    generate_suggestions = AsyncMock()
    _patch_runner(
        monkeypatch,
        [
            SimpleNamespace(
                completed=False,
                reason="still work left",
                follow_up_prompt="继续把剩余步骤做完并给出最终结果。",
            ),
            SimpleNamespace(
                completed=True,
                reason="done",
                follow_up_prompt="",
            ),
        ],
        generate_suggestions,
    )

    runner = _runner(tmp_path)
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
            content="帮我把任务做完",
        ),
    ]

    outputs = []
    async for item in runner.query_handler(msgs, request=request):
        outputs.append(item)

    assert [item[0].content for item in outputs] == [
        "我先继续处理",
        "最终答案",
    ]
    assert len(scheduled) == 1
    assert generate_suggestions.call_count == 1
    assert generate_suggestions.await_count == 0
    pending = await peek_latest_pending_continuation(
        session_id="session-1",
        tenant_id=None,
    )
    assert pending is None


@pytest.mark.asyncio
async def test_query_handler_resume_consumes_pending_and_strips_hidden_prompt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    scheduled = _patch_create_task(monkeypatch)
    generate_suggestions = AsyncMock()
    _patch_runner(
        monkeypatch,
        SimpleNamespace(completed=True, reason="done", follow_up_prompt=""),
        generate_suggestions,
    )

    stored = await store_pending_continuation(
        session_id="session-1",
        user_message="帮我把任务做完",
        assistant_response="我先继续处理",
        reason="still work left",
        follow_up_prompt="继续把剩余步骤做完并给出最终结果。",
        tenant_id=None,
        confirmed_turn_index=0,
    )

    runner = _runner(tmp_path)
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={"post_turn_validation_resume_id": stored["id"]},
    )

    outputs = []
    async for item in runner.query_handler([], request=request):
        outputs.append(item)

    assert [item[0].content for item in outputs] == ["最终答案"]
    assert len(scheduled) == 1
    assert generate_suggestions.call_count == 1
    assert generate_suggestions.await_count == 0
    assert (
        await peek_latest_pending_continuation(
            session_id="session-1",
            tenant_id=None,
        )
    ) is None

    stored_state = json.loads((tmp_path / "user-1_session-1.json").read_text())
    stored_content = stored_state["agent"]["memory"]["content"]
    stored_texts = [entry[0]["content"] for entry in stored_content]
    assert stored_texts == ["最终答案"]
    assert all(
        not entry[0].get("metadata", {}).get("swe_internal_follow_up")
        for entry in stored_content
    )


@pytest.mark.asyncio
async def test_query_handler_unknown_resume_id_returns_clear_message(
    monkeypatch,
    tmp_path: Path,
) -> None:
    scheduled = _patch_create_task(monkeypatch)
    generate_suggestions = AsyncMock()
    _patch_runner(
        monkeypatch,
        SimpleNamespace(completed=True, reason="done", follow_up_prompt=""),
        generate_suggestions,
    )

    runner = _runner(tmp_path)
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={"post_turn_validation_resume_id": "validation_missing"},
    )

    outputs = []
    async for item in runner.query_handler([], request=request):
        outputs.append(item)

    assert [item[0].content for item in outputs] == [
        "续跑请求已过期或不存在，请重新发起任务。",
    ]
    assert not scheduled
    assert generate_suggestions.call_count == 0
    assert generate_suggestions.await_count == 0

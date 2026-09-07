# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import Msg

from swe.app.crons.manager import CronManager
from swe.app.runner.context_usage import (
    CONTEXT_USAGE_INVALID_STATE_KEY,
    CONTEXT_USAGE_STATE_KEY,
    ContextUsageSnapshot,
)
from swe.app.runner.runner import AgentRunner
from swe.security.tool_guard.models import TOOL_GUARD_DENIED_MARK


class _FakeAgent:
    def __init__(self, content: str = "agent reply") -> None:
        self._content = content

    def state_dict(self) -> dict:
        return {
            "memory": {
                "content": [
                    [
                        Msg(
                            name="Friday",
                            role="assistant",
                            content=self._content,
                        ).to_dict(),
                        [],
                    ],
                ],
            },
        }


class _StateAgent:
    def __init__(self, state: dict) -> None:
        self._state = state

    def state_dict(self) -> dict:
        return copy.deepcopy(self._state)


class _AtomicSessionDouble:
    def __init__(self) -> None:
        self.state: dict = {}
        self._lock = asyncio.Lock()
        self.merge_started = asyncio.Event()
        self.allow_merge_to_continue = asyncio.Event()

    async def get_session_state_dict(
        self,
        session_id: str,
        user_id: str = "",
        allow_not_exist: bool = True,
    ) -> dict:
        del session_id, user_id, allow_not_exist
        snapshot = copy.deepcopy(self.state)
        self.merge_started.set()
        await self.allow_merge_to_continue.wait()
        return snapshot

    async def save_merged_state(
        self,
        session_id: str,
        user_id: str = "",
        state: dict | None = None,
    ) -> None:
        del session_id, user_id
        self.state = copy.deepcopy(state or {})

    async def update_session_state(
        self,
        session_id: str,
        key,
        value,
        user_id: str = "",
        create_if_not_exist: bool = True,
    ) -> None:
        del session_id, user_id, create_if_not_exist
        async with self._lock:
            state = copy.deepcopy(self.state)
            path = key.split(".") if isinstance(key, str) else list(key)
            cur = state
            for part in path[:-1]:
                if part not in cur or not isinstance(cur[part], dict):
                    cur[part] = {}
                cur = cur[part]
            cur[path[-1]] = value
            self.state = state

    async def mutate_session_state(
        self,
        session_id: str,
        mutator,
        user_id: str = "",
        create_if_not_exist: bool = True,
    ) -> dict:
        del session_id, user_id, create_if_not_exist
        async with self._lock:
            self.merge_started.set()
            await self.allow_merge_to_continue.wait()
            working = copy.deepcopy(self.state)
            updated = mutator(working)
            if updated is None:
                updated = working
            self.state = copy.deepcopy(updated)
            return copy.deepcopy(self.state)


class _MutationTrackingFileSession:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.mutate_calls = 0

    def _get_save_path(self, session_id: str, user_id: str = "") -> str:
        del session_id, user_id
        return str(self.path)

    async def mutate_session_state(
        self,
        session_id: str,
        mutator,
        user_id: str = "",
        create_if_not_exist: bool = True,
    ) -> dict:
        del session_id, user_id, create_if_not_exist
        self.mutate_calls += 1
        state = json.loads(self.path.read_text(encoding="utf-8"))
        updated = mutator(copy.deepcopy(state))
        if updated is None:
            updated = state
        self.path.write_text(
            json.dumps(updated, ensure_ascii=False),
            encoding="utf-8",
        )
        return updated


class _MutateOnlySession:
    def __init__(self) -> None:
        self.state: dict = {}
        self.mutate_calls = 0

    async def mutate_session_state(
        self,
        session_id: str,
        mutator,
        user_id: str = "",
        create_if_not_exist: bool = True,
    ) -> dict:
        del session_id, user_id, create_if_not_exist
        self.mutate_calls += 1
        updated = mutator(copy.deepcopy(self.state))
        if updated is None:
            updated = self.state
        self.state = copy.deepcopy(updated)
        return copy.deepcopy(self.state)


def _make_runner(monkeypatch, tmp_path: Path, session) -> AgentRunner:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = session
    setattr(runner, "_chat_manager", None)
    monkeypatch.setattr(
        "swe.app.runner.runner._build_lazy_mcp_clients",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._cleanup_mcp_clients",
        AsyncMock(),
    )
    return runner


@pytest.mark.asyncio
async def test_regular_session_save_preserves_concurrent_key_update(
    monkeypatch,
    tmp_path: Path,
) -> None:
    session = _AtomicSessionDouble()
    runner = _make_runner(monkeypatch, tmp_path, session)

    save_task = asyncio.create_task(
        runner._save_regular_session_state(
            _FakeAgent(),
            session_id="session-1",
            user_id="user-1",
            hook_overlay=None,
        ),
    )

    await asyncio.wait_for(session.merge_started.wait(), timeout=1)
    update_task = asyncio.create_task(
        session.update_session_state(
            "session-1",
            "task_messages",
            [{"id": "msg-1", "content": "persisted task update"}],
            user_id="user-1",
        ),
    )
    await asyncio.sleep(0)
    session.allow_merge_to_continue.set()

    await asyncio.gather(save_task, update_task)

    assert session.state["agent"]["memory"]["content"]
    assert session.state["task_messages"] == [
        {"id": "msg-1", "content": "persisted task update"},
    ]


@pytest.mark.asyncio
async def test_regular_session_save_dedupes_external_approval_message(
    monkeypatch,
    tmp_path: Path,
) -> None:
    session = _MutateOnlySession()
    runner = _make_runner(monkeypatch, tmp_path, session)
    request_id = "approval-1"
    command = f"/approve {request_id}"
    external_message = Msg(
        name="user-1",
        role="user",
        content=command,
        metadata={
            "external_approval_message": True,
            "approval_request_id": request_id,
            "approval_decision": "approve",
            "approval_source_channel": "zhaohu",
        },
    ).to_dict()
    runner_message = Msg(
        name="user-1",
        role="user",
        content=command,
    ).to_dict()
    agent = _StateAgent(
        {
            "memory": {
                "content": [
                    [external_message, []],
                    [runner_message, []],
                    [
                        Msg(
                            name="Friday",
                            role="assistant",
                            content="approved",
                        ).to_dict(),
                        [],
                    ],
                ],
            },
        },
    )

    await runner._save_regular_session_state(
        agent,
        session_id="session-1",
        user_id="user-1",
        hook_overlay=None,
    )

    content = session.state["agent"]["memory"]["content"]
    user_messages = [
        entry[0]
        for entry in content
        if isinstance(entry, list) and entry[0].get("role") == "user"
    ]
    assert [msg["content"] for msg in user_messages] == [command]
    assert user_messages[0]["metadata"]["external_approval_message"] is True


@pytest.mark.asyncio
async def test_cron_text_append_preserves_concurrent_agent_update(
    monkeypatch,
    tmp_path: Path,
) -> None:
    session = _AtomicSessionDouble()
    runner = _make_runner(monkeypatch, tmp_path, session)
    manager = CronManager(
        repo=object(),
        runner=runner,
        channel_manager=object(),
    )

    append_task = asyncio.create_task(
        manager._append_text_task_message(
            "session-1",
            "user-1",
            "cron preview",
        ),
    )

    await asyncio.wait_for(session.merge_started.wait(), timeout=1)
    update_task = asyncio.create_task(
        session.update_session_state(
            "session-1",
            "agent",
            _FakeAgent("concurrent agent state").state_dict(),
            user_id="user-1",
        ),
    )
    await asyncio.sleep(0)
    session.allow_merge_to_continue.set()

    await asyncio.gather(append_task, update_task)

    assert session.state["agent"]["memory"]["content"]
    assert session.state["task_messages"][0]["content"][0]["text"] == (
        "cron preview"
    )


@pytest.mark.asyncio
async def test_denied_cleanup_uses_atomic_session_mutation_when_available(
    monkeypatch,
    tmp_path: Path,
) -> None:
    session_path = tmp_path / "session-1.json"
    session_path.write_text(
        json.dumps(
            {
                "agent": {
                    "memory": {
                        "content": [
                            [
                                {
                                    "role": "assistant",
                                    "content": "tool call",
                                },
                                [TOOL_GUARD_DENIED_MARK],
                            ],
                            [
                                {
                                    "role": "assistant",
                                    "content": "llm denial explanation",
                                },
                                [],
                            ],
                        ],
                    },
                },
                "task_messages": [{"id": "keep-me"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    session = _MutationTrackingFileSession(session_path)
    runner = _make_runner(monkeypatch, tmp_path, session)

    await runner._cleanup_denied_session_memory(
        session_id="session-1",
        user_id="user-1",
        denial_response=Msg(
            name="Friday",
            role="assistant",
            content="❌ Tool denied",
        ),
    )

    state = json.loads(session_path.read_text(encoding="utf-8"))

    assert session.mutate_calls == 1
    assert state["task_messages"] == [{"id": "keep-me"}]
    content = state["agent"]["memory"]["content"]
    assert content[0] == [
        {"role": "assistant", "content": "tool call"},
        [],
    ]
    assert content[1][1] == []
    assert content[1][0]["name"] == "Friday"
    assert content[1][0]["role"] == "assistant"
    assert content[1][0]["content"] == "❌ Tool denied"


@pytest.mark.asyncio
async def test_regular_session_save_requires_only_atomic_mutation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    session = _MutateOnlySession()
    runner = _make_runner(monkeypatch, tmp_path, session)

    await runner._save_regular_session_state(
        _FakeAgent(),
        session_id="session-1",
        user_id="user-1",
        hook_overlay=None,
    )

    assert session.mutate_calls == 1
    assert session.state["agent"]["memory"]["content"]


def _context_snapshot() -> ContextUsageSnapshot:
    return ContextUsageSnapshot(
        used_tokens=30,
        max_tokens=100,
        remaining_tokens=70,
        usage_ratio=0.3,
        system_context_tokens=10,
        tool_definition_tokens=5,
        conversation_tokens=15,
        governance_threshold_ratio=0.65,
        active_threshold_ratio=0.8,
        emergency_threshold_ratio=0.9,
        status="normal",
        as_of=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_regular_session_save_commits_cleaned_state_and_snapshot_once(
    monkeypatch,
    tmp_path: Path,
) -> None:
    session = _MutateOnlySession()
    runner = _make_runner(monkeypatch, tmp_path, session)
    internal = Msg(
        name="user-1",
        role="user",
        content="internal continuation",
        metadata={"swe_internal_follow_up": True},
    ).to_dict()
    visible = Msg(
        name="user-1",
        role="user",
        content="visible",
    ).to_dict()
    agent = _StateAgent(
        {"memory": {"content": [[internal, []], [visible, []]]}},
    )

    async def _capture(_agent, cleaned_state):
        contents = [
            entry[0]["content"] for entry in cleaned_state["memory"]["content"]
        ]
        assert contents == ["visible"]
        return _context_snapshot()

    monkeypatch.setattr(
        "swe.app.runner.runner.capture_context_usage",
        _capture,
    )

    await runner._save_regular_session_state(
        agent,
        session_id="session-1",
        user_id="user-1",
    )

    assert session.mutate_calls == 1
    assert session.state["agent"]["memory"]["content"] == [[visible, []]]
    assert session.state[
        CONTEXT_USAGE_STATE_KEY
    ] == _context_snapshot().model_dump(
        mode="json",
    )


@pytest.mark.asyncio
async def test_regular_session_save_preserves_snapshot_when_capture_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    old_snapshot = _context_snapshot().model_dump(mode="json")
    session = _MutateOnlySession()
    session.state = {
        CONTEXT_USAGE_STATE_KEY: old_snapshot,
        "agent": {"memory": {"content": []}},
    }
    runner = _make_runner(monkeypatch, tmp_path, session)

    async def _capture(*_args, **_kwargs):
        raise RuntimeError("counter failed")

    monkeypatch.setattr(
        "swe.app.runner.runner.capture_context_usage",
        _capture,
    )

    await runner._save_regular_session_state(
        _FakeAgent("new persisted reply"),
        session_id="session-1",
        user_id="user-1",
    )

    assert session.mutate_calls == 1
    assert session.state[CONTEXT_USAGE_STATE_KEY] == old_snapshot
    assert session.state[CONTEXT_USAGE_INVALID_STATE_KEY] is True
    assert session.state["agent"]["memory"]["content"]


@pytest.mark.asyncio
async def test_regular_session_save_clears_capture_failure_marker_on_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    session = _MutateOnlySession()
    session.state = {
        CONTEXT_USAGE_STATE_KEY: _context_snapshot().model_dump(mode="json"),
        CONTEXT_USAGE_INVALID_STATE_KEY: True,
    }
    runner = _make_runner(monkeypatch, tmp_path, session)

    async def _capture(*_args, **_kwargs):
        return _context_snapshot()

    monkeypatch.setattr(
        "swe.app.runner.runner.capture_context_usage",
        _capture,
    )

    await runner._save_regular_session_state(
        _FakeAgent("fresh reply"),
        session_id="session-1",
        user_id="user-1",
    )

    assert CONTEXT_USAGE_INVALID_STATE_KEY not in session.state


@pytest.mark.asyncio
async def test_cron_text_append_requires_only_atomic_mutation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    session = _MutateOnlySession()
    runner = _make_runner(monkeypatch, tmp_path, session)
    manager = CronManager(
        repo=object(),
        runner=runner,
        channel_manager=object(),
    )

    await manager._append_text_task_message(
        "session-1",
        "user-1",
        "cron preview",
    )

    assert session.mutate_calls == 1
    assert session.state["task_messages"][0]["content"][0]["text"] == (
        "cron preview"
    )

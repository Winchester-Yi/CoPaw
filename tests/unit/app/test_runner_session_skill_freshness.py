# -*- coding: utf-8 -*-
from __future__ import annotations

import ast
import inspect
import json
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import Msg
from agentscope_runtime.adapters.agentscope.stream import (
    adapt_agentscope_message_stream,
)

from swe.agents.hook_runtime.models import (
    HookConfig,
    HookSessionOverlay,
    LoadedSkillHookSource,
)
from swe.agents.skills_manager import (
    get_builtin_skills_dir,
    get_skill_freshness_token,
    get_workspace_skill_manifest_path,
)
from swe.app.runner.runner import AgentRunner
from swe.app.runner.session import (
    SESSION_SKILL_SNAPSHOT_STATE_KEY,
    SafeJSONSession,
)
from swe.config.config import SuggestionMode


def _agent_config():
    return SimpleNamespace(
        id="test-agent",
        hooks=SimpleNamespace(enabled=False, events={}),
        mcp=None,
        running=SimpleNamespace(
            suggestions=SimpleNamespace(
                enabled=False,
                mode=SuggestionMode.DISABLED,
            ),
            query_retry=SimpleNamespace(
                enabled=False,
                max_retries=0,
                backoff_base=0.0,
                backoff_cap=0.0,
            ),
        ),
    )


class _FakeMemory:
    def __init__(self) -> None:
        self.content = []

    async def add(self, msg, marks=None):
        if marks is None:
            marks = []
        elif not isinstance(marks, list):
            marks = [marks]
        self.content.append((msg, marks))


class _FakeAgent:
    effective_skills: list[str] = []
    last_instance: "_FakeAgent | None" = None

    def __init__(self, **kwargs):
        self._workspace_skill_snapshot = kwargs.get("workspace_skill_snapshot")
        self.memory = _FakeMemory()
        self._request_context = kwargs.get("request_context", {})
        self._sys_prompt = "base sys prompt"
        self.rebuild_sys_prompt_calls = 0
        self.turn_msgs = []
        self.sys_prompt_at_call = ""
        _FakeAgent.last_instance = self

    async def register_mcp_clients(self):
        return

    def set_console_output_enabled(self, enabled=False):
        del enabled

    def get_effective_skills(self) -> list[str]:
        return list(type(self).effective_skills)

    @property
    def sys_prompt(self) -> str:
        return self._sys_prompt

    def rebuild_sys_prompt(self):
        self.rebuild_sys_prompt_calls += 1
        self._sys_prompt = "rebuilt sys prompt"

    async def setup_skill_detector(self, trace_id: str) -> None:
        del trace_id

    async def __call__(self, turn_msgs):
        self.sys_prompt_at_call = self.sys_prompt
        self.turn_msgs = list(turn_msgs)
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

    def load_state_dict(self, state):
        memory_state = (
            state.get("memory", {}) if isinstance(state, dict) else {}
        )
        restored = []
        for raw_msg, marks in memory_state.get("content", []) or []:
            restored.append((Msg.from_dict(raw_msg), marks))
        self.memory.content = restored


async def _fake_stream_printing_messages(*, agents, coroutine_task):
    del agents
    turn_msgs = await coroutine_task
    for msg in turn_msgs:
        yield msg, True


def _patch_runner(monkeypatch, tmp_path: Path) -> AgentRunner:
    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    runner.session = SafeJSONSession(save_dir=str(tmp_path / "sessions"))
    setattr(runner, "_chat_manager", None)
    monkeypatch.setattr(
        "swe.app.runner.runner._build_lazy_mcp_clients",
        lambda *_args, **_kwargs: [],
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
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: _agent_config(),
    )
    monkeypatch.setattr(
        "swe.app.runner.runner._load_tenant_hook_config",
        lambda *args, **kwargs: SimpleNamespace(enabled=False, events={}),
    )
    return runner


def _write_skill_manifest(
    workspace_dir: Path,
    *,
    skills: list[str],
) -> None:
    manifest_path = get_workspace_skill_manifest_path(workspace_dir)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "layout_version": 2,
        "skills": {
            skill_name: {
                "enabled": True,
                "channels": ["all"],
                "metadata": {"description": f"{skill_name} skill"},
            }
            for skill_name in skills
        },
    }
    manifest_path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _write_skill_dir(workspace_dir: Path, skill_name: str) -> Path:
    skill_dir = workspace_dir / "skills" / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: {skill_name}\n---\n",
        encoding="utf-8",
    )
    return skill_dir


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
    )


def _model_sys_prompt() -> str:
    assert _FakeAgent.last_instance is not None
    return _FakeAgent.last_instance.sys_prompt_at_call


def _turn_notice_messages() -> list[Msg]:
    assert _FakeAgent.last_instance is not None
    return [
        msg
        for msg in _FakeAgent.last_instance.turn_msgs
        if msg.role == "system"
        and "[Skill freshness notice]" in msg.get_text_content()
    ]


def _turn_notice_text() -> str:
    notice_messages = _turn_notice_messages()
    assert len(notice_messages) == 1
    return notice_messages[0].get_text_content()


def _streamed_notice_messages(outputs) -> list[Msg]:
    return [
        msg
        for msg, _last in outputs
        if msg.role == "system"
        and "[Skill freshness notice]" in msg.get_text_content()
    ]


def _cyclomatic_complexity(func) -> int:
    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source)
    decision_nodes = (
        ast.If,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.Try,
        ast.ExceptHandler,
        ast.BoolOp,
        ast.IfExp,
        ast.comprehension,
    )
    return 1 + sum(isinstance(node, decision_nodes) for node in ast.walk(tree))


def test_refresh_session_skill_freshness_complexity_stays_bounded() -> None:
    assert (
        _cyclomatic_complexity(
            AgentRunner._refresh_session_skill_freshness,
        )
        <= 10
    )


@pytest.mark.asyncio
async def test_safe_json_session_persists_skill_snapshot_at_top_level(
    tmp_path: Path,
) -> None:
    session = SafeJSONSession(save_dir=str(tmp_path))

    await session.save_session_skill_snapshot(
        session_id="session-1",
        user_id="user-1",
        snapshot={
            "xlsx": {
                "skill_name": "xlsx",
                "resolved_skill_dir": "/tmp/xlsx",
                "freshness_token": 123.0,
            },
        },
    )

    state = await session.get_session_state_dict("session-1", user_id="user-1")

    assert state["session_skill_snapshot"]["xlsx"]["skill_name"] == "xlsx"
    assert "session_skill_snapshot" not in state.get("agent", {})


@pytest.mark.asyncio
async def test_refresh_session_skill_freshness_preserves_builtin_skill_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _patch_runner(monkeypatch, tmp_path)
    builtin_skill_dir = get_builtin_skills_dir() / "xlsx"
    if not builtin_skill_dir.exists():
        pytest.skip("xlsx builtin skill is not available")

    _FakeAgent.effective_skills = ["xlsx"]
    await runner.session.save_session_skill_snapshot(
        session_id="session-1",
        user_id="user-1",
        snapshot={
            "xlsx": {
                "skill_name": "xlsx",
                "resolved_skill_dir": str(builtin_skill_dir),
                "freshness_token": get_skill_freshness_token(
                    builtin_skill_dir,
                ),
                "confirmed_at": 1.0,
            },
        },
    )

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="continue")],
            request=_request(),
        )
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    assert _streamed_notice_messages(outputs) == []
    state = await runner.session.get_session_state_dict(
        "session-1",
        user_id="user-1",
    )
    assert state["session_skill_snapshot"]["xlsx"][
        "resolved_skill_dir"
    ] == str(
        builtin_skill_dir,
    )


@pytest.mark.asyncio
async def test_declared_skill_start_persists_snapshot_in_same_turn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _patch_runner(monkeypatch, tmp_path)
    _FakeAgent.effective_skills = ["xlsx"]
    _write_skill_manifest(tmp_path, skills=["xlsx"])
    skill_dir = _write_skill_dir(tmp_path, "xlsx")
    monkeypatch.setattr(
        "swe.agents.skills_manager.get_skill_freshness_token",
        lambda path: 55.0 if Path(path) == skill_dir else 0.0,
    )
    original_stream_completion_lifecycle = runner._stream_completion_lifecycle

    async def confirm_skill_during_completion(*args, **kwargs):
        runtime = kwargs["runtime"]
        await runtime.session_skill_detector.start_skill(
            "xlsx",
            trigger_tool="read_file",
            trigger_reason="skill_md",
            confidence=1.0,
        )
        async for item in original_stream_completion_lifecycle(
            request=kwargs["request"],
            runtime=runtime,
            plan=kwargs["plan"],
            outcome=kwargs["outcome"],
        ):
            yield item

    monkeypatch.setattr(
        runner,
        "_stream_completion_lifecycle",
        confirm_skill_during_completion,
    )

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="use xlsx")],
            request=_request(),
        )
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    state = await runner.session.get_session_state_dict(
        "session-1",
        user_id="user-1",
    )
    snapshot = state["session_skill_snapshot"]["xlsx"]
    assert snapshot["skill_name"] == "xlsx"
    assert snapshot["resolved_skill_dir"] == str(skill_dir)
    assert snapshot["freshness_token"] == 55.0
    assert isinstance(snapshot["confirmed_at"], float)


@pytest.mark.asyncio
async def test_declared_skill_start_persists_query_snapshot_token(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _patch_runner(monkeypatch, tmp_path)
    _FakeAgent.effective_skills = ["xlsx"]
    _write_skill_manifest(tmp_path, skills=["xlsx"])
    skill_dir = _write_skill_dir(tmp_path, "xlsx")
    initial_token = get_skill_freshness_token(skill_dir)
    original_stream_completion_lifecycle = runner._stream_completion_lifecycle

    async def mutate_skill_dir_during_completion(*args, **kwargs):
        del args
        runtime = kwargs["runtime"]
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            (
                "---\n"
                "name: xlsx\n"
                "description: updated xlsx\n"
                "---\n"
                "updated during turn\n"
            ),
            encoding="utf-8",
        )
        assert get_skill_freshness_token(skill_dir) != initial_token
        await runtime.session_skill_detector.start_skill(
            "xlsx",
            trigger_tool="read_file",
            trigger_reason="skill_md",
            confidence=1.0,
        )
        async for item in original_stream_completion_lifecycle(
            request=kwargs["request"],
            runtime=kwargs["runtime"],
            plan=kwargs["plan"],
            outcome=kwargs["outcome"],
        ):
            yield item

    monkeypatch.setattr(
        runner,
        "_stream_completion_lifecycle",
        mutate_skill_dir_during_completion,
    )

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="use xlsx")],
            request=_request(),
        )
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    state = await runner.session.get_session_state_dict(
        "session-1",
        user_id="user-1",
    )
    snapshot = state["session_skill_snapshot"]["xlsx"]
    assert snapshot["skill_name"] == "xlsx"
    assert snapshot["resolved_skill_dir"] == str(skill_dir)
    assert snapshot["freshness_token"] == initial_token
    assert isinstance(snapshot["confirmed_at"], float)


@pytest.mark.asyncio
async def test_blocked_turn_persists_confirmed_skill_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _patch_runner(monkeypatch, tmp_path)
    _FakeAgent.effective_skills = ["xlsx"]
    _write_skill_manifest(tmp_path, skills=["xlsx"])
    skill_dir = _write_skill_dir(tmp_path, "xlsx")
    monkeypatch.setattr(
        "swe.agents.skills_manager.get_skill_freshness_token",
        lambda path: 55.0 if Path(path) == skill_dir else 0.0,
    )

    async def blocked_stream_completion_lifecycle(*args, **kwargs):
        runtime = kwargs["runtime"]
        await runtime.session_skill_detector.start_skill(
            "xlsx",
            trigger_tool="read_file",
            trigger_reason="skill_md",
            confidence=1.0,
        )
        outcome = kwargs["outcome"]
        outcome.task_completed = False
        outcome.completion_blocked = True
        outcome.completion_block_reason = "stop blocked"
        yield Msg(
            name="Friday",
            role="assistant",
            content="stop blocked",
        ), True

    monkeypatch.setattr(
        runner,
        "_stream_completion_lifecycle",
        blocked_stream_completion_lifecycle,
    )

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="use xlsx")],
            request=_request(),
        )
    ]

    assert outputs[-1][0].get_text_content() == "stop blocked"
    state = await runner.session.get_session_state_dict(
        "session-1",
        user_id="user-1",
    )
    snapshot = state["session_skill_snapshot"]["xlsx"]
    assert snapshot["skill_name"] == "xlsx"
    assert snapshot["resolved_skill_dir"] == str(skill_dir)
    assert snapshot["freshness_token"] == 55.0
    assert isinstance(snapshot["confirmed_at"], float)


@pytest.mark.asyncio
async def test_freshness_notice_is_model_only_system_turn_message(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _patch_runner(monkeypatch, tmp_path)
    _FakeAgent.effective_skills = ["xlsx"]
    _write_skill_manifest(tmp_path, skills=["xlsx"])
    skill_dir = _write_skill_dir(tmp_path, "xlsx")
    await runner.session.save_session_skill_snapshot(
        session_id="session-1",
        user_id="user-1",
        snapshot={
            "xlsx": {
                "skill_name": "xlsx",
                "resolved_skill_dir": str(skill_dir),
                "freshness_token": 10.0,
            },
        },
    )
    monkeypatch.setattr(
        "swe.agents.skills_manager.get_skill_freshness_token",
        lambda _path: 11.0,
    )

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="continue")],
            request=_request(),
        )
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    assert _FakeAgent.last_instance is not None
    notice_messages = [
        msg
        for msg in _FakeAgent.last_instance.turn_msgs
        if msg.role == "system"
    ]
    assert len(notice_messages) == 1
    notice_text = notice_messages[0].get_text_content()
    assert "[Skill freshness notice]" in notice_text
    assert "detected skill-directory change" in notice_text
    streamed_notice_messages = [
        msg
        for msg, _last in outputs
        if msg.role == "system"
        and "[Skill freshness notice]" in msg.get_text_content()
    ]
    assert streamed_notice_messages == []
    assert "[Skill freshness notice]" not in _model_sys_prompt()


@pytest.mark.asyncio
async def test_freshness_notice_requires_reloading_current_skill_md(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _patch_runner(monkeypatch, tmp_path)
    _FakeAgent.effective_skills = ["xlsx"]
    _write_skill_manifest(tmp_path, skills=["xlsx"])
    skill_dir = _write_skill_dir(tmp_path, "xlsx")
    updated_instruction = "CURRENT_SKILL_MD_DIRECTIVE_20260609"
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: xlsx\n"
            "description: updated xlsx\n"
            "---\n"
            f"{updated_instruction}\n"
        ),
        encoding="utf-8",
    )
    await runner.session.save_session_skill_snapshot(
        session_id="session-1",
        user_id="user-1",
        snapshot={
            "xlsx": {
                "skill_name": "xlsx",
                "resolved_skill_dir": str(skill_dir),
                "freshness_token": 10.0,
            },
        },
    )
    monkeypatch.setattr(
        "swe.agents.skills_manager.get_skill_freshness_token",
        lambda _path: 11.0,
    )

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="continue")],
            request=_request(),
        )
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    notice_text = _turn_notice_text()
    assert "You MUST re-read" in notice_text
    assert str(skill_dir / "SKILL.md") in notice_text
    assert updated_instruction not in notice_text


@pytest.mark.asyncio
async def test_freshness_notice_is_hidden_from_agentscope_runtime_stream_adapter(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _patch_runner(monkeypatch, tmp_path)
    _FakeAgent.effective_skills = ["xlsx"]
    _write_skill_manifest(tmp_path, skills=["xlsx"])
    skill_dir = _write_skill_dir(tmp_path, "xlsx")
    await runner.session.save_session_skill_snapshot(
        session_id="session-1",
        user_id="user-1",
        snapshot={
            "xlsx": {
                "skill_name": "xlsx",
                "resolved_skill_dir": str(skill_dir),
                "freshness_token": 10.0,
            },
        },
    )
    monkeypatch.setattr(
        "swe.agents.skills_manager.get_skill_freshness_token",
        lambda _path: 11.0,
    )

    async def source_stream():
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="continue")],
            request=_request(),
        ):
            yield item

    events = [
        event
        async for event in adapt_agentscope_message_stream(source_stream())
    ]
    serialized_events = [event.model_dump_json() for event in events]

    assert not any(
        "[Skill freshness notice]" in event for event in serialized_events
    )
    assert any("agent reply" in event for event in serialized_events)


@pytest.mark.asyncio
async def test_declared_skill_resume_emits_freshness_notice_before_persisting_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _patch_runner(monkeypatch, tmp_path)
    _FakeAgent.effective_skills = ["xlsx"]
    _write_skill_manifest(tmp_path, skills=["xlsx"])
    skill_dir = _write_skill_dir(tmp_path, "xlsx")
    await runner.session.save_session_skill_snapshot(
        session_id="session-1",
        user_id="user-1",
        snapshot={
            "xlsx": {
                "skill_name": "xlsx",
                "resolved_skill_dir": str(skill_dir),
                "freshness_token": 10.0,
            },
        },
    )
    monkeypatch.setattr(
        "swe.agents.skills_manager.get_skill_freshness_token",
        lambda _path: 11.0,
    )

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="use xlsx")],
            request=_request(),
        )
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    assert "detected skill-directory change" in _turn_notice_text()

    state = await runner.session.get_session_state_dict(
        "session-1",
        user_id="user-1",
    )
    assert state["session_skill_snapshot"] == {
        "xlsx": {
            "skill_name": "xlsx",
            "resolved_skill_dir": str(skill_dir),
            "freshness_token": 11.0,
        },
    }


@pytest.mark.asyncio
async def test_turn_start_aggregates_token_change_and_withdrawal_notice(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _patch_runner(monkeypatch, tmp_path)
    _FakeAgent.effective_skills = ["xlsx"]
    _write_skill_manifest(tmp_path, skills=["xlsx", "pdf"])
    xlsx_dir = _write_skill_dir(tmp_path, "xlsx")
    pdf_dir = _write_skill_dir(tmp_path, "pdf")
    await runner.session.save_session_skill_snapshot(
        session_id="session-1",
        user_id="user-1",
        snapshot={
            "xlsx": {
                "skill_name": "xlsx",
                "resolved_skill_dir": str(xlsx_dir),
                "freshness_token": 10.0,
            },
            "pdf": {
                "skill_name": "pdf",
                "resolved_skill_dir": str(pdf_dir),
                "freshness_token": 20.0,
            },
        },
    )
    monkeypatch.setattr(
        "swe.agents.skills_manager.get_skill_freshness_token",
        lambda path: 11.0 if Path(path) == xlsx_dir else 20.0,
    )

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="continue")],
            request=_request(),
        )
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    notice_text = _turn_notice_text()
    assert "xlsx" in notice_text
    assert "detected skill-directory change" in notice_text
    assert "pdf" in notice_text
    assert "no longer effective" in notice_text
    state = await runner.session.get_session_state_dict(
        "session-1",
        user_id="user-1",
    )
    assert state["session_skill_snapshot"] == {
        "xlsx": {
            "skill_name": "xlsx",
            "resolved_skill_dir": str(xlsx_dir),
            "freshness_token": 11.0,
        },
    }


@pytest.mark.asyncio
async def test_turn_start_notice_reports_directory_switch_paths(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _patch_runner(monkeypatch, tmp_path)
    _FakeAgent.effective_skills = ["xlsx"]
    _write_skill_manifest(tmp_path, skills=["xlsx"])
    new_dir = _write_skill_dir(tmp_path, "xlsx")
    old_dir = tmp_path / "legacy-skills" / "xlsx"
    old_dir.mkdir(parents=True)
    await runner.session.save_session_skill_snapshot(
        session_id="session-1",
        user_id="user-1",
        snapshot={
            "xlsx": {
                "skill_name": "xlsx",
                "resolved_skill_dir": str(old_dir),
                "freshness_token": 10.0,
            },
        },
    )
    monkeypatch.setattr(
        "swe.agents.skills_manager.get_skill_freshness_token",
        lambda _path: 44.0,
    )

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="continue")],
            request=_request(),
        )
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    notice_text = _turn_notice_text()
    assert f"{old_dir} -> {new_dir}" in notice_text
    assert "You MUST re-read" in notice_text
    assert str(new_dir / "SKILL.md") in notice_text


@pytest.mark.asyncio
async def test_manifest_confirmed_missing_skill_is_withdrawn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _patch_runner(monkeypatch, tmp_path)
    _FakeAgent.effective_skills = []
    _write_skill_manifest(tmp_path, skills=[])
    missing_dir = tmp_path / "skills" / "missing"
    await runner.session.save_session_skill_snapshot(
        session_id="session-1",
        user_id="user-1",
        snapshot={
            "missing": {
                "skill_name": "missing",
                "resolved_skill_dir": str(missing_dir),
                "freshness_token": 10.0,
            },
        },
    )
    monkeypatch.setattr(
        "swe.agents.skills_manager.get_skill_freshness_token",
        lambda _path: 0.0,
    )

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="continue")],
            request=_request(),
        )
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    assert "missing: no longer effective" in _turn_notice_text()
    state = await runner.session.get_session_state_dict(
        "session-1",
        user_id="user-1",
    )
    assert state["session_skill_snapshot"] == {}


@pytest.mark.asyncio
async def test_missing_stored_skill_dir_still_reports_directory_switch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _patch_runner(monkeypatch, tmp_path)
    _FakeAgent.effective_skills = ["xlsx"]
    _write_skill_manifest(tmp_path, skills=["xlsx"])
    new_dir = _write_skill_dir(tmp_path, "xlsx")
    old_dir = tmp_path / "legacy-skills" / "xlsx"
    await runner.session.save_session_skill_snapshot(
        session_id="session-1",
        user_id="user-1",
        snapshot={
            "xlsx": {
                "skill_name": "xlsx",
                "resolved_skill_dir": str(old_dir),
                "freshness_token": 10.0,
            },
        },
    )
    monkeypatch.setattr(
        "swe.agents.skills_manager.get_skill_freshness_token",
        lambda _path: 44.0,
    )

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="continue")],
            request=_request(),
        )
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    assert f"{old_dir} -> {new_dir}" in _turn_notice_text()
    state = await runner.session.get_session_state_dict(
        "session-1",
        user_id="user-1",
    )
    assert state["session_skill_snapshot"] == {
        "xlsx": {
            "skill_name": "xlsx",
            "resolved_skill_dir": str(new_dir),
            "freshness_token": 44.0,
        },
    }


@pytest.mark.asyncio
async def test_unchanged_snapshot_emits_no_notice(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _patch_runner(monkeypatch, tmp_path)
    _FakeAgent.effective_skills = ["xlsx"]
    _write_skill_manifest(tmp_path, skills=["xlsx"])
    skill_dir = _write_skill_dir(tmp_path, "xlsx")
    await runner.session.save_session_skill_snapshot(
        session_id="session-1",
        user_id="user-1",
        snapshot={
            "xlsx": {
                "skill_name": "xlsx",
                "resolved_skill_dir": str(skill_dir),
                "freshness_token": 42.0,
            },
        },
    )
    monkeypatch.setattr(
        "swe.agents.skills_manager.get_skill_freshness_token",
        lambda _path: 42.0,
    )

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="continue")],
            request=_request(),
        )
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    assert _FakeAgent.last_instance is not None
    assert [
        msg
        for msg in _FakeAgent.last_instance.turn_msgs
        if msg.role == "system"
    ] == []


@pytest.mark.asyncio
async def test_regular_session_save_clears_stale_hook_overlay(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _patch_runner(monkeypatch, tmp_path)
    _write_skill_manifest(tmp_path, skills=["xlsx"])
    persisted_overlay = HookSessionOverlay(
        loaded_skill_sources=[
            LoadedSkillHookSource(
                source_id="skill:xlsx",
                skill_name="xlsx",
                skill_root=str(tmp_path / "skills" / "xlsx"),
                source_path=str(
                    tmp_path / "skills" / "xlsx" / "hooks" / "hooks.json",
                ),
                hook_config=HookConfig(enabled=True),
            ),
        ],
        once_executed={"skill:xlsx:once": True},
    )
    await runner.session.save_merged_state(
        session_id="session-1",
        user_id="user-1",
        state={
            "agent": {"memory": {"content": []}},
            "hook_overlay": persisted_overlay.model_dump(
                mode="json",
                by_alias=True,
            ),
            "session_skill_snapshot": {
                "xlsx": {
                    "skill_name": "xlsx",
                    "resolved_skill_dir": str(
                        tmp_path / "skills" / "xlsx",
                    ),
                    "freshness_token": 11.0,
                },
            },
        },
    )

    agent = _FakeAgent()
    await runner._save_regular_session_state(
        agent,
        session_id="session-1",
        user_id="user-1",
        hook_overlay=None,
    )

    state = await runner.session.get_session_state_dict(
        "session-1",
        user_id="user-1",
    )
    assert "hook_overlay" not in state
    assert state["session_skill_snapshot"] == {
        "xlsx": {
            "skill_name": "xlsx",
            "resolved_skill_dir": str(tmp_path / "skills" / "xlsx"),
            "freshness_token": 11.0,
        },
    }


@pytest.mark.asyncio
async def test_regular_session_save_preserves_task_session_metadata(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _patch_runner(monkeypatch, tmp_path)
    await runner.session.save_merged_state(
        session_id="session-1",
        user_id="user-1",
        state={
            "agent": {"memory": {"content": []}},
            "task_runs": [
                {
                    "run_id": "run-1",
                    "memory_start": 0,
                    "memory_end": 2,
                },
            ],
            "task_messages": [
                {
                    "id": "msg-1",
                    "role": "assistant",
                    "content": "persisted task update",
                },
            ],
            "custom_top_level_state": {"keep": True},
        },
    )

    agent = _FakeAgent()
    await runner._save_regular_session_state(
        agent,
        session_id="session-1",
        user_id="user-1",
        hook_overlay=None,
    )

    state = await runner.session.get_session_state_dict(
        "session-1",
        user_id="user-1",
    )
    assert state["task_runs"] == [
        {
            "run_id": "run-1",
            "memory_start": 0,
            "memory_end": 2,
        },
    ]
    assert state["task_messages"] == [
        {
            "id": "msg-1",
            "role": "assistant",
            "content": "persisted task update",
        },
    ]
    assert state["custom_top_level_state"] == {"keep": True}


@pytest.mark.asyncio
async def test_applied_snapshot_prevents_repeat_notice_on_later_turn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _patch_runner(monkeypatch, tmp_path)
    _FakeAgent.effective_skills = ["xlsx"]
    _write_skill_manifest(tmp_path, skills=["xlsx"])
    skill_dir = _write_skill_dir(tmp_path, "xlsx")
    await runner.session.save_session_skill_snapshot(
        session_id="session-1",
        user_id="user-1",
        snapshot={
            "xlsx": {
                "skill_name": "xlsx",
                "resolved_skill_dir": str(skill_dir),
                "freshness_token": 10.0,
            },
        },
    )
    monkeypatch.setattr(
        "swe.agents.skills_manager.get_skill_freshness_token",
        lambda _path: 11.0,
    )

    first_outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="continue")],
            request=_request(),
        )
    ]

    assert first_outputs[-1][0].get_text_content() == "agent reply"
    assert _streamed_notice_messages(first_outputs) == []

    second_outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="continue again")],
            request=_request(),
        )
    ]

    assert second_outputs[-1][0].get_text_content() == "agent reply"
    assert _streamed_notice_messages(second_outputs) == []


@pytest.mark.asyncio
async def test_freshness_notice_is_not_persisted_in_session_memory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _patch_runner(monkeypatch, tmp_path)
    _FakeAgent.effective_skills = ["xlsx"]
    _write_skill_manifest(tmp_path, skills=["xlsx"])
    skill_dir = _write_skill_dir(tmp_path, "xlsx")
    await runner.session.save_session_skill_snapshot(
        session_id="session-1",
        user_id="user-1",
        snapshot={
            "xlsx": {
                "skill_name": "xlsx",
                "resolved_skill_dir": str(skill_dir),
                "freshness_token": 10.0,
            },
        },
    )
    monkeypatch.setattr(
        "swe.agents.skills_manager.get_skill_freshness_token",
        lambda _path: 11.0,
    )

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="continue")],
            request=_request(),
        )
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    assert _streamed_notice_messages(outputs) == []

    state = await runner.session.get_session_state_dict(
        "session-1",
        user_id="user-1",
    )
    persisted_texts = [
        entry[0]["content"]
        for entry in state["agent"]["memory"]["content"]
        if isinstance(entry, list)
        and entry
        and isinstance(entry[0], dict)
        and isinstance(entry[0].get("content"), str)
    ]
    assert not any(
        text.startswith("[Skill freshness notice]") for text in persisted_texts
    )


@pytest.mark.asyncio
async def test_retryable_plan_failure_defers_snapshot_persistence_until_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _patch_runner(monkeypatch, tmp_path)
    _FakeAgent.effective_skills = ["xlsx"]
    _write_skill_manifest(tmp_path, skills=["xlsx"])
    skill_dir = _write_skill_dir(tmp_path, "xlsx")
    await runner.session.save_session_skill_snapshot(
        session_id="session-1",
        user_id="user-1",
        snapshot={
            "xlsx": {
                "skill_name": "xlsx",
                "resolved_skill_dir": str(skill_dir),
                "freshness_token": 10.0,
            },
        },
    )
    monkeypatch.setattr(
        "swe.agents.skills_manager.get_skill_freshness_token",
        lambda _path: 11.0,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: SimpleNamespace(
            id="test-agent",
            hooks=SimpleNamespace(enabled=False, events={}),
            mcp=None,
            running=SimpleNamespace(
                suggestions=SimpleNamespace(
                    enabled=False,
                    mode=SuggestionMode.DISABLED,
                ),
                query_retry=SimpleNamespace(
                    enabled=True,
                    max_retries=1,
                    backoff_base=0.0,
                    backoff_cap=0.0,
                ),
            ),
        ),
    )

    observed_snapshots: list[dict[str, dict]] = []
    original_build_turn_plan = runner._build_turn_plan

    async def flaky_build_turn_plan(*args, **kwargs):
        session_execution = kwargs["runtime"].session_execution
        observed_snapshots.append(
            dict(
                session_execution.state.get(
                    SESSION_SKILL_SNAPSHOT_STATE_KEY,
                    {},
                ),
            ),
        )
        if len(observed_snapshots) == 1:
            raise RuntimeError("request timed out")
        return await original_build_turn_plan(
            runtime=kwargs["runtime"],
            request=kwargs["request"],
            msgs=kwargs["msgs"],
            query=kwargs["query"],
        )

    monkeypatch.setattr(runner, "_build_turn_plan", flaky_build_turn_plan)

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="continue")],
            request=_request(),
        )
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    assert observed_snapshots == [
        {
            "xlsx": {
                "skill_name": "xlsx",
                "resolved_skill_dir": str(skill_dir),
                "freshness_token": 10.0,
            },
        },
        {
            "xlsx": {
                "skill_name": "xlsx",
                "resolved_skill_dir": str(skill_dir),
                "freshness_token": 10.0,
            },
        },
    ]
    assert "detected skill-directory change" in _turn_notice_text()

    state = await runner.session.get_session_state_dict(
        "session-1",
        user_id="user-1",
    )
    assert state["session_skill_snapshot"] == {
        "xlsx": {
            "skill_name": "xlsx",
            "resolved_skill_dir": str(skill_dir),
            "freshness_token": 11.0,
        },
    }


@pytest.mark.asyncio
async def test_retryable_completion_failure_defers_snapshot_persistence_until_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _patch_runner(monkeypatch, tmp_path)
    _FakeAgent.effective_skills = ["xlsx"]
    _write_skill_manifest(tmp_path, skills=["xlsx"])
    skill_dir = _write_skill_dir(tmp_path, "xlsx")
    await runner.session.save_session_skill_snapshot(
        session_id="session-1",
        user_id="user-1",
        snapshot={
            "xlsx": {
                "skill_name": "xlsx",
                "resolved_skill_dir": str(skill_dir),
                "freshness_token": 10.0,
            },
        },
    )
    monkeypatch.setattr(
        "swe.agents.skills_manager.get_skill_freshness_token",
        lambda _path: 11.0,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: SimpleNamespace(
            id="test-agent",
            hooks=SimpleNamespace(enabled=False, events={}),
            mcp=None,
            running=SimpleNamespace(
                suggestions=SimpleNamespace(
                    enabled=False,
                    mode=SuggestionMode.DISABLED,
                ),
                query_retry=SimpleNamespace(
                    enabled=True,
                    max_retries=1,
                    backoff_base=0.0,
                    backoff_cap=0.0,
                ),
            ),
        ),
    )

    observed_snapshots: list[dict[str, dict]] = []
    original_stream_completion_lifecycle = runner._stream_completion_lifecycle

    async def flaky_stream_completion_lifecycle(*args, **kwargs):
        session_execution = kwargs["runtime"].session_execution
        observed_snapshots.append(
            dict(
                session_execution.state.get(
                    SESSION_SKILL_SNAPSHOT_STATE_KEY,
                    {},
                ),
            ),
        )
        if len(observed_snapshots) == 1:
            raise RuntimeError("request timed out")
        async for item in original_stream_completion_lifecycle(
            request=kwargs["request"],
            runtime=kwargs["runtime"],
            plan=kwargs["plan"],
            outcome=kwargs["outcome"],
        ):
            yield item

    monkeypatch.setattr(
        runner,
        "_stream_completion_lifecycle",
        flaky_stream_completion_lifecycle,
    )

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="continue")],
            request=_request(),
        )
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    assert observed_snapshots == [
        {
            "xlsx": {
                "skill_name": "xlsx",
                "resolved_skill_dir": str(skill_dir),
                "freshness_token": 10.0,
            },
        },
        {
            "xlsx": {
                "skill_name": "xlsx",
                "resolved_skill_dir": str(skill_dir),
                "freshness_token": 10.0,
            },
        },
    ]
    assert "detected skill-directory change" in _turn_notice_text()

    state = await runner.session.get_session_state_dict(
        "session-1",
        user_id="user-1",
    )
    assert state["session_skill_snapshot"] == {
        "xlsx": {
            "skill_name": "xlsx",
            "resolved_skill_dir": str(skill_dir),
            "freshness_token": 11.0,
        },
    }


@pytest.mark.asyncio
async def test_retryable_completion_failure_keeps_notice_model_only(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = _patch_runner(monkeypatch, tmp_path)
    _FakeAgent.effective_skills = ["xlsx"]
    _write_skill_manifest(tmp_path, skills=["xlsx"])
    skill_dir = _write_skill_dir(tmp_path, "xlsx")
    await runner.session.save_session_skill_snapshot(
        session_id="session-1",
        user_id="user-1",
        snapshot={
            "xlsx": {
                "skill_name": "xlsx",
                "resolved_skill_dir": str(skill_dir),
                "freshness_token": 10.0,
            },
        },
    )
    monkeypatch.setattr(
        "swe.agents.skills_manager.get_skill_freshness_token",
        lambda _path: 11.0,
    )
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *args, **kwargs: SimpleNamespace(
            id="test-agent",
            hooks=SimpleNamespace(enabled=False, events={}),
            mcp=None,
            running=SimpleNamespace(
                suggestions=SimpleNamespace(
                    enabled=False,
                    mode=SuggestionMode.DISABLED,
                ),
                query_retry=SimpleNamespace(
                    enabled=True,
                    max_retries=1,
                    backoff_base=0.0,
                    backoff_cap=0.0,
                ),
            ),
        ),
    )

    plan_notice_counts: list[int] = []
    original_stream_completion_lifecycle = runner._stream_completion_lifecycle

    async def flaky_stream_completion_lifecycle(*args, **kwargs):
        plan_notice_counts.append(
            len(
                [
                    msg
                    for msg in kwargs["plan"].turn_msgs
                    if msg.role == "system"
                    and "[Skill freshness notice]" in msg.get_text_content()
                ],
            ),
        )
        if len(plan_notice_counts) == 1:
            raise RuntimeError("request timed out")
        async for item in original_stream_completion_lifecycle(
            request=kwargs["request"],
            runtime=kwargs["runtime"],
            plan=kwargs["plan"],
            outcome=kwargs["outcome"],
        ):
            yield item

    monkeypatch.setattr(
        runner,
        "_stream_completion_lifecycle",
        flaky_stream_completion_lifecycle,
    )

    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="continue")],
            request=_request(),
        )
    ]

    assert outputs[-1][0].get_text_content() == "agent reply"
    assert plan_notice_counts == [1, 1]
    assert _streamed_notice_messages(outputs) == []


@pytest.mark.asyncio
async def test_query_retry_keeps_starting_snapshot_across_ttl(
    monkeypatch,
    tmp_path,
):
    from swe.agents import skill_runtime_snapshot as snapshots

    runner = _patch_runner(monkeypatch, tmp_path)
    _FakeAgent.effective_skills = ["xlsx"]
    _write_skill_manifest(tmp_path, skills=["xlsx"])
    directory = _write_skill_dir(tmp_path, "xlsx")
    now = [0.0]
    monkeypatch.setattr(snapshots.time, "monotonic", lambda: now[0])
    config = _agent_config()
    config.running.query_retry.enabled = True
    config.running.query_retry.max_retries = 1
    monkeypatch.setattr(
        "swe.app.runner.runner.load_agent_config",
        lambda *a, **k: config,
    )
    original = runner._build_turn_plan
    observed = []

    async def retry_after_expiry(*args, **kwargs):
        snapshot = kwargs["runtime"].agent._workspace_skill_snapshot
        observed.append(snapshot)
        if len(observed) == 1:
            (directory / "resource.txt").write_text(
                "changed after Query start",
            )
            now[0] = 600
            raise RuntimeError("request timed out")
        return await original(
            runtime=kwargs["runtime"],
            request=kwargs["request"],
            msgs=kwargs["msgs"],
            query=kwargs["query"],
        )

    monkeypatch.setattr(runner, "_build_turn_plan", retry_after_expiry)
    outputs = [
        item
        async for item in runner.query_handler(
            [Msg(name="user", role="user", content="continue")],
            request=_request(),
        )
    ]
    assert outputs[-1][0].get_text_content() == "agent reply"
    assert len(observed) == 2
    assert observed[0] is observed[1]
    following = await snapshots.get_workspace_skill_snapshot_async(tmp_path)
    assert following is not observed[0]
    assert (
        following.skills["xlsx"].freshness_token
        != observed[0].skills["xlsx"].freshness_token
    )

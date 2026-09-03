# -*- coding: utf-8 -*-
"""验证 Runner 对 Plan Mode chat 元数据的状态迁移。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from src.swe.app.runner.manager import ChatManager
from src.swe.app.runner.models import ChatSpec, ChatsFile
from src.swe.app.runner.repo import BaseChatRepository
from src.swe.app.runner.runner import AgentRunner


class _InMemoryChatRepo(BaseChatRepository):
    def __init__(self) -> None:
        self._state = ChatsFile(version=1, chats=[])
        self.path = "<memory>"

    async def load(self) -> ChatsFile:
        return self._state.model_copy(deep=True)

    async def save(self, chats_file: ChatsFile) -> None:
        self._state = chats_file.model_copy(deep=True)

    async def get_chat(self, chat_id: str) -> ChatSpec | None:
        for chat in (await self.load()).chats:
            if chat.id == chat_id:
                return chat
        return None

    async def get_chat_by_id(
        self,
        session_id: str,
        user_id: str,
        channel: str = "console",
    ) -> ChatSpec | None:
        for chat in (await self.load()).chats:
            if (
                chat.session_id == session_id
                and chat.user_id == user_id
                and chat.channel == channel
            ):
                return chat
        return None

    async def upsert_chat(self, spec: ChatSpec) -> None:
        chats_file = await self.load()
        for index, chat in enumerate(chats_file.chats):
            if chat.id == spec.id:
                chats_file.chats[index] = spec
                break
        else:
            chats_file.chats.append(spec)
        await self.save(chats_file)

    async def filter_chats(self, user_id=None, channel=None) -> list[ChatSpec]:
        chats = (await self.load()).chats
        if user_id is not None:
            chats = [chat for chat in chats if chat.user_id == user_id]
        if channel is not None:
            chats = [chat for chat in chats if chat.channel == channel]
        return chats


def _request(meta: dict) -> SimpleNamespace:
    return SimpleNamespace(channel_meta=meta)


async def _runner_with_repo() -> tuple[AgentRunner, _InMemoryChatRepo]:
    repo = _InMemoryChatRepo()
    runner = AgentRunner(agent_id="agent-a")
    runner.set_chat_manager(ChatManager(repo=repo))
    return runner, repo


async def _resolve_chat(runner: AgentRunner, request: SimpleNamespace):
    return await runner._get_or_create_chat(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        name="hello",
        request=request,
        turn_id="turn-1",
    )


async def test_explicit_plan_request_persists_plan_mode_enabled() -> None:
    runner, repo = await _runner_with_repo()
    request = _request({"mode": "plan"})

    chat = await _resolve_chat(runner, request)
    persisted = await repo.get_chat(chat.id)

    assert persisted is not None
    assert persisted.meta["plan_mode_enabled"] is True
    assert request.channel_meta["plan_mode_enabled"] is True


async def test_revise_keeps_plan_mode_enabled() -> None:
    runner, repo = await _runner_with_repo()
    request = _request(
        {
            "plan_interaction_response": {
                "plan_id": "plan-1",
                "decision": "revise",
            },
        },
    )

    chat = await _resolve_chat(runner, request)
    persisted = await repo.get_chat(chat.id)

    assert persisted is not None
    assert persisted.meta["plan_mode_enabled"] is True


async def test_execute_and_exit_disable_plan_mode() -> None:
    for decision in ("execute", "exit_plan"):
        runner, repo = await _runner_with_repo()
        await runner._chat_manager.get_or_create_chat(
            "session-1",
            "user-1",
            "console",
            meta={"plan_mode_enabled": True},
        )
        request = _request(
            {
                "plan_interaction_response": {
                    "plan_id": "plan-1",
                    "decision": decision,
                },
            },
        )

        chat = await _resolve_chat(runner, request)
        persisted = await repo.get_chat(chat.id)

        assert persisted is not None
        assert persisted.meta["plan_mode_enabled"] is False
        assert request.channel_meta["plan_mode_enabled"] is False


async def test_manual_normal_mode_disables_plan_mode() -> None:
    runner, repo = await _runner_with_repo()
    await runner._chat_manager.get_or_create_chat(
        "session-1",
        "user-1",
        "console",
        meta={"plan_mode_enabled": True},
    )
    request = _request({"mode": "normal"})

    chat = await _resolve_chat(runner, request)
    persisted = await repo.get_chat(chat.id)

    assert persisted is not None
    assert persisted.meta["plan_mode_enabled"] is False
    assert request.channel_meta["plan_mode_enabled"] is False


async def test_scheduled_request_forces_normal_mode_without_clearing_persisted_state() -> (
    None
):
    runner, repo = await _runner_with_repo()
    await runner._chat_manager.get_or_create_chat(
        "session-1",
        "user-1",
        "console",
        meta={"plan_mode_enabled": True},
    )
    request = _request({"plan_mode_enabled": True})
    request.execution_origin = "scheduled"

    chat = await _resolve_chat(runner, request)
    persisted = await repo.get_chat(chat.id)

    assert persisted is not None
    assert persisted.meta["plan_mode_enabled"] is True
    assert request.channel_meta["plan_mode_enabled"] is False


async def test_persisted_plan_mode_reaches_agent_request_context() -> None:
    runner, _repo = await _runner_with_repo()
    chat = SimpleNamespace(id="chat-1", meta={"plan_mode_enabled": True})
    request = _request(
        {
            "plan_mode_enabled": True,
            "plan_interaction_response": {"decision": "revise"},
        },
    )
    runner.session = SimpleNamespace(_get_save_path=lambda *_args: "session")

    with patch("src.swe.app.runner.runner.SWEAgent") as agent_cls:
        runner._create_agent_for_query(
            agent_config=SimpleNamespace(),
            env_context="",
            mcp_clients=[],
            request=request,
            session_id="session-1",
            user_id="user-1",
            channel="console",
            chat=chat,
            turn_id="turn-1",
            hook_overlay=SimpleNamespace(model_dump=lambda **_kwargs: {}),
            auth_token=None,
            approved_tool_call=None,
        )

    request_context = agent_cls.call_args.kwargs["request_context"]
    assert request_context["plan_mode_enabled"] is True
    assert request_context["mode"] == "plan"
    assert request_context["plan_interaction_response"] == {
        "decision": "revise",
    }


async def test_accepted_plan_context_reaches_agent_request_context() -> None:
    runner, _repo = await _runner_with_repo()
    chat = SimpleNamespace(id="chat-1", meta={"plan_mode_enabled": False})
    accepted_plan = {
        "plan_id": "plan-1",
        "title": "Persisted plan",
        "steps": ["Use persisted record"],
    }
    request = _request(
        {
            "plan_mode_enabled": False,
            "accepted_plan": accepted_plan,
            "accepted_plan_source": "server_plan_store",
            "plan_interaction_response": {
                "decision": "execute",
                "plan_snapshot": {"title": "Tampered frontend plan"},
            },
        },
    )
    runner.session = SimpleNamespace(_get_save_path=lambda *_args: "session")

    with patch("src.swe.app.runner.runner.SWEAgent") as agent_cls:
        runner._create_agent_for_query(
            agent_config=SimpleNamespace(),
            env_context="",
            mcp_clients=[],
            request=request,
            session_id="session-1",
            user_id="user-1",
            channel="console",
            chat=chat,
            turn_id="turn-1",
            hook_overlay=SimpleNamespace(model_dump=lambda **_kwargs: {}),
            auth_token=None,
            approved_tool_call=None,
        )

    request_context = agent_cls.call_args.kwargs["request_context"]
    assert request_context["mode"] == "normal"
    assert request_context["accepted_plan"] == accepted_plan
    assert request_context["accepted_plan_source"] == "server_plan_store"
    assert "plan_snapshot" not in request_context["accepted_plan"]


async def test_accepted_plan_without_server_source_is_ignored() -> None:
    runner, _repo = await _runner_with_repo()
    chat = SimpleNamespace(id="chat-1", meta={"plan_mode_enabled": False})
    request = _request(
        {
            "plan_mode_enabled": False,
            "accepted_plan": {
                "plan_id": "plan-1",
                "title": "Client supplied plan",
            },
        },
    )
    runner.session = SimpleNamespace(_get_save_path=lambda *_args: "session")

    with patch("src.swe.app.runner.runner.SWEAgent") as agent_cls:
        runner._create_agent_for_query(
            agent_config=SimpleNamespace(),
            env_context="",
            mcp_clients=[],
            request=request,
            session_id="session-1",
            user_id="user-1",
            channel="console",
            chat=chat,
            turn_id="turn-1",
            hook_overlay=SimpleNamespace(model_dump=lambda **_kwargs: {}),
            auth_token=None,
            approved_tool_call=None,
        )

    request_context = agent_cls.call_args.kwargs["request_context"]
    assert request_context["mode"] == "normal"
    assert "accepted_plan" not in request_context


async def test_goal_request_forces_plan_and_expert_selection_off() -> None:
    runner, _repo = await _runner_with_repo()
    chat = SimpleNamespace(id="chat-1", meta={"plan_mode_enabled": True})
    request = _request(
        {
            "goal_id": "goal-1",
            "goal_mode_enabled": True,
            "plan_mode_enabled": True,
            "mode": "plan",
            "selected_expert_id": "expert-1",
        },
    )
    runner.session = SimpleNamespace(_get_save_path=lambda *_args: "session")

    with patch("src.swe.app.runner.runner.SWEAgent") as agent_cls:
        runner._create_agent_for_query(
            agent_config=SimpleNamespace(),
            env_context="",
            mcp_clients=[],
            request=request,
            session_id="session-1",
            user_id="user-1",
            channel="console",
            chat=chat,
            turn_id="turn-1",
            hook_overlay=SimpleNamespace(model_dump=lambda **_kwargs: {}),
            auth_token=None,
            approved_tool_call=None,
        )

    request_context = agent_cls.call_args.kwargs["request_context"]
    assert request_context["goal_id"] == "goal-1"
    assert request_context["goal_mode_enabled"] is True
    assert request_context["plan_mode_enabled"] is False
    assert request_context["mode"] == "normal"
    assert "selected_expert_id" not in request_context

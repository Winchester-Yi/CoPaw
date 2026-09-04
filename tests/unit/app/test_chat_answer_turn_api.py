# -*- coding: utf-8 -*-
from __future__ import annotations

from types import SimpleNamespace

from agentscope.message import Msg
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.swe.app.runner.api import (
    get_chat_manager,
    get_session,
    get_workspace,
    router,
)
from src.swe.app.runner.manager import ChatManager
from src.swe.app.runner.models import ChatSpec, ChatsFile
from src.swe.app.runner.repo import BaseChatRepository


class _InMemoryChatRepository(BaseChatRepository):
    def __init__(self, chats: list[ChatSpec]) -> None:
        self.path = "<memory>"
        self._state = ChatsFile(chats=chats)

    async def load(self) -> ChatsFile:
        return self._state.model_copy(deep=True)

    async def save(self, chats_file: ChatsFile) -> None:
        self._state = chats_file.model_copy(deep=True)


class _FakeSession:
    async def get_session_state_dict(
        self,
        session_id: str,
        user_id: str,
    ) -> dict:
        assert session_id == "session-1"
        assert user_id == "user-1"
        return {"agent": {"memory": {"content": ["stored"]}}}


class _FakeMemory:
    def load_state_dict(
        self,
        state_dict: dict,
        strict: bool = False,
    ) -> None:
        del state_dict, strict

    async def get_memory(
        self,
        prepend_summary: bool = False,
    ) -> list[Msg]:
        del prepend_summary
        user_msg_1 = Msg(
            name="user-1",
            role="user",
            content="question",
            timestamp="2026-07-01T00:00:00Z",
        )
        user_msg_1.id = "user-msg-1"
        assistant_msg_1 = Msg(
            name="Friday",
            role="assistant",
            content="answer",
            timestamp="2026-07-01T00:00:01Z",
        )
        assistant_msg_1.id = "assistant-msg-1"
        tool_msg_1 = Msg(
            name="tool",
            role="assistant",
            content=[
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "search",
                    "input": {"q": "question"},
                },
            ],
            timestamp="2026-07-01T00:00:02Z",
        )
        tool_msg_1.id = "tool-msg-1"
        user_msg_2 = Msg(
            name="user-1",
            role="user",
            content="next question",
            timestamp="2026-07-01T00:00:03Z",
        )
        user_msg_2.id = "user-msg-2"
        assistant_msg_2 = Msg(
            name="Friday",
            role="assistant",
            content="next answer",
            timestamp="2026-07-01T00:00:04Z",
        )
        assistant_msg_2.id = "assistant-msg-2"
        return [
            user_msg_1,
            assistant_msg_1,
            tool_msg_1,
            user_msg_2,
            assistant_msg_2,
        ]


class _FakeTaskTracker:
    async def get_status(self, chat_id: str) -> str:
        assert chat_id == "chat-1"
        return "idle"


class _BatchCoordinator:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def statuses(self, chat_ids: list[str]) -> dict[str, object]:
        self.calls.append(chat_ids)
        from src.swe.app.answer_turn.models import TurnStatus

        return {
            chat_id: TurnStatus.RUNNING if chat_id == "chat-1" else None
            for chat_id in chat_ids
        }

    async def status(self, _chat_id: str):
        raise AssertionError("list endpoint must use batch statuses")


def _client(
    monkeypatch,
    *,
    include_user_identity: bool = True,
    chats: list[ChatSpec] | None = None,
    session: _FakeSession | None = None,
    coordinator: _BatchCoordinator | None = None,
) -> TestClient:
    from src.swe.app.runner import api as chat_api_module

    monkeypatch.setattr(chat_api_module, "InMemoryMemory", _FakeMemory)

    manager = ChatManager(
        repo=_InMemoryChatRepository(
            chats
            or [
                ChatSpec(
                    id="chat-1",
                    session_id="session-1",
                    user_id="user-1",
                    channel="console",
                    name="chat",
                ),
            ],
        ),
    )
    session = session or _FakeSession()
    workspace = SimpleNamespace(
        chat_manager=manager,
        task_tracker=_FakeTaskTracker(),
        runner=SimpleNamespace(session=session),
        answer_turn_coordinator=coordinator,
    )

    app = FastAPI()

    if include_user_identity:

        @app.middleware("http")
        async def _user_state(request: Request, call_next):
            request.state.user_id = "user-1"
            return await call_next(request)

    app.include_router(router)

    async def _get_workspace():
        return workspace

    async def _get_chat_manager():
        return manager

    async def _get_session():
        return session

    app.dependency_overrides[get_workspace] = _get_workspace
    app.dependency_overrides[get_chat_manager] = _get_chat_manager
    app.dependency_overrides[get_session] = _get_session
    return TestClient(app)


def test_chat_list_uses_one_batch_status_lookup(monkeypatch) -> None:
    coordinator = _BatchCoordinator()
    response = _client(monkeypatch, coordinator=coordinator).get("/chats")

    assert response.status_code == 200
    assert response.json()[0]["status"] == "running"
    assert coordinator.calls == [["chat-1"]]


def test_answer_turn_returns_anchor_question_and_following_messages(
    monkeypatch,
) -> None:
    response = _client(monkeypatch).get(
        "/chats/answer-turn",
        params={"sessionid": "session-1", "msgid": "user-msg-1"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["chat"]["id"] == "chat-1"
    assert payload["status"] == "idle"
    assert [
        message["metadata"]["original_id"] for message in payload["messages"]
    ] == [
        "user-msg-1",
        "assistant-msg-1",
        "tool-msg-1",
    ]


def test_chat_detail_reads_persisted_snapshot_without_waiting_for_execution(
    monkeypatch,
) -> None:
    class _PersistedSnapshotSession(_FakeSession):
        async def get_session_state_dict(self, *_args, **_kwargs) -> dict:
            raise AssertionError(
                "history must not wait for the execution lock",
            )

        async def get_persisted_session_state_dict(
            self,
            session_id: str,
            user_id: str,
        ) -> dict:
            assert (session_id, user_id) == ("session-1", "user-1")
            return {"agent": {"memory": {"content": ["stored"]}}}

    response = _client(
        monkeypatch,
        session=_PersistedSnapshotSession(),
    ).get("/chats/chat-1")

    assert response.status_code == 200


def test_answer_turn_returns_404_when_msgid_is_not_user_message(
    monkeypatch,
) -> None:
    response = _client(monkeypatch).get(
        "/chats/answer-turn",
        params={"sessionid": "session-1", "msgid": "assistant-msg-1"},
    )

    assert response.status_code == 404


def test_answer_turn_by_chat_id_returns_stopped_turn_status(
    monkeypatch,
) -> None:
    async def stopped_state(
        _self,
        _session_id: str,
        _user_id: str,
    ) -> dict:
        return {
            "agent": {"memory": {"content": ["stored"]}},
            "turn_states": {"user-msg-1": {"status": "stopped"}},
        }

    monkeypatch.setattr(
        _FakeSession,
        "get_session_state_dict",
        stopped_state,
    )

    response = _client(monkeypatch).get(
        "/chats/answer-turn",
        params={"chat_id": "chat-1", "msgid": "user-msg-1"},
    )

    assert response.status_code == 200
    assert response.json()["turn_status"] == "stopped"


def test_answer_turn_legacy_session_uses_persisted_turn_chat_id(
    monkeypatch,
) -> None:
    async def state_for_first_chat(
        _self,
        _session_id: str,
        _user_id: str,
    ) -> dict:
        return {
            "agent": {"memory": {"content": ["stored"]}},
            "turn_states": {
                "user-msg-1": {
                    "status": "stopped",
                    "chat_id": "chat-1",
                },
            },
        }

    monkeypatch.setattr(
        _FakeSession,
        "get_session_state_dict",
        state_for_first_chat,
    )
    response = _client(
        monkeypatch,
        chats=[
            ChatSpec(
                id="chat-1",
                session_id="session-1",
                user_id="user-1",
                channel="console",
                name="older chat",
            ),
            ChatSpec(
                id="chat-2",
                session_id="session-1",
                user_id="user-1",
                channel="console",
                name="newer chat",
            ),
        ],
    ).get(
        "/chats/answer-turn",
        params={"sessionid": "session-1", "msgid": "user-msg-1"},
    )

    assert response.status_code == 200
    assert response.json()["chat"]["id"] == "chat-1"


def test_answer_turn_requires_request_user_identity(
    monkeypatch,
) -> None:
    response = _client(monkeypatch, include_user_identity=False).get(
        "/chats/answer-turn",
        params={"sessionid": "session-1", "msgid": "user-msg-1"},
    )

    assert response.status_code == 400

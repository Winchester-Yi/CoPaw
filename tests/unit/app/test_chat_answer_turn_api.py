# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
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
from src.swe.app.runner.context_usage import (
    CONTEXT_USAGE_INVALID_STATE_KEY,
    CONTEXT_USAGE_STATE_KEY,
    ContextUsageSnapshot,
)
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
    session: object | None = None,
    coordinator: object | None = None,
    request_source_id: str | None = None,
    request_agent_id: str | None = None,
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
        answer_turn_coordinator=coordinator,
        agent_id="test-agent",
        runner=SimpleNamespace(session=session),
    )

    app = FastAPI()

    if include_user_identity:

        @app.middleware("http")
        async def _user_state(request: Request, call_next):
            request.state.user_id = "user-1"
            if request_source_id is not None:
                request.state.source_id = request_source_id
            if request_agent_id is not None:
                request.state.agent_id = request_agent_id
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


def _context_snapshot() -> dict:
    return ContextUsageSnapshot(
        used_tokens=27,
        max_tokens=100,
        remaining_tokens=73,
        usage_ratio=0.27,
        system_context_tokens=10,
        tool_definition_tokens=2,
        conversation_tokens=15,
        governance_threshold_ratio=0.65,
        active_threshold_ratio=0.8,
        emergency_threshold_ratio=0.9,
        status="normal",
        as_of=datetime(2026, 9, 2, tzinfo=timezone.utc),
    ).model_dump(mode="json")


class _ContextSnapshotSession:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.persisted_reads = 0

    async def get_session_state_dict(self, *_args, **_kwargs) -> dict:
        raise AssertionError("context usage must not acquire execution state")

    async def get_persisted_session_state_dict(
        self,
        session_id: str,
        user_id: str,
    ) -> dict:
        assert (session_id, user_id) == ("session-1", "user-1")
        self.persisted_reads += 1
        return self.state


class _Coordinator:
    def __init__(self, status: str | None) -> None:
        self._status = status

    async def status(self, chat_id: str):
        assert chat_id == "chat-1"
        return self._status


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


def test_context_usage_returns_committed_numeric_snapshot(monkeypatch) -> None:
    session = _ContextSnapshotSession(
        {CONTEXT_USAGE_STATE_KEY: _context_snapshot()},
    )

    response = _client(monkeypatch, session=session).get(
        "/chats/chat-1/context-usage",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "available": True,
        "stale": False,
        **_context_snapshot(),
    }
    assert session.persisted_reads == 1
    assert set(payload) == {
        "available",
        "stale",
        "schema_version",
        "used_tokens",
        "max_tokens",
        "remaining_tokens",
        "usage_ratio",
        "system_context_tokens",
        "tool_definition_tokens",
        "conversation_tokens",
        "governance_threshold_ratio",
        "active_threshold_ratio",
        "emergency_threshold_ratio",
        "status",
        "estimated",
        "as_of",
    }


def test_context_usage_returns_unavailable_without_runtime_construction(
    monkeypatch,
) -> None:
    session = _ContextSnapshotSession({"agent": {"memory": {}}})

    response = _client(monkeypatch, session=session).get(
        "/chats/chat-1/context-usage",
    )

    assert response.status_code == 200
    assert response.json() == {"available": False}
    assert session.persisted_reads == 1


@pytest.mark.parametrize("status", ["running", "stopping"])
def test_context_usage_marks_active_turn_snapshot_stale(
    monkeypatch,
    status: str,
) -> None:
    session = _ContextSnapshotSession(
        {CONTEXT_USAGE_STATE_KEY: _context_snapshot()},
    )

    response = _client(
        monkeypatch,
        session=session,
        coordinator=_Coordinator(status),
    ).get("/chats/chat-1/context-usage")

    assert response.status_code == 200
    assert response.json()["stale"] is True


def test_context_usage_marks_failed_capture_snapshot_stale_while_idle(
    monkeypatch,
) -> None:
    session = _ContextSnapshotSession(
        {
            CONTEXT_USAGE_STATE_KEY: _context_snapshot(),
            CONTEXT_USAGE_INVALID_STATE_KEY: True,
        },
    )

    response = _client(
        monkeypatch,
        session=session,
        coordinator=_Coordinator("idle"),
    ).get("/chats/chat-1/context-usage")

    assert response.status_code == 200
    assert response.json()["stale"] is True


def test_context_usage_treats_future_snapshot_as_unavailable(
    monkeypatch,
) -> None:
    snapshot = {**_context_snapshot(), "schema_version": 2}
    session = _ContextSnapshotSession({CONTEXT_USAGE_STATE_KEY: snapshot})

    response = _client(monkeypatch, session=session).get(
        "/chats/chat-1/context-usage",
    )

    assert response.status_code == 200
    assert response.json() == {"available": False}


def test_context_usage_hides_malformed_snapshot_values_from_logs(
    monkeypatch,
    caplog,
) -> None:
    snapshot = {
        **_context_snapshot(),
        "used_tokens": "not-a-number",
        "private_prompt": "must-not-be-logged",
    }
    session = _ContextSnapshotSession({CONTEXT_USAGE_STATE_KEY: snapshot})

    response = _client(monkeypatch, session=session).get(
        "/chats/chat-1/context-usage",
    )

    assert response.status_code == 200
    assert response.json() == {"available": False}
    assert "must-not-be-logged" not in caplog.text


@pytest.mark.parametrize(
    ("chat", "client_kwargs"),
    [
        (
            ChatSpec(
                id="chat-1",
                session_id="session-1",
                user_id="other-user",
                channel="console",
            ),
            {},
        ),
        (
            ChatSpec(
                id="chat-1",
                session_id="session-1",
                user_id="user-1",
                channel="console",
                meta={"source_id": "source-a"},
            ),
            {"request_source_id": "source-b"},
        ),
        (
            ChatSpec(
                id="chat-1",
                session_id="session-1",
                user_id="user-1",
                channel="console",
                meta={"agent_id": "agent-a"},
            ),
            {"request_agent_id": "agent-b"},
        ),
    ],
)
def test_context_usage_reuses_chat_ownership_404(
    monkeypatch,
    chat: ChatSpec,
    client_kwargs: dict,
) -> None:
    session = _ContextSnapshotSession(
        {CONTEXT_USAGE_STATE_KEY: _context_snapshot()},
    )

    response = _client(
        monkeypatch,
        chats=[chat],
        session=session,
        **client_kwargs,
    ).get("/chats/chat-1/context-usage")

    assert response.status_code == 404
    assert session.persisted_reads == 0


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

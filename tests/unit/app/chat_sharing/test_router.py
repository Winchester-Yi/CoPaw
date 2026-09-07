# -*- coding: utf-8 -*-
from __future__ import annotations

from types import SimpleNamespace

import pytest

from swe.app.chat_sharing import router as sharing_router
from swe.app.chat_sharing.models import ChatShareRecord
from swe.app.chat_sharing.router import (
    ChatShareCreateRequest,
    create_chat_share,
    get_chat_share_options,
    get_chat_share,
    _turn_statuses,
)
from swe.app.runner.models import ChatMessage


class FakeService:
    def __init__(self):
        self.calls = []

    async def create_snapshot(self, **kwargs):
        if not kwargs["selected_turn_ids"]:
            raise ValueError("Select at least one answer turn")
        self.calls.append(kwargs)
        return ChatShareRecord(
            token="token-1",
            chat_id=kwargs["chat_id"],
            creator_id=kwargs["creator_id"],
            snapshot_key="token-1.json",
            created_at="2026-09-02T00:00:00Z",
        )

    async def get_snapshot(self, token):
        if token == "missing":
            raise KeyError("missing")
        return {"chat_name": "Demo", "messages": []}


@pytest.mark.asyncio
async def test_create_chat_share_requires_non_empty_selection(monkeypatch):
    service = FakeService()
    monkeypatch.setattr(sharing_router, "_service", service)

    async def build_history(*args, **kwargs):
        return SimpleNamespace(messages=[])

    async def read_state(*args, **kwargs):
        return {"turn_states": {}}

    monkeypatch.setattr(sharing_router, "_build_chat_history", build_history)
    monkeypatch.setattr(sharing_router, "_read_history_state", read_state)
    monkeypatch.setattr(sharing_router, "_authorize_chat", lambda *args: None)

    request = SimpleNamespace(
        state=SimpleNamespace(user_id="alice"),
        app=SimpleNamespace(),
    )

    async def get_chat(chat_id):
        return _chat(chat_id)

    workspace = SimpleNamespace(
        chat_manager=SimpleNamespace(get_chat=get_chat),
    )
    workspace.runner = SimpleNamespace(session=object())

    with pytest.raises(Exception) as exc_info:
        await create_chat_share(
            "chat-1",
            ChatShareCreateRequest(turn_ids=[]),
            request,
            workspace,
        )

    assert getattr(exc_info.value, "status_code", None) == 400


@pytest.mark.asyncio
async def test_public_share_returns_snapshot_and_hides_missing_token(
    monkeypatch,
):
    service = FakeService()
    monkeypatch.setattr(sharing_router, "_service", service)

    result = await get_chat_share("token-1")
    assert result.chat_name == "Demo"

    with pytest.raises(Exception) as exc_info:
        await get_chat_share("missing")
    assert getattr(exc_info.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_share_options_returns_authoritative_turn_statuses(monkeypatch):
    service = FakeService()
    monkeypatch.setattr(sharing_router, "_service", service)

    async def build_history(*args, **kwargs):
        return SimpleNamespace(
            messages=[
                _message("q1", "user", "first"),
                _message("a1", "assistant", "answer"),
                _message("q2", "user", "second"),
            ],
        )

    async def read_state(*args, **kwargs):
        return {
            "turn_states": {
                "q1": {"status": "completed"},
                "q2": {"status": "running"},
            },
        }

    monkeypatch.setattr(sharing_router, "_build_chat_history", build_history)
    monkeypatch.setattr(sharing_router, "_read_history_state", read_state)
    monkeypatch.setattr(sharing_router, "_authorize_chat", lambda *args: None)
    request = SimpleNamespace(
        state=SimpleNamespace(user_id="alice", tenant_id="tenant-a"),
    )

    async def get_chat(chat_id):
        return _chat(chat_id)

    workspace = SimpleNamespace(
        chat_manager=SimpleNamespace(get_chat=get_chat),
        runner=SimpleNamespace(session=object()),
    )

    result = await get_chat_share_options("chat-1", request, workspace)

    assert result.turn_statuses == {"q1": "completed", "q2": "running"}
    assert [item["id"] for item in result.messages] == ["q1", "a1", "q2"]


@pytest.mark.parametrize(
    "status",
    ["stopped", "cancelled", "error", "partial"],
)
def test_turn_status_fallback_does_not_promote_non_completed_output(status):
    messages = [
        _message("q1", "user", "first"),
        _message("a1", "assistant", "answer"),
    ]
    messages[1]["metadata"]["turn_status"] = status

    typed_messages = [
        ChatMessage.model_validate(message) for message in messages
    ]
    assert _turn_statuses(typed_messages, {"turn_states": {}}) == {
        "q1": status,
    }


def test_turn_status_fallback_ignores_transport_message_status():
    messages = [
        _message("q1", "user", "first"),
        _message("a1", "assistant", "answer"),
    ]

    typed_messages = [
        ChatMessage.model_validate(message) for message in messages
    ]

    assert typed_messages[1].status == "created"
    assert _turn_statuses(typed_messages, {"turn_states": {}}) == {
        "q1": "completed",
    }


@pytest.mark.parametrize("status", ["completed", "failed", "running"])
def test_message_turn_status_accepts_explicit_answer_status(status):
    message = SimpleNamespace(metadata={}, status=status)

    assert sharing_router._message_turn_status(message) == status


@pytest.mark.asyncio
async def test_create_chat_share_maps_storage_failure_to_503(monkeypatch):
    class FailingService(FakeService):
        async def create_snapshot(self, **kwargs):
            raise OSError("shared volume unavailable")

    monkeypatch.setattr(sharing_router, "_service", FailingService())

    async def build_history(*args, **kwargs):
        return _history([])

    async def read_state(*args, **kwargs):
        return _state({})

    monkeypatch.setattr(sharing_router, "_build_chat_history", build_history)
    monkeypatch.setattr(sharing_router, "_read_history_state", read_state)
    monkeypatch.setattr(sharing_router, "_authorize_chat", lambda *args: None)
    request = SimpleNamespace(state=SimpleNamespace(user_id="alice"))

    async def get_chat(chat_id):
        return _chat(chat_id)

    workspace = SimpleNamespace(
        chat_manager=SimpleNamespace(get_chat=get_chat),
        runner=SimpleNamespace(session=object()),
    )

    with pytest.raises(Exception) as exc_info:
        await create_chat_share(
            "chat-1",
            ChatShareCreateRequest(turn_ids=["q1"]),
            request,
            workspace,
        )

    assert getattr(exc_info.value, "status_code", None) == 503


@pytest.mark.asyncio
async def test_public_share_maps_access_audit_failure_to_503(monkeypatch):
    class FailingReadService(FakeService):
        async def get_snapshot(self, token):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(sharing_router, "_service", FailingReadService())

    with pytest.raises(Exception) as exc_info:
        await get_chat_share("token-1")

    assert getattr(exc_info.value, "status_code", None) == 503


@pytest.mark.asyncio
async def test_public_share_maps_malformed_snapshot_to_503(monkeypatch):
    class MalformedService(FakeService):
        async def get_snapshot(self, token):
            return {"chat_name": "Demo", "messages": "not-a-list"}

    monkeypatch.setattr(sharing_router, "_service", MalformedService())

    with pytest.raises(Exception) as exc_info:
        await get_chat_share("token-1")

    assert getattr(exc_info.value, "status_code", None) == 503


@pytest.mark.asyncio
async def test_create_chat_share_maps_history_failure_to_503(monkeypatch):
    monkeypatch.setattr(sharing_router, "_service", FakeService())
    monkeypatch.setattr(
        sharing_router,
        "_share_context",
        lambda *args, **kwargs: _raise_unavailable(),
    )
    monkeypatch.setattr(sharing_router, "_authorize_chat", lambda *args: None)
    request = SimpleNamespace(state=SimpleNamespace(user_id="alice"))

    async def get_chat(chat_id):
        return _chat(chat_id)

    workspace = SimpleNamespace(
        chat_manager=SimpleNamespace(get_chat=get_chat),
        runner=SimpleNamespace(session=object()),
    )

    with pytest.raises(Exception) as exc_info:
        await create_chat_share(
            "chat-1",
            ChatShareCreateRequest(turn_ids=["q1"]),
            request,
            workspace,
        )

    assert getattr(exc_info.value, "status_code", None) == 503


def _chat(chat_id: str):
    return SimpleNamespace(
        id=chat_id,
        name="Demo",
        session_id="session-1",
        user_id="alice",
        meta={},
    )


def _history(messages):
    return SimpleNamespace(messages=messages)


def _state(turn_states):
    return {"turn_states": turn_states}


def _message(message_id: str, role: str, text: str):
    return {
        "id": message_id,
        "role": role,
        "type": "message",
        "content": [{"type": "text", "text": text}],
        "metadata": {},
    }


async def _raise_unavailable():
    raise OSError("history unavailable")

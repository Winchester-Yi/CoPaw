# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.swe.app.routers import console as console_router
from swe.app.answer_turn.models import TurnIdentity, TurnLease


class _FakeConsoleChannel:
    def resolve_session_id(self, sender_id: str, channel_meta: dict) -> str:
        return channel_meta.get("session_id") or f"console:{sender_id}"

    async def stream_one(self, _payload):
        yield 'data: {"object":"response","status":"created","id":"response-1","created_at":1,"output":[]}\n\n'
        yield 'data: {"object":"response","status":"in_progress","id":"response-1","created_at":1,"output":[]}\n\n'
        yield 'data: {"object":"response","status":"completed","id":"response-1","created_at":1,"completed_at":2,"output":[]}\n\n'


class _FakeChannelManager:
    async def get_channel(self, name: str):
        assert name == "console"
        return _FakeConsoleChannel()


class _FakeChatManager:
    async def get_or_create_chat(
        self,
        session_id: str,
        user_id: str,
        channel_id: str,
        name: str,
        meta=None,
    ):
        del meta
        self.chat = SimpleNamespace(
            id=f"chat:{session_id}",
            session_id=session_id,
            user_id=user_id,
            channel=channel_id,
            name=name,
        )
        return self.chat

    async def get_chat(self, chat_id: str):
        return self.chat if self.chat.id == chat_id else None

    async def release_current_expert_binding(self, _chat_id: str):
        return None


class _FakeTaskTracker:
    def __init__(self) -> None:
        self.payload = None

    async def attach_or_start(self, _identity, payload, _stream_fn, **_kwargs):
        self.payload = payload
        return payload, True

    async def attach(self, _identity):
        return None

    async def stream(self, _identity, queue):
        await asyncio.sleep(0)
        async for event in _FakeConsoleChannel().stream_one(queue):
            yield event


class _FakeCoordinator:
    def __init__(self, tracker: _FakeTaskTracker) -> None:
        self.tracker = tracker

    async def current_identity(self, _chat_id):
        return None

    async def status(self, _chat_id):
        return None

    async def start_or_attach(self, chat_id, payload, producer, **kwargs):
        identity = TurnIdentity(
            chat_id=chat_id,
            msgid=kwargs.get("msgid") or "msg-1",
            turn_id="turn-1",
        )
        queue, is_new = await self.tracker.attach_or_start(
            identity,
            payload,
            producer,
        )
        return TurnLease(identity, queue, is_new)


def test_console_chat_allows_terminal_response_frame_without_output(
    monkeypatch,
) -> None:
    app = FastAPI()
    app.include_router(console_router.router)

    workspace = SimpleNamespace(
        channel_manager=_FakeChannelManager(),
        chat_manager=_FakeChatManager(),
        task_tracker=_FakeTaskTracker(),
    )
    workspace.answer_turn_coordinator = _FakeCoordinator(
        workspace.task_tracker,
    )

    async def _fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        _fake_get_agent_for_request,
    )

    client = TestClient(app)
    payload = {
        "input": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ],
        "session_id": "session-1",
        "user_id": "user-1",
        "channel": "console",
    }

    with client.stream(
        "POST",
        "/console/chat",
        headers={"X-Source-Id": "src-a"},
        json=payload,
    ) as response:
        assert response.status_code == 200
        events = [
            line.removeprefix("data: ")
            for line in response.iter_lines()
            if line.startswith("data: ")
        ]

    parsed = [json.loads(event) for event in events]
    assert [event["status"] for event in parsed] == [
        "created",
        "in_progress",
        "completed",
    ]
    assert parsed[-1]["output"] == []


def test_console_chat_returns_user_question_msgid_header(
    monkeypatch,
) -> None:
    app = FastAPI()
    app.include_router(console_router.router)

    task_tracker = _FakeTaskTracker()
    workspace = SimpleNamespace(
        channel_manager=_FakeChannelManager(),
        chat_manager=_FakeChatManager(),
        task_tracker=task_tracker,
    )
    workspace.answer_turn_coordinator = _FakeCoordinator(task_tracker)

    async def _fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        _fake_get_agent_for_request,
    )

    client = TestClient(app)
    payload = {
        "input": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ],
        "session_id": "session-1",
        "user_id": "user-1",
        "channel": "console",
    }

    with client.stream(
        "POST",
        "/console/chat",
        headers={"X-Source-Id": "src-a"},
        json=payload,
    ) as response:
        assert response.status_code == 200
        msgid = response.headers.get("X-Swe-Msgid")
        assert msgid
        assert response.headers["X-Swe-Sessionid"] == "session-1"
        list(response.iter_lines())

    assert task_tracker.payload is not None
    assert task_tracker.payload["meta"]["msgid"] == msgid

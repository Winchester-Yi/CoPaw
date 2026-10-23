# -*- coding: utf-8 -*-
"""Negotiated SSE cursor handling at the Console transport boundary."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from swe.app.answer_turn.models import TurnIdentity
from swe.app.routers import console
from swe.app.runner.task_tracker import TaskTracker


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cursor,valid",
    [("turn-a:1", True), ("turn-other:1", False), ("turn-a:9", False)],
)
async def test_console_replay_validates_cursor_and_emits_ids(
    monkeypatch,
    cursor,
    valid,
):
    tracker = TaskTracker()
    identity = TurnIdentity("chat-a", "message-a", "turn-a")
    release = asyncio.Event()

    async def producer(*_args):
        yield "data: first\n\n"
        yield "data: second\n\n"
        await release.wait()

    original, _ = await tracker.attach_or_start(identity, {}, producer)
    await original.get()
    await original.get()
    replay = await tracker.attach(identity)

    async def resolve(**_kwargs):
        return replay, "chat-a", identity, "message-a"

    monkeypatch.setattr(console, "_resolve_current_reconnect_target", resolve)
    try:
        call = console._dispatch_console_stream(
            workspace=SimpleNamespace(task_tracker=tracker),
            console_channel=None,
            native_payload={},
            session_id="session-a",
            identity={},
            request_mapping={
                "stream_protocol": "cursor-v1",
                "after_event_id": cursor,
            },
            is_reconnect=True,
            is_current_reconnect=True,
        )
        if not valid:
            with pytest.raises(HTTPException) as caught:
                await call
            assert caught.value.status_code == 409
            assert replay not in tracker._runs[identity.chat_id].queues
        else:
            response = await call
            release.set()
            text = "".join([chunk async for chunk in response.body_iterator])
            assert response.headers["x-swe-stream-protocol"] == "cursor-v1"
            assert "id: turn-a:2\n" in text
            assert "data: first" not in text
            assert "data: second" in text
            assert "event: stream.end" in text
    finally:
        release.set()
        await tracker.wait_all_done(timeout=2)
        await original.stream.aclose()


@pytest.mark.asyncio
async def test_negotiated_response_runs_through_asgi_with_ordered_frames(
    monkeypatch,
):
    tracker = TaskTracker()
    identity = TurnIdentity("chat-asgi", "message-a", "turn-asgi")
    release = asyncio.Event()

    async def producer(*_args):
        yield "data: first\n\n"
        await release.wait()
        yield "data: second\n\n"

    original, _ = await tracker.attach_or_start(identity, {}, producer)

    async def start(**_kwargs):
        return original, identity.chat_id, identity, identity.msgid

    monkeypatch.setattr(console, "_start_console_stream_target", start)
    response = await console._dispatch_console_stream(
        workspace=SimpleNamespace(task_tracker=tracker),
        console_channel=None,
        native_payload={},
        session_id="session-asgi",
        identity={},
        request_mapping={"stream_protocol": "cursor-v1"},
        is_reconnect=False,
    )
    messages = []

    async def send(message):
        messages.append(message)
        if message.get("body", b"").find(b"data: first") >= 0:
            release.set()

    async def receive():
        await asyncio.Event().wait()

    try:
        await asyncio.wait_for(
            response(
                {"type": "http", "asgi": {"spec_version": "2.4"}},
                receive,
                send,
            ),
            3,
        )
        body = b"".join(message.get("body", b"") for message in messages)
        assert body.index(b"id: turn-asgi:1") < body.index(b"id: turn-asgi:2")
        assert b"event: stream.end" in body
        assert messages[-1] == {
            "type": "http.response.body",
            "body": b"",
            "more_body": False,
        }
        assert await tracker.has_active_tasks() is False
    finally:
        release.set()
        await tracker.wait_all_done(timeout=2)
        await original.stream.aclose()


@pytest.mark.asyncio
async def test_stalled_initial_keepalive_detaches_but_does_not_stop_producer(
    monkeypatch,
):
    tracker = TaskTracker()
    identity = TurnIdentity("chat-stalled", "message-a", "turn-stalled")
    release = asyncio.Event()

    async def producer(*_args):
        await release.wait()
        yield "data: completed\n\n"

    subscriber, _ = await tracker.attach_or_start(identity, {}, producer)

    async def start(**_kwargs):
        return subscriber, identity.chat_id, identity, identity.msgid

    monkeypatch.setattr(console, "_start_console_stream_target", start)
    response = await console._dispatch_console_stream(
        workspace=SimpleNamespace(task_tracker=tracker),
        console_channel=None,
        native_payload={},
        session_id="session-a",
        identity={},
        request_mapping={"stream_protocol": "cursor-v1"},
        is_reconnect=False,
    )
    response.send_timeout = 0.01

    async def blocked_send(message):
        if message["type"] == "http.response.body":
            await asyncio.Event().wait()

    try:
        await asyncio.wait_for(response.stream_response(blocked_send), 1)
        assert await tracker.has_active_tasks() is True
        assert tracker._runs[identity.chat_id].queues == []
        assert subscriber.detached is True
    finally:
        release.set()
        await tracker.wait_all_done(timeout=2)
        await subscriber.stream.aclose()


@pytest.mark.asyncio
async def test_stalled_response_start_detaches_unstarted_subscription(
    monkeypatch,
):
    tracker = TaskTracker()
    identity = TurnIdentity(
        "chat-header-stalled",
        "message-a",
        "turn-header-stalled",
    )
    release = asyncio.Event()

    async def producer(*_args):
        await release.wait()
        yield "data: completed\n\n"

    subscriber, _ = await tracker.attach_or_start(identity, {}, producer)

    async def start(**_kwargs):
        return subscriber, identity.chat_id, identity, identity.msgid

    monkeypatch.setattr(console, "_start_console_stream_target", start)
    response = await console._dispatch_console_stream(
        workspace=SimpleNamespace(task_tracker=tracker),
        console_channel=None,
        native_payload={},
        session_id="session-a",
        identity={},
        request_mapping={"stream_protocol": "cursor-v1"},
        is_reconnect=False,
    )
    response.send_timeout = 0.01

    async def blocked_send(_message):
        await asyncio.Event().wait()

    try:
        await asyncio.wait_for(response.stream_response(blocked_send), 1)
        assert subscriber.detached is True
        assert tracker._runs[identity.chat_id].queues == []
    finally:
        release.set()
        await tracker.wait_all_done(timeout=2)
        await subscriber.stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect_after_event", [False, True])
@pytest.mark.parametrize("busy_tracker", [False, True])
@pytest.mark.parametrize("blocked_body_send", [False, True])
async def test_asgi_disconnect_releases_subscription_without_stopping_producer(
    monkeypatch,
    disconnect_after_event,
    busy_tracker,
    blocked_body_send,
):
    tracker = TaskTracker()
    identity = TurnIdentity("chat-disconnect", "message-a", "turn-disconnect")
    release = asyncio.Event()
    disconnect = asyncio.Event()

    async def producer(*_args):
        yield "data: first\n\n"
        await release.wait()

    subscriber, _ = await tracker.attach_or_start(identity, {}, producer)

    async def start(**_kwargs):
        return subscriber, identity.chat_id, identity, identity.msgid

    monkeypatch.setattr(console, "_start_console_stream_target", start)
    response = await console._dispatch_console_stream(
        workspace=SimpleNamespace(task_tracker=tracker),
        console_channel=None,
        native_payload={},
        session_id="session-a",
        identity={},
        request_mapping={"stream_protocol": "cursor-v1"},
        is_reconnect=False,
    )

    async def send(message):
        body = message.get("body", b"")
        if body and (not disconnect_after_event or b"data: first" in body):
            if busy_tracker:
                await tracker.lock.acquire()
                asyncio.get_running_loop().call_later(
                    0.01,
                    tracker.lock.release,
                )
            disconnect.set()
            if blocked_body_send:
                await asyncio.Event().wait()

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    try:
        await asyncio.wait_for(
            response(
                {"type": "http", "asgi": {"spec_version": "2.0"}},
                receive,
                send,
            ),
            2,
        )
        assert await tracker.has_active_tasks() is True
        assert tracker._runs[identity.chat_id].queues == []
        assert subscriber.detached is True
    finally:
        release.set()
        await tracker.wait_all_done(timeout=2)
        await subscriber.stream.aclose()

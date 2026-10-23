# -*- coding: utf-8 -*-
"""Transport contract for answer-turn stream delivery."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from swe.app.answer_turn.models import TurnIdentity
from swe.app.runner.task_tracker import TaskTracker
from swe.app.runner.tool_output_frames import (
    emit_tool_output_text,
    tool_output_invocation,
)


def _identity(
    chat_id: str = "chat-1",
    turn_id: str = "turn-1",
) -> TurnIdentity:
    return TurnIdentity(chat_id=chat_id, msgid="message-1", turn_id=turn_id)


@pytest.mark.asyncio
async def test_tail_drain_keeps_transport_discoverable(monkeypatch):
    from swe.app.runner.sse_replay import ReplayLog

    entered, release = threading.Event(), threading.Event()
    original = ReplayLog._append_many

    def delayed(log, texts):
        entered.set()
        assert release.wait(5)
        return original(log, texts)

    monkeypatch.setattr(ReplayLog, "_append_many", delayed)
    tracker = TaskTracker()
    identity = _identity("tail-drain")

    async def producer(*_args):
        yield "data: tail\n\n"

    subscriber, _ = await tracker.attach_or_start(identity, {}, producer)
    try:
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        assert identity.chat_id in tracker._runs
        release.set()
        await tracker.wait_all_done(timeout=2)
        assert await subscriber.get() == "data: tail\n\n"
        assert await subscriber.get() is None
    finally:
        release.set()
        await tracker.wait_all_done(timeout=2)
        await subscriber.stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setting,batching",
    [(None, True), ("0", False), ("1", True)],
)
async def test_tracker_batching_defaults_on_and_honors_override(
    monkeypatch,
    setting,
    batching,
):
    if setting is None:
        monkeypatch.delenv("SWE_SSE_BATCH_WRITES", raising=False)
    else:
        monkeypatch.setenv("SWE_SSE_BATCH_WRITES", setting)
    tracker = TaskTracker()
    identity = _identity("burst")

    async def producer(*_args):
        for index in range(100):
            yield f"data: {index}\n\n"

    subscriber, _ = await tracker.attach_or_start(identity, {}, producer)
    budget = subscriber.stream.log.budget
    before = budget.snapshot()["write_batches"]
    try:
        await tracker.wait_all_done(timeout=2)
        batches = budget.snapshot()["write_batches"] - before
        assert batches <= 5 if batching else batches == 100
        assert [await subscriber.get() for _ in range(100)] == [
            f"data: {i}\n\n" for i in range(100)
        ]
        assert await subscriber.get() is None
    finally:
        await subscriber.stream.aclose()


@pytest.mark.asyncio
async def test_repeated_producer_cancel_still_unregisters_after_tail_drain(
    monkeypatch,
):
    from swe.app.runner.sse_replay import ReplayLog

    monkeypatch.setenv("SWE_SSE_BATCH_WRITES", "1")
    started, release = threading.Event(), threading.Event()
    original = ReplayLog._append_many

    def delayed(log, texts):
        started.set()
        assert release.wait(5)
        return original(log, texts)

    monkeypatch.setattr(ReplayLog, "_append_many", delayed)
    tracker = TaskTracker()
    identity = _identity("cancel-tail")
    produced = asyncio.Event()

    async def producer(*_args):
        yield "data: tail\n\n"
        produced.set()
        await asyncio.Event().wait()

    subscriber, _ = await tracker.attach_or_start(identity, {}, producer)
    task = tracker._runs[identity.chat_id].task
    try:
        await produced.wait()
        task.cancel()
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.001)
        task.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert identity.chat_id not in tracker._runs
        assert await subscriber.get() == "data: tail\n\n"
    finally:
        release.set()
        await subscriber.stream.aclose()


@pytest.mark.asyncio
async def test_attach_uses_shared_replay_without_prefilling_subscriber():
    tracker = TaskTracker()
    identity = _identity("bounded-replay")
    release = asyncio.Event()

    async def producer(*_args):
        for index in range(300):
            yield f"data: {index}\n\n"
        await release.wait()

    original, _ = await tracker.attach_or_start(identity, {}, producer)
    try:
        for _ in range(300):
            await asyncio.wait_for(original.get(), 2)
        replay = await tracker.attach(identity)
        assert replay.buffered_bytes == 0
        assert replay.stream is original.stream
        assert replay.stream.log.count == 300
        assert await replay.get() == "data: 0\n\n"
    finally:
        release.set()
        await tracker.wait_all_done(timeout=2)


@pytest.mark.asyncio
async def test_attach_or_start_binds_identity_and_replays_one_producer_stream():
    tracker = TaskTracker()
    release = asyncio.Event()
    received: list[tuple[TurnIdentity, object]] = []
    identity = _identity()

    async def producer(bound_identity, payload):
        received.append((bound_identity, payload))
        yield 'data: {"first": true}\n\n'
        await release.wait()

    first, is_new = await tracker.attach_or_start(
        identity,
        {"query": "hi"},
        producer,
    )
    assert is_new is True
    assert (
        await asyncio.wait_for(first.get(), timeout=1)
        == 'data: {"first": true}\n\n'
    )

    second, is_new = await tracker.attach_or_start(
        identity,
        {"ignored": True},
        producer,
    )
    assert is_new is False
    assert (
        await asyncio.wait_for(second.get(), timeout=1)
        == 'data: {"first": true}\n\n'
    )
    assert received == [(identity, {"query": "hi"})]

    release.set()
    await asyncio.wait_for(tracker.wait_all_done(timeout=1), timeout=2)


@pytest.mark.asyncio
async def test_attach_replays_buffer_and_stream_detaches_on_consumer_close():
    tracker = TaskTracker()
    release = asyncio.Event()
    identity = _identity()

    async def producer(_identity, _payload):
        yield 'data: {"first": true}\n\n'
        await release.wait()

    queue, _ = await tracker.attach_or_start(identity, {}, producer)
    assert await asyncio.wait_for(queue.get(), timeout=1)
    replay = await tracker.attach(identity)
    assert replay is not None
    stream = tracker.stream(identity, replay)
    assert await anext(stream) == 'data: {"first": true}\n\n'
    await stream.aclose()
    assert replay not in tracker._runs[identity.chat_id].queues

    release.set()
    await asyncio.wait_for(tracker.wait_all_done(timeout=1), timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("attach_or_start", [False, True])
async def test_large_replay_yields_to_event_loop_without_live_interleaving(
    attach_or_start,
):
    tracker = TaskTracker()
    identity = _identity("chat-replay-batch")
    release = asyncio.Event()
    replay_count = 300

    async def producer(_identity, _payload):
        for index in range(replay_count):
            yield f'data: {{"index": {index}}}\n\n'
        await release.wait()
        yield 'data: {"live": true}\n\n'

    original, _ = await tracker.attach_or_start(identity, {}, producer)
    for _ in range(replay_count):
        await original.get()

    heartbeat = 0
    heartbeat_done = asyncio.Event()

    async def tick():
        nonlocal heartbeat
        for _ in range(3):
            heartbeat += not attach_task.done()
            await asyncio.sleep(0)
        heartbeat_done.set()

    attach_task = asyncio.create_task(
        (
            tracker.attach_or_start(identity, {}, producer)
            if attach_or_start
            else tracker.attach(identity)
        ),
    )
    tick_task = asyncio.create_task(tick())
    release.set()
    replay = await attach_task
    if attach_or_start:
        replay, is_new = replay
        assert is_new is False
    heartbeat_during_replay = heartbeat
    await heartbeat_done.wait()
    await tick_task
    assert heartbeat_during_replay > 0
    assert replay is not None
    events = [await replay.get() for _ in range(replay_count)]
    assert events == [
        f'data: {{"index": {index}}}\n\n' for index in range(replay_count)
    ]
    assert await replay.get() == 'data: {"live": true}\n\n'
    assert await replay.get() is None
    assert await original.get() == 'data: {"live": true}\n\n'

    release.set()
    await asyncio.wait_for(tracker.wait_all_done(timeout=1), timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("attach_or_start", [False, True])
async def test_cancelled_replay_does_not_leave_subscriber_registered(
    attach_or_start,
):
    tracker = TaskTracker()
    identity = _identity("chat-replay-cancel")
    replay_count = 300
    release = asyncio.Event()

    async def producer(_identity, _payload):
        for index in range(replay_count):
            yield f'data: {{"index": {index}}}\n\n'
        await release.wait()

    original, _ = await tracker.attach_or_start(identity, {}, producer)
    for _ in range(replay_count):
        await original.get()

    attach_task = asyncio.create_task(
        (
            tracker.attach_or_start(identity, {}, producer)
            if attach_or_start
            else tracker.attach(identity)
        ),
    )
    await asyncio.sleep(0)
    attach_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attach_task
    assert tracker._runs[identity.chat_id].queues == [original]

    release.set()
    await asyncio.wait_for(tracker.wait_all_done(timeout=1), timeout=2)


@pytest.mark.asyncio
async def test_close_during_replay_delivers_history_before_end_marker():
    tracker = TaskTracker()
    identity = _identity("chat-replay-close")
    release = asyncio.Event()
    history = [f"data: {index}\n\n" for index in range(300)]

    async def producer(_identity, _payload):
        for event in history:
            yield event
        await release.wait()

    original, _ = await tracker.attach_or_start(identity, {}, producer)
    for _ in history:
        await original.get()

    attach_task = asyncio.create_task(tracker.attach(identity))
    await asyncio.sleep(0)
    close_task = asyncio.create_task(tracker.close(identity))
    replay = await attach_task
    await asyncio.wait_for(close_task, timeout=1)
    assert replay is not None
    assert [
        event async for event in tracker.stream(identity, replay)
    ] == history
    assert tracker._runs[identity.chat_id].queues == []

    release.set()
    await asyncio.wait_for(tracker.wait_all_done(timeout=1), timeout=2)


@pytest.mark.asyncio
async def test_producer_exception_is_delivered_then_ends_stream():
    tracker = TaskTracker()
    identity = _identity()

    async def producer(_identity, _payload):
        if _payload is not None:
            raise RuntimeError("boom")
        yield "data: {}\n\n"

    queue, _ = await tracker.attach_or_start(identity, {}, producer)
    events = [event async for event in tracker.stream(identity, queue)]
    assert events == ['data: {"error": "internal server error"}\n\n']
    assert await tracker.has_active_tasks() is False


@pytest.mark.asyncio
async def test_prefilled_stream_yields_during_history_delivery():
    tracker = TaskTracker()
    identity = _identity("chat-stream-batch")
    queue = asyncio.Queue()
    history = [f"data: {index}\n\n" for index in range(300)]
    for event in (*history, None):
        queue.put_nowait(event)
    received = []

    async def consume():
        async for event in tracker.stream(identity, queue):
            received.append(event)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0)
    delivered_before_other_task = len(received)
    await consumer
    assert 0 < delivered_before_other_task < len(history)
    assert received == history


@pytest.mark.asyncio
async def test_cancelled_prefilled_stream_detaches_subscriber():
    tracker = TaskTracker()
    identity = _identity("chat-stream-cancel")
    release = asyncio.Event()

    async def producer(_identity, _payload):
        for index in range(300):
            yield f"data: {index}\n\n"
        await release.wait()

    original, _ = await tracker.attach_or_start(identity, {}, producer)
    for _ in range(300):
        await original.get()
    replay = await tracker.attach(identity)
    assert replay is not None
    received = []
    first_delivered = asyncio.Event()

    async def consume():
        async for event in tracker.stream(identity, replay):
            received.append(event)
            first_delivered.set()

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(first_delivered.wait(), 2)
    consumer.cancel()
    await consumer
    assert 0 < len(received) < 300
    assert replay not in tracker._runs[identity.chat_id].queues

    release.set()
    await asyncio.wait_for(tracker.wait_all_done(timeout=1), timeout=2)


@pytest.mark.asyncio
async def test_close_unblocks_subscribers_without_cancelling_producer():
    tracker = TaskTracker()
    release = asyncio.Event()
    identity = _identity()

    async def producer(_identity, _payload):
        yield 'data: {"first": true}\n\n'
        await release.wait()

    queue, _ = await tracker.attach_or_start(identity, {}, producer)
    assert await asyncio.wait_for(queue.get(), timeout=1)
    await tracker.close(identity)
    assert [event async for event in tracker.stream(identity, queue)] == []
    assert await tracker.has_active_tasks() is True
    with pytest.raises(
        RuntimeError,
        match="live stream belongs to another turn",
    ):
        await tracker.attach_or_start(
            _identity(turn_id="turn-2"),
            {},
            producer,
        )

    release.set()
    await asyncio.wait_for(tracker.wait_all_done(timeout=1), timeout=2)


@pytest.mark.asyncio
async def test_before_start_is_atomic_with_call_if_idle_for_one_chat():
    tracker = TaskTracker()
    identity = _identity("chat-serialized")
    callback_started = threading.Event()
    release_callback = threading.Event()
    durable_state = {"active": True}

    def recover():
        durable_state["active"] = False
        callback_started.set()
        assert release_callback.wait(timeout=2)
        return "recovered"

    recovery = asyncio.create_task(tracker.call_if_idle(identity, recover))
    assert await asyncio.to_thread(callback_started.wait, 1)

    async def producer(_identity, _payload):
        yield 'data: {"done": true}\n\n'

    def validate_claim():
        if not durable_state["active"]:
            raise RuntimeError("claim is no longer active")

    start = asyncio.create_task(
        tracker.attach_or_start(
            identity,
            {},
            producer,
            before_start=validate_claim,
        ),
    )
    await asyncio.sleep(0)
    assert start.done() is False
    release_callback.set()
    assert await recovery == (True, "recovered")
    with pytest.raises(RuntimeError, match="claim is no longer active"):
        await start


@pytest.mark.asyncio
async def test_active_task_helpers_and_progress_are_transport_support():
    tracker = TaskTracker()
    release = asyncio.Event()
    identity = _identity("chat-active")

    async def producer(_identity, _payload):
        yield 'data: {"started": true}\n\n'
        await release.wait()

    await tracker.attach_or_start(identity, {}, producer)
    await asyncio.sleep(0)
    assert await tracker.has_active_tasks() is True
    assert await tracker.list_active_tasks() == [identity.chat_id]

    payload = {"status": "running", "steps": [{"title": "work"}]}
    stored = await tracker.update_task_progress(identity.chat_id, payload)
    stored["steps"][0]["title"] = "changed"
    assert (await tracker.get_task_progress(identity.chat_id))["steps"][0][
        "title"
    ] == "work"

    release.set()
    assert await tracker.wait_all_done(timeout=1) is True
    assert await tracker.get_task_progress(identity.chat_id) is None


@pytest.mark.asyncio
async def test_tool_output_frames_are_broadcast_and_buffered_for_reconnect():
    tracker = TaskTracker()
    release = asyncio.Event()
    identity = _identity()

    async def producer(_identity, _payload):
        with tool_output_invocation(
            tool_call_id="call-1",
            tool_name="execute_shell_command",
        ):
            await emit_tool_output_text("stdout", "live output\n")
        await release.wait()
        yield "data: {}\n\n"

    queue, _ = await tracker.attach_or_start(identity, {}, producer)
    live_sse = await asyncio.wait_for(queue.get(), timeout=1)
    assert (
        json.loads(live_sse.removeprefix("data: ").strip())["text"]
        == "live output\n"
    )
    replay = await tracker.attach(identity)
    assert replay is not None
    replay_sse = await asyncio.wait_for(replay.get(), timeout=1)
    assert (
        json.loads(replay_sse.removeprefix("data: ").strip())["text"]
        == "live output\n"
    )

    release.set()
    await asyncio.wait_for(tracker.wait_all_done(timeout=1), timeout=2)


def test_tracker_has_no_answer_turn_stop_or_status_api():
    tracker = TaskTracker()
    for name in (
        "claim_stop",
        "request_stop",
        "mark_stopping",
        "get_status",
        "get_run_identity",
        "is_turn_stopping",
    ):
        assert not hasattr(tracker, name)


@pytest.mark.asyncio
async def test_different_identity_same_chat_cannot_start_second_producer():
    tracker = TaskTracker()
    release = asyncio.Event()

    async def producer(_identity, _payload):
        await release.wait()
        yield "data: {}\n\n"

    first = _identity(turn_id="turn-1")
    second = _identity(turn_id="turn-2")
    _, is_new = await tracker.attach_or_start(first, {}, producer)
    with pytest.raises(
        RuntimeError,
        match="live stream belongs to another turn",
    ):
        await tracker.attach_or_start(second, {}, producer)
    assert is_new is True
    release.set()
    await tracker.wait_all_done(timeout=1)

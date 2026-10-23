# -*- coding: utf-8 -*-
"""Bounded shared SSE replay storage and cursor subscriptions."""

import asyncio
import gc
import threading
import weakref

import pytest

from swe.app.runner.sse_replay import (
    ReplayBudget,
    ReplayLog,
    ReplayStream,
    ReplayUnavailable,
)


@pytest.mark.asyncio
async def test_log_replays_in_order_in_byte_bounded_batches():
    budget = ReplayBudget(max_bytes=4096)
    log = ReplayLog(budget=budget, max_bytes=4096)
    try:
        for index in range(20):
            assert await log.append(f"data: {index}\n\n") == index + 1
        batch = await log.read(1, max_bytes=40)
        assert [seq for seq, _ in batch] == list(range(1, len(batch) + 1))
        assert sum(len(text.encode()) for _, text in batch) <= 40
        assert await log.read(20) == [(20, "data: 19\n\n")]
        assert budget.used_bytes > 0
    finally:
        await log.aclose()
    assert budget.used_bytes == 0


@pytest.mark.asyncio
async def test_log_quota_failure_does_not_publish_or_leak_budget():
    budget = ReplayBudget(max_bytes=80)
    first = ReplayLog(budget=budget, max_bytes=80)
    second = ReplayLog(budget=budget, max_bytes=80)
    try:
        await first.append("a" * 40)
        used = budget.used_bytes
        with pytest.raises(ReplayUnavailable):
            await second.append("b" * 40)
        assert second.count == 0
        assert budget.used_bytes == used
        assert await first.read(1) == [(1, "a" * 40)]
    finally:
        await first.aclose()
        await second.aclose()
    assert budget.used_bytes == 0


@pytest.mark.asyncio
async def test_slow_subscription_does_not_retain_events_or_block_fast_one():
    stream = ReplayStream()
    slow = stream.subscribe()
    fast = stream.subscribe()
    try:
        for index in range(150):
            event = f"data: {index}\n\n"
            await stream.append(event)
            assert await fast.get() == event
        assert slow.buffered_bytes == 0
        assert slow.last_sequence == 0
        stream.finish()
        assert [await slow.get() for _ in range(150)] == [
            f"data: {i}\n\n" for i in range(150)
        ]
        assert await slow.get() is None
        assert await fast.get() is None
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_subscription_cursor_and_control_do_not_need_queue_capacity():
    stream = ReplayStream()
    try:
        for index in range(3):
            await stream.append(f"data: {index}\n\n")
        subscriber = stream.subscribe()
        subscriber.seek(2)
        assert await subscriber.get() == "data: 2\n\n"
        with pytest.raises(ReplayUnavailable):
            subscriber.seek(4)
        subscriber.interrupt("slow_consumer", retryable=True)
        assert "stream.recover" in await subscriber.get()
        assert await subscriber.get() is None
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_close_wakes_waiting_subscriber():
    stream = ReplayStream()
    subscriber = stream.subscribe()
    waiting = asyncio.create_task(subscriber.get())
    await asyncio.sleep(0)
    await stream.aclose()
    assert await asyncio.wait_for(waiting, 1) is None


@pytest.mark.asyncio
async def test_resume_rejects_other_turn_and_reads_only_after_cursor():
    stream = ReplayStream()
    subscriber = stream.subscribe()
    try:
        await stream.append("data: first\n\n")
        await stream.append("data: second\n\n")
        subscriber.resume("turn-a", "turn-a:1")
        assert await subscriber.get() == "data: second\n\n"
        with pytest.raises(ReplayUnavailable):
            subscriber.resume("turn-a", "turn-b:1")
        with pytest.raises(ReplayUnavailable):
            subscriber.resume("turn-a", "turn-a:3")
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_only_negotiated_slow_subscriber_is_interrupted(monkeypatch):
    from swe.app.runner import sse_replay

    stream = ReplayStream()
    slow = stream.subscribe()
    legacy = stream.subscribe()
    slow.resumable = True
    monkeypatch.setattr(sse_replay, "SLOW_SUBSCRIBER_SECONDS", 0)
    monkeypatch.setattr(sse_replay, "SLOW_SUBSCRIBER_BYTES", 1)
    try:
        await stream.append("data: enough lag\n\n")
        assert "stream.recover" in await slow.get()
        assert await legacy.get() == "data: enough lag\n\n"
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_stream_worker_and_budget_are_observable(monkeypatch):
    from swe import performance_metrics
    from swe.app.runner.sse_replay import run_stream_io

    metrics = performance_metrics.PerformanceMetrics()
    monkeypatch.setattr(performance_metrics, "METRICS", metrics)
    assert await run_stream_io(lambda: "ok") == "ok"
    assert metrics.snapshot()["workers"]["stream"]["completed_count"] == 1
    budget = ReplayBudget(max_bytes=100)
    assert budget.snapshot()["reserved_bytes"] == 0
    budget.reserve(50)
    assert budget.snapshot()["reserved_bytes"] == 50
    with pytest.raises(ReplayUnavailable):
        budget.reserve(60)
    assert budget.snapshot()["quota_rejections"] == 1
    budget.release(50)


@pytest.mark.asyncio
async def test_process_subscriber_memory_is_reserved_until_delivery_advances():
    budget = ReplayBudget()
    stream = ReplayStream(ReplayLog(budget=budget))
    subscriber = stream.subscribe()
    try:
        await stream.append("data: payload\n\n")
        assert await subscriber.get() == "data: payload\n\n"
        assert budget.snapshot()["subscriber_memory_bytes"] > 0
        stream.finish()
        assert await subscriber.get() is None
        assert budget.snapshot()["subscriber_memory_bytes"] == 0
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_cancelled_publish_waits_for_worker_then_commits(monkeypatch):
    stream = ReplayStream()
    subscriber = stream.subscribe()
    started, release = threading.Event(), threading.Event()
    original = stream.log._append_many

    def delayed(text):
        started.set()
        assert release.wait(5)
        return original(text)

    monkeypatch.setattr(stream.log, "_append_many", delayed)
    task = asyncio.create_task(stream.append("data: retained\n\n"))
    try:
        async with asyncio.timeout(5):
            while not started.is_set():
                await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    try:
        assert stream.committed == 1
        assert await subscriber.get() == "data: retained\n\n"
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_short_write_rolls_back_quota_and_does_not_publish(monkeypatch):
    import tempfile

    budget = ReplayBudget()
    log = ReplayLog(budget=budget)
    file = tempfile.TemporaryFile(mode="w+b", buffering=0)

    class ShortFile:
        def write(self, data):
            return file.write(data[:-1])

        def __getattr__(self, name):
            return getattr(file, name)

    log.file = ShortFile()
    try:
        with pytest.raises(OSError, match="Incomplete"):
            await log.append("data: incomplete\n\n")
        assert log.count == 0
        assert file.seek(0, 2) == 0
        assert budget.used_bytes == 0
    finally:
        await log.aclose()


@pytest.mark.asyncio
async def test_expired_replay_cannot_look_like_a_successful_end():
    stream = ReplayStream()
    subscriber = stream.subscribe()
    await stream.append("data: unread\n\n")
    stream.finish()
    await stream.aclose()
    control = await subscriber.get()
    assert control is not None and "stream.unavailable" in control
    assert await subscriber.get() is None


@pytest.mark.asyncio
async def test_replay_reads_contiguous_records_with_one_file_read():
    log = ReplayLog()
    try:
        for text in ("a", "😀" * 30, "tail"):
            await log.append(text)
        original = log.file
        calls = []

        class CountingFile:
            def read(self, size):
                calls.append(("read", size))
                return original.read(size)

            def seek(self, *args):
                calls.append(("seek", args))
                return original.seek(*args)

            def __getattr__(self, name):
                return getattr(original, name)

        log.file = CountingFile()
        assert await log.read(1) == [(1, "a"), (2, "😀" * 30), (3, "tail")]
        assert [call[0] for call in calls] == ["seek", "read"]
        assert await log.read(2, max_bytes=10) == [(2, "😀" * 30)]
    finally:
        await log.aclose()


@pytest.mark.asyncio
async def test_append_many_is_atomic_on_short_write():
    log = ReplayLog()
    try:
        await log.append("prefix")
        original = log.file
        before = log.budget.used_bytes

        class ShortFile:
            def write(self, data):
                return original.write(data[:-1])

            def __getattr__(self, name):
                return getattr(original, name)

        log.file = ShortFile()
        with pytest.raises(OSError, match="Incomplete"):
            await log.append_many(["second", "third"])
        assert log.count == 1
        assert log.budget.used_bytes == before
        assert await log.read(1) == [(1, "prefix")]
        log.file = original
        assert await log.append_many(["second", "third"]) == 3
        assert await log.read(2) == [(2, "second"), (3, "third")]
    finally:
        await log.aclose()


@pytest.mark.asyncio
async def test_offer_batches_and_end_commits_the_tail(monkeypatch):
    budget = ReplayBudget()
    stream = ReplayStream(ReplayLog(budget=budget))
    subscriber = stream.subscribe()
    batches = []
    original = stream.log._append_many

    def append_many(texts):
        batches.append(len(texts))
        return original(texts)

    monkeypatch.setattr(stream.log, "_append_many", append_many)
    try:
        for index in range(100):
            assert await stream.offer(f"data: {index}\n\n") == index + 1
        await stream.end()
        assert stream.committed == 100
        assert max(batches) <= 32
        assert len(batches) <= 5
        assert [await subscriber.get() for _ in range(100)] == [
            f"data: {i}\n\n" for i in range(100)
        ]
        assert await subscriber.get() is None
        assert budget.snapshot()["pending_memory_bytes"] == 0
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_sparse_offer_is_committed_without_explicit_flush():
    stream = ReplayStream()
    subscriber = stream.subscribe()
    try:
        await stream.offer("data: sparse\n\n")
        assert (
            await asyncio.wait_for(subscriber.get(), 1) == "data: sparse\n\n"
        )
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_full_pending_buffer_cancellation_does_not_accept_or_leak(
    monkeypatch,
):
    budget = ReplayBudget()
    stream = ReplayStream(ReplayLog(budget=budget))
    subscriber = stream.subscribe()
    started, release = threading.Event(), threading.Event()
    original = stream.log._append_many

    def delayed(texts):
        started.set()
        assert release.wait(5)
        return original(texts)

    monkeypatch.setattr(stream.log, "_append_many", delayed)
    try:
        for _ in range(64):
            await stream.offer("data: retained\n\n")
        waiting = asyncio.create_task(stream.offer("data: cancelled\n\n"))
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.001)
        assert not waiting.done()
        assert stream.accepted == 64
        assert stream.pending_count == 64
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert stream.accepted == 64
        ending = asyncio.create_task(stream.end())
        await asyncio.sleep(0)
        ending.cancel()
        await asyncio.sleep(0)
        ending.cancel()
        assert not ending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await ending
        assert stream.committed == 64
        assert stream.ended
        assert budget.snapshot()["pending_memory_bytes"] == 0
        assert [await subscriber.get() for _ in range(64)] == [
            "data: retained\n\n",
        ] * 64
    finally:
        release.set()
        await stream.aclose()


@pytest.mark.asyncio
async def test_failed_batch_wakes_flush_and_clears_pending(monkeypatch):
    stream = ReplayStream()
    subscriber = stream.subscribe()

    def fail(_texts):
        raise OSError("disk full")

    monkeypatch.setattr(stream.log, "_append_many", fail)
    try:
        for _ in range(60):
            await stream.offer("data: pending\n\n")
        await asyncio.wait_for(stream.end(), 1)
        assert stream.committed == 0
        assert stream.pending_count == 0
        assert stream.log.budget.snapshot()["pending_memory_bytes"] == 0
        assert "stream.unavailable" in await subscriber.get()
        assert await subscriber.get() is None
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_oversize_utf8_frame_is_not_partially_committed():
    stream = ReplayStream()
    subscriber = stream.subscribe()
    try:
        await stream.offer("😀" * (300 * 1024))
        await asyncio.wait_for(stream.end(), 1)
        assert stream.committed == 0
        assert stream.log.budget.snapshot()["pending_memory_bytes"] == 0
        assert "stream.unavailable" in await subscriber.get()
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_cancelled_writer_settles_actual_commit_and_end(monkeypatch):
    stream = ReplayStream()
    subscriber = stream.subscribe()
    started, release = threading.Event(), threading.Event()
    original = stream.log._append_many

    def delayed(texts):
        started.set()
        assert release.wait(5)
        return original(texts)

    monkeypatch.setattr(stream.log, "_append_many", delayed)
    try:
        await stream.offer("data: retained\n\n")
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.001)
        stream.writer.cancel()
        release.set()
        await asyncio.sleep(0.01)
        assert stream.committed == stream.log.count == 1
        await stream.end()
        assert await subscriber.get() == "data: retained\n\n"
    finally:
        release.set()
        stream.error = stream.error or "cleanup"
        stream.progress.set()
        if stream.writer.cancelled():
            stream.writer = None
        await stream.aclose()


@pytest.mark.asyncio
async def test_global_pending_budget_and_close_wake_waiting_offer(monkeypatch):
    from swe.app.runner import sse_replay

    monkeypatch.setattr(sse_replay, "GLOBAL_PENDING_BYTES", 5000)
    budget = ReplayBudget()
    first = ReplayStream(ReplayLog(budget=budget))
    second = ReplayStream(ReplayLog(budget=budget))
    release, started = threading.Event(), threading.Event()
    original = first.log._append_many

    def delayed(texts):
        started.set()
        assert release.wait(5)
        return original(texts)

    monkeypatch.setattr(first.log, "_append_many", delayed)
    try:
        await first.offer("a" * 4000)
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.001)
        blocked = asyncio.create_task(second.offer("b" * 4000))
        await asyncio.sleep(0.01)
        assert not blocked.done()
        assert budget.snapshot()["pending_memory_bytes"] < 5000
        await second.end()
        assert await asyncio.wait_for(blocked, 1) is None
        release.set()
        await first.end()
        assert budget.snapshot()["pending_memory_bytes"] == 0
    finally:
        release.set()
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
async def test_large_legal_unicode_frame_is_exclusive_and_not_resumable_until_commit(
    monkeypatch,
):
    stream = ReplayStream()
    subscriber = stream.subscribe()
    release, started = threading.Event(), threading.Event()
    original = stream.log._append_many
    text = "😀" * (200 * 1024)

    def delayed(texts):
        started.set()
        assert release.wait(5)
        return original(texts)

    monkeypatch.setattr(stream.log, "_append_many", delayed)
    try:
        assert await stream.offer(text) == 1
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.001)
        with pytest.raises(ReplayUnavailable):
            subscriber.resume("turn-large", "turn-large:1")
        waiting = asyncio.create_task(stream.offer("tail"))
        await asyncio.sleep(0.01)
        assert not waiting.done()
        release.set()
        assert await waiting == 2
        await stream.end()
        assert await subscriber.get() == text
        assert await subscriber.get() == "tail"
    finally:
        release.set()
        await stream.aclose()


@pytest.mark.asyncio
async def test_later_batch_short_write_with_failed_truncate_holds_reservation():
    log = ReplayLog(budget=ReplayBudget())
    try:
        await log.append("prefix")
        before = log.budget.used_bytes
        original = log.file

        class FailingFile:
            calls = 0

            def write(self, data):
                self.calls += 1
                return original.write(data if self.calls == 1 else data[:-1])

            def truncate(self, _end):
                raise OSError("truncate failed")

            def __getattr__(self, name):
                return getattr(original, name)

        log.file = FailingFile()
        with pytest.raises(OSError):
            await log.append_many(["a" * 40000, "b" * 40000])
        assert log.count == 1
        assert log.budget.used_bytes > before
        assert await log.read(1) == [(1, "prefix")]
    finally:
        await log.aclose()
    assert log.budget.used_bytes == 0


@pytest.mark.asyncio
async def test_writer_cancel_before_start_releases_pending():
    stream = ReplayStream()
    subscriber = stream.subscribe()
    try:
        await stream.offer("data: accepted\n\n")
        stream.writer.cancel()
        await asyncio.gather(stream.writer, return_exceptions=True)
        assert stream.pending_count == 0
        assert stream.error is not None
        await stream.end()
        assert "stream.unavailable" in await subscriber.get()
    finally:
        stream.error = stream.error or "cleanup"
        stream.progress.set()
        stream.writer = None
        await stream.aclose()


@pytest.mark.asyncio
async def test_append_after_end_remains_a_noop_for_oversized_frame():
    stream = ReplayStream()
    subscriber = stream.subscribe()
    try:
        await stream.end()
        await stream.append("a" * (1024 * 1024 + 1))
        assert stream.error is None
        assert await subscriber.get() is None
    finally:
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [OSError(), RuntimeError()])
async def test_empty_message_write_failure_still_settles_flush(
    monkeypatch,
    failure,
):
    stream = ReplayStream()
    subscriber = stream.subscribe()

    def fail(_texts):
        raise failure

    monkeypatch.setattr(stream.log, "_append_many", fail)
    try:
        await stream.offer("data: test\n\n")
        await asyncio.gather(stream.writer, return_exceptions=True)
        assert stream.error
        await stream.end()
        assert "stream.unavailable" in await subscriber.get()
        assert stream.log.budget.snapshot()["pending_memory_bytes"] == 0
    finally:
        stream.error = stream.error or "cleanup"
        stream.progress.set()
        await stream.aclose()


@pytest.mark.asyncio
async def test_concurrent_emitters_replay_in_accepted_sequence_order():
    stream = ReplayStream()
    subscriber = stream.subscribe()
    accepted = []

    async def emit(source):
        for index in range(40):
            text = f"data: {source}:{index}\n\n"
            sequence = await stream.offer(text)
            accepted.append((sequence, text))
            await asyncio.sleep(0)

    try:
        await asyncio.gather(emit("one"), emit("two"), emit("three"))
        await stream.end()
        assert [await subscriber.get() for _ in accepted] == [
            text for _, text in sorted(accepted)
        ]
        assert await subscriber.get() is None
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_new_offers_do_not_reset_first_event_deadline(monkeypatch):
    from swe.app.runner import sse_replay

    monkeypatch.setattr(sse_replay, "WRITE_DELAY_SECONDS", 0.04)
    stream = ReplayStream()
    producer = None

    async def emit():
        for _ in range(20):
            await stream.offer("data: sparse\n\n")
            await asyncio.sleep(0.02)

    try:
        producer = asyncio.create_task(emit())
        async with asyncio.timeout(0.3):
            while stream.committed == 0:
                await stream.progress.wait()
        assert not producer.done()
        await producer
        await stream.end()
    finally:
        if producer is not None and not producer.done():
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [OSError, RuntimeError])
async def test_failed_writer_releases_payload_without_cyclic_gc(
    monkeypatch,
    failure_type,
):
    class Payload(str):
        pass

    def fail(_texts):
        raise failure_type("write failed")

    stream = ReplayStream(log=ReplayLog(budget=ReplayBudget()))
    monkeypatch.setattr(stream.log, "_append_many", fail)
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        payload = Payload("data: retained\n\n")
        reference = weakref.ref(payload)
        await stream.offer(payload)
        del payload
        await asyncio.gather(stream.writer, return_exceptions=True)
        assert stream.log.budget.snapshot()["pending_memory_bytes"] == 0
        assert reference() is None
    finally:
        await stream.aclose()
        if was_enabled:
            gc.enable()


@pytest.mark.asyncio
async def test_cancelled_failed_worker_releases_payload_without_gc(
    monkeypatch,
):
    class Payload(str):
        pass

    started, release = threading.Event(), threading.Event()

    def fail(texts):
        text = texts[0]
        started.set()
        assert release.wait(5)
        raise OSError(f"failed length {len(text)}")

    stream = ReplayStream(log=ReplayLog(budget=ReplayBudget()))
    monkeypatch.setattr(stream.log, "_append_many", fail)
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        payload = Payload("data: cancelled\n\n")
        reference = weakref.ref(payload)
        await stream.offer(payload)
        del payload
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.001)
        stream.writer.cancel()
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(stream.writer, return_exceptions=True)
        assert stream.log.budget.snapshot()["pending_memory_bytes"] == 0
        assert reference() is None
    finally:
        release.set()
        await stream.aclose()
        if was_enabled:
            gc.enable()

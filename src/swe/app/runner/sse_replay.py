# -*- coding: utf-8 -*-
"""Bounded process-local replay logs; subscribers retain cursors, not history."""

from __future__ import annotations

import asyncio
from array import array
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
import struct
import sys
import tempfile
import threading
import time
import traceback
from typing import Any, Callable

from ...performance_metrics import record_stream_timing, timed_work

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="swe-stream")
_SLOTS = threading.BoundedSemaphore(4)
_HEADER = struct.Struct("!I")
MAX_FRAME_BYTES = 1024 * 1024
MAX_LOG_BYTES = 64 * 1024 * 1024
MAX_LOG_RECORDS = 65536
READ_BATCH_BYTES = 64 * 1024
WRITE_BATCH_RECORDS = 32
WRITE_DELAY_SECONDS = 0.005
PENDING_RECORDS = 64
PENDING_BYTES = 256 * 1024
GLOBAL_PENDING_BYTES = 32 * 1024 * 1024
REPLAY_TTL_SECONDS = 300
SLOW_SUBSCRIBER_SECONDS = 30
SLOW_SUBSCRIBER_BYTES = 2 * 1024 * 1024


class ReplayUnavailable(RuntimeError):
    """The requested stream cannot be replayed without losing events."""


async def run_stream_io(
    function: Callable,
    *args: Any,
    operation: str | None = None,
):
    """Bound submissions and keep resources alive through repeated cancellation."""
    queued_at = time.monotonic()

    async def submit():
        # The worker owns the permit until its actual completion.
        # pylint: disable=consider-using-with
        while not _SLOTS.acquire(blocking=False):
            await asyncio.sleep(0.005)
        context = copy_context()

        def execute():
            started = time.monotonic()
            try:
                return context.run(
                    timed_work,
                    "stream",
                    queued_at,
                    function,
                    *args,
                )
            finally:
                finished = time.monotonic()
                try:
                    if operation is not None:
                        record_stream_timing(
                            f"{operation}_queue",
                            started - queued_at,
                        )
                        record_stream_timing(
                            f"{operation}_execution",
                            finished - started,
                        )
                finally:
                    _SLOTS.release()

        try:
            future = _EXECUTOR.submit(execute)
        except BaseException:
            _SLOTS.release()
            raise
        return await asyncio.wrap_future(future)

    return await _await_stream_task(asyncio.create_task(submit()))


async def _await_stream_task(task: asyncio.Task):
    """Finish resource ownership transitions even under repeated cancellation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        if not task.cancelled():
            failure = task.exception()
            if failure is not None:
                traceback.clear_frames(failure.__traceback__)
        raise asyncio.CancelledError
    return task.result()


class ReplayBudget:
    """One process's disk/index and object-count admission budget."""

    def __init__(self, max_bytes: int = 512 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.used_bytes = 0
        self.logs = 0
        self.subscribers = 0
        self.subscriber_memory_bytes = 0
        self.pending_memory_bytes = 0
        self.lock = threading.Lock()
        self.counters = dict.fromkeys(
            (
                "quota_rejections",
                "slow_disconnects",
                "replay_errors",
                "reconnects",
                "written_bytes",
                "read_bytes",
                "write_batches",
                "written_records",
            ),
            0,
        )

    def record(self, name: str, value: int = 1) -> None:
        with self.lock:
            self.counters[name] += value

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return {
                "reserved_bytes": self.used_bytes,
                "active_logs": self.logs,
                "active_subscribers": self.subscribers,
                "subscriber_memory_bytes": self.subscriber_memory_bytes,
                "pending_memory_bytes": self.pending_memory_bytes,
                **self.counters,
            }

    def try_reserve_pending(self, size: int) -> bool:
        with self.lock:
            if self.pending_memory_bytes + size > GLOBAL_PENDING_BYTES:
                return False
            self.pending_memory_bytes += size
            return True

    def release_pending(self, size: int) -> None:
        with self.lock:
            self.pending_memory_bytes -= size

    def reserve_memory(self, size: int) -> None:
        with self.lock:
            if self.subscriber_memory_bytes + size > 64 * 1024 * 1024:
                self.counters["quota_rejections"] += 1
                raise ReplayUnavailable(
                    "Stream subscriber memory capacity exceeded",
                )
            self.subscriber_memory_bytes += size

    def release_memory(self, size: int) -> None:
        with self.lock:
            self.subscriber_memory_bytes -= size

    def reserve(self, size: int) -> None:
        with self.lock:
            if self.used_bytes + size > self.max_bytes:
                self.counters["quota_rejections"] += 1
                raise ReplayUnavailable("Stream replay capacity exceeded")
            self.used_bytes += size

    def release(self, size: int) -> None:
        with self.lock:
            self.used_bytes -= size

    def admit(self, kind: str, maximum: int) -> None:
        with self.lock:
            if getattr(self, kind) >= maximum:
                self.counters["quota_rejections"] += 1
                raise ReplayUnavailable(
                    "Stream subscription capacity exceeded",
                )
            setattr(self, kind, getattr(self, kind) + 1)

    def retire(self, kind: str) -> None:
        with self.lock:
            setattr(self, kind, getattr(self, kind) - 1)


_BUDGET = ReplayBudget()


class ReplayLog:
    """Length-prefixed temporary file with a bounded compact offset index."""

    def __init__(
        self,
        *,
        budget: ReplayBudget | None = None,
        max_bytes: int = MAX_LOG_BYTES,
    ):
        self.budget = budget or _BUDGET
        self.max_bytes = max_bytes
        self.offsets = array("Q")
        self.file = None
        self.size = 0
        self.data_end = 0
        self.closed = False
        self.admitted = False
        self.lock = threading.Lock()
        self.budget.admit("logs", 128)
        self.admitted = True

    @property
    def count(self) -> int:
        return len(self.offsets)

    def _append(self, text: str) -> int:
        return self._append_many([text])

    def _append_many(self, texts: list[str]) -> int:
        with self.lock:
            if self.closed:
                raise ReplayUnavailable("Stream replay expired")
            records = []
            for text in texts:
                if len(text) > MAX_FRAME_BYTES:
                    self.budget.record("quota_rejections")
                    raise ReplayUnavailable(
                        "Stream frame exceeds replay capacity",
                    )
                data = text.encode("utf-8")
                if len(data) > MAX_FRAME_BYTES:
                    self.budget.record("quota_rejections")
                    raise ReplayUnavailable(
                        "Stream frame exceeds replay capacity",
                    )
                records.append(_HEADER.pack(len(data)) + data)
            cost = (
                sum(map(len, records)) + len(records) * self.offsets.itemsize
            )
            if (
                self.size + cost > self.max_bytes
                or self.count + len(records) > MAX_LOG_RECORDS
            ):
                self.budget.record("quota_rejections")
                raise ReplayUnavailable("Stream replay capacity exceeded")
            self.budget.reserve(cost)
            end = None
            try:
                if self.file is None:
                    self.file = tempfile.TemporaryFile(mode="w+b", buffering=0)
                end = self.file.seek(0, 2)
                offsets = array("Q")
                position = end
                block = bytearray()
                for record in records:
                    offsets.append(position)
                    position += len(record)
                    if block and len(block) + len(record) > READ_BATCH_BYTES:
                        if self.file.write(block) != len(block):
                            raise OSError("Incomplete stream replay write")
                        block.clear()
                    block.extend(record)
                if block and self.file.write(block) != len(block):
                    raise OSError("Incomplete stream replay write")
                self.offsets.extend(offsets)
                self.data_end = position
                self.size += cost
                self.budget.record("written_bytes", position - end)
                self.budget.record("write_batches")
                self.budget.record("written_records", len(records))
                return self.count
            except BaseException:
                try:
                    if end is not None:
                        self.file.truncate(end)
                except OSError:
                    self.size += cost
                else:
                    self.budget.release(cost)
                raise

    async def append_many(self, texts: list[str]) -> int:
        return await run_stream_io(self._append_many, texts, operation="write")

    async def append(self, text: str) -> int:
        return await run_stream_io(self._append, text, operation="write")

    def _read(self, first: int, max_bytes: int, upto: int | None):
        with self.lock:
            if self.closed:
                raise ReplayUnavailable("Stream replay expired")
            last = min(self.count, upto if upto is not None else self.count)
            if first < 1 or first > last + 1:
                raise ReplayUnavailable("Invalid stream replay cursor")
            stop = first - 1
            payload_bytes = 0
            while stop < min(last, first + 63):
                end = (
                    self.offsets[stop + 1]
                    if stop + 1 < self.count
                    else self.data_end
                )
                length = end - self.offsets[stop] - _HEADER.size
                if length < 0 or length > MAX_FRAME_BYTES:
                    raise ReplayUnavailable("Invalid stream replay record")
                if stop >= first and payload_bytes + length > max_bytes:
                    break
                payload_bytes += length
                stop += 1
            if stop < first:
                return []
            start_offset = self.offsets[first - 1]
            end_offset = (
                self.offsets[stop] if stop < self.count else self.data_end
            )
            self.file.seek(start_offset)
            data = self.file.read(end_offset - start_offset)
            if len(data) != end_offset - start_offset:
                raise ReplayUnavailable("Incomplete stream replay record")
            result = []
            position = 0
            for sequence in range(first, stop + 1):
                if position + _HEADER.size > len(data):
                    raise ReplayUnavailable("Incomplete stream replay record")
                length = _HEADER.unpack_from(data, position)[0]
                position += _HEADER.size
                if length > MAX_FRAME_BYTES or position + length > len(data):
                    raise ReplayUnavailable("Invalid stream replay record")
                if position + length != (
                    self.offsets[sequence] - start_offset
                    if sequence < self.count
                    else self.data_end - start_offset
                ):
                    raise ReplayUnavailable("Invalid stream replay record")
                result.append(
                    (
                        sequence,
                        data[position : position + length].decode("utf-8"),
                    ),
                )
                position += length
            self.budget.record("read_bytes", len(data))
            return result

    async def read(
        self,
        first: int,
        *,
        max_bytes: int = READ_BATCH_BYTES,
        upto: int | None = None,
    ):
        started = time.monotonic()
        try:
            return await run_stream_io(
                self._read,
                first,
                max_bytes,
                upto,
                operation="read",
            )
        finally:
            record_stream_timing("read_total", time.monotonic() - started)

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if not self.admitted:
                return
            try:
                if self.file is not None:
                    self.file.close()
            finally:
                self.budget.release(self.size)
                self.budget.retire("logs")
                self.offsets = array("Q")

    async def aclose(self) -> None:
        await run_stream_io(self.close)


class ReplayStream:
    """One ordered publisher, shared replay, and independent subscribers."""

    def __init__(self, log: ReplayLog | None = None):
        self.log = log or ReplayLog()
        self.committed = 0
        self.ended = False
        self.closed = False
        self.error: str | None = None
        self.subscribers: list[ReplaySubscription] = []
        self.accepted = 0
        self.closing = False
        self.pending: deque[tuple[str, int, float]] = deque()
        self.pending_bytes = 0
        self.pending_count = 0
        self.writer: asyncio.Task | None = None
        self.writer_wake = asyncio.Event()
        self.progress = asyncio.Event()
        self.force_flush = False
        self.expiry: asyncio.TimerHandle | None = None

    def subscribe(self) -> ReplaySubscription:
        if self.closed or len(self.subscribers) >= 8:
            self.log.budget.record("quota_rejections")
            raise ReplayUnavailable("Stream subscription capacity exceeded")
        self.log.budget.admit("subscribers", 256)
        subscriber = ReplaySubscription(self)
        self.subscribers.append(subscriber)
        return subscriber

    def __del__(self):
        # Best effort when an event loop is destroyed before its TTL callback.
        # Normal transport lifetime always uses the async close path.
        log = getattr(self, "log", None)
        if log is not None:
            for subscriber in getattr(self, "subscribers", ()):
                if not subscriber.detached:
                    log.budget.retire("subscribers")
                    subscriber.detached = True
                subscriber.release_batch_memory()
            log.close()

    async def offer(self, text: str) -> int | None:
        """Accept into a bounded buffer; only committed records are readable."""
        started = time.monotonic()
        if self.writer is not None and self.writer.done():
            self._writer_done(self.writer)
        if self.closing or self.ended or self.closed or self.error:
            return None
        if len(text) > MAX_FRAME_BYTES:
            self.error = "Stream frame exceeds replay capacity"
            self.log.budget.record("quota_rejections")
            self._notify_subscribers()
            return None
        cost = sys.getsizeof(text) + sys.getsizeof((text, 0, 0.0)) + 64
        while not (self.closing or self.ended or self.closed or self.error):
            fits = self.pending_count < PENDING_RECORDS and (
                self.pending_bytes + cost <= PENDING_BYTES
                or self.pending_count == 0
            )
            if fits and self.log.budget.try_reserve_pending(cost):
                self.accepted += 1
                accepted_at = time.monotonic()
                self.pending.append((text, cost, accepted_at))
                record_stream_timing("admission", accepted_at - started)
                self.pending_bytes += cost
                self.pending_count += 1
                if self.writer is None or self.writer.done():
                    self.writer = asyncio.create_task(self._write_pending())
                    self.writer.add_done_callback(self._writer_done)
                if (
                    len(self.pending) >= WRITE_BATCH_RECORDS
                    or self.pending_bytes >= READ_BATCH_BYTES
                ):
                    self.writer_wake.set()
                return self.accepted
            # No tracker/file lock is held while waiting for local/global space.
            self.writer_wake.set()
            await asyncio.sleep(WRITE_DELAY_SECONDS)
        return None

    async def flush(self, through_sequence: int | None = None) -> None:
        target = (
            self.accepted if through_sequence is None else through_sequence
        )
        self.force_flush = True
        self.writer_wake.set()
        while self.committed < target and not (self.error or self.closed):
            self.progress.clear()
            await self.progress.wait()

    async def append(self, text: str) -> None:
        sequence = await self.offer(text)
        if sequence is not None:
            await _await_stream_task(asyncio.create_task(self.flush(sequence)))

    async def _write_pending(self) -> None:
        try:
            while self.pending:
                delay = max(
                    0,
                    self.pending[0][2]
                    + WRITE_DELAY_SECONDS
                    - time.monotonic(),
                )
                if (
                    not self.force_flush
                    and len(self.pending) < WRITE_BATCH_RECORDS
                    and self.pending_bytes < READ_BATCH_BYTES
                ):
                    try:
                        await asyncio.wait_for(self.writer_wake.wait(), delay)
                    except asyncio.TimeoutError:
                        pass
                self.writer_wake.clear()
                rows = []
                size = 0
                while self.pending and len(rows) < WRITE_BATCH_RECORDS:
                    row = self.pending[0]
                    if rows and size + row[1] > READ_BATCH_BYTES:
                        break
                    rows.append(self.pending.popleft())
                    size += row[1]
                del row
                submitted_at = time.monotonic()
                for _, _, accepted_at in rows:
                    record_stream_timing(
                        "batch_wait",
                        submitted_at - accepted_at,
                    )
                texts = [row[0] for row in rows]
                try:
                    self.committed = await self.log.append_many(texts)
                    committed_at = time.monotonic()
                    for _, _, accepted_at in rows:
                        record_stream_timing(
                            "accepted_to_committed",
                            committed_at - accepted_at,
                        )
                finally:
                    # Drop payload references before returning their memory permit.
                    count = len(rows)
                    texts.clear()
                    rows.clear()
                    self.pending_count -= count
                    self.pending_bytes -= size
                    self.log.budget.release_pending(size)
                self.progress.set()
                self._notify_subscribers()
        except asyncio.CancelledError:
            # A submitted write finishes before run_stream_io re-raises cancel.
            self.committed = self.log.count
            self.error = "Stream writer cancelled"
            self.log.budget.record("replay_errors")
        except Exception as exc:
            self.log.budget.record("replay_errors")
            self.error = str(exc) or "Stream replay failed"
            traceback.clear_frames(exc.__traceback__)
        finally:
            self._release_pending()

    def _release_pending(self) -> None:
        remaining = sum(row[1] for row in self.pending)
        self.pending.clear()
        self.pending_count = 0
        self.pending_bytes = 0
        self.log.budget.release_pending(remaining)
        self.force_flush = False
        self.progress.set()
        self._notify_subscribers()

    def _writer_done(self, task: asyncio.Task) -> None:
        # A task cancelled before its first step never enters its finally block.
        failure = None if task.cancelled() else task.exception()
        if task.cancelled() or failure is not None:
            if not self.error:
                self.error = (
                    "Stream writer cancelled"
                    if task.cancelled()
                    else str(failure) or "Stream replay failed"
                )
                self.log.budget.record("replay_errors")
            self.committed = self.log.count
            self._release_pending()

    def _notify_subscribers(self) -> None:
        for subscriber in self.subscribers:
            lag = self.log.size - (
                self.log.offsets[subscriber.last_sequence]
                if subscriber.last_sequence < self.committed
                else self.log.size
            )
            if (
                subscriber.resumable
                and not subscriber.control
                and not subscriber.finished
                and lag > SLOW_SUBSCRIBER_BYTES
                and time.monotonic() - subscriber.last_progress
                > SLOW_SUBSCRIBER_SECONDS
            ):
                subscriber.interrupt("slow_consumer", retryable=True)
                self.log.budget.record("slow_disconnects")
            subscriber.changed.set()

    def finish(self) -> None:
        if self.ended:
            return
        self.ended = True
        for subscriber in self.subscribers:
            subscriber.changed.set()
        self.expiry = asyncio.get_running_loop().call_later(
            REPLAY_TTL_SECONDS,
            lambda: asyncio.create_task(self.aclose()),
        )

    async def end(self) -> None:
        async def complete():
            self.closing = True
            await self.flush()
            if self.writer is not None:
                await asyncio.gather(self.writer, return_exceptions=True)
            self.finish()
            if not self.subscribers:
                await self.aclose()

        await _await_stream_task(asyncio.create_task(complete()))

    def detach(self, subscriber: ReplaySubscription) -> None:
        if subscriber in self.subscribers:
            self.subscribers.remove(subscriber)
            self.log.budget.retire("subscribers")
        subscriber.batch.clear()
        subscriber.release_batch_memory()
        subscriber.detached = True
        subscriber.changed.set()

    async def aclose(self) -> None:
        async def close():
            self.closing = True
            await self.flush()
            if self.writer is not None:
                await asyncio.gather(self.writer, return_exceptions=True)
            await self._close_log()

        await _await_stream_task(asyncio.create_task(close()))

    async def _close_log(self) -> None:
        self.closed = True
        if self.expiry:
            self.expiry.cancel()
        for subscriber in tuple(self.subscribers):
            if subscriber.last_sequence < self.committed or self.error:
                subscriber.interrupt("replay_expired", retryable=False)
            self.detach(subscriber)
        await self.log.aclose()


class ReplaySubscription:
    """Queue-compatible handle whose payload memory is limited to one batch."""

    def __init__(self, stream: ReplayStream):
        self.stream = stream
        self.last_sequence = 0
        self.batch: deque[tuple[int, str]] = deque()
        self.changed = asyncio.Event()
        self.detached = False
        self.control: str | None = None
        self.finished = False
        self.last_progress = time.monotonic()
        self.resumable = False
        self.memory_bytes = 0

    def release_batch_memory(self) -> None:
        self.stream.log.budget.release_memory(self.memory_bytes)
        self.memory_bytes = 0

    @property
    def buffered_bytes(self) -> int:
        return sum(len(text.encode("utf-8")) for _, text in self.batch)

    def seek(self, after: int) -> None:
        if after < 0 or after > self.stream.committed:
            raise ReplayUnavailable("Invalid stream replay cursor")
        self.last_sequence = after
        self.batch.clear()
        self.release_batch_memory()

    def resume(self, turn_id: str, cursor: str | None) -> None:
        if cursor is not None:
            if not isinstance(cursor, str) or len(cursor) > 128:
                raise ReplayUnavailable("Invalid stream replay cursor")
            prefix, separator, sequence = cursor.rpartition(":")
            if (
                separator != ":"
                or prefix != turn_id
                or not sequence.isascii()
                or not sequence.isdigit()
            ):
                raise ReplayUnavailable("Invalid stream replay cursor")
            self.seek(int(sequence))
            self.stream.log.budget.record("reconnects")
        self.resumable = True

    def interrupt(self, reason: str, *, retryable: bool) -> None:
        self.batch.clear()
        event = "stream.recover" if retryable else "stream.unavailable"
        self.control = (
            f"event: {event}\ndata: "
            + json.dumps(
                {
                    "object": "stream_recovery",
                    "reason": reason,
                    "retryable": retryable,
                },
            )
            + "\n\n"
        )
        self.changed.set()

    async def get(self) -> str | None:
        if not self.batch:
            self.release_batch_memory()
        while True:
            if self.control:
                self.release_batch_memory()
                control, self.control = self.control, None
                self.finished = True
                return control
            if self.finished or self.detached or self.stream.closed:
                return None
            if self.batch:
                self.last_sequence, text = self.batch.popleft()
                self.last_progress = time.monotonic()
                return text
            if self.last_sequence < self.stream.committed:
                try:
                    rows = await self.stream.log.read(
                        self.last_sequence + 1,
                        upto=self.stream.committed,
                    )
                    if self.detached or self.stream.closed or self.control:
                        continue
                    memory_bytes = sum(
                        sys.getsizeof(row) + sys.getsizeof(row[1])
                        for row in rows
                    )
                    self.stream.log.budget.reserve_memory(memory_bytes)
                    self.memory_bytes = memory_bytes
                    self.batch.extend(rows)
                except (OSError, ReplayUnavailable) as exc:
                    self.interrupt(str(exc), retryable=False)
                continue
            if self.stream.error:
                self.interrupt(self.stream.error, retryable=False)
                continue
            if self.stream.ended:
                return None
            self.changed.clear()
            # No await separates the predicate above and notification reset.
            await self.changed.wait()

# -*- coding: utf-8 -*-
"""Single-owner, bounded Span writes and durable-version barriers."""

import asyncio
import logging
from collections import deque
from contextvars import Context
from dataclasses import dataclass
from typing import Optional

from .config import TracingConfig
from .models import Span
from .store import TraceStore

logger = logging.getLogger(__name__)


def _estimate_bytes(span: Span) -> int:
    """Conservatively budget text-protocol values without serializing JSON."""
    size = 256  # SQL syntax and separators per row
    values = [(value, False) for value in span.__dict__.values()]
    ancestors: set[int] = set()
    while values:
        value, leaving = values.pop()
        if leaving:
            ancestors.remove(id(value))
            continue
        if isinstance(value, str):
            # Non-BMP JSON surrogate pairs plus SQL backslash escaping.
            size += 14 * len(value) + 2
        elif isinstance(value, (dict, list, tuple)):
            if id(value) in ancestors:
                raise ValueError("Circular reference in tracing input")
            ancestors.add(id(value))
            values.append((value, True))
            if isinstance(value, dict):
                values.extend((key, False) for key in value)
                values.extend((item, False) for item in value.values())
            else:
                values.extend((item, False) for item in value)
            size += 4 * len(value) + 2
        elif isinstance(value, int):
            size += len(str(value)) + 2
        else:
            size += 64
    return size


@dataclass(eq=False)
class _Entry:
    span: Span
    size: int
    inserted: bool = False
    version: int = 0
    acknowledged: int = -1
    queued: bool = True


class SpanWriter:
    """Own writes until confirmed, including retries and concurrent updates."""

    def __init__(self, config: TracingConfig):
        self.config = config
        self.pending: dict[str, Span] = {}
        self.queue: deque[_Entry] = deque()
        self.task: Optional[asyncio.Task] = None
        self._entries: dict[str, _Entry] = {}
        self._bytes = 0
        self._changed = asyncio.Condition()
        self._wake = asyncio.Event()
        self._accepting = False
        self._store: Optional[TraceStore] = None

    def start(self, store: TraceStore) -> None:
        """Start outside any request's transaction or tenant context."""
        self._store = store
        self._accepting = True
        self.task = Context().run(asyncio.create_task, self._run())

    def _has_capacity(self, size: int, entry: Optional[_Entry]) -> bool:
        count = len(self._entries) + (entry is None)
        total = self._bytes + size - (entry.size if entry else 0)
        # A single oversized row must not deadlock or silently lose payload.
        alone = count == 1
        return count <= self.config.max_queue_spans and (
            total <= self.config.max_queue_bytes or alone
        )

    async def submit(self, span: Span, *, inserted: bool = False) -> None:
        """Admit a version, waiting for capacity without starting another writer."""
        async with self._changed:
            while True:
                if not self._accepting:
                    raise RuntimeError("Tracing writer is closing")
                size = _estimate_bytes(span)
                entry = self._entries.get(span.span_id)
                if self._has_capacity(size, entry):
                    break
                self._wake.set()
                await self._changed.wait()

            # Own nested input too: caller mutation must not bypass the budget.
            span = span.model_copy(deep=True)
            if entry is None:
                entry = _Entry(span, size, inserted=inserted)
                self._entries[span.span_id] = entry
                self.queue.append(entry)
            else:
                self._bytes -= entry.size
                entry.span = span
                entry.size = size
                entry.version += 1
                self._enqueue(entry)
            self._bytes += size
            self.pending[span.span_id] = span
            if (
                len(self.queue) >= self.config.batch_size
                or self._bytes >= self.config.max_batch_bytes
            ):
                self._wake.set()

    def _enqueue(self, entry: _Entry) -> None:
        if not entry.queued:
            entry.queued = True
            self.queue.append(entry)

    async def flush(self, trace_id: Optional[str] = None) -> None:
        """Confirm versions already accepted for a trace, including in-flight."""
        async with self._changed:
            targets = [
                (entry, entry.version)
                for entry in self._entries.values()
                if trace_id is None or entry.span.trace_id == trace_id
            ]
            self._wake.set()
            try:
                await asyncio.wait_for(
                    self._changed.wait_for(
                        lambda: all(
                            entry.acknowledged >= version
                            for entry, version in targets
                        ),
                    ),
                    self.config.write_timeout,
                )
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    "Tracing writes remain unconfirmed",
                ) from exc

    async def close(self) -> None:
        """Stop admission and drain; report unconfirmed data at the deadline."""
        async with self._changed:
            self._accepting = False
            self._wake.set()
            self._changed.notify_all()
        if self.task is None:
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(self.task),
                self.config.shutdown_timeout,
            )
        except asyncio.TimeoutError as exc:
            self.task.cancel()
            # wait_for(task) would itself wait for cancellation indefinitely.
            done, _ = await asyncio.wait(
                {self.task},
                timeout=min(self.config.shutdown_timeout, 1.0),
            )
            for task in done:
                if not task.cancelled():
                    error = task.exception()
                    if error is not None:
                        logger.error(
                            "Tracing writer failed during cancellation cleanup",
                            exc_info=(type(error), error, error.__traceback__),
                        )
            message = (
                f"Tracing shutdown timed out: {len(self._entries)} "
                f"unconfirmed spans ({self._bytes} estimated bytes)"
            )
            if not self.task.done():
                message += "; writer still running after cancellation"
            logger.error(message)
            raise TimeoutError(message) from exc

    def _take_batch(self) -> list[tuple[_Entry, Span, int]]:
        batch = []
        size = 0
        for entry in self.queue:
            if len(batch) >= self.config.batch_size:
                break
            if batch and size + entry.size > self.config.max_batch_bytes:
                break
            batch.append(
                (entry, entry.span.model_copy(), entry.version),
            )
            size += entry.size
        # Snapshot failure must leave the whole batch owned by the queue.
        for entry, _, _ in batch:
            self.queue.popleft()
            entry.queued = False
        return batch

    async def _persist(self, batch: list[tuple[_Entry, Span, int]]) -> None:
        assert self._store is not None
        if self._store.db is None:
            for _, span, _ in batch:
                if span.skill_name:
                    logger.info(
                        "[SKILL SPAN] skill='%s', type=%s",
                        span.skill_name,
                        span.event_type,
                    )
            return
        inserts = [span for entry, span, _ in batch if not entry.inserted]
        updates = [(entry, span) for entry, span, _ in batch if entry.inserted]
        if inserts:
            count = await self._store.batch_create_spans(inserts)
            if count != len(inserts):
                raise RuntimeError("Tracing batch was not fully confirmed")
            for entry, _, _ in batch:
                entry.inserted = True
        for _, span in updates:
            await self._store.update_span(span)

    async def _acknowledge(
        self,
        batch: list[tuple[_Entry, Span, int]],
    ) -> None:
        async with self._changed:
            for entry, _, version in batch:
                entry.acknowledged = version
                if entry.version == version:
                    self._entries.pop(entry.span.span_id)
                    self.pending.pop(entry.span.span_id)
                    self._bytes -= entry.size
                else:
                    self._enqueue(entry)
            self._changed.notify_all()

    def _requeue(self, batch: list[tuple[_Entry, Span, int]]) -> None:
        # The same entry may already hold an accepted, newer queued version.
        for entry, _, _ in reversed(batch):
            if not entry.queued:
                entry.queued = True
                self.queue.appendleft(entry)
        self._wake.set()

    async def _run(self) -> None:
        failures = 0
        while self._accepting or self._entries:
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    self.config.flush_interval,
                )
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            while self.queue:
                batch = []
                try:
                    batch = self._take_batch()
                    await self._persist(batch)
                    await self._acknowledge(batch)
                except asyncio.CancelledError:
                    self._requeue(batch)
                    raise
                except Exception:
                    self._requeue(batch)
                    failures += 1
                    logger.exception("Tracing write failed; retaining batch")
                    await asyncio.sleep(
                        min(0.1 * 2 ** min(failures - 1, 6), 5),
                    )
                    break
                failures = 0
                await asyncio.sleep(0)

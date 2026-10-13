# -*- coding: utf-8 -*-
"""Bounded, nonblocking publication with revision-aware best-effort writes."""

import asyncio
import copy
import json
import logging
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)
_ID_FIELD = {
    "run": "branch_run_id",
    "call": "call_id",
    "event": "event_id",
    "context": "context_id",
    "context_operation": "operation_id",
}


def record_revision(record):
    key = "context_revision" if record["kind"] == "context" else "revision"
    return record["data"].get(key, 1)


class GapCounters(OrderedDict):
    def __init__(self, limit):
        super().__init__()
        self.limit, self.limited = limit, False

    def __missing__(self, key):
        return 0

    def __setitem__(self, key, value):
        if key not in self and len(self) >= self.limit:
            self.popitem(last=False)
            self.limited = True
        super().__setitem__(key, value)


class DiagnosticWriter:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.pending = OrderedDict()
        self.gaps = GapCounters(config.max_records * 2)
        self.buffer_bytes = 0
        self.active_bytes = 0
        self.inflight_bytes = 0
        self.active_count = 0
        self.inflight_count = 0
        self.task = None
        self.closed = False

    @property
    def pending_count(self):
        return len(self.pending)

    def _gap(self, record):
        self.gaps[record["data"].get("branch_run_id", "unknown")] += 1

    def add(self, record):
        if self.closed:
            self._gap(record)
            return False
        data = copy.deepcopy(record)
        branch_id = data["data"].get("branch_run_id", "unknown")
        data["data"].update(
            gap_count=self.gaps[branch_id],
            gap_tracking_limited=self.gaps.limited,
        )
        key = (data["kind"], data["data"][_ID_FIELD[data["kind"]]])
        old = self.pending.get(key)
        if old and record_revision(old[0]) >= record_revision(data):
            return True
        size = len(json.dumps(data, ensure_ascii=False).encode())
        if size > self.config.max_record_bytes:
            self._gap(data)
            return False
        previous_size = old[2] if old else 0
        count_full = (
            not old
            and len(self.pending) + self.active_count + self.inflight_count
            >= self.config.max_records
        )
        bytes_full = (
            self.buffer_bytes
            - previous_size
            + size
            + self.active_bytes
            + self.inflight_bytes
            > self.config.max_bytes
        )
        if count_full or bytes_full:
            self._gap(data)
            return False
        self.pending[key] = (data, old[1] if old else time.monotonic(), size)
        self.buffer_bytes += size - previous_size
        return True

    async def flush_once(self):
        batch = []
        now = time.monotonic()
        for key in list(self.pending)[: self.config.batch_size]:
            if (
                batch
                and sum(x[3] for x in batch) + self.pending[key][2]
                > 1024 * 1024
            ):
                break
            record, created, size = self.pending.pop(key)
            self.buffer_bytes -= size
            if now - created >= self.config.retry_age:
                self._gap(record)
                continue
            record["data"]["gap_count"] = self.gaps[
                record["data"]["branch_run_id"]
            ]
            record["data"]["gap_tracking_limited"] = self.gaps.limited
            size = len(json.dumps(record, ensure_ascii=False).encode())
            if (
                size > self.config.max_record_bytes
                or self.buffer_bytes
                + self.active_bytes
                + sum(x[3] for x in batch)
                + size
                > self.config.max_bytes
            ):
                self._gap(record)
                continue
            batch.append((key, record, created, size))
        if not batch:
            return True
        self.inflight_bytes = sum(x[3] for x in batch)
        self.inflight_count = len(batch)
        try:
            await asyncio.wait_for(
                self.store.write_batch([x[1] for x in batch]),
                self.config.write_timeout,
            )
            return True
        except (Exception, asyncio.CancelledError) as exc:
            logger.warning(
                "Token diagnostic write failed: %s",
                type(exc).__name__,
            )
            for key, record, created, size in reversed(batch):
                newer = self.pending.get(key)
                if newer is None:
                    self.pending[key] = (record, created, size)
                    self.pending.move_to_end(key, last=False)
                    self.buffer_bytes += size
            if isinstance(exc, asyncio.CancelledError):
                raise
            return False
        finally:
            self.inflight_bytes = 0
            self.inflight_count = 0

    def start(self):
        if self.task is None and not self.closed:
            self.task = asyncio.create_task(self._run())

    async def _run(self):
        backoff = self.config.flush_interval
        while not self.closed:
            await asyncio.sleep(backoff)
            success = await self.flush_once()
            backoff = (
                self.config.flush_interval
                if success
                else min(30, max(1, backoff * 2))
            )

    async def close(self):
        if self.closed:
            return
        self.closed = True
        deadline = time.monotonic() + self.config.shutdown_timeout
        if self.task:
            self.task.cancel()
            done, _ = await asyncio.wait(
                {self.task},
                timeout=max(0, deadline - time.monotonic()),
            )
            if done:
                try:
                    self.task.result()
                except (Exception, asyncio.CancelledError):
                    pass
        try:

            async def drain():
                while self.pending:
                    if not await self.flush_once():
                        break

            await asyncio.wait_for(
                drain(),
                max(0.001, deadline - time.monotonic()),
            )
        except (Exception, asyncio.CancelledError) as exc:
            logger.warning(
                "Token diagnostics close incomplete: %s",
                type(exc).__name__,
            )
        try:
            await asyncio.wait_for(
                self.store.close(),
                max(0.001, deadline - time.monotonic()),
            )
        except (Exception, asyncio.CancelledError) as exc:
            logger.warning(
                "Token diagnostics pool close incomplete: %s",
                type(exc).__name__,
            )
            abort = getattr(self.store, "abort", None)
            if abort:
                abort()
        for record, _, _ in self.pending.values():
            self._gap(record)
        self.pending.clear()
        self.buffer_bytes = 0

# -*- coding: utf-8 -*-
"""Process-local independent collector; callbacks cannot fail business work."""

import logging
from dataclasses import replace

from .config import DiagnosticConfig
from .store import TokenStore
from .writer import DiagnosticWriter

logger = logging.getLogger(__name__)
_collector = None


class Collector:
    def __init__(self, config, store=None):
        self.config = config
        self.writer = DiagnosticWriter(store or TokenStore(), config)

    def emit(self, kind, data):
        if not self.config.enabled:
            return False
        try:
            self.writer.start()
            return self.writer.add({"kind": kind, "data": data})
        except Exception as exc:
            logger.warning("Token capture unavailable: %s", type(exc).__name__)
            self.writer.gaps[data.get("branch_run_id", "unknown")] += 1
            return False

    def reserve(self, size, count=1):
        writer = self.writer
        if (
            writer.active_count
            + writer.inflight_count
            + len(writer.pending)
            + count
            > self.config.max_records
        ):
            return False
        if (
            size
            + writer.active_bytes
            + writer.buffer_bytes
            + writer.inflight_bytes
            > self.config.max_bytes
        ):
            return False
        writer.active_bytes += size
        writer.active_count += count
        return True

    def release(self, size, count=1):
        self.writer.active_bytes = max(0, self.writer.active_bytes - size)
        if size:
            self.writer.active_count = max(0, self.writer.active_count - count)

    async def close(self):
        await self.writer.close()


def get_collector():
    global _collector
    if _collector is None or _collector.writer.closed:
        _collector = Collector(DiagnosticConfig.from_env())
    return _collector


def start_collection():
    collector = get_collector()
    if collector.config.enabled:
        collector.writer.start()


def apply_worker_snapshot(snapshot):
    global _collector
    if snapshot and type(snapshot.get("capture_enabled")) is bool:
        config = replace(
            DiagnosticConfig.from_env(),
            enabled=snapshot["capture_enabled"],
        )
        _collector = Collector(config)


async def close_collection():
    global _collector
    if _collector is not None:
        try:
            await _collector.close()
        finally:
            _collector = None

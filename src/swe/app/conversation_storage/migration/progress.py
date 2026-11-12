# -*- coding: utf-8 -*-
"""CLI-only counters; never record SQL, parameters, or conversation bodies."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import threading

import psutil

_CURRENT = ContextVar("migration_progress", default=None)


def current_progress():
    return _CURRENT.get()


class MigrationProgress:
    def __init__(self, operation, migration_id, *, stream=None, interval=5):
        self.stream = stream if stream is not None else sys.stderr
        self.interval = interval
        self.started = time.monotonic()
        self.data = {
            "operation": operation,
            "migration_id": migration_id,
            "stage": "starting",
            "position": None,
            "failure_position": None,
            "completed": 0,
            "counter_unit": "tasks",
            "task_total": None,
            "sql_wait_seconds": 0.0,
            "sql_seconds": 0.0,
            "sql_queue_depth": 0,
            "sql_queue_peak": 0,
            "io_active": 0,
            "io_peak": 0,
            "io_seconds": 0.0,
            "io_calls": 0,
            "rss_peak_bytes": 0,
            "temp_peak_bytes": 0,
        }
        self._disk_lock = threading.Lock()
        self._disk_used = 0
        self.open_io = 0
        self.uncertain_io = False

    def disk_change(self, size):
        with self._disk_lock:
            self._disk_used += size
            self.data["temp_peak_bytes"] = max(
                self.data["temp_peak_bytes"],
                self._disk_used,
            )

    def stage(self, name):
        self.data.update(stage=name, position=None, completed=0)

    def snapshot(self):
        try:
            self.data["rss_peak_bytes"] = max(
                self.data["rss_peak_bytes"],
                psutil.Process().memory_info().rss,
            )
        except (OSError, psutil.Error):
            pass
        return {
            **self.data,
            "elapsed_seconds": round(time.monotonic() - self.started, 3),
            "io_drained": (
                "unknown" if self.open_io or self.uncertain_io else True
            ),
        }

    def emit(self):
        try:
            self.stream.write(
                json.dumps(self.snapshot(), sort_keys=True) + "\n",
            )
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def failed_at(self, position):
        if self.data["failure_position"] is None and position is not None:
            self.data["failure_position"] = position
            self.data["position"] = position

    async def _report(self):
        while True:
            await asyncio.sleep(self.interval)
            self.emit()

    @contextmanager
    def activate(self):
        token = _CURRENT.set(self)
        try:
            yield self
        finally:
            _CURRENT.reset(token)

    async def run(self, operation):
        reporter = asyncio.create_task(self._report())
        try:
            return await operation
        finally:
            reporter.cancel()
            await asyncio.gather(reporter, return_exceptions=True)
            self.emit()

    def save_failure(self, manifest_path, error):
        path = Path(str(manifest_path) + ".failure.json")
        report = {
            "version": 1,
            "passed": False,
            "error_type": type(error).__name__,
            **self.snapshot(),
        }
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=path.parent,
                delete=False,
                encoding="utf-8",
            ) as output:
                temporary = Path(output.name)
                os.chmod(temporary, 0o600)
                json.dump(report, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

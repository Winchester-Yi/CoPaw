# -*- coding: utf-8 -*-
"""Bounded process-local performance measurements without tenant labels."""

import asyncio
from contextlib import contextmanager
from functools import wraps
import inspect
import logging
from time import perf_counter

from collections import deque
from collections.abc import Callable
import math
import threading
import time
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")
_SAMPLE_LIMIT = 2048
_WORKERS = ("output", "runtime_state", "provider", "backup", "stream")
_COUNTERS = (
    "checkpoint_read_bytes",
    "checkpoint_read_calls",
    "s3_list_pages",
    "journal_append_bytes",
    "journal_read_bytes",
    "journal_frames_decoded",
    "ledger_lookup_read_bytes",
)
_STORAGE_OPERATIONS = (
    "session_commit",
    "checkpoint_commit",
    "session_recovery",
    "checkpoint_recovery",
    "session_migration",
    "checkpoint_migration",
    "session_migration_preview",
    "checkpoint_migration_preview",
)
_STORAGE_OUTCOMES = ("succeeded", "failed", "cancelled")
_SESSION_QUANTITIES = (
    "session_hot_terminal_count",
    "session_hot_terminal_bytes",
    "session_protected_count",
    "session_protected_bytes",
)
_COUNTERS += tuple(
    f"{operation}_failures" for operation in _STORAGE_OPERATIONS
)
logger = logging.getLogger(__name__)
_STREAM_STAGES = (
    "admission",
    "batch_wait",
    "accepted_to_committed",
    "write_queue",
    "write_execution",
    "read_queue",
    "read_execution",
    "read_total",
)


def _summary(values: list[float], prefix: str) -> dict:
    ordered = sorted(values)
    result = {
        f"{prefix}_avg_ms": sum(values) / len(values) if values else None,
    }
    for percentile in (95, 99):
        result[f"{prefix}_p{percentile}_ms"] = (
            ordered[math.ceil(len(ordered) * percentile / 100) - 1]
            if ordered
            else None
        )
    result[f"{prefix}_max_ms"] = max(values) if values else None
    return result


def _quantity_summary(values: list[int]) -> dict:
    ordered = sorted(values)
    return {
        "last": values[-1] if values else None,
        "avg": sum(values) / len(values) if values else None,
        "p95": ordered[math.ceil(len(values) * 0.95) - 1] if values else None,
        "max": ordered[-1] if values else None,
    }


class PerformanceMetrics:
    """Keep rolling task timings and cumulative I/O counters under a lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._samples: dict[str, deque[tuple[float, float]]] = {
            name: deque(maxlen=_SAMPLE_LIMIT) for name in _WORKERS
        }
        self._completed = dict.fromkeys(_WORKERS, 0)
        self._counters = dict.fromkeys(_COUNTERS, 0)
        self._stream_samples: dict[str, deque[float]] = {
            name: deque(maxlen=_SAMPLE_LIMIT) for name in _STREAM_STAGES
        }
        self._stream_observed = dict.fromkeys(_STREAM_STAGES, 0)
        self._storage_samples: dict[str, deque[float]] = {
            name: deque(maxlen=_SAMPLE_LIMIT) for name in _STORAGE_OPERATIONS
        }
        self._storage_counts = {
            name: dict.fromkeys(_STORAGE_OUTCOMES, 0)
            for name in _STORAGE_OPERATIONS
        }
        self._session_samples: deque[tuple[int, int, int, int]] = deque(
            maxlen=_SAMPLE_LIMIT,
        )
        self._session_observed = 0

    def record_session_memory(
        self,
        hot_terminal_count: int,
        hot_terminal_bytes: int,
        protected_count: int,
        protected_bytes: int,
    ) -> None:
        values = (
            hot_terminal_count,
            hot_terminal_bytes,
            protected_count,
            protected_bytes,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError(
                "Session memory quantities must be non-negative integers",
            )
        with self._lock:
            self._session_samples.append(values)
            self._session_observed += 1

    def record_storage_operation(
        self,
        operation: str,
        seconds: float,
        outcome: str,
    ) -> None:
        if (
            operation not in self._storage_samples
            or outcome not in _STORAGE_OUTCOMES
        ):
            raise ValueError(
                "Unknown storage performance operation or outcome",
            )
        with self._lock:
            self._storage_samples[operation].append(max(0.0, seconds) * 1000)
            self._storage_counts[operation][outcome] += 1
            if outcome == "failed":
                self._counters[f"{operation}_failures"] += 1

    def record_stream_timing(self, stage: str, seconds: float) -> None:
        if stage not in self._stream_samples:
            raise ValueError("Unknown stream performance stage")
        with self._lock:
            self._stream_samples[stage].append(max(0.0, seconds) * 1000)
            self._stream_observed[stage] += 1

    def record_worker_timing(
        self,
        worker: str,
        queue_seconds: float,
        execution_seconds: float,
    ) -> None:
        if worker not in self._samples:
            raise ValueError("Unknown performance worker")
        with self._lock:
            self._samples[worker].append(
                (
                    max(0.0, queue_seconds) * 1000,
                    max(0.0, execution_seconds) * 1000,
                ),
            )
            self._completed[worker] += 1

    def increment(self, name: str, value: int = 1) -> None:
        if name not in self._counters:
            raise ValueError("Unknown performance counter")
        with self._lock:
            self._counters[name] += value

    def snapshot(self) -> dict:
        with self._lock:
            samples = {
                name: list(rows) for name, rows in self._samples.items()
            }
            completed = self._completed.copy()
            counters = self._counters.copy()
            stream_samples = {
                name: list(rows) for name, rows in self._stream_samples.items()
            }
            stream_observed = self._stream_observed.copy()
            storage_samples = {
                name: list(rows)
                for name, rows in self._storage_samples.items()
            }
            storage_counts = {
                name: counts.copy()
                for name, counts in self._storage_counts.items()
            }
            session_samples = list(self._session_samples)
            session_observed = self._session_observed
        workers = {}
        for name, rows in samples.items():
            workers[name] = {
                "completed_count": completed[name],
                "sample_count": len(rows),
                **_summary([row[0] for row in rows], "queue"),
                **_summary([row[1] for row in rows], "execution"),
            }
        stream_stages = {
            name: {
                "observed_count": stream_observed[name],
                "sample_count": len(rows),
                **_summary(rows, "duration"),
            }
            for name, rows in stream_samples.items()
        }
        return {
            "workers": workers,
            "counters": counters,
            "stream_stages": stream_stages,
            "storage_operations": {
                name: {
                    "attempted_count": sum(storage_counts[name].values()),
                    **{
                        f"{outcome}_count": value
                        for outcome, value in storage_counts[name].items()
                    },
                    "sample_count": len(rows),
                    **_summary(rows, "duration"),
                }
                for name, rows in storage_samples.items()
            },
            "session_memory": {
                "observed_count": session_observed,
                "sample_count": len(session_samples),
                **{
                    name: _quantity_summary(
                        [row[index] for row in session_samples],
                    )
                    for index, name in enumerate(_SESSION_QUANTITIES)
                },
            },
        }


METRICS = PerformanceMetrics()


def record_stream_timing(stage: str, seconds: float) -> None:
    """Record one bounded SSE stage observation without payload references."""
    METRICS.record_stream_timing(stage, seconds)


def increment(name: str, value: int = 1) -> None:
    """Record actual checkpoint payload reads or attempted S3 page calls."""
    METRICS.increment(name, value)


@contextmanager
def _storage_operation(operation):
    started = perf_counter()
    outcome = "succeeded"
    try:
        yield
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except BaseException:
        outcome = "failed"
        raise
    finally:
        try:
            METRICS.record_storage_operation(
                operation,
                perf_counter() - started,
                outcome,
            )
        except Exception:
            logger.warning("Failed to record storage performance observation")


def measure_storage_operation(operation, *, dry_run_default=None):
    """Measure real sync/async execution, without retaining request payloads."""
    if operation not in _STORAGE_OPERATIONS:
        raise ValueError("Unknown storage performance operation")

    def decorate(function):
        def selected(kwargs):
            if dry_run_default is not None and kwargs.get(
                "dry_run",
                dry_run_default,
            ):
                return f"{operation}_preview"
            return operation

        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def async_measured(*args, **kwargs):
                with _storage_operation(selected(kwargs)):
                    return await function(*args, **kwargs)

            return async_measured

        @wraps(function)
        def measured(*args, **kwargs):
            with _storage_operation(selected(kwargs)):
                return function(*args, **kwargs)

        return measured

    return decorate


def timed_work(
    worker: str,
    queued_at: float,
    function: Callable[P, T],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Measure wait until real thread start and execution until real finish."""
    started = time.monotonic()
    try:
        return function(*args, **kwargs)
    finally:
        METRICS.record_worker_timing(
            worker,
            started - queued_at,
            time.monotonic() - started,
        )

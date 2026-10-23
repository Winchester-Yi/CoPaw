# -*- coding: utf-8 -*-
"""Process-local runtime diagnostic metric collection and logging."""

from __future__ import annotations

import asyncio
import gc
import inspect
import json
import logging
import math
import os
import random
import sys
import traceback
import time
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import Any

import anyio
import psutil

from swe.performance_metrics import METRICS
from swe.app.runner.sse_replay import _BUDGET as STREAM_REPLAY_BUDGET

from swe.app.pod_resources import (
    collect_pod_disk_io_bytes,
    collect_pod_open_fd_count,
)

logger = logging.getLogger(__name__)

_LOG_PREFIX = "RUNTIME_DIAGNOSTIC "
_SCHEMA = "runtime_diagnostic.v1"
_STORAGE_PATH = "/opt/deployments/app"
_BLOCKED_LAG_THRESHOLD_MS = 1000.0
_SAMPLE_INTERVAL_SECONDS = 10.0
_FIRST_DIAGNOSTIC_DELAY_SECONDS = 120.0
_FIRST_DIAGNOSTIC_JITTER_SECONDS = 10.0
_DIAGNOSTIC_INTERVAL_SECONDS = 1800.0
_MEMORY_DIFF_PROBE_INTERVAL_SECONDS = 300.0
_MEMORY_DIFF_PROBE_LIMIT = 20
_WATCHFILES_STACK_PROBE_LIMIT = 50
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
_WATCHFILES_STACK_EVENTS: list[dict[str, object]] = []
_WATCHFILES_STACK_PROBE_INSTALLED = False


def _percentile(values: list[float], percentile: float) -> float | None:
    """Return a deterministic nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _normalize_limit(limit: int) -> int:
    return min(100, max(1, int(limit)))


def _env_flag_enabled(name: str) -> bool:
    value = os.environ.get(name, "")
    return value.strip().lower() in _TRUTHY_ENV_VALUES


def _env_float(name: str, default: float, *, minimum: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return max(minimum, float(value))
    except ValueError:
        logger.warning(
            "Invalid %s=%r; using %.1f",
            name,
            value,
            default,
        )
        return default


def _diagnostic_import(
    module_name: str,
    errors: list[dict[str, str]],
):
    try:
        return import_module(module_name)
    except Exception as exc:  # pylint: disable=broad-except
        errors.append(
            {
                "collector": module_name,
                "message": str(exc),
            },
        )
        return None


def _safe_repr(value: object, *, limit: int = 300) -> str:
    try:
        text = repr(value)
    except Exception as exc:  # pylint: disable=broad-except
        text = f"<repr failed: {exc!r}>"
    return text[:limit]


def _qualified_type_name(value: object) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_inotify_watch_line(line: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for token in line.split():
        if ":" not in token:
            continue
        key, value = token.split(":", 1)
        if key in {"wd", "ino", "sdev", "mask"}:
            fields[key] = value
    return fields


def _record_watchfiles_stack_event(function_name: str, args: tuple) -> None:
    paths = [str(arg) for arg in args if isinstance(arg, (str, os.PathLike))][
        :10
    ]
    event: dict[str, object] = {
        "function": function_name,
        "paths": paths,
        "stack": traceback.format_stack(limit=16)[:-2],
    }
    _WATCHFILES_STACK_EVENTS.append(event)
    del _WATCHFILES_STACK_EVENTS[:-_WATCHFILES_STACK_PROBE_LIMIT]


def install_watchfiles_stack_probe() -> bool:
    """Patch watchfiles watch APIs to record creation call stacks.

    The probe is opt-in because it monkeypatches a third-party module for
    diagnostics. It should be enabled before code creates file watchers.
    """
    global _WATCHFILES_STACK_PROBE_INSTALLED  # pylint: disable=global-statement
    if _WATCHFILES_STACK_PROBE_INSTALLED:
        return True
    if not _env_flag_enabled("SWE_WATCHFILES_STACK_PROBE_ENABLED"):
        return False

    try:
        watchfiles = import_module("watchfiles")
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Unable to install watchfiles stack probe: %s", exc)
        return False

    original_to_wrapped: dict[object, object] = {}
    for name in ("watch", "awatch"):
        original = getattr(watchfiles, name, None)
        if original is None or getattr(original, "_swe_stack_probe", False):
            continue

        def _wrapped(*args, __original=original, __name=name, **kwargs):
            _record_watchfiles_stack_event(__name, args)
            return __original(*args, **kwargs)

        _wrapped._swe_stack_probe = True  # type: ignore[attr-defined]
        _wrapped._swe_original = original  # type: ignore[attr-defined]
        setattr(watchfiles, name, _wrapped)
        original_to_wrapped[original] = _wrapped

    if original_to_wrapped:
        for module in tuple(sys.modules.values()):
            if module is None or module is watchfiles:
                continue
            module_globals = getattr(module, "__dict__", None)
            if not isinstance(module_globals, dict):
                continue
            for name in ("watch", "awatch"):
                current = module_globals.get(name)
                wrapped = original_to_wrapped.get(current)
                if wrapped is not None:
                    module_globals[name] = wrapped

    _WATCHFILES_STACK_PROBE_INSTALLED = True
    logger.info("Installed watchfiles stack probe")
    return True


class RuntimeDiagnosticManager:
    """Collect and emit Runtime Instance diagnostic metrics."""

    def __init__(
        self,
        *,
        hostname: str | None = None,
        wall_time: Callable[[], float] = time.time,
        process: Any | None = None,
        disk_usage: Callable[[str], Any] = psutil.disk_usage,
        pod_open_fd_count: Callable[[], int] = collect_pod_open_fd_count,
        pod_disk_io_bytes: Callable[
            [],
            tuple[int, int],
        ] = collect_pod_disk_io_bytes,
        workspace_metrics: Callable[[], dict[str, int]] | None = None,
        thread_limiter: Callable[[], Any] = (
            anyio.to_thread.current_default_thread_limiter
        ),
        log_sink: Callable[[str], None] | None = None,
        monotonic_time: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.hostname = (
            hostname
            if hostname is not None
            else os.environ.get(
                "HOSTNAME",
                "",
            )
        )
        self._wall_time = wall_time
        self._process = process if process is not None else psutil.Process()
        self._disk_usage = disk_usage
        self._pod_open_fd_count = pod_open_fd_count
        self._pod_disk_io_bytes = pod_disk_io_bytes
        self._workspace_metrics = workspace_metrics
        self._thread_limiter = thread_limiter
        self._log_sink = log_sink or logger.info
        self._monotonic_time = monotonic_time
        self._sleep = sleep
        self._jitter = jitter

        self._sse_active_connections = 0
        self._sse_peak_connections = 0
        self._event_loop_lag_samples: list[float] = []
        self._event_loop_blocked_count = 0
        self._process_cpu_samples: list[float] = []
        self._process_rss_sampled_peak_bytes: int | None = None
        self._previous_pod_disk_io_bytes: tuple[int, int] | None = None
        self._previous_pod_disk_io_at: float | None = None
        self._pod_disk_io_collection_failed = False
        self._pod_disk_io_valid = True
        self._pod_disk_read_bytes_per_second: float | None = None
        self._pod_disk_read_bytes_per_second_peak: float | None = None
        self._pod_disk_write_bytes_per_second: float | None = None
        self._pod_disk_write_bytes_per_second_peak: float | None = None
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._memory_diff_probe_enabled = _env_flag_enabled(
            "SWE_MEMORY_DIFF_PROBE_ENABLED",
        )
        self._memory_diff_interval_seconds = _env_float(
            "SWE_MEMORY_DIFF_PROBE_INTERVAL_SECONDS",
            _MEMORY_DIFF_PROBE_INTERVAL_SECONDS,
            minimum=1.0,
        )
        self._last_memory_diff_snapshot: dict[str, object] | None = None
        self._watchfiles_stack_probe_enabled = install_watchfiles_stack_probe()

    def set_workspace_metrics(
        self,
        workspace_metrics: Callable[[], dict[str, int]] | None,
    ) -> None:
        """Set the workspace cache metric collector."""
        self._workspace_metrics = workspace_metrics

    def record_sse_opened(self) -> None:
        """Record one newly active SSE connection."""
        self._sse_active_connections += 1
        self._sse_peak_connections = max(
            self._sse_peak_connections,
            self._sse_active_connections,
        )

    def record_sse_closed(self) -> None:
        """Record one completed SSE connection."""
        self._sse_active_connections = max(0, self._sse_active_connections - 1)

    def record_sample(
        self,
        *,
        lag_ms: float,
        cpu_percent: float | None,
        pod_disk_io_bytes: tuple[int, int] | None = None,
        sampled_at: float | None = None,
        rss_bytes: int | None = None,
    ) -> None:
        """Record one event-loop and process CPU sample."""
        self._event_loop_lag_samples.append(max(0.0, lag_ms))
        if lag_ms > _BLOCKED_LAG_THRESHOLD_MS:
            self._event_loop_blocked_count += 1
        if cpu_percent is not None:
            self._process_cpu_samples.append(cpu_percent)
        if rss_bytes is not None:
            self._process_rss_sampled_peak_bytes = max(
                self._process_rss_sampled_peak_bytes or 0,
                rss_bytes,
            )
        self._record_pod_disk_io_sample(
            pod_disk_io_bytes=pod_disk_io_bytes,
            sampled_at=sampled_at,
        )

    def _record_pod_disk_io_sample(
        self,
        *,
        pod_disk_io_bytes: tuple[int, int] | None,
        sampled_at: float | None,
    ) -> None:
        if pod_disk_io_bytes is None or sampled_at is None:
            self._pod_disk_io_valid = False
            self._previous_pod_disk_io_bytes = None
            self._previous_pod_disk_io_at = None
            return

        if (
            self._previous_pod_disk_io_bytes is not None
            and self._previous_pod_disk_io_at is not None
        ):
            elapsed = sampled_at - self._previous_pod_disk_io_at
            read_delta = (
                pod_disk_io_bytes[0] - self._previous_pod_disk_io_bytes[0]
            )
            write_delta = (
                pod_disk_io_bytes[1] - self._previous_pod_disk_io_bytes[1]
            )
            if elapsed <= 0 or read_delta < 0 or write_delta < 0:
                self._pod_disk_io_valid = False
            else:
                read_rate = read_delta / elapsed
                write_rate = write_delta / elapsed
                self._pod_disk_read_bytes_per_second = read_rate
                self._pod_disk_write_bytes_per_second = write_rate
                self._pod_disk_read_bytes_per_second_peak = max(
                    self._pod_disk_read_bytes_per_second_peak or 0.0,
                    read_rate,
                )
                self._pod_disk_write_bytes_per_second_peak = max(
                    self._pod_disk_write_bytes_per_second_peak or 0.0,
                    write_rate,
                )

        self._previous_pod_disk_io_bytes = pod_disk_io_bytes
        self._previous_pod_disk_io_at = sampled_at

    def rotate_window(self) -> None:
        """Reset window metrics while preserving active SSE connections."""
        self._sse_peak_connections = self._sse_active_connections
        self._event_loop_lag_samples.clear()
        self._event_loop_blocked_count = 0
        self._process_cpu_samples.clear()
        self._process_rss_sampled_peak_bytes = None
        self._pod_disk_io_valid = True
        self._pod_disk_read_bytes_per_second = None
        self._pod_disk_read_bytes_per_second_peak = None
        self._pod_disk_write_bytes_per_second = None
        self._pod_disk_write_bytes_per_second_peak = None

    def prime_process_cpu(self) -> None:
        """Prime psutil's non-blocking process CPU measurement."""
        try:
            self._process.cpu_percent()
        except Exception:  # pylint: disable=broad-except
            logger.exception("Failed to prime runtime diagnostic process CPU")

    async def sample_once(self, *, planned_wakeup: float) -> None:
        """Record one event-loop lag and process CPU sample."""
        sampled_at = self._monotonic_time()
        lag_ms = max(0.0, (sampled_at - planned_wakeup) * 1000)
        cpu_percent = None
        rss_bytes = None
        pod_disk_io_bytes = None
        try:
            cpu_percent = float(self._process.cpu_percent())
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "Failed to collect runtime diagnostic process CPU",
            )
        if not self._pod_disk_io_collection_failed:
            try:
                pod_disk_io_bytes = self._pod_disk_io_bytes()
            except Exception:  # pylint: disable=broad-except
                self._pod_disk_io_collection_failed = True
                logger.exception(
                    "Failed to collect runtime diagnostic Pod disk I/O",
                )
        try:
            rss_bytes = int(self._process.memory_info().rss)
        except Exception:  # pylint: disable=broad-except
            logger.exception("Failed to sample runtime diagnostic RSS")
        self.record_sample(
            lag_ms=lag_ms,
            cpu_percent=cpu_percent,
            pod_disk_io_bytes=pod_disk_io_bytes,
            sampled_at=sampled_at,
            rss_bytes=rss_bytes,
        )

    async def run_sampler_loop(self) -> None:
        """Sample event-loop lag and process CPU at a fixed interval."""
        planned_wakeup = self._monotonic_time() + _SAMPLE_INTERVAL_SECONDS
        while True:
            await self._sleep(
                max(0.0, planned_wakeup - self._monotonic_time()),
            )
            try:
                await self.sample_once(planned_wakeup=planned_wakeup)
            except asyncio.CancelledError:
                raise
            except Exception:  # pylint: disable=broad-except
                logger.exception("Runtime diagnostic sampler iteration failed")
            planned_wakeup += _SAMPLE_INTERVAL_SECONDS

    async def run_periodic_loop(self) -> None:
        """Emit the first delayed diagnostic, then one every 30 minutes."""
        initial_delay = _FIRST_DIAGNOSTIC_DELAY_SECONDS + self._jitter(
            0.0,
            _FIRST_DIAGNOSTIC_JITTER_SECONDS,
        )
        await self._sleep(initial_delay)
        while True:
            try:
                self.emit_diagnostic()
            except asyncio.CancelledError:
                raise
            except Exception:  # pylint: disable=broad-except
                logger.exception("Runtime diagnostic emission failed")
            await self._sleep(_DIAGNOSTIC_INTERVAL_SECONDS)

    async def run_memory_diff_probe_loop(self) -> None:
        """Emit opt-in memory diff snapshots at a fixed interval."""
        self.emit_memory_diff_probe(tag="baseline")
        while True:
            await self._sleep(self._memory_diff_interval_seconds)
            try:
                self.emit_memory_diff_probe(tag="periodic")
            except asyncio.CancelledError:
                raise
            except Exception:  # pylint: disable=broad-except
                logger.exception("Runtime memory diff probe failed")

    async def start(self) -> None:
        """Emit registration and start background diagnostic tasks."""
        if self._running:
            return
        if not self.hostname:
            logger.error("Runtime diagnostic disabled: HOSTNAME is missing")
            return
        self._running = True
        self.emit_registered()
        self.prime_process_cpu()
        self._tasks = [
            asyncio.create_task(
                self.run_sampler_loop(),
                name="runtime-diagnostic-sampler",
            ),
            asyncio.create_task(
                self.run_periodic_loop(),
                name="runtime-diagnostic-emitter",
            ),
        ]
        if self._memory_diff_probe_enabled:
            self._tasks.append(
                asyncio.create_task(
                    self.run_memory_diff_probe_loop(),
                    name="runtime-memory-diff-probe",
                ),
            )

    async def stop(self) -> None:
        """Stop background tasks and emit graceful deregistration."""
        if not self._running:
            return
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.emit_deregistered()

    def _base_payload(self, event_type: str) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "event_type": event_type,
            "hostname": self.hostname,
            "event_at_ms": int(self._wall_time() * 1000),
        }

    def _process_metrics(self) -> dict[str, object]:
        metrics: dict[str, object] = {
            "process_rss_bytes": None,
            "process_vms_bytes": None,
            "process_thread_count": None,
            "process_open_fd_count": None,
            "process_uptime_seconds": None,
        }
        try:
            memory = self._process.memory_info()
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "Failed to collect runtime diagnostic process memory",
            )
        else:
            for field, attribute in (
                ("process_rss_bytes", "rss"),
                ("process_vms_bytes", "vms"),
            ):
                try:
                    metrics[field] = int(getattr(memory, attribute))
                except Exception:  # pylint: disable=broad-except
                    logger.exception(
                        "Failed to collect runtime diagnostic metric %s",
                        field,
                    )

        collectors = {
            "process_thread_count": lambda: int(self._process.num_threads()),
            "process_open_fd_count": lambda: int(self._process.num_fds()),
            "process_uptime_seconds": lambda: max(
                0,
                int(self._wall_time() - self._process.create_time()),
            ),
        }
        for field, collect in collectors.items():
            try:
                metrics[field] = collect()
            except Exception:  # pylint: disable=broad-except
                logger.exception(
                    "Failed to collect runtime diagnostic metric %s",
                    field,
                )
        return metrics

    def collect_memory_diagnostic(
        self,
        *,
        limit: int = 20,
        collect_gc: bool = True,
    ) -> dict[str, object]:
        """Collect on-demand memory diagnostics for the current process."""
        normalized_limit = _normalize_limit(limit)
        errors: list[dict[str, str]] = []
        payload = self._base_payload("memory_diagnostic")
        payload.update(
            {
                "limit": normalized_limit,
                "gc_collected": None,
                "gc_object_count": None,
                "objgraph_available": False,
                "objgraph_type_count": None,
                "top_object_types": [],
                "pympler_available": False,
                "pympler_object_count": None,
                "top_memory_types": [],
                "errors": errors,
            },
        )
        payload.update(self._process_metrics())

        if collect_gc:
            try:
                payload["gc_collected"] = int(gc.collect())
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(
                    {
                        "collector": "gc.collect",
                        "message": str(exc),
                    },
                )
        try:
            payload["gc_object_count"] = len(gc.get_objects())
        except Exception as exc:  # pylint: disable=broad-except
            errors.append(
                {
                    "collector": "gc.get_objects",
                    "message": str(exc),
                },
            )

        payload.update(
            self._collect_objgraph_diagnostic(
                normalized_limit,
                errors,
            ),
        )
        payload.update(
            self._collect_pympler_diagnostic(
                normalized_limit,
                errors,
            ),
        )
        return payload

    def collect_memory_type_holders(
        self,
        *,
        type_name: str,
        target_index: int = 0,
        holder_type_filter: str | None = None,
        max_samples_per_type: int = 5,
        collect_gc: bool = True,
    ) -> dict[str, object]:
        """Report direct referrer types for one live object of a type."""
        normalized_samples = _normalize_limit(max_samples_per_type)
        errors: list[dict[str, str]] = []
        payload = self._base_payload("memory_type_holders")
        payload.update(
            {
                "type_name": type_name,
                "target_index": target_index,
                "holder_type_filter": holder_type_filter,
                "max_samples_per_type": normalized_samples,
                "gc_collected": None,
                "objgraph_available": False,
                "target_count": 0,
                "target_repr": None,
                "holder_type_counts": [],
                "samples": [],
                "errors": errors,
            },
        )
        payload.update(self._process_metrics())

        if collect_gc:
            try:
                payload["gc_collected"] = int(gc.collect())
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(
                    {
                        "collector": "gc.collect",
                        "message": str(exc),
                    },
                )

        objgraph = _diagnostic_import("objgraph", errors)
        if objgraph is None:
            return payload
        payload["objgraph_available"] = True

        try:
            targets = objgraph.by_type(type_name)
        except Exception as exc:  # pylint: disable=broad-except
            errors.append(
                {
                    "collector": "objgraph.by_type",
                    "message": str(exc),
                },
            )
            return payload

        payload["target_count"] = len(targets)
        if not targets:
            return payload

        if target_index < 0 or target_index >= len(targets):
            errors.append(
                {
                    "collector": "target_index",
                    "message": (
                        f"target_index {target_index} out of range for "
                        f"{len(targets)} {type_name} objects"
                    ),
                },
            )
            return payload

        target = targets[target_index]
        payload["target_repr"] = _safe_repr(target)

        try:
            referrers = gc.get_referrers(target)
        except Exception as exc:  # pylint: disable=broad-except
            errors.append(
                {
                    "collector": "gc.get_referrers",
                    "message": str(exc),
                },
            )
            return payload

        holder_counts: dict[str, int] = {}
        samples: list[dict[str, object]] = []
        for referrer in referrers:
            if inspect.isframe(referrer):
                continue

            holder_type = _qualified_type_name(referrer)
            if holder_type_filter and holder_type_filter not in holder_type:
                continue

            holder_counts[holder_type] = holder_counts.get(holder_type, 0) + 1
            if holder_counts[holder_type] > normalized_samples:
                continue

            samples.append(
                {
                    "holder_type": holder_type,
                    "holder_py_type": type(referrer).__name__,
                    "holder_repr": _safe_repr(referrer),
                    "owner_hint": self._memory_referrer_owner_hint(referrer),
                },
            )

        payload["holder_type_counts"] = [
            {
                "type": holder_type,
                "count": count,
            }
            for holder_type, count in sorted(
                holder_counts.items(),
                key=lambda item: item[1],
                reverse=True,
            )
        ]
        payload["samples"] = samples
        return payload

    def collect_inotify_diagnostic(
        self,
        *,
        proc_root: str | Path = "/proc",
        max_fdinfo_bytes: int = 65536,
        include_fdinfo: bool = False,
        include_details: bool = True,
    ) -> dict[str, object]:
        """Collect process inotify fd and watch diagnostics from procfs."""
        errors: list[dict[str, str]] = []
        pid = int(getattr(self._process, "pid", os.getpid()))
        proc_dir = Path(proc_root) / str(pid)
        fd_dir = proc_dir / "fd"
        fdinfo_dir = proc_dir / "fdinfo"
        inotify_fds: list[dict[str, object]] = []
        inotify_watch_count = 0

        try:
            fd_paths = sorted(
                fd_dir.iterdir(),
                key=lambda path: (
                    int(path.name) if path.name.isdigit() else path.name
                ),
            )
        except Exception as exc:  # pylint: disable=broad-except
            errors.append(
                {
                    "collector": "proc.fd",
                    "message": str(exc),
                },
            )
            fd_paths = []

        for fd_path in fd_paths:
            try:
                target = os.readlink(fd_path)
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(
                    {
                        "collector": "proc.fd.readlink",
                        "message": f"{fd_path.name}: {exc}",
                    },
                )
                continue
            if target != "anon_inode:inotify":
                continue

            fdinfo_text = ""
            try:
                fdinfo_text = (fdinfo_dir / fd_path.name).read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(
                    {
                        "collector": "proc.fdinfo",
                        "message": f"{fd_path.name}: {exc}",
                    },
                )

            watches = [
                parsed
                for parsed in (
                    _parse_inotify_watch_line(line)
                    for line in fdinfo_text.splitlines()
                    if line.startswith("inotify ")
                )
                if parsed
            ]
            item: dict[str, object] = {
                "fd": (
                    int(fd_path.name)
                    if fd_path.name.isdigit()
                    else fd_path.name
                ),
                "target": target,
                "watch_count": len(watches),
            }
            if include_details:
                item["watches"] = watches
            if include_fdinfo:
                fdinfo_limit = max(0, int(max_fdinfo_bytes))
                item["fdinfo"] = fdinfo_text[:fdinfo_limit]
                item["fdinfo_truncated"] = len(fdinfo_text) > fdinfo_limit
            inotify_fds.append(item)
            inotify_watch_count += len(watches)

        payload = self._base_payload("inotify_diagnostic")
        payload.update(
            {
                "pid": pid,
                "inotify_fd_count": len(inotify_fds),
                "inotify_watch_count": inotify_watch_count,
                "inotify_fds": inotify_fds,
                "watchfiles_stack_probe_enabled": (
                    self._watchfiles_stack_probe_enabled
                ),
                "watchfiles_stack_events": (
                    list(_WATCHFILES_STACK_EVENTS) if include_details else []
                ),
                "errors": errors,
            },
        )
        return payload

    def _memory_referrer_owner_hint(
        self,
        referrer: object,
    ) -> dict[str, object] | None:
        if not isinstance(referrer, dict):
            return None

        try:
            owners = gc.get_referrers(referrer)
        except Exception:  # pylint: disable=broad-except
            return None

        for owner in owners:
            if inspect.isframe(owner) or isinstance(
                owner,
                (dict, list, tuple, set),
            ):
                continue
            return {
                "owner_type": _qualified_type_name(owner),
                "owner_py_type": type(owner).__name__,
                "owner_repr": _safe_repr(owner),
            }
        return None

    def collect_memory_diff_snapshot(self, *, tag: str) -> dict[str, object]:
        """Collect a memory diagnostic snapshot and compare it to the last one."""
        current = self.collect_memory_diagnostic(
            limit=_MEMORY_DIFF_PROBE_LIMIT,
            collect_gc=True,
        )
        current["tag"] = tag
        previous = self._last_memory_diff_snapshot
        self._last_memory_diff_snapshot = current

        payload = self._base_payload("memory_diff_probe")
        payload.update(
            {
                "tag": tag,
                "has_previous_snapshot": previous is not None,
                "from_tag": (
                    previous.get("tag") if isinstance(previous, dict) else None
                ),
                "to_tag": tag,
                "elapsed_ms": self._memory_snapshot_delta(
                    previous,
                    current,
                    "event_at_ms",
                ),
                "rss_delta_bytes": self._memory_snapshot_delta(
                    previous,
                    current,
                    "process_rss_bytes",
                ),
                "vms_delta_bytes": self._memory_snapshot_delta(
                    previous,
                    current,
                    "process_vms_bytes",
                ),
                "thread_delta": self._memory_snapshot_delta(
                    previous,
                    current,
                    "process_thread_count",
                ),
                "open_fd_delta": self._memory_snapshot_delta(
                    previous,
                    current,
                    "process_open_fd_count",
                ),
                "gc_object_delta": self._memory_snapshot_delta(
                    previous,
                    current,
                    "gc_object_count",
                ),
                "objgraph_type_count_delta": self._memory_snapshot_delta(
                    previous,
                    current,
                    "objgraph_type_count",
                ),
                "pympler_object_count_delta": self._memory_snapshot_delta(
                    previous,
                    current,
                    "pympler_object_count",
                ),
                "latest_process_rss_bytes": current.get("process_rss_bytes"),
                "latest_process_vms_bytes": current.get("process_vms_bytes"),
                "latest_process_thread_count": current.get(
                    "process_thread_count",
                ),
                "latest_process_open_fd_count": current.get(
                    "process_open_fd_count",
                ),
                "latest_gc_object_count": current.get("gc_object_count"),
                "top_object_type_deltas": self._top_type_deltas(
                    previous,
                    current,
                    "top_object_types",
                    ("count",),
                ),
                "top_memory_type_deltas": self._top_type_deltas(
                    previous,
                    current,
                    "top_memory_types",
                    ("count", "size_bytes"),
                ),
                "latest_top_object_types": current.get(
                    "top_object_types",
                    [],
                ),
                "latest_top_memory_types": current.get(
                    "top_memory_types",
                    [],
                ),
                "errors": current.get("errors", []),
            },
        )
        return payload

    def _memory_snapshot_delta(
        self,
        previous: dict[str, object] | None,
        current: dict[str, object],
        key: str,
    ) -> int | None:
        if previous is None:
            return None
        previous_value = _int_or_none(previous.get(key))
        current_value = _int_or_none(current.get(key))
        if previous_value is None or current_value is None:
            return None
        return current_value - previous_value

    def _top_type_deltas(
        self,
        previous: dict[str, object] | None,
        current: dict[str, object],
        key: str,
        fields: tuple[str, ...],
    ) -> list[dict[str, object]]:
        if previous is None:
            return []

        previous_rows = self._rows_by_type(previous.get(key))
        current_rows = self._rows_by_type(current.get(key))
        rows: list[dict[str, object]] = []
        for type_name in sorted(set(previous_rows) | set(current_rows)):
            row: dict[str, object] = {"type": type_name}
            has_delta = False
            for field in fields:
                delta = int(
                    current_rows.get(type_name, {}).get(field, 0),
                ) - int(previous_rows.get(type_name, {}).get(field, 0))
                row[f"{field}_delta"] = delta
                has_delta = has_delta or delta != 0
            if has_delta:
                rows.append(row)

        primary_field = f"{fields[-1]}_delta"
        return sorted(
            rows,
            key=lambda item: abs(_int_or_none(item.get(primary_field)) or 0),
            reverse=True,
        )[:_MEMORY_DIFF_PROBE_LIMIT]

    def _rows_by_type(self, rows: object) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        if not isinstance(rows, list):
            return result
        for row in rows:
            if not isinstance(row, dict) or "type" not in row:
                continue
            type_name = str(row["type"])
            result[type_name] = {}
            for key, value in row.items():
                if key == "type":
                    continue
                try:
                    result[type_name][key] = int(value)
                except (TypeError, ValueError):
                    result[type_name][key] = 0
        return result

    def emit_memory_diff_probe(self, *, tag: str) -> None:
        """Emit one memory diff probe event."""
        self._emit(self.collect_memory_diff_snapshot(tag=tag))

    def _collect_objgraph_diagnostic(
        self,
        limit: int,
        errors: list[dict[str, str]],
    ) -> dict[str, object]:
        objgraph = _diagnostic_import("objgraph", errors)
        if objgraph is None:
            return {
                "objgraph_available": False,
                "objgraph_type_count": None,
                "top_object_types": [],
            }

        try:
            type_stats = objgraph.typestats()
            top_object_types = [
                {
                    "type": str(type_name),
                    "count": int(count),
                }
                for type_name, count in sorted(
                    type_stats.items(),
                    key=lambda item: item[1],
                    reverse=True,
                )[:limit]
            ]
        except Exception as exc:  # pylint: disable=broad-except
            errors.append(
                {
                    "collector": "objgraph.typestats",
                    "message": str(exc),
                },
            )
            return {
                "objgraph_available": True,
                "objgraph_type_count": None,
                "top_object_types": [],
            }
        return {
            "objgraph_available": True,
            "objgraph_type_count": len(type_stats),
            "top_object_types": top_object_types,
        }

    def _collect_pympler_diagnostic(
        self,
        limit: int,
        errors: list[dict[str, str]],
    ) -> dict[str, object]:
        muppy = _diagnostic_import("pympler.muppy", errors)
        summary = _diagnostic_import("pympler.summary", errors)
        if muppy is None or summary is None:
            return {
                "pympler_available": False,
                "pympler_object_count": None,
                "top_memory_types": [],
            }

        objects = None
        object_count = None
        try:
            objects = muppy.get_objects()
            object_count = len(objects)
            rows = summary.summarize(objects)
            top_memory_types = [
                {
                    "type": str(row[0]),
                    "count": int(row[1]),
                    "size_bytes": int(row[2]),
                }
                for row in sorted(
                    rows,
                    key=lambda item: item[2],
                    reverse=True,
                )[:limit]
            ]
        except Exception as exc:  # pylint: disable=broad-except
            errors.append(
                {
                    "collector": "pympler.summary",
                    "message": str(exc),
                },
            )
            return {
                "pympler_available": True,
                "pympler_object_count": None,
                "top_memory_types": [],
            }
        finally:
            del objects

        return {
            "pympler_available": True,
            "pympler_object_count": object_count,
            "top_memory_types": top_memory_types,
        }

    def _storage_metrics(self) -> dict[str, object]:
        empty: dict[str, object] = {
            "storage_total_bytes": None,
            "storage_used_bytes": None,
            "storage_free_bytes": None,
            "storage_used_percent": None,
        }
        try:
            usage = self._disk_usage(_STORAGE_PATH)
            return {
                "storage_total_bytes": int(usage.total),
                "storage_used_bytes": int(usage.used),
                "storage_free_bytes": int(usage.free),
                "storage_used_percent": float(usage.percent),
            }
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "Failed to collect runtime diagnostic storage metrics",
            )
            return empty

    def _pod_metrics(self) -> dict[str, object]:
        try:
            pod_open_fd_count = self._pod_open_fd_count()
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "Failed to collect runtime diagnostic Pod open file descriptors",
            )
            pod_open_fd_count = None

        disk_metrics: dict[str, object] = {
            "pod_disk_read_bytes_per_second": None,
            "pod_disk_read_bytes_per_second_peak": None,
            "pod_disk_write_bytes_per_second": None,
            "pod_disk_write_bytes_per_second_peak": None,
        }
        if self._pod_disk_io_valid:
            disk_metrics.update(
                {
                    "pod_disk_read_bytes_per_second": (
                        self._pod_disk_read_bytes_per_second
                    ),
                    "pod_disk_read_bytes_per_second_peak": (
                        self._pod_disk_read_bytes_per_second_peak
                    ),
                    "pod_disk_write_bytes_per_second": (
                        self._pod_disk_write_bytes_per_second
                    ),
                    "pod_disk_write_bytes_per_second_peak": (
                        self._pod_disk_write_bytes_per_second_peak
                    ),
                },
            )
        return {"pod_open_fd_count": pod_open_fd_count, **disk_metrics}

    def _workspace_cache_metrics(self) -> dict[str, object]:
        empty: dict[str, object] = {
            "workspace_cache_size": None,
            "workspace_start_tasks": None,
            "workspace_cache_max_size": None,
            "workspace_start_max_concurrent": None,
            "workspace_evictions_total": None,
            "workspace_eviction_stop_failures_total": None,
        }
        if self._workspace_metrics is None:
            return empty
        try:
            metrics = self._workspace_metrics()
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "Failed to collect runtime diagnostic workspace metrics",
            )
            return empty
        return {**empty, **metrics}

    def _threadpool_metrics(self) -> dict[str, object]:
        empty: dict[str, object] = {
            "anyio_threadpool_total_tokens": None,
            "anyio_threadpool_borrowed_tokens": None,
            "anyio_threadpool_available_tokens": None,
        }
        try:
            limiter = self._thread_limiter()
            total_tokens = int(limiter.total_tokens)
            borrowed_tokens = int(limiter.borrowed_tokens)
        except Exception:  # pylint: disable=broad-except
            logger.exception(
                "Failed to collect runtime diagnostic AnyIO threadpool metrics",
            )
            return empty

        return {
            "anyio_threadpool_total_tokens": total_tokens,
            "anyio_threadpool_borrowed_tokens": borrowed_tokens,
            "anyio_threadpool_available_tokens": total_tokens
            - borrowed_tokens,
        }

    def build_diagnostic_payload(self) -> dict[str, object]:
        """Build the current flat diagnostic-flow payload."""
        lag_samples = self._event_loop_lag_samples
        cpu_samples = self._process_cpu_samples
        payload = self._base_payload("diagnostic_flow")
        performance_metrics = METRICS.snapshot()
        performance_metrics["sse_replay"] = STREAM_REPLAY_BUDGET.snapshot()
        payload.update(
            {
                "sse_active_connections": self._sse_active_connections,
                "sse_peak_connections": self._sse_peak_connections,
                "event_loop_lag_avg_ms": (
                    sum(lag_samples) / len(lag_samples)
                    if lag_samples
                    else None
                ),
                "event_loop_lag_p95_ms": _percentile(lag_samples, 0.95),
                "event_loop_lag_p99_ms": _percentile(lag_samples, 0.99),
                "event_loop_lag_sample_count": len(lag_samples),
                "process_rss_sampled_peak_bytes": self._process_rss_sampled_peak_bytes,
                "performance_metrics": performance_metrics,
                "event_loop_lag_max_ms": (
                    max(lag_samples) if lag_samples else None
                ),
                "event_loop_blocked_count": self._event_loop_blocked_count,
                "process_cpu_avg_percent": (
                    sum(cpu_samples) / len(cpu_samples)
                    if cpu_samples
                    else None
                ),
                "process_cpu_max_percent": (
                    max(cpu_samples) if cpu_samples else None
                ),
            },
        )
        payload.update(self._threadpool_metrics())
        payload.update(self._process_metrics())
        payload.update(self._pod_metrics())
        payload.update(self._workspace_cache_metrics())
        payload.update(self._storage_metrics())
        return payload

    def _emit(self, payload: dict[str, object]) -> None:
        self._log_sink(
            _LOG_PREFIX
            + json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    def emit_registered(self) -> None:
        """Emit one Runtime Instance registration event."""
        self._emit(self._base_payload("instance_registered"))

    def emit_diagnostic(self) -> None:
        """Emit one diagnostic-flow event and rotate its metric window."""
        self._emit(self.build_diagnostic_payload())
        self.rotate_window()

    def emit_deregistered(self) -> None:
        """Emit one Runtime Instance deregistration event."""
        self._emit(self._base_payload("instance_deregistered"))

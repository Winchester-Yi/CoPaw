# -*- coding: utf-8 -*-
"""Tests for process-local runtime diagnostic collection."""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest
from swe.app import runtime_diagnostic
from swe.app.runtime_diagnostic import RuntimeDiagnosticManager


class _ThreadLimiter:
    total_tokens = 64
    borrowed_tokens = 20


class _Process:
    def __init__(self) -> None:
        self.cpu_values = iter([0.0, 25.0])

    def cpu_percent(self) -> float:
        return next(self.cpu_values)

    def memory_info(self):
        return type("Memory", (), {"rss": 100, "vms": 200})()

    def num_threads(self) -> int:
        return 3

    def num_fds(self) -> int:
        return 4

    def create_time(self) -> float:
        return 900.0


class _BrokenProcess(_Process):
    def memory_info(self):
        raise RuntimeError("memory unavailable")


class _PartiallyBrokenProcess(_Process):
    def memory_info(self):
        class _Memory:
            @property
            def rss(self):
                raise RuntimeError("rss unavailable")

            vms = 200

        return _Memory()

    def num_threads(self) -> int:
        raise RuntimeError("threads unavailable")


def _disk_usage(_path: str):
    return type(
        "Disk",
        (),
        {"total": 1000, "used": 600, "free": 400, "percent": 60.0},
    )()


def _payload(message: str) -> dict[str, object]:
    prefix = "RUNTIME_DIAGNOSTIC "
    assert message.startswith(prefix)
    return json.loads(message.removeprefix(prefix))


def _symlink_or_skip(link, target: str) -> None:
    try:
        link.symlink_to(target)
    except OSError as exc:
        if sys.platform == "win32" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is not available")
        raise


def _manager(**overrides) -> RuntimeDiagnosticManager:
    messages: list[str] = overrides.pop("messages", [])
    options = {
        "hostname": "pod-a",
        "wall_time": lambda: 1000.0,
        "process": _Process(),
        "disk_usage": _disk_usage,
        "pod_open_fd_count": lambda: 6,
        "pod_disk_io_bytes": lambda: (100, 200),
        "thread_limiter": _ThreadLimiter,
        "log_sink": messages.append,
    }
    options.update(overrides)
    return RuntimeDiagnosticManager(
        **options,
    )


def test_diagnostic_exposes_bounded_stream_stage_metrics(monkeypatch):
    from swe.performance_metrics import PerformanceMetrics

    metrics = PerformanceMetrics()
    metrics.record_stream_timing("accepted_to_committed", 0.012)
    monkeypatch.setattr(runtime_diagnostic, "METRICS", metrics)
    manager = _manager()
    stage = manager.build_diagnostic_payload()["performance_metrics"][
        "stream_stages"
    ]["accepted_to_committed"]
    assert stage["observed_count"] == 1
    assert stage["sample_count"] == 1
    assert stage["duration_p99_ms"] == 12
    manager.rotate_window()
    assert (
        manager.build_diagnostic_payload()["performance_metrics"][
            "stream_stages"
        ]["accepted_to_committed"]
        == stage
    )


@pytest.mark.asyncio
async def test_diagnostic_records_p99_rss_peak_and_worker_metrics():
    manager = _manager(monotonic_time=lambda: 1.0)
    for value in range(1, 101):
        manager.record_sample(lag_ms=float(value), cpu_percent=None)
    await manager.sample_once(planned_wakeup=1.0)
    payload = manager.build_diagnostic_payload()
    assert payload["event_loop_lag_p95_ms"] == 95.0
    assert payload["event_loop_lag_p99_ms"] == 99.0
    assert payload["event_loop_lag_sample_count"] == 101
    assert payload["process_rss_sampled_peak_bytes"] == 100
    assert set(payload["performance_metrics"]["workers"]) == {
        "output",
        "runtime_state",
        "backup",
        "provider",
        "stream",
    }
    manager.rotate_window()
    assert (
        manager.build_diagnostic_payload()["process_rss_sampled_peak_bytes"]
        is None
    )


def test_lifecycle_events_use_flat_versioned_contract() -> None:
    messages: list[str] = []
    manager = _manager(messages=messages)

    manager.emit_registered()
    manager.emit_deregistered()

    assert _payload(messages[0]) == {
        "schema": "runtime_diagnostic.v1",
        "event_type": "instance_registered",
        "hostname": "pod-a",
        "event_at_ms": 1000000,
    }
    assert _payload(messages[1]) == {
        "schema": "runtime_diagnostic.v1",
        "event_type": "instance_deregistered",
        "hostname": "pod-a",
        "event_at_ms": 1000000,
    }


def test_diagnostic_payload_contains_confirmed_metrics() -> None:
    manager = _manager()
    manager.record_sse_opened()
    manager.record_sse_opened()
    manager.record_sse_closed()
    manager.record_sample(lag_ms=10.0, cpu_percent=20.0)
    manager.record_sample(lag_ms=1200.0, cpu_percent=40.0)

    payload = manager.build_diagnostic_payload()

    assert set(payload.pop("performance_metrics")["workers"]) == {
        "output",
        "runtime_state",
        "backup",
        "provider",
        "stream",
    }
    assert payload == {
        "schema": "runtime_diagnostic.v1",
        "event_type": "diagnostic_flow",
        "hostname": "pod-a",
        "event_at_ms": 1000000,
        "sse_active_connections": 1,
        "sse_peak_connections": 2,
        "event_loop_lag_avg_ms": 605.0,
        "event_loop_lag_p95_ms": 1200.0,
        "event_loop_lag_p99_ms": 1200.0,
        "event_loop_lag_sample_count": 2,
        "process_rss_sampled_peak_bytes": None,
        "event_loop_lag_max_ms": 1200.0,
        "event_loop_blocked_count": 1,
        "process_cpu_avg_percent": 30.0,
        "process_cpu_max_percent": 40.0,
        "anyio_threadpool_total_tokens": 64,
        "anyio_threadpool_borrowed_tokens": 20,
        "anyio_threadpool_available_tokens": 44,
        "process_rss_bytes": 100,
        "process_vms_bytes": 200,
        "process_thread_count": 3,
        "process_open_fd_count": 4,
        "process_uptime_seconds": 100,
        "pod_open_fd_count": 6,
        "pod_disk_read_bytes_per_second": None,
        "pod_disk_read_bytes_per_second_peak": None,
        "pod_disk_write_bytes_per_second": None,
        "pod_disk_write_bytes_per_second_peak": None,
        "workspace_cache_size": None,
        "workspace_start_tasks": None,
        "workspace_cache_max_size": None,
        "workspace_start_max_concurrent": None,
        "workspace_evictions_total": None,
        "workspace_eviction_stop_failures_total": None,
        "storage_total_bytes": 1000,
        "storage_used_bytes": 600,
        "storage_free_bytes": 400,
        "storage_used_percent": 60.0,
    }


def test_diagnostic_payload_includes_workspace_cache_metrics() -> None:
    manager = _manager(
        workspace_metrics=lambda: {
            "workspace_cache_size": 32,
            "workspace_start_tasks": 3,
            "workspace_cache_max_size": 64,
            "workspace_start_max_concurrent": 4,
            "workspace_evictions_total": 12,
            "workspace_eviction_stop_failures_total": 1,
        },
    )

    payload = manager.build_diagnostic_payload()

    assert payload["workspace_cache_size"] == 32
    assert payload["workspace_start_tasks"] == 3
    assert payload["workspace_cache_max_size"] == 64
    assert payload["workspace_start_max_concurrent"] == 4
    assert payload["workspace_evictions_total"] == 12
    assert payload["workspace_eviction_stop_failures_total"] == 1


def test_failed_metric_group_emits_null_fields_without_suppressing_payload() -> (
    None
):
    manager = _manager(
        process=_BrokenProcess(),
        disk_usage=lambda _path: (_ for _ in ()).throw(
            RuntimeError("disk unavailable"),
        ),
    )

    payload = manager.build_diagnostic_payload()

    assert payload["process_rss_bytes"] is None
    assert payload["process_vms_bytes"] is None
    assert payload["process_thread_count"] == 3
    assert payload["process_open_fd_count"] == 4
    assert payload["process_uptime_seconds"] == 100
    assert payload["storage_total_bytes"] is None
    assert payload["storage_used_bytes"] is None
    assert payload["storage_free_bytes"] is None
    assert payload["storage_used_percent"] is None
    assert payload["sse_active_connections"] == 0


def test_failed_threadpool_metrics_emit_null_without_suppressing_payload() -> (
    None
):
    manager = _manager(
        thread_limiter=lambda: (_ for _ in ()).throw(
            RuntimeError("limiter unavailable"),
        ),
    )

    payload = manager.build_diagnostic_payload()

    assert payload["anyio_threadpool_total_tokens"] is None
    assert payload["anyio_threadpool_borrowed_tokens"] is None
    assert payload["anyio_threadpool_available_tokens"] is None
    assert payload["process_rss_bytes"] == 100


def test_collect_inotify_diagnostic_summarizes_proc_fdinfo(tmp_path) -> None:
    proc_dir = tmp_path / "proc" / "123"
    fd_dir = proc_dir / "fd"
    fdinfo_dir = proc_dir / "fdinfo"
    fd_dir.mkdir(parents=True)
    fdinfo_dir.mkdir()
    _symlink_or_skip(fd_dir / "3", "anon_inode:inotify")
    _symlink_or_skip(fd_dir / "4", "socket:[1]")
    (fdinfo_dir / "3").write_text(
        "pos:\t0\n"
        "flags:\t02000000\n"
        "inotify wd:1 ino:abc sdev:01 mask:00000800 ignored_mask:0 "
        "fhandle-bytes:8 fhandle-type:1 f_handle:01020304\n",
        encoding="utf-8",
    )

    process = _Process()
    process.pid = 123
    manager = _manager(process=process)

    payload = manager.collect_inotify_diagnostic(
        proc_root=tmp_path / "proc",
        include_fdinfo=True,
    )

    assert payload["schema"] == "runtime_diagnostic.v1"
    assert payload["event_type"] == "inotify_diagnostic"
    assert payload["pid"] == 123
    assert payload["inotify_fd_count"] == 1
    assert payload["inotify_watch_count"] == 1
    assert payload["errors"] == []
    assert payload["inotify_fds"] == [
        {
            "fd": 3,
            "target": "anon_inode:inotify",
            "watch_count": 1,
            "watches": [
                {
                    "wd": "1",
                    "ino": "abc",
                    "sdev": "01",
                    "mask": "00000800",
                },
            ],
            "fdinfo": (
                "pos:\t0\n"
                "flags:\t02000000\n"
                "inotify wd:1 ino:abc sdev:01 mask:00000800 "
                "ignored_mask:0 fhandle-bytes:8 fhandle-type:1 "
                "f_handle:01020304\n"
            ),
            "fdinfo_truncated": False,
        },
    ]


def test_collect_inotify_diagnostic_counts_before_fdinfo_truncation(
    tmp_path,
) -> None:
    proc_dir = tmp_path / "proc" / "123"
    fd_dir = proc_dir / "fd"
    fdinfo_dir = proc_dir / "fdinfo"
    fd_dir.mkdir(parents=True)
    fdinfo_dir.mkdir()
    _symlink_or_skip(fd_dir / "3", "anon_inode:inotify")
    (fdinfo_dir / "3").write_text(
        "pos:\t0\n"
        "inotify wd:1 ino:abc sdev:01 mask:00000800 ignored_mask:0\n"
        "inotify wd:2 ino:def sdev:01 mask:00000800 ignored_mask:0\n",
        encoding="utf-8",
    )

    process = _Process()
    process.pid = 123
    manager = _manager(process=process)

    payload = manager.collect_inotify_diagnostic(
        proc_root=tmp_path / "proc",
        max_fdinfo_bytes=0,
        include_fdinfo=True,
    )

    assert payload["inotify_fd_count"] == 1
    assert payload["inotify_watch_count"] == 2
    assert payload["inotify_fds"][0]["watch_count"] == 2
    assert payload["inotify_fds"][0]["fdinfo"] == ""
    assert payload["inotify_fds"][0]["fdinfo_truncated"] is True


def test_collect_inotify_diagnostic_can_redact_details(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc_dir = tmp_path / "proc" / "123"
    fd_dir = proc_dir / "fd"
    fdinfo_dir = proc_dir / "fdinfo"
    fd_dir.mkdir(parents=True)
    fdinfo_dir.mkdir()
    _symlink_or_skip(fd_dir / "3", "anon_inode:inotify")
    (fdinfo_dir / "3").write_text(
        "pos:\t0\n"
        "inotify wd:1 ino:abc sdev:01 mask:00000800 ignored_mask:0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        runtime_diagnostic,
        "_WATCHFILES_STACK_EVENTS",
        [{"function": "watch", "paths": ["/secret"], "stack": ["frame"]}],
    )

    process = _Process()
    process.pid = 123
    manager = _manager(process=process)

    payload = manager.collect_inotify_diagnostic(
        proc_root=tmp_path / "proc",
        include_details=False,
    )

    assert payload["inotify_fd_count"] == 1
    assert payload["inotify_watch_count"] == 1
    assert payload["inotify_fds"] == [
        {
            "fd": 3,
            "target": "anon_inode:inotify",
            "watch_count": 1,
        },
    ]
    assert payload["watchfiles_stack_events"] == []


def test_watchfiles_stack_probe_is_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SWE_WATCHFILES_STACK_PROBE_ENABLED", raising=False)
    monkeypatch.setattr(
        runtime_diagnostic,
        "_WATCHFILES_STACK_PROBE_INSTALLED",
        False,
    )
    monkeypatch.setattr(runtime_diagnostic, "_WATCHFILES_STACK_EVENTS", [])

    manager = _manager()

    assert manager._watchfiles_stack_probe_enabled is False


def test_watchfiles_stack_probe_records_watch_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    fake_watchfiles = types.ModuleType("watchfiles")

    def watch(*_args, **_kwargs):
        return iter(())

    class _EmptyAsyncIterator:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    def awatch(*_args, **_kwargs):
        return _EmptyAsyncIterator()

    fake_watchfiles.watch = watch
    fake_watchfiles.awatch = awatch
    monkeypatch.setitem(sys.modules, "watchfiles", fake_watchfiles)
    monkeypatch.setenv("SWE_WATCHFILES_STACK_PROBE_ENABLED", "1")
    monkeypatch.setattr(
        runtime_diagnostic,
        "_WATCHFILES_STACK_PROBE_INSTALLED",
        False,
    )
    monkeypatch.setattr(runtime_diagnostic, "_WATCHFILES_STACK_EVENTS", [])

    manager = _manager()
    fake_watchfiles.watch(tmp_path)
    fake_watchfiles.awatch(tmp_path / "async")

    payload = manager.collect_inotify_diagnostic(proc_root=tmp_path / "proc")

    assert manager._watchfiles_stack_probe_enabled is True
    assert payload["watchfiles_stack_probe_enabled"] is True
    assert [
        event["function"] for event in payload["watchfiles_stack_events"]
    ] == ["watch", "awatch"]
    assert payload["watchfiles_stack_events"][0]["paths"] == [str(tmp_path)]
    assert payload["watchfiles_stack_events"][0]["stack"]


def test_watchfiles_stack_probe_patches_existing_awatch_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    fake_watchfiles = types.ModuleType("watchfiles")

    class _EmptyAsyncIterator:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    def awatch(*_args, **_kwargs):
        return _EmptyAsyncIterator()

    fake_watchfiles.awatch = awatch
    fake_watchfiles.watch = lambda *_args, **_kwargs: iter(())
    fake_reme_watcher = types.ModuleType(
        "reme.core.file_watcher.base_file_watcher",
    )
    fake_reme_watcher.awatch = awatch
    monkeypatch.setitem(sys.modules, "watchfiles", fake_watchfiles)
    monkeypatch.setitem(
        sys.modules,
        "reme.core.file_watcher.base_file_watcher",
        fake_reme_watcher,
    )
    monkeypatch.setenv("SWE_WATCHFILES_STACK_PROBE_ENABLED", "1")
    monkeypatch.setattr(
        runtime_diagnostic,
        "_WATCHFILES_STACK_PROBE_INSTALLED",
        False,
    )
    monkeypatch.setattr(runtime_diagnostic, "_WATCHFILES_STACK_EVENTS", [])

    manager = _manager()
    fake_reme_watcher.awatch(tmp_path / "memory")

    payload = manager.collect_inotify_diagnostic(proc_root=tmp_path / "proc")
    assert payload["watchfiles_stack_probe_enabled"] is True
    assert [
        event["function"] for event in payload["watchfiles_stack_events"]
    ] == ["awatch"]
    assert payload["watchfiles_stack_events"][0]["paths"] == [
        str(tmp_path / "memory"),
    ]


def test_watchfiles_stack_probe_does_not_trigger_module_getattr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    fake_watchfiles = types.ModuleType("watchfiles")
    triggered = False

    def watch(*_args, **_kwargs):
        return iter(())

    fake_watchfiles.watch = watch
    fake_watchfiles.awatch = lambda *_args, **_kwargs: iter(())

    class LazyModule(types.ModuleType):
        def __getattr__(self, _name):
            nonlocal triggered
            triggered = True
            raise RuntimeError("lazy import should not run")

    lazy_module = LazyModule("lazy_watch_module")
    lazy_module.__dict__["watch"] = watch
    monkeypatch.setitem(sys.modules, "watchfiles", fake_watchfiles)
    monkeypatch.setitem(sys.modules, "lazy_watch_module", lazy_module)
    monkeypatch.setenv("SWE_WATCHFILES_STACK_PROBE_ENABLED", "1")
    monkeypatch.setattr(
        runtime_diagnostic,
        "_WATCHFILES_STACK_PROBE_INSTALLED",
        False,
    )
    monkeypatch.setattr(runtime_diagnostic, "_WATCHFILES_STACK_EVENTS", [])

    manager = _manager()
    lazy_module.watch(tmp_path / "memory")

    payload = manager.collect_inotify_diagnostic(proc_root=tmp_path / "proc")
    assert triggered is False
    assert [
        event["function"] for event in payload["watchfiles_stack_events"]
    ] == ["watch"]


def test_failed_process_fields_do_not_suppress_other_process_fields() -> None:
    manager = _manager(process=_PartiallyBrokenProcess())

    payload = manager.build_diagnostic_payload()

    assert payload["process_rss_bytes"] is None
    assert payload["process_vms_bytes"] == 200
    assert payload["process_thread_count"] is None
    assert payload["process_open_fd_count"] == 4
    assert payload["process_uptime_seconds"] == 100


def test_pod_disk_io_reports_latest_and_peak_rates() -> None:
    manager = _manager()
    manager.record_sample(
        lag_ms=1.0,
        cpu_percent=1.0,
        pod_disk_io_bytes=(100, 200),
        sampled_at=10.0,
    )
    manager.record_sample(
        lag_ms=1.0,
        cpu_percent=1.0,
        pod_disk_io_bytes=(300, 500),
        sampled_at=11.0,
    )
    manager.record_sample(
        lag_ms=1.0,
        cpu_percent=1.0,
        pod_disk_io_bytes=(500, 900),
        sampled_at=13.0,
    )

    payload = manager.build_diagnostic_payload()

    assert payload["pod_disk_read_bytes_per_second"] == 100.0
    assert payload["pod_disk_read_bytes_per_second_peak"] == 200.0
    assert payload["pod_disk_write_bytes_per_second"] == 200.0
    assert payload["pod_disk_write_bytes_per_second_peak"] == 300.0


def test_failed_pod_metrics_emit_null_without_suppressing_other_fields() -> (
    None
):
    manager = _manager(
        pod_open_fd_count=lambda: (_ for _ in ()).throw(
            PermissionError("fd unavailable"),
        ),
    )
    manager.record_sample(
        lag_ms=1.0,
        cpu_percent=1.0,
        pod_disk_io_bytes=(100, 200),
        sampled_at=10.0,
    )
    manager.record_sample(
        lag_ms=1.0,
        cpu_percent=1.0,
        pod_disk_io_bytes=None,
        sampled_at=11.0,
    )

    payload = manager.build_diagnostic_payload()

    assert payload["pod_open_fd_count"] is None
    assert payload["pod_disk_read_bytes_per_second"] is None
    assert payload["pod_disk_read_bytes_per_second_peak"] is None
    assert payload["pod_disk_write_bytes_per_second"] is None
    assert payload["pod_disk_write_bytes_per_second_peak"] is None
    assert payload["process_rss_bytes"] == 100


def test_window_rotation_preserves_active_sse_and_clears_samples() -> None:
    manager = _manager()
    manager.record_sse_opened()
    manager.record_sse_opened()
    manager.record_sse_closed()
    manager.record_sample(
        lag_ms=100.0,
        cpu_percent=50.0,
        pod_disk_io_bytes=(100, 200),
        sampled_at=10.0,
    )
    manager.record_sample(
        lag_ms=100.0,
        cpu_percent=50.0,
        pod_disk_io_bytes=(200, 400),
        sampled_at=11.0,
    )

    manager.rotate_window()
    manager.record_sample(
        lag_ms=100.0,
        cpu_percent=50.0,
        pod_disk_io_bytes=(500, 1000),
        sampled_at=13.0,
    )
    payload = manager.build_diagnostic_payload()

    assert payload["sse_active_connections"] == 1
    assert payload["sse_peak_connections"] == 1
    assert payload["event_loop_lag_avg_ms"] == 100.0
    assert payload["event_loop_lag_p95_ms"] == 100.0
    assert payload["event_loop_lag_max_ms"] == 100.0
    assert payload["event_loop_blocked_count"] == 0
    assert payload["process_cpu_avg_percent"] == 50.0
    assert payload["process_cpu_max_percent"] == 50.0
    assert payload["pod_disk_read_bytes_per_second"] == 150.0
    assert payload["pod_disk_read_bytes_per_second_peak"] == 150.0
    assert payload["pod_disk_write_bytes_per_second"] == 300.0
    assert payload["pod_disk_write_bytes_per_second_peak"] == 300.0


@pytest.mark.asyncio
async def test_start_and_stop_emit_lifecycle_events() -> None:
    messages: list[str] = []
    manager = _manager(messages=messages)

    await manager.start()
    await manager.stop()

    assert _payload(messages[0])["event_type"] == "instance_registered"
    assert _payload(messages[-1])["event_type"] == "instance_deregistered"


@pytest.mark.asyncio
async def test_sample_once_records_event_loop_lag_and_process_cpu() -> None:
    manager = _manager(monotonic_time=lambda: 11.25)

    manager.prime_process_cpu()
    await manager.sample_once(planned_wakeup=10.0)
    payload = manager.build_diagnostic_payload()

    assert payload["event_loop_lag_avg_ms"] == 1250.0
    assert payload["event_loop_blocked_count"] == 1
    assert payload["process_cpu_avg_percent"] == 25.0


@pytest.mark.asyncio
async def test_sample_once_latches_pod_disk_io_collection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    exception_logs: list[str] = []

    def unavailable_pod_disk_io_bytes() -> tuple[int, int]:
        nonlocal calls
        calls += 1
        raise OSError("cgroup io counters unavailable")

    monkeypatch.setattr(
        runtime_diagnostic.logger,
        "exception",
        lambda message, *args: exception_logs.append(
            message % args if args else message,
        ),
    )
    manager = _manager(
        monotonic_time=lambda: 11.0,
        pod_disk_io_bytes=unavailable_pod_disk_io_bytes,
    )

    await manager.sample_once(planned_wakeup=10.0)
    await manager.sample_once(planned_wakeup=11.0)

    payload = manager.build_diagnostic_payload()

    assert calls == 1
    assert exception_logs == [
        "Failed to collect runtime diagnostic Pod disk I/O",
    ]
    assert payload["pod_disk_read_bytes_per_second"] is None
    assert payload["pod_disk_read_bytes_per_second_peak"] is None
    assert payload["pod_disk_write_bytes_per_second"] is None
    assert payload["pod_disk_write_bytes_per_second_peak"] is None


@pytest.mark.asyncio
async def test_sampler_loop_uses_ten_second_interval() -> None:
    sleeps: list[float] = []
    planned_wakeups: list[float] = []
    now = 100.0

    async def fake_sleep(delay: float) -> None:
        nonlocal now
        sleeps.append(delay)
        now += delay
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    manager = _manager(
        monotonic_time=lambda: now,
        sleep=fake_sleep,
    )

    async def fake_sample_once(*, planned_wakeup: float) -> None:
        planned_wakeups.append(planned_wakeup)

    manager.sample_once = fake_sample_once  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await manager.run_sampler_loop()

    assert sleeps == [10.0, 10.0, 10.0]
    assert planned_wakeups == [110.0, 120.0]


@pytest.mark.asyncio
async def test_periodic_loop_uses_initial_jitter_then_regular_interval() -> (
    None
):
    sleeps: list[float] = []
    messages: list[str] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    manager = _manager(
        messages=messages,
        sleep=fake_sleep,
        jitter=lambda _start, _end: 7.0,
    )

    with pytest.raises(asyncio.CancelledError):
        await manager.run_periodic_loop()

    assert sleeps == [127.0, 1800.0]
    assert [_payload(message)["event_type"] for message in messages] == [
        "diagnostic_flow",
    ]

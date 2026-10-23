# -*- coding: utf-8 -*-
"""Bounded performance accounting and real execution boundary tests."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest


def test_journal_counters_are_fixed_aggregate_labels():
    from swe.performance_metrics import PerformanceMetrics

    metrics = PerformanceMetrics()
    for key in (
        "journal_append_bytes",
        "journal_read_bytes",
        "journal_frames_decoded",
        "ledger_lookup_read_bytes",
    ):
        metrics.increment(key, 2)
        assert metrics.snapshot()["counters"][key] == 2


def test_stream_stage_samples_are_bounded_and_have_fixed_labels():
    from swe.performance_metrics import PerformanceMetrics

    metrics = PerformanceMetrics()
    for value in range(1, 2101):
        metrics.record_stream_timing("admission", value / 1000)
    stages = metrics.snapshot()["stream_stages"]
    assert set(stages) == {
        "admission",
        "batch_wait",
        "accepted_to_committed",
        "write_queue",
        "write_execution",
        "read_queue",
        "read_execution",
        "read_total",
    }
    result = stages["admission"]
    assert result["observed_count"] == 2100
    assert result["sample_count"] == 2048
    assert result["duration_p95_ms"] == 1998
    assert result["duration_p99_ms"] == 2080
    assert stages["read_total"]["duration_p99_ms"] is None
    with pytest.raises(ValueError):
        metrics.record_stream_timing("tenant-secret", 0)


def test_stream_stage_timings_are_thread_safe():
    from swe.performance_metrics import PerformanceMetrics

    metrics = PerformanceMetrics()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda _: metrics.record_stream_timing(
                    "write_execution",
                    0.002,
                ),
                range(500),
            ),
        )
    result = metrics.snapshot()["stream_stages"]["write_execution"]
    assert result["observed_count"] == 500
    assert result["duration_p99_ms"] == 2


def test_metrics_bound_samples_and_keep_cumulative_counts():
    from swe.performance_metrics import PerformanceMetrics

    metrics = PerformanceMetrics()
    for value in range(1, 2101):
        metrics.record_worker_timing("output", value / 1000, value / 500)
    metrics.increment("checkpoint_read_bytes", 12)
    metrics.increment("checkpoint_read_calls")
    snapshot = metrics.snapshot()
    output = snapshot["workers"]["output"]
    assert output["completed_count"] == 2100
    assert output["sample_count"] == 2048
    assert output["queue_p95_ms"] == 1998
    assert output["queue_p99_ms"] == 2080
    assert output["execution_p99_ms"] == 4160
    assert snapshot["counters"]["checkpoint_read_bytes"] == 12
    assert snapshot["counters"]["checkpoint_read_calls"] == 1


def test_metrics_are_thread_safe_and_have_fixed_labels():
    from swe.performance_metrics import PerformanceMetrics

    metrics = PerformanceMetrics()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(lambda _: metrics.increment("s3_list_pages"), range(500)),
        )
    assert metrics.snapshot()["counters"]["s3_list_pages"] == 500
    with pytest.raises(ValueError):
        metrics.increment("tenant-secret")
    with pytest.raises(ValueError):
        metrics.record_worker_timing("tenant-secret", 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "worker",
    ["output", "runtime_state", "backup", "provider"],
)
async def test_actual_workers_record_execution_and_exception(
    monkeypatch,
    worker,
):
    from swe import performance_metrics

    metrics = performance_metrics.PerformanceMetrics()
    monkeypatch.setattr(performance_metrics, "METRICS", metrics)

    def work():
        time.sleep(0.015)
        raise ValueError("expected failure")

    with pytest.raises(ValueError, match="expected failure"):
        if worker == "output":
            from swe.output_workers import run_output_work

            await run_output_work(work)
        elif worker == "runtime_state":
            from swe.runtime_workers import run_runtime_state_work

            await run_runtime_state_work(work)
        elif worker == "backup":
            from swe.app.backup.io_worker import run_backup_io

            await run_backup_io(work)
        else:
            from swe.providers.provider_runtime_cache import (
                ProviderRuntimeCache,
            )

            cache = ProviderRuntimeCache()
            try:
                await asyncio.wrap_future(cache.submit(work))
            finally:
                cache.init_executor.shutdown()
    result = metrics.snapshot()["workers"][worker]
    assert result["completed_count"] == 1
    assert result["queue_p99_ms"] >= 0
    assert result["execution_p99_ms"] >= 10


@pytest.mark.asyncio
async def test_output_queue_wait_and_cancelled_work_are_measured(monkeypatch):
    from swe import output_workers, performance_metrics

    metrics = performance_metrics.PerformanceMetrics()
    monkeypatch.setattr(performance_metrics, "METRICS", metrics)
    monkeypatch.setattr(
        output_workers,
        "_SLOTS",
        threading.BoundedSemaphore(1),
    )
    started = threading.Event()
    release = threading.Event()

    def work():
        started.set()
        assert release.wait(5)

    first = asyncio.create_task(output_workers.run_output_work(work))
    while not started.is_set():
        await asyncio.sleep(0.001)
    second = asyncio.create_task(output_workers.run_output_work(lambda: None))
    try:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.sleep(0.02)
        assert metrics.snapshot()["workers"]["output"]["completed_count"] == 0
    finally:
        release.set()
    await asyncio.wait_for(second, 5)
    result = metrics.snapshot()["workers"]["output"]
    assert result["completed_count"] == 2
    assert result["queue_p99_ms"] >= 10


def test_provider_startup_is_counted_once_for_shared_future(monkeypatch):
    from swe import performance_metrics
    from swe.providers.provider_runtime_cache import ProviderRuntimeCache

    metrics = performance_metrics.PerformanceMetrics()
    monkeypatch.setattr(performance_metrics, "METRICS", metrics)
    cache = ProviderRuntimeCache()
    try:
        expected = object()
        future = cache.get_or_start_instance("tenant-a", lambda _: expected)
        assert future.result(timeout=5) is expected
        assert (
            cache.get_or_start_instance("tenant-a", lambda _: None).result()
            is expected
        )
        assert (
            metrics.snapshot()["workers"]["provider"]["completed_count"] == 1
        )
    finally:
        cache.init_executor.shutdown()


def test_session_memory_observations_are_bounded_numeric_and_isolated():
    from swe.performance_metrics import PerformanceMetrics

    metrics = PerformanceMetrics()
    for value in range(1, 2101):
        metrics.record_session_memory(value, value * 3, value * 2, value * 5)
    result = metrics.snapshot()["session_memory"]
    assert result["observed_count"] == 2100
    assert result["sample_count"] == 2048
    assert result["session_hot_terminal_count"] == {
        "last": 2100,
        "avg": (53 + 2100) / 2,
        "p95": 1998,
        "max": 2100,
    }
    assert result["session_hot_terminal_bytes"]["last"] == 6300
    assert result["session_protected_count"]["last"] == 4200
    assert result["session_protected_bytes"]["last"] == 10500
    result["session_hot_terminal_count"]["last"] = -1
    assert (
        metrics.snapshot()["session_memory"]["session_hot_terminal_count"][
            "last"
        ]
        == 2100
    )


def test_storage_operation_labels_outcomes_and_windows_are_bounded():
    from swe.performance_metrics import PerformanceMetrics

    metrics = PerformanceMetrics()
    expected = {
        "session_commit",
        "checkpoint_commit",
        "session_recovery",
        "checkpoint_recovery",
        "session_migration",
        "checkpoint_migration",
        "session_migration_preview",
        "checkpoint_migration_preview",
    }
    assert set(metrics.snapshot()["storage_operations"]) == expected
    assert (
        metrics.snapshot()["session_memory"]["session_protected_bytes"]["last"]
        is None
    )
    for value in range(1, 2101):
        metrics.record_storage_operation(
            "session_commit",
            value / 1000,
            "succeeded",
        )
    metrics.record_storage_operation("session_migration", 0.1, "failed")
    metrics.record_storage_operation("session_migration", 0.2, "cancelled")
    result = metrics.snapshot()
    commit = result["storage_operations"]["session_commit"]
    assert commit["attempted_count"] == commit["succeeded_count"] == 2100
    assert commit["sample_count"] == 2048
    assert commit["duration_p95_ms"] == 1998
    assert commit["failed_count"] == commit["cancelled_count"] == 0
    assert result["counters"]["session_migration_failures"] == 1
    assert (
        result["storage_operations"]["session_migration"]["cancelled_count"]
        == 1
    )
    with pytest.raises(ValueError):
        metrics.record_storage_operation("private-tenant-id", 0, "failed")
    with pytest.raises(ValueError):
        metrics.record_storage_operation(
            "session_commit",
            0,
            "private-error-text",
        )


def test_storage_operation_updates_are_thread_safe():
    from swe.performance_metrics import PerformanceMetrics

    metrics = PerformanceMetrics()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda _: metrics.record_storage_operation(
                    "session_recovery",
                    0.01,
                    "failed",
                ),
                range(500),
            ),
        )
    result = metrics.snapshot()
    assert result["counters"]["session_recovery_failures"] == 500
    assert (
        result["storage_operations"]["session_recovery"]["failed_count"] == 500
    )


def test_measured_storage_operation_preserves_results_errors_and_name(
    monkeypatch,
):
    from swe import performance_metrics as module

    metrics = module.PerformanceMetrics()
    monkeypatch.setattr(module, "METRICS", metrics)
    values = iter([1, 1.25, 2, 2.5])
    monkeypatch.setattr(module, "perf_counter", lambda: next(values))

    @module.measure_storage_operation("checkpoint_commit")
    def commit(fail=False):
        if fail:
            raise OSError("disk failure")
        return "receipt"

    assert commit.__name__ == "commit"
    assert commit() == "receipt"
    with pytest.raises(OSError, match="disk failure"):
        commit(fail=True)
    result = metrics.snapshot()["storage_operations"]["checkpoint_commit"]
    assert result["attempted_count"] == 2
    assert result["succeeded_count"] == result["failed_count"] == 1
    assert result["duration_avg_ms"] == 375


@pytest.mark.asyncio
async def test_async_storage_timing_waits_for_real_completion_and_counts_cancellation(
    monkeypatch,
):
    from swe import performance_metrics as module

    metrics = module.PerformanceMetrics()
    monkeypatch.setattr(module, "METRICS", metrics)
    now = [10.0]
    monkeypatch.setattr(module, "perf_counter", lambda: now[0])
    entered, release = asyncio.Event(), asyncio.Event()

    @module.measure_storage_operation(
        "session_migration",
        dry_run_default=True,
    )
    async def migrate(*, dry_run=True):
        entered.set()
        await release.wait()
        return 7

    task = asyncio.create_task(migrate(dry_run=False))
    await entered.wait()
    assert (
        metrics.snapshot()["storage_operations"]["session_migration"][
            "attempted_count"
        ]
        == 0
    )
    now[0] = 15.0
    release.set()
    assert await task == 7
    assert (
        metrics.snapshot()["storage_operations"]["session_migration"][
            "duration_max_ms"
        ]
        == 5000
    )
    entered.clear()
    release.clear()
    task = asyncio.create_task(migrate())
    await entered.wait()
    now[0] = 17.0
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = metrics.snapshot()
    assert (
        result["storage_operations"]["session_migration_preview"][
            "cancelled_count"
        ]
        == 1
    )
    assert result["counters"]["session_migration_preview_failures"] == 0


def test_broken_metrics_cannot_mask_storage_result_or_exception(monkeypatch):
    from swe import performance_metrics as module

    metrics = module.PerformanceMetrics()
    monkeypatch.setattr(module, "METRICS", metrics)
    monkeypatch.setattr(
        metrics,
        "record_storage_operation",
        lambda *a: (_ for _ in ()).throw(RuntimeError("metrics failed")),
    )

    @module.measure_storage_operation("session_commit")
    def commit(fail=False):
        if fail:
            raise OSError("original storage error")
        return 3

    assert commit() == 3
    with pytest.raises(OSError, match="original storage error"):
        commit(True)

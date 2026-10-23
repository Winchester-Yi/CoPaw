# -*- coding: utf-8 -*-
"""Measure actual SSE lifecycle boundaries without performance claims."""

import asyncio
import threading

import pytest

from swe import performance_metrics
from swe.app.runner import sse_replay


@pytest.fixture
def metrics(monkeypatch):
    result = performance_metrics.PerformanceMetrics()
    monkeypatch.setattr(performance_metrics, "METRICS", result)
    return result


@pytest.mark.asyncio
async def test_stage_metrics_follow_successful_commit_and_read(
    monkeypatch,
    metrics,
):
    stream = sse_replay.ReplayStream(
        log=sse_replay.ReplayLog(budget=sse_replay.ReplayBudget()),
    )
    subscriber = stream.subscribe()
    started, release = threading.Event(), threading.Event()
    original = stream.log._append_many

    def delayed(texts):
        started.set()
        assert release.wait(5)
        return original(texts)

    monkeypatch.setattr(stream.log, "_append_many", delayed)
    try:
        await stream.offer("data: first\n\n")
        await stream.offer("data: second\n\n")
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.001)
        stages = metrics.snapshot()["stream_stages"]
        assert stages["admission"]["observed_count"] == 2
        assert stages["batch_wait"]["observed_count"] == 2
        assert stages["accepted_to_committed"]["observed_count"] == 0
        assert stages["write_execution"]["observed_count"] == 0
        release.set()
        await stream.end()
        assert await subscriber.get() == "data: first\n\n"
        assert await subscriber.get() == "data: second\n\n"
        stages = metrics.snapshot()["stream_stages"]
        assert stages["accepted_to_committed"]["observed_count"] == 2
        for phase in (
            "write_queue",
            "write_execution",
            "read_queue",
            "read_execution",
            "read_total",
        ):
            assert stages[phase]["observed_count"] == 1
            assert stages[phase]["duration_p99_ms"] >= 0
    finally:
        release.set()
        await stream.aclose()
    assert stream.log.budget.snapshot()["pending_memory_bytes"] == 0


@pytest.mark.asyncio
async def test_write_stage_queue_includes_submission_permit_wait(
    monkeypatch,
    metrics,
):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()  # pylint: disable=consider-using-with
    monkeypatch.setattr(sse_replay, "_SLOTS", slots)
    log = sse_replay.ReplayLog(budget=sse_replay.ReplayBudget())
    task = asyncio.create_task(log.append("queued"))
    try:
        await asyncio.sleep(0.025)
        slots.release()
        assert await task == 1
        stage = metrics.snapshot()["stream_stages"]["write_queue"]
        assert stage["observed_count"] == 1
        assert stage["duration_p99_ms"] >= 15
    finally:
        if not task.done():
            slots.release()
            await task
        await log.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_write_timing_waits_for_real_worker(
    monkeypatch,
    metrics,
    cancelled,
):
    started, release = threading.Event(), threading.Event()

    def fail(_texts):
        started.set()
        assert release.wait(5)
        raise OSError("disk unavailable")

    stream = sse_replay.ReplayStream(
        log=sse_replay.ReplayLog(budget=sse_replay.ReplayBudget()),
    )
    monkeypatch.setattr(stream.log, "_append_many", fail)
    try:
        await stream.offer("data: failed\n\n")
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.001)
        if cancelled:
            stream.writer.cancel()
            await asyncio.sleep(0)
        assert (
            metrics.snapshot()["stream_stages"]["write_execution"][
                "observed_count"
            ]
            == 0
        )
        release.set()
        await asyncio.gather(stream.writer, return_exceptions=True)
        stages = metrics.snapshot()["stream_stages"]
        assert stages["write_execution"]["observed_count"] == 1
        assert stages["accepted_to_committed"]["observed_count"] == 0
    finally:
        release.set()
        await stream.aclose()
    assert stream.log.budget.snapshot()["pending_memory_bytes"] == 0


@pytest.mark.asyncio
async def test_admission_stage_includes_capacity_wait(monkeypatch, metrics):
    monkeypatch.setattr(sse_replay, "PENDING_RECORDS", 1)
    stream = sse_replay.ReplayStream(
        log=sse_replay.ReplayLog(budget=sse_replay.ReplayBudget()),
    )
    started, release = threading.Event(), threading.Event()
    original = stream.log._append_many

    def delayed(texts):
        started.set()
        assert release.wait(5)
        return original(texts)

    monkeypatch.setattr(stream.log, "_append_many", delayed)
    waiting = None
    try:
        await stream.offer("first")
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.001)
        waiting = asyncio.create_task(stream.offer("second"))
        await asyncio.sleep(0.025)
        assert not waiting.done()
        assert (
            metrics.snapshot()["stream_stages"]["admission"]["observed_count"]
            == 1
        )
        release.set()
        assert await waiting == 2
        await stream.end()
        stage = metrics.snapshot()["stream_stages"]["admission"]
        assert stage["observed_count"] == 2
        assert stage["duration_max_ms"] >= 15
    finally:
        release.set()
        if waiting is not None:
            await waiting
        await stream.aclose()


@pytest.mark.asyncio
async def test_failed_read_is_included_in_read_stage_timings(metrics):
    log = sse_replay.ReplayLog(budget=sse_replay.ReplayBudget())
    try:
        await log.append("original")
        log.file.truncate(1)
        with pytest.raises(sse_replay.ReplayUnavailable):
            await log.read(1)
        stages = metrics.snapshot()["stream_stages"]
        for phase in ("read_queue", "read_execution", "read_total"):
            assert stages[phase]["observed_count"] == 1
    finally:
        await log.aclose()

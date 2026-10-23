# -*- coding: utf-8 -*-
"""Tests for runtime-state worker helpers."""

from __future__ import annotations

import threading
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar

import pytest

from swe.runtime_workers import run_runtime_state_work


@pytest.mark.asyncio
async def test_runtime_state_is_not_queued_behind_default_executor():
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    previous = loop._default_executor
    loop.set_default_executor(executor)
    release = threading.Event()
    started = threading.Event()

    def occupy_default():
        started.set()
        assert release.wait(5)

    blocker = asyncio.create_task(asyncio.to_thread(occupy_default))
    try:
        while not started.is_set():
            await asyncio.sleep(0.001)
        assert (
            await asyncio.wait_for(
                run_runtime_state_work(lambda: "ready"),
                0.1,
            )
            == "ready"
        )
    finally:
        release.set()
        await blocker
        loop._default_executor = previous
        executor.shutdown()


@pytest.mark.asyncio
async def test_cancelled_runtime_work_holds_slot_until_actual_finish(
    monkeypatch,
):
    from swe import runtime_workers

    monkeypatch.setattr(
        runtime_workers,
        "_SLOTS",
        threading.BoundedSemaphore(1),
        raising=False,
    )
    started = threading.Event()
    release = threading.Event()
    second_started = threading.Event()

    def work():
        started.set()
        assert release.wait(5)

    task = asyncio.create_task(runtime_workers.run_runtime_state_work(work))
    second = None
    try:
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        second = asyncio.create_task(
            runtime_workers.run_runtime_state_work(second_started.set),
        )
        await asyncio.sleep(0.02)
        assert not second_started.is_set()
    finally:
        release.set()
        if second is not None:
            await asyncio.wait_for(second, 5)


_scope = ContextVar("scope", default="")


@pytest.mark.asyncio
async def test_run_runtime_state_work_preserves_contextvars() -> None:
    _scope.set("tenant-a.source-b")

    def read_scope() -> str:
        return _scope.get()

    assert await run_runtime_state_work(read_scope) == "tenant-a.source-b"


@pytest.mark.asyncio
async def test_run_runtime_state_work_runs_off_event_loop_thread() -> None:
    event_loop_thread_id = threading.get_ident()

    def read_worker_thread_id() -> int:
        return threading.get_ident()

    assert (
        await run_runtime_state_work(read_worker_thread_id)
        != event_loop_thread_id
    )


@pytest.mark.asyncio
async def test_run_runtime_state_work_passes_args_and_kwargs() -> None:
    def combine(left: str, right: str, *, separator: str) -> str:
        return f"{left}{separator}{right}"

    result = await run_runtime_state_work(
        combine,
        "runtime",
        "state",
        separator="-",
    )

    assert result == "runtime-state"

# -*- coding: utf-8 -*-
import asyncio
from contextvars import ContextVar
import gc
import threading

import pytest


@pytest.mark.asyncio
async def test_output_worker_propagates_context_off_loop():
    from swe.output_workers import run_output_work

    value = ContextVar("output_test_value", default="missing")
    token = value.set("tenant-a")
    try:
        result = await run_output_work(
            lambda: (threading.get_ident(), value.get()),
        )
    finally:
        value.reset(token)
    assert result == (result[0], "tenant-a")
    assert result[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_cancelled_output_work_holds_slot_until_thread_finishes(
    monkeypatch,
):
    from swe import output_workers

    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(output_workers, "_SLOTS", slots)
    started = threading.Event()
    release = threading.Event()
    second_started = threading.Event()

    def first():
        started.set()
        assert release.wait(5)

    task = asyncio.create_task(output_workers.run_output_work(first))
    try:
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        second = asyncio.create_task(
            output_workers.run_output_work(second_started.set),
        )
        await asyncio.sleep(0.02)
        assert not second_started.is_set()
    finally:
        release.set()
    await asyncio.wait_for(second, 5)
    assert second_started.is_set()


@pytest.mark.asyncio
async def test_cancelled_output_failure_is_retrieved(monkeypatch):
    from swe import output_workers

    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(output_workers, "_SLOTS", slots)
    started = threading.Event()
    release = threading.Event()
    failures = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, context: failures.append(context))

    def failing_work():
        started.set()
        assert release.wait(5)
        raise ValueError("cancelled worker failed")

    task = asyncio.create_task(output_workers.run_output_work(failing_work))
    try:
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.wait_for(output_workers.run_output_work(lambda: None), 5)
        await asyncio.sleep(0.01)
        gc.collect()
        assert failures == []
    finally:
        release.set()
        loop.set_exception_handler(previous_handler)

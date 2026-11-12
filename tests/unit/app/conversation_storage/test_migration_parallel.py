# -*- coding: utf-8 -*-
import asyncio
from contextvars import ContextVar
import threading
import time

import pytest

from swe.app.conversation_storage.migration.parallel import (
    MigrationCoordinator,
    MigrationIO,
    MigrationStopped,
)


def test_freshness_reuses_scanned_ancestors_only_within_one_check(
    tmp_path,
    monkeypatch,
):
    from swe.app.conversation_storage.migration import freshness

    child = tmp_path / "workspace"
    child.mkdir()
    payload = child / "state.json"
    payload.write_text("{}")
    cutoff = time.time_ns() + 1_000_000_000
    calls = []
    original = freshness.os.walk

    def walk(root, **kwargs):
        calls.append(root)
        return original(root, **kwargs)

    monkeypatch.setattr(freshness.os, "walk", walk)
    checked = set()
    assert (
        freshness.changed_source([tmp_path], cutoff, checked=checked) is None
    )
    assert freshness.changed_source([child], cutoff, checked=checked) is None
    assert calls == [tmp_path]
    import os

    os.utime(payload, ns=(cutoff + 1, cutoff + 1))
    assert freshness.changed_source([tmp_path], cutoff)[0] == payload


def test_shared_freshness_scans_parent_once_and_retains_change(monkeypatch):
    from swe.app.conversation_storage.migration import inventory_parallel
    from swe.app.conversation_storage.migration.inventory_v3 import (
        _source_change,
    )

    calls = []

    def changed(paths, cutoff, *, recursive=True):
        calls.append((paths, cutoff, recursive))
        return ("/source/new-file", cutoff + 1)

    monkeypatch.setattr(inventory_parallel, "changed_source", changed)
    data = {
        "tenant_sync_started_at_ns": {"SERVICE\x00tenant": 100},
        "workspaces": [
            {
                "service_unit_id": "SERVICE",
                "tenant_id": "tenant",
                "evidence_refs": ["/source/config.json"],
            }
            for _ in range(10000)
        ],
    }
    cache = inventory_parallel._shared_freshness(data)
    assert calls == [(["/source"], 100, False)]
    result = _source_change(
        "workspace",
        ("SERVICE", "tenant"),
        ["/source"],
        100,
        cached=cache,
    )
    assert result["path"] == "/source/new-file"
    assert result["mtime_ns"] == 101
    inventory_parallel._shared_freshness(data)
    assert calls[-1] == (["/source"], 100, False)
    assert len(calls) == 2


async def test_sql_commands_run_in_original_task_with_bounded_workers():
    owner = asyncio.current_task()
    active = peak = 0
    completed = []

    async def commit(value):
        assert asyncio.current_task() is owner
        await asyncio.sleep(0)
        return value * 2

    async def workspace(value, request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        completed.append(await request("commit", value))
        active -= 1

    coordinator = MigrationCoordinator({"commit": commit}, workers=4)
    await coordinator.run(range(100), workspace)
    assert sorted(completed) == [value * 2 for value in range(100)]
    assert peak == 4
    with pytest.raises(MigrationStopped):
        await coordinator.request("commit", 1)


async def test_failure_rejects_waiting_requests_without_deadlock():
    async def commit(value):
        if value == 0:
            raise ValueError("failed transaction")
        pytest.fail("started SQL after failed transaction")

    async def workspace(value, request):
        await request("commit", value)

    coordinator = MigrationCoordinator({"commit": commit}, workers=8)
    with pytest.raises(ValueError, match="failed transaction"):
        await asyncio.wait_for(coordinator.run(range(100), workspace), 1)


async def test_io_has_real_concurrency_and_does_not_copy_sql_context():
    binding = ContextVar("sql-owner", default=None)
    binding.set("must not cross threads")
    barrier = threading.Barrier(2)
    active = peak = 0
    lock = threading.Lock()

    def read():
        nonlocal active, peak
        assert binding.get() is None
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=2)
        with lock:
            active -= 1

    async with MigrationIO(2) as io:
        await asyncio.gather(*(io.call(read) for _ in range(8)))
    assert peak == 2


async def test_cancel_waits_for_real_io_even_with_repeated_cancel():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def write():
        started.set()
        assert release.wait(timeout=3)
        finished.set()

    async with MigrationIO(1) as io:
        task = asyncio.create_task(io.call(write))
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()


async def test_coordinator_cancel_settles_inflight_and_pending_commands():
    entered = asyncio.Event()
    worker_done = []

    async def commit(_value):
        entered.set()
        await asyncio.Event().wait()

    async def workspace(value, request):
        try:
            await request("commit", value)
        finally:
            worker_done.append(value)

    async def execute():
        coordinator = MigrationCoordinator({"commit": commit}, workers=4)
        await coordinator.run(range(100), workspace)

    task = asyncio.create_task(execute())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert sorted(worker_done) == [0, 1, 2, 3]


async def test_single_worker_preserves_order_and_rejects_unknown_command():
    values = []

    async def record(value):
        values.append(value)

    async def workspace(value, request):
        await request("record", value)

    coordinator = MigrationCoordinator({"record": record}, workers=1)
    await coordinator.run(range(20), workspace)
    assert values == list(range(20))
    with pytest.raises(ValueError, match="unknown migration command"):
        await coordinator.request("raw SQL", "DROP TABLE")


async def test_reused_io_thread_has_fresh_context_for_each_call():
    binding = ContextVar("thread-state", default=None)
    async with MigrationIO(1) as io:
        await io.call(binding.set, "must not leak to next job")
        assert await io.call(binding.get) is None


async def test_close_drains_thread_then_propagates_repeated_cancel():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def write():
        started.set()
        assert release.wait(timeout=3)
        finished.set()

    io = MigrationIO(1)
    writer = asyncio.create_task(io.call(write))
    while not started.is_set():
        await asyncio.sleep(0)
    closing = asyncio.create_task(io.close())
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await writer
    assert finished.is_set()


@pytest.mark.parametrize("stop_mode", ("cancel", "failure"))
async def test_full_rpc_slots_cancel_settles_acquirers_queued_and_active(
    stop_mode,
):
    from collections import deque

    entered = asyncio.Event()
    fail = asyncio.Event()
    commands = []
    callers = []
    coordinator = MigrationCoordinator({}, workers=1)

    class ObservedCommands(deque):
        def append(self, x):
            commands.append(x)
            super().append(x)

    coordinator._commands = ObservedCommands()

    async def commit(_value):
        entered.set()
        await fail.wait()
        raise ValueError("active SQL command failed")

    coordinator._handlers["commit"] = commit

    async def workspace(_item, request):
        callers.extend(
            asyncio.create_task(request("commit", i)) for i in range(8)
        )
        await asyncio.gather(*callers)

    running = asyncio.create_task(coordinator.run([1], workspace))
    await entered.wait()
    for _ in range(20):
        if len(coordinator._slots._waiters or ()) == 6:
            break
        await asyncio.sleep(0)
    # One handler active, one command queued, six callers blocked on capacity.
    assert len(commands) == 2
    assert len(coordinator._commands) == 1
    assert len(coordinator._slots._waiters) == 6
    if stop_mode == "cancel":
        running.cancel()
        await asyncio.sleep(0)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, 1)
    else:
        fail.set()
        with pytest.raises(ValueError, match="active SQL command failed"):
            await asyncio.wait_for(running, 1)
    assert all(caller.done() for caller in callers)
    assert all(command.result.done() for command in commands)
    assert not coordinator._commands
    assert not coordinator._slots._waiters
    assert coordinator._slots._value == 2


@pytest.mark.parametrize("stop_mode", ["failure", "cancel"])
async def test_worker_stop_during_health_probe_prevents_queued_commit(
    stop_mode,
):
    from collections import deque

    health_started, release_health, release_worker = (
        asyncio.Event() for _ in range(3)
    )
    queued = asyncio.Event()
    commands, calls, failed_workers = [], [], []
    failure = ValueError("worker failed during health probe")

    class ObservedCommands(deque):
        def append(self, x):
            commands.append(x)
            super().append(x)
            queued.set()

    async def health():
        health_started.set()
        await release_health.wait()

    async def commit():
        calls.append("commit")

    async def workspace(item, request):
        if item == 0:
            failed_workers.append(asyncio.current_task())
            await release_worker.wait()
            if stop_mode == "cancel":
                raise asyncio.CancelledError
            raise failure
        await request("commit")

    coordinator = MigrationCoordinator(
        {"commit": commit},
        workers=2,
        health_check=health,
    )
    coordinator._commands = ObservedCommands()
    running = asyncio.create_task(coordinator.run([0, 1], workspace))
    await asyncio.wait_for(health_started.wait(), 1)
    await asyncio.wait_for(queued.wait(), 1)
    release_worker.set()
    done, _pending = await asyncio.wait(failed_workers, timeout=1)
    assert len(done) == 1
    release_health.set()
    error = asyncio.CancelledError if stop_mode == "cancel" else ValueError
    with pytest.raises(error) as stopped:
        await asyncio.wait_for(running, 1)
    if stop_mode == "failure":
        assert stopped.value is failure
    assert calls == []
    assert all(command.result.done() for command in commands)
    assert not coordinator._commands
    assert coordinator._slots._value == 4


async def test_worker_failure_preserves_inflight_transaction_and_rejects_next():
    entered, release = asyncio.Event(), asyncio.Event()
    failed_workers, committed = [], []
    failure = ValueError("NAS worker failed during current transaction")

    async def commit(value):
        assert value == "accepted", "started another transaction after failure"
        entered.set()
        await release.wait()
        committed.append(value)

    async def workspace(item, request):
        if item == 0:
            await request("commit", "accepted")
        elif item == 1:
            failed_workers.append(asyncio.current_task())
            await entered.wait()
            raise failure
        else:
            await entered.wait()
            await request("commit", "queued")

    coordinator = MigrationCoordinator({"commit": commit}, workers=3)
    running = asyncio.create_task(coordinator.run([0, 1, 2], workspace))
    await asyncio.wait_for(entered.wait(), 1)
    done, _pending = await asyncio.wait(failed_workers, timeout=1)
    assert len(done) == 1
    assert not running.done() and committed == []
    release.set()
    with pytest.raises(ValueError) as stopped:
        await asyncio.wait_for(running, 1)
    assert stopped.value is failure
    assert committed == ["accepted"]
    assert not coordinator._commands
    assert coordinator._slots._value == 6

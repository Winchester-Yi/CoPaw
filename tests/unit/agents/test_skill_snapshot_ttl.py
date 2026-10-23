# -*- coding: utf-8 -*-
"""Fixed freshness observation windows and fail-closed refresh results."""

import json
from pathlib import Path

import pytest

from swe.agents import skill_runtime_snapshot as snapshots
from swe.agents import skills_manager


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(snapshots.time, "monotonic", lambda: now[0])
    return now


def write_workspace(workspace: Path, names=("demo",)):
    for name in names:
        directory = workspace / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: cached\n---\nbody\n",
        )
    (workspace / "skill.json").write_text(
        json.dumps(
            {
                "layout_version": 2,
                "skills": {name: {"enabled": True} for name in names},
            },
        ),
    )


def test_hits_do_no_freshness_io_and_do_not_extend_expiry(
    tmp_path,
    monkeypatch,
    clock,
):
    write_workspace(tmp_path)
    first = snapshots.get_workspace_skill_snapshot(tmp_path)
    original_stat = snapshots._stat
    original_token = skills_manager.get_skill_freshness_token
    with monkeypatch.context() as patch:

        def forbidden(*args, **kwargs):
            pytest.fail("cache hit performed freshness IO")

        patch.setattr(snapshots, "_stat", forbidden)
        patch.setattr(skills_manager, "get_skill_freshness_token", forbidden)
        clock[0] = 599
        assert snapshots.get_workspace_skill_snapshot(tmp_path) is first
    probes = []
    monkeypatch.setattr(
        snapshots,
        "_stat",
        lambda p: (probes.append(p), original_stat(p))[1],
    )
    clock[0] = 600
    monkeypatch.setattr(
        skills_manager,
        "_build_signature",
        lambda p: pytest.fail("unchanged snapshot rehashed"),
    )
    assert snapshots.get_workspace_skill_snapshot(tmp_path) is first
    assert probes
    assert (
        original_token(first.skills["demo"].directory)
        == first.skills["demo"].freshness_token
    )


def test_whole_failure_drops_expired_skills_and_cools_down(
    tmp_path,
    monkeypatch,
    clock,
):
    write_workspace(tmp_path)
    first = snapshots.get_workspace_skill_snapshot(tmp_path)
    (tmp_path / "skill.json").write_text("{invalid")
    clock[0] = 600
    failed = snapshots.get_workspace_skill_snapshot(tmp_path, fail_closed=True)
    assert failed.skills == {}
    assert not failed.manifest_available
    assert failed is not first
    with monkeypatch.context() as patch:
        patch.setattr(
            skills_manager,
            "read_skill_manifest",
            lambda *a, **k: pytest.fail("retry during cooldown"),
        )
        clock[0] = 629
        assert (
            snapshots.get_workspace_skill_snapshot(tmp_path, fail_closed=True)
            is failed
        )
    (tmp_path / "skill.json").write_text(
        json.dumps({"layout_version": 2, "skills": {}}),
    )
    clock[0] = 630
    recovered = snapshots.get_workspace_skill_snapshot(
        tmp_path,
        fail_closed=True,
    )
    assert recovered.manifest_available


def test_partial_failure_retries_missing_skill_after_three_minutes(
    tmp_path,
    monkeypatch,
    clock,
):
    write_workspace(tmp_path, ("a", "b", "c"))
    original = snapshots._build_workspace_skill_runtime_snapshot
    attempts = []

    def build(workspace, name, entry, **kwargs):
        attempts.append(name)
        if name == "c" and clock[0] < 180:
            raise OSError("temporarily unreadable")
        return original(workspace, name, entry, **kwargs)

    monkeypatch.setattr(
        snapshots,
        "_build_workspace_skill_runtime_snapshot",
        build,
    )
    partial = snapshots.get_workspace_skill_snapshot(tmp_path)
    assert set(partial.skills) == {"a", "b"}
    assert partial.unconfirmed_skills == frozenset({"c"})
    count = len(attempts)
    clock[0] = 179
    assert snapshots.get_workspace_skill_snapshot(tmp_path) is partial
    assert len(attempts) == count
    clock[0] = 180
    complete = snapshots.get_workspace_skill_snapshot(tmp_path)
    assert set(complete.skills) == {"a", "b", "c"}
    assert not complete.unconfirmed_skills


def test_runtime_snapshot_never_invokes_security_scanner(
    tmp_path,
    monkeypatch,
):
    from swe.security import skill_scanner

    write_workspace(tmp_path)
    monkeypatch.setattr(
        skill_scanner,
        "scan_skill_directory",
        lambda *a, **k: pytest.fail("runtime security scan"),
    )
    assert "demo" in snapshots.get_workspace_skill_snapshot(tmp_path).skills


def test_manifest_change_is_deferred_then_rebuilt(tmp_path, clock):
    write_workspace(tmp_path)
    first = snapshots.get_workspace_skill_snapshot(tmp_path)
    (tmp_path / "skill.json").write_text(
        json.dumps(
            {
                "layout_version": 2,
                "skills": {"demo": {"enabled": False}},
            },
        ),
    )
    clock[0] = 599
    assert snapshots.get_workspace_skill_snapshot(tmp_path) is first
    clock[0] = 600
    second = snapshots.get_workspace_skill_snapshot(tmp_path)
    assert second.skills == {}
    assert second.manifest_available
    clock[0] = 1199
    assert snapshots.get_workspace_skill_snapshot(tmp_path) is second


def test_token_failure_on_expiry_only_excludes_failed_skill(
    tmp_path,
    monkeypatch,
    clock,
):
    write_workspace(tmp_path, ("a", "b", "c"))
    snapshots.get_workspace_skill_snapshot(tmp_path)
    original = skills_manager.get_skill_freshness_token

    def token(directory):
        if directory.name == "c":
            raise PermissionError("unreadable")
        return original(directory)

    monkeypatch.setattr(skills_manager, "get_skill_freshness_token", token)
    clock[0] = 600
    partial = snapshots.get_workspace_skill_snapshot(
        tmp_path,
        fail_closed=True,
    )
    assert set(partial.skills) == {"a", "b"}
    assert partial.unconfirmed_skills == frozenset({"c"})


def test_concurrent_refresh_runs_once_without_blocking_other_workspaces(
    tmp_path,
    monkeypatch,
    clock,
):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    write_workspace(first_dir)
    write_workspace(second_dir)
    snapshots.get_workspace_skill_snapshot(first_dir)
    clock[0] = 600
    started = threading.Event()
    release = threading.Event()
    original = snapshots._fresh
    probes = []

    def fresh(snapshot, manifest):
        probes.append(snapshot.workspace_dir)
        started.set()
        assert release.wait(3)
        return original(snapshot, manifest)

    monkeypatch.setattr(snapshots, "_fresh", fresh)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(snapshots.get_workspace_skill_snapshot, first_dir)
            for _ in range(3)
        ]
        assert started.wait(3)
        try:
            other = pool.submit(
                snapshots.get_workspace_skill_snapshot,
                second_dir,
            )
            assert "demo" in other.result(timeout=3).skills
        finally:
            release.set()
        results = [future.result(timeout=3) for future in futures]
    assert all(snapshot is results[0] for snapshot in results)
    assert len(probes) == 1


@pytest.mark.asyncio
async def test_async_hit_never_submits_thread_work(
    tmp_path,
    monkeypatch,
    clock,
):
    write_workspace(tmp_path)
    first = await snapshots.get_workspace_skill_snapshot_async(tmp_path)
    monkeypatch.setattr(
        snapshots.asyncio,
        "to_thread",
        lambda *a, **k: pytest.fail("thread submitted for hot cache"),
    )
    monkeypatch.setattr(
        snapshots._REFRESH_EXECUTOR,
        "submit",
        lambda *a, **k: pytest.fail("executor submitted for hot cache"),
    )
    assert (
        await snapshots.get_workspace_skill_snapshot_async(tmp_path) is first
    )


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_workspace_refresh(
    tmp_path,
    monkeypatch,
):
    import asyncio
    import threading

    write_workspace(tmp_path)
    started = threading.Event()
    release = threading.Event()
    original = snapshots._build_workspace_skill_runtime_snapshot
    calls = []

    def build(*args, **kwargs):
        calls.append(1)
        started.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        snapshots,
        "_build_workspace_skill_runtime_snapshot",
        build,
    )
    first = asyncio.create_task(
        snapshots.get_workspace_skill_snapshot_async(tmp_path),
    )
    try:
        assert await asyncio.to_thread(started.wait, 3)
        second = asyncio.create_task(
            snapshots.get_workspace_skill_snapshot_async(tmp_path),
        )
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    finally:
        release.set()
    snapshot = await asyncio.wait_for(second, timeout=3)
    assert "demo" in snapshot.skills
    assert len(calls) == 1


def test_continuously_changing_skill_is_unconfirmed(
    tmp_path,
    monkeypatch,
    clock,
):
    write_workspace(tmp_path, ("stable", "changing"))
    original = skills_manager.get_skill_freshness_token
    calls = []

    def token(directory):
        if directory.name == "changing":
            calls.append(1)
            return f"changing:{len(calls)}"
        return original(directory)

    monkeypatch.setattr(skills_manager, "get_skill_freshness_token", token)
    snapshot = snapshots.get_workspace_skill_snapshot(tmp_path)
    assert set(snapshot.skills) == {"stable"}
    assert snapshot.unconfirmed_skills == frozenset({"changing"})
    assert len(calls) == 4
    assert snapshots._CACHE[tmp_path.resolve()].expires_at == 180


@pytest.mark.asyncio
async def test_async_waiters_share_refresh_without_occupying_worker_threads(
    tmp_path,
    monkeypatch,
):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    import threading

    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    write_workspace(first_dir)
    write_workspace(second_dir)
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    original = snapshots._build_workspace_skill_runtime_snapshot

    def build(workspace, *args, **kwargs):
        if workspace == first_dir.resolve():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3)
        return original(workspace, *args, **kwargs)

    with ThreadPoolExecutor(max_workers=2) as pool:

        monkeypatch.setattr(snapshots, "_REFRESH_EXECUTOR", pool)
        monkeypatch.setattr(
            snapshots,
            "_build_workspace_skill_runtime_snapshot",
            build,
        )
        requests = [
            asyncio.create_task(
                snapshots.get_workspace_skill_snapshot_async(first_dir),
            )
            for _ in range(3)
        ]
        await asyncio.wait_for(entered.wait(), 3)
        other = asyncio.create_task(
            snapshots.get_workspace_skill_snapshot_async(second_dir),
        )
        try:
            try:
                result = await asyncio.wait_for(asyncio.shield(other), 0.5)
            except asyncio.TimeoutError:
                result = None
        finally:
            release.set()
            await asyncio.gather(*requests, other)
        assert (
            result is not None
        ), "same-workspace waiters exhausted the worker pool"


def test_queued_refresh_survives_owner_event_loop_shutdown(
    tmp_path,
    monkeypatch,
):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    import threading

    write_workspace(tmp_path)
    release = threading.Event()
    occupied = threading.Event()
    cancelled = threading.Event()
    errors = []

    def occupy_worker():
        occupied.set()
        assert release.wait(3)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(occupy_worker)
        assert occupied.wait(3)
        monkeypatch.setattr(
            snapshots,
            "_REFRESH_EXECUTOR",
            pool,
            raising=False,
        )

        async def owner_loop():
            asyncio.get_running_loop().set_default_executor(pool)
            task = asyncio.create_task(
                snapshots.get_workspace_skill_snapshot_async(tmp_path),
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        def run_owner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(owner_loop())
                # Finish cancellation of queued owner-loop Tasks before the
                # worker is released; otherwise queue startup can win the race.
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True),
                )
            except Exception as exc:
                errors.append(exc)
            finally:
                loop.close()
                asyncio.set_event_loop(None)
                cancelled.set()

        thread = threading.Thread(target=run_owner)
        thread.start()
        try:
            assert cancelled.wait(3)
        finally:
            release.set()
        thread.join(3)
        assert not thread.is_alive()
        assert not errors

        async def next_loop():
            return await asyncio.wait_for(
                snapshots.get_workspace_skill_snapshot_async(tmp_path),
                0.5,
            )

        snapshot = asyncio.run(next_loop())
        assert "demo" in snapshot.skills


@pytest.mark.asyncio
async def test_async_refresh_preserves_request_context(tmp_path, monkeypatch):
    from contextvars import ContextVar

    value = ContextVar("freshness_request_test", default=None)
    write_workspace(tmp_path)
    original = snapshots._build_workspace_skill_runtime_snapshot
    observed = []

    def build(*args, **kwargs):
        observed.append(value.get())
        return original(*args, **kwargs)

    monkeypatch.setattr(
        snapshots,
        "_build_workspace_skill_runtime_snapshot",
        build,
    )
    token = value.set("alice")
    try:
        await snapshots.get_workspace_skill_snapshot_async(tmp_path)
    finally:
        value.reset(token)
    assert observed == ["alice"]

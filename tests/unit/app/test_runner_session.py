# -*- coding: utf-8 -*-
import asyncio
import builtins
import errno
import json
import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from swe.app.runner import session as session_module
from swe.app.runner.session import SafeJSONSession, SessionExecution
from swe.app.runner.session_lock import SessionLockTimeout
from swe.app.runner.turn_ledger import (
    atomic_json as write_atomic_json,
    sync_directory,
)


def _write_json_text(path: str, content: str) -> None:
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    Path(path).write_text(content, encoding="utf-8")


def _mutate_session_state_in_process(
    save_dir: str,
    session_id: str,
    key: str,
    value: object,
    started_event,
    wait_for_event,
    delay_seconds: float,
) -> None:
    async def _run() -> None:
        session = SafeJSONSession(save_dir=save_dir)

        def _mutate(states: dict[str, object]) -> dict[str, object]:
            states[key] = value
            if started_event is not None:
                started_event.set()
            if delay_seconds:
                time.sleep(delay_seconds)
            return states

        if wait_for_event is not None and not wait_for_event.wait(timeout=5):
            raise TimeoutError("timed out waiting for session mutation")
        await session.mutate_session_state(session_id, _mutate)

    asyncio.run(_run())


def _hold_session_execution_in_process(
    save_dir: str,
    session_id: str,
    entered_event,
    release_event,
) -> None:
    async def _run() -> None:
        session = SafeJSONSession(save_dir=save_dir)
        async with session.execution(session_id):
            entered_event.set()
            if not release_event.wait(timeout=5):
                raise TimeoutError("timed out waiting to release execution")

    asyncio.run(_run())


def _try_session_execution_in_process(
    save_dir: str,
    session_id: str,
    result_queue,
) -> None:
    async def _run() -> None:
        session = SafeJSONSession(save_dir=save_dir)
        try:
            async with session.execution(session_id, timeout_seconds=0.1):
                result_queue.put("entered")
        except TimeoutError:
            result_queue.put("timed_out")

    asyncio.run(_run())


class _GuardedReadableFile:
    def __init__(self, file, state: dict[str, bool], operations: list[str]):
        self._file = file
        self._state = state
        self._operations = operations

    def read(self, *args, **kwargs):
        assert self._state["in_worker"], "session read ran outside worker"
        self._operations.append("read")
        return self._file.read(*args, **kwargs)

    def __enter__(self):
        self._file.__enter__()
        return self

    def __exit__(self, *args):
        return self._file.__exit__(*args)

    def __iter__(self):
        return iter(self._file)

    def __getattr__(self, name: str):
        return getattr(self._file, name)


def test_session_write_locks_are_scoped_to_event_loop_and_normalized_path(
    tmp_path: Path,
) -> None:
    async def get_locks() -> tuple[asyncio.Lock, asyncio.Lock]:
        direct_path = str(tmp_path / "session.json")
        equivalent_path = str(tmp_path / "nested" / ".." / "session.json")
        return (
            session_module._get_session_write_lock(direct_path),
            session_module._get_session_write_lock(equivalent_path),
        )

    first_loop_locks = asyncio.run(get_locks())
    second_loop_locks = asyncio.run(get_locks())

    assert first_loop_locks[0] is first_loop_locks[1]
    assert second_loop_locks[0] is second_loop_locks[1]
    assert first_loop_locks[0] is not second_loop_locks[0]


@pytest.mark.asyncio
async def test_execution_holds_one_file_lock_and_commits_revision(
    tmp_path: Path,
) -> None:
    session = SafeJSONSession(save_dir=str(tmp_path))

    async with session.execution("session-1") as transaction:
        assert await transaction.read_state() == {}
        assert transaction.revision == 0
        await transaction.commit_state({"agent": {"memory": {"content": []}}})

    saved = await session.get_session_state_dict("session-1")
    assert saved["schema_version"] == 2
    assert saved["revision"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["timeout", "cancel"])
async def test_interrupted_commit_waits_for_write_before_releasing_execution_lock(
    monkeypatch,
    tmp_path: Path,
    interruption: str,
) -> None:
    """A cancelled worker write must not let a second execution overtake it."""
    session = SafeJSONSession(save_dir=str(tmp_path))
    write_started = threading.Event()
    release_write = threading.Event()
    holder_finished = asyncio.Event()
    second_entered = asyncio.Event()
    original_write = session_module.atomic_json

    def blocking_write(path: str, content: str) -> None:
        write_started.set()
        if not release_write.wait(timeout=5):
            raise TimeoutError(
                "test did not release the blocked session write",
            )
        original_write(path, content)

    monkeypatch.setattr(session_module, "atomic_json", blocking_write)

    async def hold_interrupted_commit() -> None:
        async with session.execution("shared") as transaction:
            if interruption == "timeout":
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        transaction.commit_state({"agent": {}}),
                        timeout=0.01,
                    )
            else:
                commit_task = asyncio.create_task(
                    transaction.commit_state({"agent": {}}),
                )
                await asyncio.wait_for(
                    asyncio.to_thread(write_started.wait),
                    timeout=1,
                )
                commit_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await commit_task
            holder_finished.set()

    async def enter_second_execution() -> None:
        async with session.execution("shared"):
            second_entered.set()

    holder = asyncio.create_task(hold_interrupted_commit())
    contender: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(
            asyncio.to_thread(write_started.wait),
            timeout=1,
        )
        contender = asyncio.create_task(enter_second_execution())
        await asyncio.sleep(0.05)
        assert not holder_finished.is_set()
        assert not second_entered.is_set()
    finally:
        release_write.set()
        await asyncio.wait_for(holder, timeout=1)
        if contender is not None:
            await asyncio.wait_for(contender, timeout=1)


@pytest.mark.asyncio
async def test_cancelled_commit_updates_revision_before_next_commit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    session = SafeJSONSession(save_dir=str(tmp_path))
    write_started = threading.Event()
    release_write = threading.Event()
    original_write = session_module.atomic_json

    def blocking_first_write(path: str, content: str) -> None:
        if not write_started.is_set():
            write_started.set()
            if not release_write.wait(timeout=5):
                raise TimeoutError(
                    "test did not release the blocked session write",
                )
        original_write(path, content)

    monkeypatch.setattr(
        session_module,
        "atomic_json",
        blocking_first_write,
    )

    async with session.execution("shared") as transaction:
        first_commit = asyncio.create_task(
            transaction.commit_state({"agent": {"first": True}}),
        )
        await asyncio.wait_for(
            asyncio.to_thread(write_started.wait),
            timeout=1,
        )
        first_commit.cancel()
        release_write.set()
        with pytest.raises(asyncio.CancelledError):
            await first_commit

        await transaction.commit_state({"agent": {"second": True}})
        assert transaction.revision == 2

    saved = await session.get_session_state_dict("shared")
    assert saved["revision"] == 2
    assert saved["agent"] == {"second": True}


@pytest.mark.asyncio
async def test_second_execution_times_out_until_first_exits(
    tmp_path: Path,
) -> None:
    session = SafeJSONSession(save_dir=str(tmp_path))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_lock() -> None:
        async with session.execution("shared"):
            entered.set()
            await release.wait()

    holder = asyncio.create_task(hold_lock())
    await entered.wait()
    with pytest.raises(TimeoutError):
        async with session.execution("shared", timeout_seconds=0.01):
            pass
    release.set()
    await holder


@pytest.mark.asyncio
async def test_short_lock_apis_reject_the_active_execution_session(
    tmp_path: Path,
) -> None:
    class StateModule:
        def state_dict(self) -> dict[str, bool]:
            return {"saved": True}

    session = SafeJSONSession(save_dir=str(tmp_path))

    async with session.execution("shared"):
        for operation in (
            lambda: session.load_session_state("shared"),
            lambda: session.save_session_state("shared", agent=StateModule()),
            lambda: session.mutate_session_state(
                "shared",
                lambda state: state,
            ),
        ):
            with pytest.raises(RuntimeError, match="active session execution"):
                await asyncio.wait_for(operation(), timeout=0.1)

        assert await session.mutate_session_state(
            "other",
            lambda state: {**state, "allowed": True},
        ) == {"allowed": True}


@pytest.mark.asyncio
async def test_child_task_in_active_execution_rejects_short_lock_api(
    tmp_path: Path,
) -> None:
    session = SafeJSONSession(save_dir=str(tmp_path))

    async def mutate_current_session() -> None:
        await session.mutate_session_state("shared", lambda state: state)

    async with session.execution("shared"):
        child_task = asyncio.create_task(mutate_current_session())
        with pytest.raises(RuntimeError, match="active session execution"):
            await asyncio.wait_for(child_task, timeout=0.1)


@pytest.mark.asyncio
async def test_second_execution_without_timeout_waits_for_first_to_exit(
    tmp_path: Path,
) -> None:
    session = SafeJSONSession(save_dir=str(tmp_path))
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def hold_first_execution() -> None:
        async with session.execution("shared"):
            first_entered.set()
            await release_first.wait()

    async def enter_second_execution() -> None:
        async with session.execution("shared"):
            second_entered.set()

    first_task = asyncio.create_task(hold_first_execution())
    await first_entered.wait()
    second_task = asyncio.create_task(enter_second_execution())
    await asyncio.sleep(0)
    assert not second_entered.is_set()

    release_first.set()
    await first_task
    await asyncio.wait_for(second_task, timeout=1)
    assert second_entered.is_set()


@pytest.mark.asyncio
async def test_execution_reentry_does_not_release_the_outer_lock(
    tmp_path: Path,
) -> None:
    session = SafeJSONSession(save_dir=str(tmp_path))
    transaction = SessionExecution(session._get_save_path("shared", ""))

    async def reenter_transaction() -> None:
        async with transaction:
            pass

    start_contender = asyncio.Event()

    async def enter_competing_execution() -> None:
        await start_contender.wait()
        async with session.execution("shared", timeout_seconds=0.01):
            pass

    contender_task = asyncio.create_task(enter_competing_execution())

    async with transaction:
        with pytest.raises(RuntimeError, match="already active"):
            await asyncio.wait_for(reenter_transaction(), timeout=0.1)
        with pytest.raises(TimeoutError):
            start_contender.set()
            await asyncio.wait_for(contender_task, timeout=0.1)

    async with session.execution("shared", timeout_seconds=0.1):
        pass


@pytest.mark.asyncio
async def test_child_task_can_use_short_lock_after_execution_exits(
    tmp_path: Path,
) -> None:
    session = SafeJSONSession(save_dir=str(tmp_path))
    start_short_lock = asyncio.Event()

    async def mutate_current_session() -> dict[str, bool]:
        await start_short_lock.wait()
        return await session.mutate_session_state(
            "shared",
            lambda state: {**state, "mutated": True},
        )

    async with session.execution("shared"):
        child_task = asyncio.create_task(mutate_current_session())

    start_short_lock.set()
    assert await child_task == {"mutated": True}


def test_parent_directory_sync_propagates_open_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def deny_open(*args, **kwargs):
        raise PermissionError(errno.EACCES, "permission denied")

    monkeypatch.setattr(session_module.os, "open", deny_open)

    with pytest.raises(PermissionError, match="permission denied"):
        sync_directory(tmp_path)


def test_parent_directory_sync_propagates_supported_fsync_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directory_fd = os.open(tmp_path, os.O_RDONLY)

    monkeypatch.setattr(session_module.os, "open", lambda *args: directory_fd)

    def fail_fsync(file_descriptor: int) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(session_module.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="I/O error"):
        sync_directory(tmp_path)


@pytest.mark.asyncio
async def test_execution_upgrades_legacy_state_from_default_revision(
    tmp_path: Path,
) -> None:
    (tmp_path / "session-1.json").write_text(
        '{"agent": {"memory": {"content": ["old"]}}}',
        encoding="utf-8",
    )
    session = SafeJSONSession(save_dir=str(tmp_path))

    async with session.execution("session-1") as transaction:
        state = await transaction.read_state()
        assert transaction.schema_version == 1
        assert transaction.revision == 0
        state["agent"]["memory"]["content"].append("new")
        await transaction.commit_state(state)

    assert await session.get_session_state_dict("session-1") == {
        "agent": {"memory": {"content": ["old", "new"]}},
        "schema_version": 2,
        "revision": 1,
    }


@pytest.mark.asyncio
async def test_session_file_lock_timeout_maps_to_builtin_timeout_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class BusyFileLock:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            raise SessionLockTimeout("busy")

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            pass

    monkeypatch.setattr(session_module, "AsyncSessionFileLock", BusyFileLock)
    session = SafeJSONSession(save_dir=str(tmp_path))

    with pytest.raises(TimeoutError, match="busy"):
        async with session.session_write_lock(
            "shared",
            timeout_seconds=0.01,
        ):
            pass


@pytest.mark.asyncio
async def test_session_load_uses_runtime_state_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "session-1.json"
    path.write_text('{"message": "你好"}', encoding="utf-8")
    calls: list[str] = []
    operations: list[str] = []
    state = {"in_worker": False}
    original_open = builtins.open
    original_loads = json.loads

    async def fake_worker(func, /, *args, **kwargs):
        calls.append(func.__name__)
        assert not state["in_worker"]
        state["in_worker"] = True
        try:
            return func(*args, **kwargs)
        finally:
            state["in_worker"] = False

    def guarded_open(*args, **kwargs):
        # pylint: disable=consider-using-with
        file = original_open(*args, **kwargs)
        if args and str(args[0]) == str(path):
            return _GuardedReadableFile(file, state, operations)
        return file

    def guarded_loads(*args, **kwargs):
        assert state["in_worker"], "json.loads ran outside worker"
        operations.append("parse")
        return original_loads(*args, **kwargs)

    monkeypatch.setattr(
        session_module,
        "run_runtime_state_work",
        fake_worker,
        raising=False,
    )
    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(session_module.json, "loads", guarded_loads)

    session = SafeJSONSession(save_dir=str(tmp_path))

    assert await session.get_session_state_dict("session-1") == {
        "message": "你好",
    }
    assert calls == ["_read_json_state_sync"]
    assert operations == ["read", "parse"]


@pytest.mark.asyncio
async def test_session_save_serializes_inside_runtime_state_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class StateModule:
        def state_dict(self) -> dict:
            return {"value": "saved"}

    (tmp_path / "session-2.json").write_text(
        '{"existing": true}',
        encoding="utf-8",
    )
    calls: list[str] = []
    operations: list[str] = []
    state = {"in_worker": False}
    original_loads = json.loads
    original_iterencode = json.JSONEncoder.iterencode

    async def fake_worker(func, /, *args, **kwargs):
        calls.append(func.__name__)
        assert not state["in_worker"]
        state["in_worker"] = True
        try:
            return func(*args, **kwargs)
        finally:
            state["in_worker"] = False

    def guarded_loads(*args, **kwargs):
        assert state["in_worker"], "json.loads ran outside worker"
        operations.append("parse")
        return original_loads(*args, **kwargs)

    def guarded_iterencode(*args, **kwargs):
        assert state["in_worker"], "JSONEncoder.iterencode ran outside worker"
        operations.append("encode")
        return original_iterencode(*args, **kwargs)

    def guarded_write(path: str, content: str) -> None:
        assert state["in_worker"], "_write_json_text ran outside worker"
        operations.append("write")
        write_atomic_json(Path(path), content)

    monkeypatch.setattr(
        session_module,
        "run_runtime_state_work",
        fake_worker,
        raising=False,
    )
    monkeypatch.setattr(session_module.json, "loads", guarded_loads)
    monkeypatch.setattr(json.JSONEncoder, "iterencode", guarded_iterencode)
    monkeypatch.setattr(session_module, "atomic_json", guarded_write)

    session = SafeJSONSession(save_dir=str(tmp_path))

    await session.save_merged_state("session-1", state={"message": "你好"})
    await session.save_session_state("session-2", agent=StateModule())

    assert calls == [
        "_read_existing_state_for_save_sync",
        "_write_json_state_sync",
        "_read_existing_state_for_save_sync",
        "_write_json_state_sync",
    ]
    assert operations == ["write", "encode", "parse", "write", "encode"]
    assert original_loads((tmp_path / "session-1.json").read_text()) == {
        "message": "你好",
    }
    assert original_loads((tmp_path / "session-2.json").read_text()) == {
        "existing": True,
        "agent": {"value": "saved"},
    }


@pytest.mark.asyncio
async def test_mutate_session_state_uses_runtime_state_worker_for_json_io(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "session-1.json"
    path.write_text('{"existing": true}', encoding="utf-8")
    calls: list[str] = []
    operations: list[str] = []
    state = {"in_worker": False}
    original_open = builtins.open
    original_loads = json.loads
    original_iterencode = json.JSONEncoder.iterencode

    async def fake_worker(func, /, *args, **kwargs):
        calls.append(func.__name__)
        assert not state["in_worker"]
        state["in_worker"] = True
        try:
            return func(*args, **kwargs)
        finally:
            state["in_worker"] = False

    def guarded_open(*args, **kwargs):
        # pylint: disable=consider-using-with
        file = original_open(*args, **kwargs)
        if args and str(args[0]) == str(path):
            return _GuardedReadableFile(file, state, operations)
        return file

    def guarded_loads(*args, **kwargs):
        assert state["in_worker"], "json.loads ran outside worker"
        operations.append("parse")
        return original_loads(*args, **kwargs)

    def guarded_iterencode(*args, **kwargs):
        assert state["in_worker"], "JSONEncoder.iterencode ran outside worker"
        operations.append("encode")
        return original_iterencode(*args, **kwargs)

    def guarded_write(write_path: str, content: str) -> None:
        assert state["in_worker"], "_write_json_text ran outside worker"
        operations.append("write")
        write_atomic_json(Path(write_path), content)

    def mutate(states: dict[str, object]) -> None:
        assert not state["in_worker"], "mutator should stay on event loop"
        states["mutated"] = True

    monkeypatch.setattr(
        session_module,
        "run_runtime_state_work",
        fake_worker,
        raising=False,
    )
    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(session_module.json, "loads", guarded_loads)
    monkeypatch.setattr(json.JSONEncoder, "iterencode", guarded_iterencode)
    monkeypatch.setattr(session_module, "atomic_json", guarded_write)

    session = SafeJSONSession(save_dir=str(tmp_path))

    assert await session.mutate_session_state("session-1", mutate) == {
        "existing": True,
        "mutated": True,
    }
    assert calls == ["_read_json_state_sync", "_write_json_state_sync"]
    assert operations == ["read", "parse", "write", "encode"]


@pytest.mark.asyncio
async def test_session_write_runs_off_event_loop_thread(
    monkeypatch,
    tmp_path: Path,
) -> None:
    write_started = threading.Event()
    allow_write = threading.Event()
    write_thread_ids: list[int] = []
    event_loop_thread_id = threading.get_ident()

    def slow_write(path: str, content: str) -> None:
        write_thread_ids.append(threading.get_ident())
        write_started.set()
        assert allow_write.wait(timeout=2)
        _write_json_text(path, content)

    monkeypatch.setattr(
        session_module,
        "atomic_json",
        slow_write,
        raising=False,
    )
    session = SafeJSONSession(save_dir=str(tmp_path))

    save_task = asyncio.create_task(
        session.save_merged_state("session-1", state={"message": "你好"}),
    )

    assert await asyncio.to_thread(write_started.wait, 1)
    assert not save_task.done()
    assert write_thread_ids == [write_thread_ids[0]]
    assert write_thread_ids[0] != event_loop_thread_id

    allow_write.set()
    await save_task
    assert json.loads(
        (tmp_path / "session-1.json").read_text(encoding="utf-8"),
    ) == {
        "message": "你好",
    }


@pytest.mark.asyncio
async def test_same_path_writes_serialize_across_session_objects(
    monkeypatch,
    tmp_path: Path,
) -> None:
    first_write_started = threading.Event()
    allow_first_write = threading.Event()
    calls: list[str] = []
    calls_guard = threading.Lock()

    def controlled_write(path: str, content: str) -> None:
        with calls_guard:
            calls.append(content)
            call_number = len(calls)
        if call_number == 1:
            first_write_started.set()
            assert allow_first_write.wait(timeout=2)
        _write_json_text(path, content)

    monkeypatch.setattr(
        session_module,
        "atomic_json",
        controlled_write,
        raising=False,
    )
    first_session = SafeJSONSession(save_dir=str(tmp_path))
    second_session = SafeJSONSession(save_dir=str(tmp_path))

    first_task = asyncio.create_task(
        first_session.save_merged_state("shared", state={"writer": "first"}),
    )
    assert await asyncio.to_thread(first_write_started.wait, 1)
    second_task = asyncio.create_task(
        second_session.save_merged_state("shared", state={"writer": "second"}),
    )
    await asyncio.sleep(0.05)

    assert len(calls) == 1

    allow_first_write.set()
    await asyncio.gather(first_task, second_task)
    assert json.loads((tmp_path / "shared.json").read_text()) == {
        "writer": "second",
    }


@pytest.mark.asyncio
async def test_different_path_writes_can_overlap(
    monkeypatch,
    tmp_path: Path,
) -> None:
    both_writes_started = threading.Event()
    allow_writes = threading.Event()
    active_writes = 0
    maximum_active_writes = 0
    active_guard = threading.Lock()

    def controlled_write(path: str, content: str) -> None:
        nonlocal active_writes, maximum_active_writes
        with active_guard:
            active_writes += 1
            maximum_active_writes = max(maximum_active_writes, active_writes)
            if active_writes == 2:
                both_writes_started.set()
        assert allow_writes.wait(timeout=2)
        _write_json_text(path, content)
        with active_guard:
            active_writes -= 1

    monkeypatch.setattr(
        session_module,
        "atomic_json",
        controlled_write,
        raising=False,
    )
    session = SafeJSONSession(save_dir=str(tmp_path))

    tasks = [
        asyncio.create_task(
            session.save_merged_state("first", state={"path": "first"}),
        ),
        asyncio.create_task(
            session.save_merged_state("second", state={"path": "second"}),
        ),
    ]

    assert await asyncio.to_thread(both_writes_started.wait, 1)
    allow_writes.set()
    await asyncio.gather(*tasks)
    assert maximum_active_writes == 2


@pytest.mark.asyncio
async def test_concurrent_key_updates_preserve_both_values(
    monkeypatch,
    tmp_path: Path,
) -> None:
    session_path = tmp_path / "shared.json"
    session_path.write_text("{}", encoding="utf-8")
    first_write_started = threading.Event()
    allow_first_write = threading.Event()
    call_count = 0
    call_guard = threading.Lock()

    def controlled_write(path: str, content: str) -> None:
        nonlocal call_count
        with call_guard:
            call_count += 1
            call_number = call_count
        if call_number == 1:
            first_write_started.set()
            assert allow_first_write.wait(timeout=2)
        _write_json_text(path, content)

    monkeypatch.setattr(
        session_module,
        "atomic_json",
        controlled_write,
        raising=False,
    )
    session = SafeJSONSession(save_dir=str(tmp_path))

    first_task = asyncio.create_task(
        session.update_session_state("shared", "first", 1),
    )
    assert await asyncio.to_thread(first_write_started.wait, 1)
    second_task = asyncio.create_task(
        session.update_session_state("shared", "second", 2),
    )
    await asyncio.sleep(0.05)
    allow_first_write.set()

    await asyncio.gather(first_task, second_task)
    assert json.loads(session_path.read_text()) == {"first": 1, "second": 2}


def test_cross_process_key_updates_preserve_both_values(
    tmp_path: Path,
) -> None:
    session_path = tmp_path / "shared.json"
    session_path.write_text("{}", encoding="utf-8")
    context = multiprocessing.get_context("spawn")
    first_started = context.Event()

    first_process = context.Process(
        target=_mutate_session_state_in_process,
        args=(
            str(tmp_path),
            "shared",
            "first",
            1,
            first_started,
            None,
            0.4,
        ),
    )
    second_process = context.Process(
        target=_mutate_session_state_in_process,
        args=(
            str(tmp_path),
            "shared",
            "second",
            2,
            None,
            first_started,
            0,
        ),
    )

    first_process.start()
    second_process.start()
    first_process.join(timeout=10)
    second_process.join(timeout=10)

    if first_process.is_alive():
        first_process.terminate()
        first_process.join(timeout=2)
    if second_process.is_alive():
        second_process.terminate()
        second_process.join(timeout=2)

    assert first_process.exitcode == 0
    assert second_process.exitcode == 0
    assert json.loads(session_path.read_text()) == {
        "first": 1,
        "second": 2,
    }


def test_cross_process_execution_waits_for_lock_release(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    entered_event = context.Event()
    release_event = context.Event()
    result_queue = context.Queue()

    holder = context.Process(
        target=_hold_session_execution_in_process,
        args=(str(tmp_path), "shared", entered_event, release_event),
    )
    contender = context.Process(
        target=_try_session_execution_in_process,
        args=(str(tmp_path), "shared", result_queue),
    )

    holder.start()
    assert entered_event.wait(timeout=5)
    contender.start()
    contender.join(timeout=10)

    if contender.is_alive():
        contender.terminate()
        contender.join(timeout=2)
    assert contender.exitcode == 0
    assert result_queue.get(timeout=1) == "timed_out"

    release_event.set()
    holder.join(timeout=10)
    if holder.is_alive():
        holder.terminate()
        holder.join(timeout=2)
    assert holder.exitcode == 0

    retry = context.Process(
        target=_try_session_execution_in_process,
        args=(str(tmp_path), "shared", result_queue),
    )
    retry.start()
    retry.join(timeout=10)
    if retry.is_alive():
        retry.terminate()
        retry.join(timeout=2)
    assert retry.exitcode == 0
    assert result_queue.get(timeout=1) == "entered"


@pytest.mark.asyncio
async def test_state_module_save_preserves_concurrent_key_update(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class StateModule:
        def state_dict(self) -> dict:
            return {"value": "saved"}

    session_path = tmp_path / "shared.json"
    session_path.write_text("{}", encoding="utf-8")
    first_write_started = threading.Event()
    allow_first_write = threading.Event()
    call_count = 0
    call_guard = threading.Lock()

    def controlled_write(path: str, content: str) -> None:
        nonlocal call_count
        with call_guard:
            call_count += 1
            call_number = call_count
        if call_number == 1:
            first_write_started.set()
            assert allow_first_write.wait(timeout=2)
        _write_json_text(path, content)

    monkeypatch.setattr(
        session_module,
        "atomic_json",
        controlled_write,
    )
    session = SafeJSONSession(save_dir=str(tmp_path))

    state_save_task = asyncio.create_task(
        session.save_session_state("shared", agent=StateModule()),
    )
    assert await asyncio.to_thread(first_write_started.wait, 1)
    update_task = asyncio.create_task(
        session.update_session_state("shared", "message", "preserved"),
    )
    await asyncio.sleep(0.05)
    allow_first_write.set()

    await asyncio.gather(state_save_task, update_task)
    assert json.loads(session_path.read_text()) == {
        "agent": {"value": "saved"},
        "message": "preserved",
    }


@pytest.mark.asyncio
async def test_skill_snapshot_save_preserves_concurrent_state_update(
    monkeypatch,
    tmp_path: Path,
) -> None:
    session_path = tmp_path / "shared.json"
    session_path.write_text("{}", encoding="utf-8")
    first_write_started = threading.Event()
    allow_first_write = threading.Event()
    call_count = 0
    call_guard = threading.Lock()

    def controlled_write(path: str, content: str) -> None:
        nonlocal call_count
        with call_guard:
            call_count += 1
            call_number = call_count
        if call_number == 1:
            first_write_started.set()
            assert allow_first_write.wait(timeout=2)
        _write_json_text(path, content)

    monkeypatch.setattr(
        session_module,
        "atomic_json",
        controlled_write,
        raising=False,
    )
    session = SafeJSONSession(save_dir=str(tmp_path))

    update_task = asyncio.create_task(
        session.update_session_state("shared", "message", "preserved"),
    )
    assert await asyncio.to_thread(first_write_started.wait, 1)
    snapshot_task = asyncio.create_task(
        session.save_session_skill_snapshot(
            "shared",
            snapshot={"xlsx": {"freshness_token": 1}},
        ),
    )
    await asyncio.sleep(0.05)
    allow_first_write.set()

    await asyncio.gather(update_task, snapshot_task)
    state = json.loads(session_path.read_text())
    assert state["message"] == "preserved"
    assert state["session_skill_snapshot"] == {
        "xlsx": {"freshness_token": 1},
    }


@pytest.mark.asyncio
async def test_get_session_state_dict_avoids_truncated_json_during_write(
    monkeypatch,
    tmp_path: Path,
) -> None:
    session_path = tmp_path / "shared.json"
    session_path.write_text('{"version": "old"}', encoding="utf-8")
    write_truncated = threading.Event()
    allow_write = threading.Event()

    def truncating_write(path: str, content: str) -> None:
        with open(path, "w", encoding="utf-8") as file:
            file.write("")
            file.flush()
            write_truncated.set()
            assert allow_write.wait(timeout=2)
            file.write(json.dumps(content, ensure_ascii=False))
            file.flush()

    monkeypatch.setattr(
        session_module,
        "atomic_json",
        truncating_write,
        raising=False,
    )
    session = SafeJSONSession(save_dir=str(tmp_path))

    save_task = asyncio.create_task(
        session.save_merged_state("shared", state={"version": "new"}),
    )
    assert await asyncio.to_thread(write_truncated.wait, 1)

    read_task = asyncio.create_task(
        session.get_session_state_dict("shared"),
    )
    await asyncio.sleep(0.05)
    allow_write.set()

    await save_task
    state = await read_task

    assert state in ({"version": "old"}, {"version": "new"})


@pytest.mark.asyncio
async def test_persisted_snapshot_read_does_not_wait_for_active_execution(
    tmp_path: Path,
) -> None:
    session = SafeJSONSession(save_dir=str(tmp_path))
    await session.save_merged_state("shared", state={"version": "saved"})
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_execution() -> None:
        async with session.execution("shared"):
            entered.set()
            await release.wait()

    execution_task = asyncio.create_task(hold_execution())
    await asyncio.wait_for(entered.wait(), timeout=1)

    snapshot = await asyncio.wait_for(
        session.get_persisted_session_state_dict("shared"),
        timeout=0.1,
    )

    assert snapshot == {"version": "saved"}
    release.set()
    await execution_task


@pytest.mark.asyncio
async def test_load_session_state_avoids_truncated_json_during_write(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class StateModule:
        def __init__(self) -> None:
            self.loaded_state: dict | None = None

        def load_state_dict(self, state: dict) -> None:
            self.loaded_state = state

    session_path = tmp_path / "shared.json"
    session_path.write_text(
        '{"agent": {"version": "old"}}',
        encoding="utf-8",
    )
    write_truncated = threading.Event()
    allow_write = threading.Event()

    def truncating_write(path: str, content: str) -> None:
        with open(path, "w", encoding="utf-8") as file:
            file.write("")
            file.flush()
            write_truncated.set()
            assert allow_write.wait(timeout=2)
            file.write(json.dumps(content, ensure_ascii=False))
            file.flush()

    monkeypatch.setattr(
        session_module,
        "atomic_json",
        truncating_write,
        raising=False,
    )
    session = SafeJSONSession(save_dir=str(tmp_path))
    state_module = StateModule()

    save_task = asyncio.create_task(
        session.save_merged_state(
            "shared",
            state={"agent": {"version": "new"}},
        ),
    )
    assert await asyncio.to_thread(write_truncated.wait, 1)

    load_task = asyncio.create_task(
        session.load_session_state("shared", agent=state_module),
    )
    await asyncio.sleep(0.05)
    allow_write.set()

    await save_task
    await load_task

    assert state_module.loaded_state in (
        {"version": "old"},
        {"version": "new"},
    )

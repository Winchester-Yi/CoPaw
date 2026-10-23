# -*- coding: utf-8 -*-
"""Runtime storage metrics follow durable boundaries without reading cold history."""

from types import SimpleNamespace

import pytest

from swe import performance_metrics
from swe.app.runner.session import SafeJSONSession, PublishedLedgerCommitError
from swe.app.runner.turn_ledger import TurnLedger, TERMINAL, _digest, _size
from swe.agents.memory.chat_checkpoint import CheckpointEvent, CheckpointRecord
from swe.agents.memory.checkpoint_storage_v2 import CheckpointStorageV2
from swe.agents.memory.conversation_archive import ConversationArchiveStore

CHAT = "16374f34-c02e-4bb9-87bf-eb5e64348cef"


@pytest.fixture
def metrics(monkeypatch):
    value = performance_metrics.PerformanceMetrics()
    monkeypatch.setattr(performance_metrics, "METRICS", value)
    return value


def turns(count):
    return {
        f"t{i}": {
            "status": "completed",
            "message": {"id": f"t{i}", "content": "中" * 10},
            "chat_id": CHAT,
        }
        for i in range(count)
    }


async def migrated_session(tmp_path):
    session = SafeJSONSession(str(tmp_path))
    await session.save_merged_state("s", state={"turn_states": turns(80)})
    await session.migrate_terminal_turns("s", dry_run=False)
    return session


@pytest.mark.asyncio
async def test_committed_hot_and_protected_quantities_use_utf8_without_cold_reads(
    tmp_path,
    monkeypatch,
):
    session = await migrated_session(tmp_path)
    metrics = performance_metrics.PerformanceMetrics()
    monkeypatch.setattr(performance_metrics, "METRICS", metrics)
    async with session.execution("s") as tx:
        baseline = {
            key: _digest(value)
            for key, value in tx.state["turn_states"].items()
        }
        await tx.hydrate_turn("t0")
        tx.state["turn_states"]["t0"]["message"]["content"] = "更新" * 50
        tx.state["turn_states"]["pending"] = {
            "status": "admitted",
            "message": {"content": "待处理"},
        }
        tx.state["turn_states"]["unknown"] = {
            "status": "future-status",
            "message": {"content": "保护"},
        }
        monkeypatch.setattr(
            TurnLedger,
            "_read_turn",
            lambda *a: pytest.fail("metrics cannot read cold bodies"),
        )
        await tx.commit_state(tx.state)
        hot = tx.state["turn_states"]
        terminal = {
            key: value
            for key, value in hot.items()
            if value["status"] in TERMINAL
        }
        protected = {
            key: value
            for key, value in hot.items()
            if value["status"] not in TERMINAL
            or baseline.get(key) != _digest(value)
        }
    result = metrics.snapshot()
    observed = result["session_memory"]
    assert observed["observed_count"] == 1
    assert observed["session_hot_terminal_count"]["last"] == len(terminal)
    assert observed["session_hot_terminal_bytes"]["last"] == sum(
        _size({k: v}) for k, v in terminal.items()
    )
    assert observed["session_protected_count"]["last"] == 3
    assert observed["session_protected_bytes"]["last"] == sum(
        _size({k: v}) for k, v in protected.items()
    )
    assert (
        result["storage_operations"]["session_commit"]["succeeded_count"] == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("published", [False, True])
async def test_root_write_failures_observe_only_published_roots(
    tmp_path,
    monkeypatch,
    published,
):
    from swe.app.runner import session as module

    session = await migrated_session(tmp_path)
    metrics = performance_metrics.PerformanceMetrics()
    monkeypatch.setattr(performance_metrics, "METRICS", metrics)
    writer = module._write_json_state_sync

    def fail(path, state):
        if published:
            writer(path, state)
        raise OSError("durability failed")

    monkeypatch.setattr(module, "_write_json_state_sync", fail)
    error_type = PublishedLedgerCommitError if published else OSError
    async with session.execution("s") as tx:
        with pytest.raises(error_type):
            await tx.commit_state(tx.state)
    result = metrics.snapshot()
    assert result["session_memory"]["observed_count"] == int(published)
    assert result["counters"]["session_commit_failures"] == 1
    assert result["storage_operations"]["session_commit"]["failed_count"] == 1


@pytest.mark.asyncio
async def test_session_recovery_and_preview_migration_failures_are_separate(
    tmp_path,
    monkeypatch,
):
    session = await migrated_session(tmp_path)
    state = await session.get_session_state_dict("s")
    ledger = TurnLedger(tmp_path / "s.json")
    (ledger.directory(state) / "pending.json").write_text("{")
    metrics = performance_metrics.PerformanceMetrics()
    monkeypatch.setattr(performance_metrics, "METRICS", metrics)
    with pytest.raises(ValueError):
        ledger.recover(state)
    (tmp_path / "bad.json").write_text("{")
    for preview in (True, False):
        with pytest.raises(ValueError):
            await session.migrate_terminal_turns("bad", dry_run=preview)
    counters = metrics.snapshot()["counters"]
    assert counters["session_recovery_failures"] == 1
    assert counters["session_migration_failures"] == 1
    assert counters["session_migration_preview_failures"] == 1


def test_checkpoint_preview_and_migration_failures_are_separate(
    tmp_path,
    metrics,
):
    from swe.agents.memory import conversation_archive as module

    store = ConversationArchiveStore(tmp_path, cursor_secret=b"test")
    chat = store.path_for(CHAT)
    chat.mkdir(parents=True)
    (chat / module._CHECKPOINT_EVENTS_NAME).write_text("{\n")
    for preview in (True, False):
        with pytest.raises(ValueError):
            store.migrate_checkpoint_v2(CHAT, dry_run=preview)
    counters = metrics.snapshot()["counters"]
    assert counters["checkpoint_migration_failures"] == 1
    assert counters["checkpoint_migration_preview_failures"] == 1


def event(sequence):
    return CheckpointEvent.new(
        sequence=sequence,
        epoch=1,
        type="message_added",
        facts={"message_id": str(sequence), "role": "user"},
        source_refs=(f"message:{sequence}",),
    )


def test_checkpoint_commit_and_recovery_failures_keep_tail_semantics(
    tmp_path,
    metrics,
    monkeypatch,
):
    storage = CheckpointStorageV2(tmp_path)
    storage.initialize(
        CheckpointRecord.new(chat_id=CHAT, epoch=1),
        {"boundaries": []},
        {},
        {},
    )
    publisher = storage._publish
    monkeypatch.setattr(
        storage,
        "_publish",
        lambda *a: (_ for _ in ()).throw(OSError("root failure")),
    )
    first = event(1)
    with pytest.raises(OSError):
        storage.append(first)
    monkeypatch.setattr(storage, "_publish", publisher)
    storage.append(first)
    path = storage.segment_path(storage.head())
    assert [item.sequence for item in storage.events(storage.head())] == [1]
    payload = path.read_bytes()
    path.write_bytes(b"!" + payload[1:])
    with pytest.raises(ValueError, match="committed segment"):
        storage.append(event(2))
    result = metrics.snapshot()
    assert result["counters"]["checkpoint_commit_failures"] == 2
    assert result["counters"]["checkpoint_recovery_failures"] == 1
    assert (
        result["storage_operations"]["checkpoint_commit"]["succeeded_count"]
        == 2
    )


@pytest.mark.asyncio
async def test_metrics_failure_does_not_turn_published_session_into_error(
    tmp_path,
    monkeypatch,
    metrics,
):
    session = await migrated_session(tmp_path)
    monkeypatch.setattr(
        metrics,
        "record_session_memory",
        lambda *a: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    async with session.execution("s") as tx:
        before = tx.revision
        await tx.commit_state(tx.state)
        assert tx.revision == before + 1
    result = metrics.snapshot()["storage_operations"]["session_commit"]
    assert result["succeeded_count"] >= 1
    assert result["failed_count"] == 0


def test_runtime_diagnostic_includes_storage_observations(
    monkeypatch,
    metrics,
):
    from swe.app import runtime_diagnostic

    monkeypatch.setattr(runtime_diagnostic, "METRICS", metrics)
    manager = runtime_diagnostic.RuntimeDiagnosticManager(
        hostname="metrics-test",
        disk_usage=lambda _: SimpleNamespace(
            total=0,
            used=0,
            free=0,
            percent=0,
        ),
        pod_open_fd_count=lambda: 0,
        pod_disk_io_bytes=lambda: (0, 0),
        thread_limiter=lambda: SimpleNamespace(
            total_tokens=1,
            borrowed_tokens=0,
        ),
    )
    metrics.record_session_memory(4, 100, 2, 80)
    metrics.record_storage_operation("session_commit", 0.002, "succeeded")
    payload = manager.build_diagnostic_payload()["performance_metrics"]
    assert payload["session_memory"]["session_hot_terminal_count"]["last"] == 4
    assert (
        payload["storage_operations"]["session_commit"]["duration_max_ms"] == 2
    )
    assert payload["counters"]["session_recovery_failures"] == 0


@pytest.mark.asyncio
async def test_cancelled_waiter_records_real_thread_commit_completion(
    tmp_path,
    monkeypatch,
):
    import asyncio
    import threading
    from swe.app.runner import session as module

    session = await migrated_session(tmp_path)
    metrics = performance_metrics.PerformanceMetrics()
    monkeypatch.setattr(performance_metrics, "METRICS", metrics)
    now = [0.0]
    monkeypatch.setattr(performance_metrics, "perf_counter", lambda: now[0])
    entered, release = threading.Event(), threading.Event()
    writer = module._write_json_state_sync

    def blocking_write(path, state):
        entered.set()
        assert release.wait(5)
        writer(path, state)

    monkeypatch.setattr(module, "_write_json_state_sync", blocking_write)

    async def commit():
        async with session.execution("s") as tx:
            await tx.commit_state(tx.state)

    task = asyncio.create_task(commit())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        now[0] = 5.0
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        assert (
            metrics.snapshot()["storage_operations"]["session_commit"][
                "attempted_count"
            ]
            == 0
        )
        now[0] = 10.0
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = metrics.snapshot()["storage_operations"]["session_commit"]
    assert result["succeeded_count"] == 1
    assert result["failed_count"] == result["cancelled_count"] == 0
    assert result["duration_max_ms"] == 10000

# -*- coding: utf-8 -*-
"""Deterministic regression coverage for confirmed tracing writes."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from swe.tracing.config import TracingConfig
from swe.tracing.manager import TraceManager, set_current_trace
from swe.tracing.models import EventType


class GatedDatabase:
    """Hold real store writes at the database boundary."""

    is_connected = True

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.rows = []
        self.batches = []
        self.updates = []
        self.trace_completed = asyncio.Event()
        self.fail_updates = False

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def insert_prepared(self, _query, params, **_limits):
        params = list(params)
        self.batches.append(list(params))
        self.rows.extend(params)
        self.started.set()
        await self.release.wait()
        return len(params)

    async def execute(self, query, params=None):
        if "UPDATE swe_tracing_traces SET" in query:
            self.trace_completed.set()
        if "UPDATE swe_tracing_spans SET" in query:
            if self.fail_updates:
                raise OSError("update unavailable")
            self.updates.append(params)
        return 1

    async def fetch_one(self, _query, _params=None):
        return None

    async def fetch_all(self, _query, _params=None):
        return []


async def new_manager(**options):
    db = GatedDatabase()
    config = TracingConfig(
        enabled=True,
        retention_days=0,
        flush_interval=60,
        **options,
    )
    manager = TraceManager(config, db)
    await manager.initialize()
    trace_id = await manager.start_trace(
        "user",
        "session",
        "console",
        "source",
    )
    return manager, db, trace_id


async def emit(manager, trace_id, **kwargs):
    return await manager.emit_span(
        trace_id,
        EventType.TOOL_CALL_START,
        "tool",
        "source",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_end_trace_waits_for_inflight_insert():
    manager, db, trace_id = await new_manager(batch_size=1)
    producer = asyncio.create_task(emit(manager, trace_id))
    await db.started.wait()
    ending = asyncio.create_task(manager.end_trace(trace_id))
    await asyncio.sleep(0.02)
    try:
        assert not db.trace_completed.is_set()
        assert not ending.done()
    finally:
        db.release.set()
        await producer
        await ending
        await manager.close()
        set_current_trace(None)


@pytest.mark.asyncio
async def test_close_drains_inflight_without_cancelling_it():
    manager, db, trace_id = await new_manager(batch_size=100)
    await emit(manager, trace_id)
    flush = asyncio.create_task(manager._flush_spans())
    await db.started.wait()
    closing = asyncio.create_task(manager.close())
    await asyncio.sleep(0.02)
    try:
        assert not closing.done()
    finally:
        db.release.set()
        await flush
        await closing
        set_current_trace(None)


@pytest.mark.asyncio
async def test_update_during_insert_is_confirmed_before_trace_end():
    manager, db, trace_id = await new_manager(batch_size=100)
    span_id = await emit(manager, trace_id)
    flush = asyncio.create_task(manager._flush_spans())
    await db.started.wait()
    await manager.update_span(span_id, trace_id, tool_output="finished")
    ending = asyncio.create_task(manager.end_trace(trace_id))
    await asyncio.sleep(0.02)
    try:
        assert not ending.done()
    finally:
        db.release.set()
        await flush
        await ending
        await manager.close()
        set_current_trace(None)
    assert len(db.updates) == 1
    assert db.updates[0][4] == "finished"
    assert isinstance(db.updates[0][0], datetime)


@pytest.mark.asyncio
async def test_emit_signals_writer_without_waiting_for_database():
    manager, db, trace_id = await new_manager(batch_size=1)
    producer = asyncio.create_task(emit(manager, trace_id))
    await db.started.wait()
    try:
        await asyncio.wait_for(asyncio.shield(producer), 0.05)
    finally:
        db.release.set()
        await producer
        await manager.close()


@pytest.mark.asyncio
async def test_close_caller_cancellation_does_not_cancel_drain():
    manager, db, trace_id = await new_manager(batch_size=100)
    await emit(manager, trace_id)
    closing = asyncio.create_task(manager.close())
    await db.started.wait()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    db.release.set()
    await manager.close()
    assert not manager._pending_spans


@pytest.mark.asyncio
async def test_failed_update_keeps_trace_unconfirmed():
    manager, db, trace_id = await new_manager(write_timeout=0.04)
    span_id = await emit(manager, trace_id)
    flushing = asyncio.create_task(manager._flush_spans())
    await db.started.wait()
    await manager.update_span(span_id, trace_id, tool_output="finished")
    db.fail_updates = True
    db.release.set()
    try:
        with pytest.raises(TimeoutError):
            await manager.end_trace(trace_id)
        assert not db.trace_completed.is_set()
        assert trace_id in manager._active_traces
        assert span_id in manager._pending_spans
    finally:
        await flushing
        db.fail_updates = False
        await manager.close()


@pytest.mark.asyncio
async def test_shutdown_deadline_reports_unconfirmed_work():
    manager, db, trace_id = await new_manager(shutdown_timeout=0.03)
    await emit(manager, trace_id)
    with pytest.raises(TimeoutError, match="unconfirmed"):
        await manager.close()
    assert manager._pending_spans
    assert manager._flush_task is None
    assert not db.trace_completed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("close_manager", [False, True])
async def test_shutdown_returns_when_writer_cancellation_cleanup_stalls(
    close_manager,
):
    manager, db, trace_id = await new_manager(
        batch_size=1,
        shutdown_timeout=0.02,
    )
    cancelling = asyncio.Event()
    release_cleanup = asyncio.Event()
    store_closed = asyncio.Event()
    manager.store.close = AsyncMock(side_effect=store_closed.set)

    async def stalled_cleanup(_query, _params, **_limits):
        db.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await release_cleanup.wait()
            raise

    db.insert_prepared = stalled_cleanup
    await emit(manager, trace_id)
    await db.started.wait()
    close = manager.close if close_manager else manager._writer.close
    closing = asyncio.create_task(close())
    await cancelling.wait()
    try:
        done, _ = await asyncio.wait({closing}, timeout=0.2)
        assert done, "shutdown waited indefinitely for cancellation cleanup"
        with pytest.raises(
            TimeoutError,
            match="unconfirmed.*writer still running",
        ):
            await closing
        assert not manager._writer.task.done()
        assert manager._pending_spans
        assert not store_closed.is_set()
    finally:
        release_cleanup.set()
        await asyncio.gather(manager._writer.task, return_exceptions=True)
        await asyncio.gather(closing, return_exceptions=True)
        if close_manager:
            await asyncio.wait_for(store_closed.wait(), 0.2)
        else:
            await asyncio.gather(manager.close(), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("close_manager", [False, True])
async def test_shutdown_logs_cleanup_failure_during_cancellation_grace(
    monkeypatch,
    close_manager,
):
    from swe.tracing.writer import SpanWriter

    started = asyncio.Event()
    failure = OSError("writer cancellation cleanup failed")
    log = MagicMock()

    async def failing_cleanup(_self):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise failure

    monkeypatch.setattr(SpanWriter, "_run", failing_cleanup)
    monkeypatch.setattr("swe.tracing.writer.logger", log)
    manager, _db, trace_id = await new_manager(shutdown_timeout=0.02)
    manager.store.close = AsyncMock()
    await emit(manager, trace_id)
    await started.wait()
    close = manager.close if close_manager else manager._writer.close
    try:
        with pytest.raises(TimeoutError, match="unconfirmed"):
            await close()
        assert manager._writer.task.done()
        assert manager._pending_spans
        failures = [
            call.kwargs["exc_info"]
            for call in log.error.call_args_list
            if "exc_info" in call.kwargs
        ]
        assert len(failures) == 1
        assert failures[0][0] is OSError
        assert failures[0][1] is failure
        assert failures[0][2] is failure.__traceback__
        if close_manager:
            manager.store.close.assert_awaited_once()
    finally:
        await asyncio.gather(manager.close(), return_exceptions=True)
        await asyncio.gather(manager._writer.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_batches_are_limited_by_rows():
    manager, db, trace_id = await new_manager(batch_size=2)
    db.release.set()
    for _ in range(7):
        await emit(manager, trace_id)
    await manager.close()
    assert [len(batch) for batch in db.batches] == [2, 2, 2, 1]


@pytest.mark.asyncio
async def test_batches_are_limited_by_estimated_bytes():
    manager, db, trace_id = await new_manager(max_batch_bytes=5000)
    db.release.set()
    for _ in range(3):
        await emit(manager, trace_id, tool_input={"text": "x" * 200})
    await manager.close()
    assert [len(batch) for batch in db.batches] == [1, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limits",
    [{"max_queue_spans": 1}, {"max_queue_bytes": 5000}],
)
async def test_inflight_work_counts_toward_backpressure(limits):
    manager, db, trace_id = await new_manager(batch_size=100, **limits)
    await emit(manager, trace_id, tool_input={"text": "x" * 200})
    producer = asyncio.create_task(
        emit(manager, trace_id, tool_input={"text": "x" * 200}),
    )
    await db.started.wait()
    try:
        await asyncio.sleep(0.02)
        assert not producer.done()
    finally:
        db.release.set()
        await producer
        await manager.close()


@pytest.mark.asyncio
async def test_cancelled_flush_waiter_does_not_cancel_writer():
    manager, db, trace_id = await new_manager()
    await emit(manager, trace_id)
    flushing = asyncio.create_task(manager._flush_spans())
    await db.started.wait()
    flushing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await flushing
    db.release.set()
    await manager.close()
    assert not manager._pending_spans


@pytest.mark.asyncio
async def test_oversized_span_is_written_alone_without_truncation():
    manager, db, trace_id = await new_manager(
        max_queue_bytes=1000,
        max_batch_bytes=1000,
        sanitize_output=False,
    )
    db.release.set()
    await emit(manager, trace_id, tool_input={"text": "x" * 2000})
    await manager.close()
    assert len(db.batches) == 1
    assert "x" * 2000 in db.batches[0][0][18]


@pytest.mark.asyncio
async def test_non_bmp_json_escaping_counts_toward_byte_limit():
    manager, db, trace_id = await new_manager(sanitize_output=False)
    db.release.set()
    for _ in range(2):
        await emit(manager, trace_id, tool_input={"text": "😀" * 10000})
    await manager.close()
    assert [len(batch) for batch in db.batches] == [1, 1]


@pytest.mark.asyncio
async def test_snapshot_failure_rejects_admission_without_retaining_data():
    class Input:
        fail = True

        def __deepcopy__(self, _memo):
            if self.fail:
                raise RuntimeError("snapshot unavailable")
            return "safe value"

    manager, db, trace_id = await new_manager(
        batch_size=1,
        sanitize_output=False,
    )
    db.release.set()
    try:
        with pytest.raises(RuntimeError, match="snapshot unavailable"):
            await emit(manager, trace_id, tool_input={"value": Input()})
        assert not manager._pending_spans
    finally:
        Input.fail = False
        await manager.close()


@pytest.mark.asyncio
async def test_task_creation_supports_python310_signature(monkeypatch):
    create_task = asyncio.create_task

    def python310_create_task(coro, *, name=None):
        return create_task(coro, name=name)

    monkeypatch.setattr(asyncio, "create_task", python310_create_task)
    manager, db, trace_id = await new_manager()
    db.release.set()
    await emit(manager, trace_id)
    await manager.close()


@pytest.mark.asyncio
async def test_admission_freezes_input_before_caller_mutation():
    manager, db, trace_id = await new_manager(sanitize_output=False)
    payload = {"items": [["original"]]}
    await emit(manager, trace_id, tool_input=payload)
    payload["items"][0][0] = "changed" * 10000
    db.release.set()
    await manager.close()
    assert '"original"' in db.rows[0][18]
    assert "changed" not in db.rows[0][18]


@pytest.mark.asyncio
async def test_failed_insert_retries_latest_version_with_stable_identity():
    manager, db, trace_id = await new_manager(batch_size=1)
    original = db.insert_prepared
    failed = False

    async def unknown_commit(query, params, **limits):
        nonlocal failed
        count = await original(query, params, **limits)
        if not failed:
            failed = True
            raise OSError("commit acknowledgement lost")
        return count

    db.insert_prepared = unknown_commit
    span_id = await emit(manager, trace_id)
    await db.started.wait()
    await manager.update_span(span_id, trace_id, tool_output="finished")
    db.release.set()
    await manager.end_trace(trace_id)
    await manager.close()
    assert len(db.rows) == 2
    assert all(row[0] == span_id for row in db.rows)
    assert db.rows[-1][19] == "finished"


@pytest.mark.asyncio
async def test_close_rejects_waiting_admission_without_cancelling_inflight():
    manager, db, trace_id = await new_manager(max_queue_spans=1)
    await emit(manager, trace_id)
    blocked = asyncio.create_task(emit(manager, trace_id))
    await db.started.wait()
    closing = asyncio.create_task(manager.close())
    try:
        with pytest.raises(RuntimeError, match="closing"):
            await asyncio.wait_for(blocked, 0.1)
    finally:
        db.release.set()
        await closing
    assert len(db.rows) == 1


@pytest.mark.asyncio
async def test_writer_does_not_inherit_request_transaction_or_trace():
    from swe.database.connection import _CURRENT_CONNECTION
    from swe.tracing.manager import TraceContext, get_current_trace

    db = GatedDatabase()
    observed = []
    original = db.insert_prepared

    async def check_context(query, params, **limits):
        observed.append((_CURRENT_CONNECTION.get(), get_current_trace()))
        return await original(query, params, **limits)

    db.insert_prepared = check_context
    manager = TraceManager(
        TracingConfig(enabled=True, retention_days=0),
        db,
    )
    token = _CURRENT_CONNECTION.set(object())
    set_current_trace(TraceContext("old", "u", "s", "c", "source"))
    try:
        await manager.initialize()
    finally:
        _CURRENT_CONNECTION.reset(token)
        set_current_trace(None)
    db.release.set()
    await emit(manager, "trace")
    await manager.close()
    assert observed == [(None, None)]


@pytest.mark.parametrize(
    "field",
    [
        "batch_size",
        "flush_interval",
        "max_batch_bytes",
        "max_queue_spans",
        "max_queue_bytes",
        "write_timeout",
        "shutdown_timeout",
    ],
)
def test_writer_limits_must_be_positive(field):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TracingConfig(**{field: 0})

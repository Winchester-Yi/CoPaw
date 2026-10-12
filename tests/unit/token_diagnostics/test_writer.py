# -*- coding: utf-8 -*-
"""Best-effort diagnostics must never backpressure the model."""

import asyncio
import copy

import pytest


def implementation():
    try:
        from swe.token_diagnostics.config import DiagnosticConfig
        from swe.token_diagnostics.writer import DiagnosticWriter
    except ModuleNotFoundError:
        pytest.fail("Independent diagnostic writer is missing")
    return DiagnosticConfig, DiagnosticWriter


class FakeStore:
    def __init__(self, fail=False):
        self.records = []
        self.fail = fail

    async def write_batch(self, records):
        if self.fail:
            raise RuntimeError("database unavailable")
        self.records.extend(copy.deepcopy(records))

    async def close(self):
        pass


def record(revision=1):
    return {
        "kind": "call",
        "data": {
            "call_id": "c1",
            "branch_run_id": "b1",
            "revision": revision,
            "usage_status": "reported" if revision == 2 else "pending",
            "input_tokens": 0 if revision == 2 else None,
            "output_tokens": 0 if revision == 2 else None,
            "items": [],
        },
    }


@pytest.mark.asyncio
async def test_pending_updates_coalesce_and_are_frozen():
    config_cls, writer_cls = implementation()
    store = FakeStore()
    writer = writer_cls(store, config_cls(enabled=True))
    old, new = record(), record(2)
    assert writer.add(old)
    assert writer.add(new)
    new["data"]["input_tokens"] = 500
    assert writer.add(old)
    await writer.flush_once()
    assert len(store.records) == 1
    assert store.records[0]["data"]["revision"] == 2
    assert store.records[0]["data"]["input_tokens"] == 0


@pytest.mark.asyncio
async def test_database_failure_retains_records_then_success_replays_same_id():
    config_cls, writer_cls = implementation()
    store = FakeStore(fail=True)
    writer = writer_cls(store, config_cls(enabled=True))
    writer.add(record(2))
    assert await writer.flush_once() is False
    assert writer.pending_count == 1
    store.fail = False
    assert await writer.flush_once() is True
    assert store.records[0]["data"]["call_id"] == "c1"


def test_overflow_returns_immediately_and_counts_gap():
    config_cls, writer_cls = implementation()
    writer = writer_cls(FakeStore(), config_cls(enabled=True, max_records=1))
    assert writer.add(record())
    other = record()
    other["data"]["call_id"] = "c2"
    assert writer.add(other) is False
    assert writer.gaps["b1"] == 1


@pytest.mark.asyncio
async def test_expiry_does_not_claim_successful_persistence():
    config_cls, writer_cls = implementation()
    writer = writer_cls(FakeStore(), config_cls(enabled=True, retry_age=0))
    writer.add(record())
    await writer.flush_once()
    assert writer.pending_count == 0
    assert writer.gaps["b1"] == 1


@pytest.mark.asyncio
async def test_shutdown_is_bounded_even_if_storage_hangs():
    config_cls, writer_cls = implementation()

    class HangingStore(FakeStore):
        async def write_batch(self, records):
            await asyncio.Event().wait()

    writer = writer_cls(
        HangingStore(), config_cls(enabled=True, shutdown_timeout=0.02)
    )
    writer.add(record())
    await asyncio.wait_for(writer.close(), timeout=0.2)


@pytest.mark.asyncio
async def test_token_store_does_not_borrow_legacy_transaction_connection():
    implementation()
    from swe.database.connection import _CURRENT_CONNECTION
    from swe.token_diagnostics.store import TokenStore

    class Cursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, query, params=()):
            assert "swe_tracing_" not in query

        async def fetchone(self):
            return None

    class Connection:
        committed = False

        def cursor(self, *args):
            return Cursor()

        async def begin(self):
            pass

        async def commit(self):
            self.committed = True

        async def rollback(self):
            pass

    connection = Connection()

    class Acquire:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, *args):
            pass

    class Pool:
        def acquire(self):
            return Acquire()

    token = _CURRENT_CONNECTION.set(object())
    try:
        data = record(2)
        data["data"].update(
            source_id="s", tenant_id="u", task_run_id="t", payload={}
        )
        await TokenStore(pool=Pool()).write_batch([data])
    finally:
        _CURRENT_CONNECTION.reset(token)
    assert connection.committed


@pytest.mark.asyncio
async def test_cancelled_flush_retains_inflight_terminal_snapshot():
    config_cls, writer_cls = implementation()

    class SlowStore(FakeStore):
        started = asyncio.Event()

        async def write_batch(self, records):
            self.started.set()
            await asyncio.Event().wait()

    store = SlowStore()
    writer = writer_cls(store, config_cls(enabled=True))
    writer.add(record(2))
    task = asyncio.create_task(writer.flush_once())
    await store.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.pending_count == 1
    assert writer.inflight_bytes == 0


@pytest.mark.asyncio
async def test_storage_is_closed_even_when_shutdown_drain_times_out():
    config_cls, writer_cls = implementation()

    class Store(FakeStore):
        closed = False

        async def write_batch(self, records):
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    store = Store()
    writer = writer_cls(store, config_cls(enabled=True, shutdown_timeout=0.02))
    writer.add(record())
    await asyncio.wait_for(writer.close(), timeout=0.2)
    assert store.closed

# -*- coding: utf-8 -*-
"""Exercise tracing transactions through the real database adapter."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from swe.database import DatabaseConfig, DatabaseConnection
from swe.database.pool import ConnectionPool, PoolEntry
from swe.tracing.config import TracingConfig
from swe.tracing.models import EventType, Span
from swe.tracing.store import TraceStore
from swe.tracing.writer import SpanWriter


def transactional_store():
    cursor = MagicMock()
    cursor.rowcount = 0  # An idempotent duplicate can affect zero rows.
    cursor.execute = AsyncMock()
    cursor.close = AsyncMock()
    state_cursor = MagicMock()
    state_cursor.execute = AsyncMock()
    state_cursor.fetchone = AsyncMock(
        return_value={"max_allowed_packet": 1048576},
    )
    state_cursor.close = AsyncMock()
    connection = MagicMock()
    connection.in_transaction = False
    connection.start_transaction = AsyncMock()
    connection.commit = AsyncMock()
    connection.rollback = AsyncMock()
    connection.ping = AsyncMock()
    connection.get_autocommit = AsyncMock(return_value=True)
    connection.shutdown = AsyncMock()
    connection.close = AsyncMock()
    connection.cursor = AsyncMock(
        side_effect=lambda **options: (
            cursor if options.get("prepared") else state_cursor
        ),
    )
    db = DatabaseConnection(DatabaseConfig(cleanup_timeout=0.02))
    db._pool = ConnectionPool(db.config, None)
    db._connected = True
    entry = PoolEntry(
        connection,
        max_allowed_packet=1048576,
        baseline={"max_allowed_packet": 1048576},
    )
    db._pool._entries.add(entry)
    db._pool._idle.append(entry)
    store = TraceStore(TracingConfig(), db)
    spans = [
        Span(
            span_id="span",
            trace_id="trace",
            source_id="source",
            name="tool",
            event_type=EventType.TOOL_CALL_START,
            start_time=datetime.now(),
        ),
    ]
    return store, spans, connection, cursor


@pytest.mark.asyncio
async def test_zero_affected_rows_confirms_idempotent_batch_in_one_transaction():
    store, spans, connection, cursor = transactional_store()
    assert await store.batch_create_spans(spans) == 1
    connection.start_transaction.assert_awaited_once()
    connection.commit.assert_awaited_once()
    connection.rollback.assert_not_awaited()
    query, _params = cursor.execute.call_args.args
    assert "ON DUPLICATE KEY UPDATE" in query
    assert query.count("?") == 23


@pytest.mark.asyncio
async def test_driver_chunk_failure_rolls_back_batch():
    store, spans, connection, cursor = transactional_store()
    store.config.batch_size = 1
    spans.append(spans[0].model_copy(update={"span_id": "span-2"}))
    cursor.execute.side_effect = [None, OSError("second SQL chunk failed")]
    with pytest.raises(OSError):
        await store.batch_create_spans(spans)
    assert cursor.execute.await_count == 2
    connection.rollback.assert_awaited_once()
    connection.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_write_discards_connection_before_reuse():
    store, spans, connection, cursor = transactional_store()
    started = asyncio.Event()

    async def blocked(*_args):
        started.set()
        await asyncio.Event().wait()

    cursor.execute.side_effect = blocked
    writing = asyncio.create_task(store.batch_create_spans(spans))
    await started.wait()
    writing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writing
    assert connection.shutdown.await_count >= 1
    assert not store.db._pool._idle
    connection.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_transaction_is_not_committed_by_span_batch():
    store, spans, connection, _cursor = transactional_store()
    async with store.db.transaction():
        await store.batch_create_spans(spans)
        connection.commit.assert_not_awaited()
        connection.start_transaction.assert_awaited_once()
    connection.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_closed_connection_rollback_does_not_replace_cancellation():
    store, spans, connection, cursor = transactional_store()
    started = asyncio.Event()

    async def cancelled_transport(*_args):
        started.set()
        await asyncio.Event().wait()

    connection.rollback.side_effect = OSError("connection already closed")
    cursor.execute.side_effect = cancelled_transport
    writing = asyncio.create_task(store.batch_create_spans(spans))
    await started.wait()
    writing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writing


@pytest.mark.asyncio
async def test_shutdown_deadline_survives_cancelled_transport_rollback_error():
    store, spans, connection, cursor = transactional_store()
    started = asyncio.Event()

    async def cancelled_transport(*_args):
        started.set()
        await asyncio.Event().wait()

    connection.rollback.side_effect = OSError("connection already closed")
    cursor.execute.side_effect = cancelled_transport
    writer = SpanWriter(TracingConfig(batch_size=1, shutdown_timeout=0.02))
    writer.start(store)
    await writer.submit(spans[0])
    await started.wait()
    closing = asyncio.create_task(writer.close())
    await asyncio.sleep(0.08)
    try:
        assert closing.done()
        assert writer.task.done()
        assert writer.pending
    finally:
        if not closing.done():
            connection.rollback.side_effect = None
            writer.task.cancel()
        with pytest.raises(TimeoutError, match="unconfirmed"):
            await closing

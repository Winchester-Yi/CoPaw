# -*- coding: utf-8 -*-
"""Bounded, narrow reference queries on the existing task-owned lease."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy

import pytest

from swe.app.conversation_storage.errors import (
    IntegrityError,
    StorageUnavailable,
)

SERVICE = b"schema-test-service"


class ReferenceDatabase:
    def __init__(self, **tables):
        self.tables = tables
        self.calls = []
        self.acquisitions = 0
        self.max_allowed_packet = 1024 * 1024
        self.depth = 0
        self.failure = None

    @asynccontextmanager
    async def acquire(self):
        self.acquisitions += 1
        yield self

    @asynccontextmanager
    async def transaction(self):
        self.depth += 1
        try:
            yield self
        finally:
            self.depth -= 1

    async def fetch_one(self, query, params):
        raise AssertionError("reference lookup must be batched")

    async def fetch_all(self, query, params):
        assert query.count("%s") == len(params)
        assert "service_unit_key=%s AND id IN (" in query
        assert "ORDER BY id" in query
        assert "*" not in query
        self.calls.append((query, params))
        if self.failure:
            raise self.failure
        table = query.split(" FROM swe_conversation_", 1)[1].split()[0]
        columns = query.removeprefix("SELECT ").split(" FROM ")[0].split(",")
        return [
            {key: deepcopy(row[key]) for key in columns}
            for identity, row in reversed(list(self.tables[table].items()))
            if identity in params[1:]
        ]


async def test_empty_references_do_not_acquire():
    from swe.app.conversation_storage.reference_queries import (
        ReferenceQuery,
        fetch_reference_rows,
    )

    db = ReferenceDatabase()
    assert (
        await fetch_reference_rows(
            db,
            ReferenceQuery.MESSAGE_OWNERSHIP,
            [],
            SERVICE,
        )
        == {}
    )
    assert db.acquisitions == 0
    assert db.calls == []


@pytest.mark.parametrize(
    "count",
    [1, 255, 256, 257, 4095, 4096, 4097, 8193, 16385],
)
async def test_reference_queries_are_bounded_and_ignore_return_order(count):
    from swe.app.conversation_storage.reference_queries import (
        ReferenceQuery,
        fetch_reference_rows,
    )

    rows = {
        i: {
            "id": i,
            "service_unit_key": SERVICE,
            "session_id": 1,
            "publication_id": 1,
        }
        for i in range(1, count + 1)
    }
    db = ReferenceDatabase(messages=rows)
    result = await fetch_reference_rows(
        db,
        ReferenceQuery.MESSAGE_OWNERSHIP,
        [*reversed(rows), 1],
        SERVICE,
    )
    assert result == rows
    assert db.acquisitions == 1
    assert len(db.calls) == (count + 255) // 256
    assert [
        identity for _, params in db.calls for identity in params[1:]
    ] == list(rows)


@pytest.mark.parametrize(
    "identity",
    [True, False, 0, -1, 2**64, 1.0, "1", None],
)
async def test_invalid_reference_id_rejected_before_lease(identity):
    from swe.app.conversation_storage.reference_queries import (
        ReferenceQuery,
        fetch_reference_rows,
    )

    db = ReferenceDatabase()
    with pytest.raises(ValueError):
        await fetch_reference_rows(
            db,
            ReferenceQuery.TURN_REFERENCE,
            [identity],
            SERVICE,
        )
    assert db.acquisitions == 0


@pytest.mark.parametrize("service", [b"", b"x" * 257, "service", None])
async def test_invalid_service_rejected_before_lease(service):
    from swe.app.conversation_storage.reference_queries import (
        ReferenceQuery,
        fetch_reference_rows,
    )

    db = ReferenceDatabase()
    with pytest.raises(ValueError):
        await fetch_reference_rows(
            db,
            ReferenceQuery.TURN_REFERENCE,
            [1],
            service,
        )
    assert db.acquisitions == 0


async def test_packet_budget_can_reduce_batch_or_fail_before_sql():
    from swe.app.conversation_storage.reference_queries import (
        ReferenceQuery,
        fetch_reference_rows,
    )

    db = ReferenceDatabase(messages={})
    db.max_allowed_packet = 2048
    assert (
        await fetch_reference_rows(
            db,
            ReferenceQuery.MESSAGE_OWNERSHIP,
            range(1, 257),
            SERVICE,
        )
        == {}
    )
    assert len(db.calls) > 1
    assert all(len(params) - 1 < 256 for _, params in db.calls)
    db.calls.clear()
    db.max_allowed_packet = 1024
    with pytest.raises(StorageUnavailable, match="packet"):
        await fetch_reference_rows(
            db,
            ReferenceQuery.MESSAGE_OWNERSHIP,
            [1],
            SERVICE,
        )
    assert db.calls == []


@pytest.mark.parametrize("bad", ["duplicate", "extra", "service", "bool"])
async def test_structurally_invalid_result_is_not_silently_accepted(bad):
    from swe.app.conversation_storage.reference_queries import (
        ReferenceQuery,
        fetch_reference_rows,
    )

    row = {"id": 1, "service_unit_key": SERVICE}
    results = {
        "duplicate": [row, row],
        "extra": [{**row, "id": 257}],
        "service": [{**row, "service_unit_key": b"foreign"}],
        "bool": [{**row, "id": True}],
    }
    db = ReferenceDatabase()

    async def fetch(*_):
        return results[bad]

    db.fetch_all = fetch
    with pytest.raises(IntegrityError):
        await fetch_reference_rows(
            db,
            ReferenceQuery.MESSAGE_OWNERSHIP,
            [1],
            SERVICE,
        )


async def test_fixed_query_shapes_cover_all_required_columns():
    from swe.app.conversation_storage.reference_queries import (
        ReferenceQuery,
        fetch_reference_rows,
    )
    from swe.app.conversation_storage.source_messages import SOURCE_FIELDS

    db = ReferenceDatabase(messages={}, turns={}, publications={})
    for kind in ReferenceQuery:
        await fetch_reference_rows(db, kind, [2**64 - 1], SERVICE)
    assert len(db.calls) == 5
    for query, params in db.calls:
        assert params == (SERVICE, 2**64 - 1)
        if "in_context" in query:
            columns = set(query[7:].split(" FROM ")[0].split(","))
            assert set(SOURCE_FIELDS) <= columns
        assert "result_metadata_text" not in query
        assert "execution_identity_text" not in query
    assert sum(query.endswith("FOR SHARE") for query, _ in db.calls) == 1
    with pytest.raises(ValueError):
        await fetch_reference_rows(db, "DROP TABLE messages", [1], SERVICE)


@pytest.mark.parametrize(
    "error",
    [OSError("SQL failed"), asyncio.CancelledError()],
)
@pytest.mark.parametrize("failure_at", [1, 2])
async def test_query_failure_and_cancel_propagate_without_partial_result(
    error,
    failure_at,
):
    from swe.app.conversation_storage.reference_queries import (
        ReferenceQuery,
        fetch_reference_rows,
    )

    db = ReferenceDatabase(
        messages={
            i: {
                "id": i,
                "service_unit_key": SERVICE,
                "session_id": 1,
                "publication_id": 1,
            }
            for i in range(1, 258)
        },
    )
    original = db.fetch_all

    async def fetch(query, params):
        if len(db.calls) + 1 == failure_at:
            db.failure = error
        return await original(query, params)

    db.fetch_all = fetch
    with pytest.raises(type(error)):
        await fetch_reference_rows(
            db,
            ReferenceQuery.MESSAGE_OWNERSHIP,
            range(1, 258),
            SERVICE,
        )
    assert len(db.calls) == failure_at

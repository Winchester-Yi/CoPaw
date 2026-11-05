# -*- coding: utf-8 -*-
"""Real checkpoint codecs, bounded pins and IO lifecycle at actual limits."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from swe.agents.memory.chat_checkpoint import CheckpointEvent, CheckpointRecord
from swe.app.conversation_storage.archive_reader import ArchiveReader
from swe.app.conversation_storage.errors import ConflictError, IntegrityError
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.retention import (
    PinBatch,
    RetentionStore,
    _ids,
)


class CheckpointDatabase:
    def __init__(self, count, active=False, shared=False, group=1):
        self.depth = 0
        self.event_queries = []
        self.chat_id = str(uuid4())
        self.chat_row = {
            "id": 1,
            "session_id": 2,
            "epoch": 1,
            "event_high": count,
            "checkpoint_revision": int(active),
            "active_checkpoint_publication_id": None,
        }
        self.objects, self.bodies = {}, {}
        self.reads = []
        for first in range(1, count + 1, group):
            last = min(first + group - 1, count)
            events = [
                CheckpointEvent.new(
                    sequence=i,
                    epoch=1,
                    type="message_added",
                    facts={"message_id": str(i), "role": "user"},
                    source_refs=(f"message:{i}",),
                )
                for i in range(first, last + 1)
            ]
            self.add(
                len(self.objects) + 1,
                "event",
                [e.to_dict() for e in events],
                event_first=first,
                event_last=last,
            )
        if active:
            pub_id = 1 if shared else len(self.objects) + 1
            record = replace(
                CheckpointRecord.new(chat_id=self.chat_id, epoch=1),
                revision=1,
            )
            self.add(
                pub_id,
                "checkpoint",
                record.to_dict(),
                checkpoint_event_watermark=0,
            )
            self.chat_row["active_checkpoint_publication_id"] = pub_id

    def add(self, pub_id, kind, body, **values):
        offset = 0 if kind == "checkpoint" else 1
        raw = json.dumps(body).encode()
        row = self.objects.setdefault(
            pub_id,
            {
                "id": pub_id,
                "session_id": 2,
                "chat_id": 1,
                "epoch": 1,
                "state": "committed",
                "gc_state": "live",
                "relative_path": str(pub_id).encode(),
                "byte_length": 10**7,
            },
        )
        row.update(
            values,
            **{kind + "_offset": offset, kind + "_length": len(raw)},
        )
        self.bodies[(str(pub_id), offset)] = raw

    @asynccontextmanager
    async def transaction(self):
        self.depth += 1
        try:
            yield self
        finally:
            self.depth -= 1

    async def fetch_one(self, query, params):
        if "swe_conversation_chats" in query:
            return deepcopy(self.chat_row)
        return deepcopy(self.objects.get(params[0]))

    async def fetch_all(self, query, params):
        assert query.count("%s") == len(params)
        assert self.depth
        if "WHERE id IN" in query:
            return [deepcopy(self.objects[key]) for key in params[:-1]]
        self.event_queries.append((query, params))
        _, epoch, after, high, *bound = params
        rows = sorted(
            (
                row
                for row in self.objects.values()
                if row.get("event_last", 0) > after
                and row["epoch"] == epoch
                and row.get("event_first", high + 1) <= high
            ),
            key=lambda row: (row["event_last"], row["id"]),
        )
        return deepcopy(rows[: bound[0] if bound else 4096])


class CheckpointPins(RetentionStore):
    def __init__(self, db, scope):
        super().__init__(db, scope)
        self.active = {}
        self.sizes = []
        self.max_active = 0

    async def pin(self, publication_ids, *, kind, retention_key=None):
        assert self.db.depth and kind == "reader"
        ids = _ids(publication_ids)
        rows = tuple(deepcopy(self.db.objects[i]) for i in ids)
        if any(r["gc_state"] != "live" for r in rows):
            raise ConflictError("publication is not live")
        key = str(len(self.sizes)).encode()
        self.sizes.append(len(ids))
        self.active[key] = set(ids)
        self.max_active = max(
            self.max_active,
            sum(map(len, self.active.values())),
        )
        return PinBatch(key, rows)

    async def release(self, retention_key):
        self.active.pop(retention_key, None)


def reader(count, **options):
    db = CheckpointDatabase(count, **options)
    scope = WorkspaceScope(
        "t",
        "s",
        "a",
        service_unit_id="schema-test-service",
    )
    pins = CheckpointPins(db, scope)

    def read(path, offset, length, **_kwargs):
        assert any(int(path) in ids for ids in pins.active.values())
        assert not db.depth
        db.reads.append((path, offset))
        return db.bodies[(path, offset)]

    result = ArchiveReader(db, scope, SimpleNamespace(read_extent=read))
    result.retentions = pins
    return result, db, pins


@pytest.mark.parametrize("count", [4095, 4096, 4097, 8193, 16385])
@pytest.mark.parametrize("active", [False, True])
async def test_full_checkpoint_covers_actual_boundaries(count, active):
    archive, db, pins = reader(count, active=active)
    state = await archive.checkpoint(db.chat_id)
    assert [event.sequence for event in state.events] == list(
        range(1, count + 1),
    )
    assert max(pins.sizes) <= 4096
    assert not pins.active


async def test_record_does_not_query_or_read_events():
    archive, db, pins = reader(8193, active=True)
    record = await archive.checkpoint_record(db.chat_id)
    assert record.revision == 1
    assert db.event_queries == []
    assert db.reads == [
        (str(db.chat_row["active_checkpoint_publication_id"]), 0),
    ]
    assert not pins.active


async def test_prefix_exposes_more_and_pages_keep_captured_high():
    archive, db, pins = reader(8193, active=True, shared=True, group=17)
    first = await archive.checkpoint_prefix(db.chat_id, 4096)
    assert len(first.events) == 4096 and first.has_more
    assert first.next_cursor is not None
    db.chat_row[
        "event_high"
    ] += 1  # append after captured high is not included
    second = await archive.checkpoint_page(
        db.chat_id,
        cursor=first.next_cursor,
        limit=4096,
    )
    third = await archive.checkpoint_page(
        db.chat_id,
        cursor=second.next_cursor,
        limit=4096,
    )
    assert [
        e.sequence for e in (*first.events, *second.events, *third.events)
    ] == list(range(1, 8194))
    assert not third.has_more and third.next_cursor is None
    assert not pins.active


@pytest.mark.parametrize(
    "field,value",
    [
        ("epoch", 2),
        ("checkpoint_revision", 2),
        ("active_checkpoint_publication_id", 999),
        ("event_high", 0),
    ],
)
async def test_page_rejects_changed_snapshot(field, value):
    archive, db, pins = reader(5, active=True)
    page = await archive.checkpoint_prefix(db.chat_id, 2)
    db.chat_row[field] = value
    with pytest.raises(ConflictError):
        await archive.checkpoint_page(
            db.chat_id,
            cursor=page.next_cursor,
            limit=2,
        )
    assert not pins.active


async def test_missing_late_publication_cannot_succeed_as_full():
    archive, db, pins = reader(4097)
    del db.objects[4097]
    with pytest.raises(IntegrityError, match="incomplete"):
        await archive.checkpoint(db.chat_id)
    assert not pins.active


async def test_shared_extent_bad_tail_is_validated_even_for_prefix():
    archive, db, pins = reader(10, group=10)
    body = json.loads(db.bodies[("1", 1)])
    body[-1]["facts"] = {"raw_tool_output": "must fail"}
    db.add(1, "event", body)
    with pytest.raises(IntegrityError, match="facts"):
        await archive.checkpoint_prefix(db.chat_id, 2)
    assert not pins.active


async def test_stream_releases_each_page_and_reuses_one_shared_extent():
    archive, db, pins = reader(8193, active=True, shared=True, group=8193)
    observed = []
    async for page in archive.iter_checkpoint_pages(db.chat_id):
        assert not pins.active
        observed.extend(e.sequence for e in page.events)
    assert observed == list(range(1, 8194))
    assert db.reads.count(("1", 1)) == 1
    assert pins.max_active <= 4096


async def test_event_budget_rejects_before_reading_events():
    from swe.app.conversation_storage.errors import StorageUnavailable

    archive, db, pins = reader(4097, active=True)
    with pytest.raises(StorageUnavailable, match="event budget"):
        await archive.checkpoint_full(db.chat_id, max_events=4096)
    assert not db.event_queries
    assert all(offset == 0 for _, offset in db.reads)
    assert not pins.active


async def test_byte_budget_rejects_before_reading_page_body():
    from swe.app.conversation_storage.errors import StorageUnavailable

    archive, db, pins = reader(1)
    with pytest.raises(StorageUnavailable, match="byte budget"):
        await archive.checkpoint_full(db.chat_id, max_bytes=1)
    assert not db.reads
    assert not pins.active


async def test_repeated_cancellation_holds_pins_until_worker_finishes():
    archive, db, pins = reader(1)
    started, proceed = threading.Event(), threading.Event()
    original = archive.bundles.read_extent

    def slow(*args, **kwargs):
        started.set()
        assert proceed.wait(5)
        return original(*args, **kwargs)

    archive.bundles.read_extent = slow
    task = asyncio.create_task(archive.checkpoint(db.chat_id))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert pins.active and not task.done()
    finally:
        proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not pins.active


async def test_extra_overlapping_publication_is_not_hidden_by_page_limit():
    archive, db, pins = reader(4096)
    duplicate = json.loads(db.bodies[("4096", 1)])
    db.add(4097, "event", duplicate, event_first=4096, event_last=4096)
    with pytest.raises(IntegrityError):
        await archive.checkpoint(db.chat_id)
    assert not pins.active


async def test_stream_exhaustion_rechecks_version_after_last_yield():
    archive, db, pins = reader(1)
    pages = archive.iter_checkpoint_pages(db.chat_id)
    assert not (await anext(pages)).has_more
    db.chat_row["epoch"] += 1
    with pytest.raises(ConflictError):
        await anext(pages)
    assert not pins.active


async def test_zero_tail_needs_no_nas_and_fits_zero_budgets():
    archive, db, pins = reader(0)
    state = await archive.checkpoint_full(
        db.chat_id,
        max_events=0,
        max_bytes=0,
    )
    assert not state.events and not db.reads and not db.event_queries
    assert not pins.sizes


async def test_shared_event_extent_can_straddle_active_watermark():
    archive, db, pins = reader(33, active=True, shared=True, group=17)
    record = json.loads(db.bodies[("1", 0)])
    record["applied_event_sequence"] = 10
    db.add(1, "checkpoint", record, checkpoint_event_watermark=10)
    state = await archive.checkpoint_full(db.chat_id)
    assert [e.sequence for e in state.events] == list(range(11, 34))
    assert not pins.active


async def test_record_watermark_mismatch_is_integrity_error():
    archive, db, pins = reader(2, active=True)
    db.objects[3]["checkpoint_event_watermark"] = 1
    with pytest.raises(IntegrityError, match="metadata"):
        await archive.checkpoint_record(db.chat_id)
    assert not db.event_queries and not pins.active


@pytest.mark.parametrize("limit", [0, -1, 4097, True, 1.5])
async def test_invalid_page_capacity_fails_before_any_nas(limit):
    archive, db, pins = reader(1, active=True)
    with pytest.raises(ValueError, match="page"):
        await archive.checkpoint_page(db.chat_id, limit=limit)
    assert not db.reads and not pins.sizes


async def test_deleted_chat_rejects_continuation_as_conflict():
    archive, db, pins = reader(2)
    page = await archive.checkpoint_prefix(db.chat_id, 1)
    db.chat_row = None
    with pytest.raises(ConflictError):
        await archive.checkpoint_page(db.chat_id, cursor=page.next_cursor)
    assert not pins.active


async def test_offline_full_traversal_uses_read_only_adapter_without_sql_writes():
    from swe.app.conversation_storage.migration.manifest import SourceBoundary
    from swe.app.conversation_storage.migration.readonly import (
        ReadOnlyDatabase,
        read_only_adapter,
    )

    archive, db, _ = reader(4097, active=True)
    for row in db.objects.values():
        row.update(scope_kind="workspace", **archive.scope.fields)
    boundary = SourceBoundary(
        "snapshot",
        "2026-11-05T00:00:00Z",
        "old@sha256:" + "1" * 64,
        ("writers and GC stopped",),
    )
    archive.db = ReadOnlyDatabase(db, boundary)
    read_only_adapter(archive)
    archive.bundles.read_extent = (
        lambda path, offset, *_args, **_kwargs: db.bodies[(path, offset)]
    )
    observed = []
    async for page in archive.iter_checkpoint_pages(db.chat_id):
        observed.extend(e.sequence for e in page.events)
    assert observed == list(range(1, 4098))

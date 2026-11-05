# -*- coding: utf-8 -*-
"""Opt-in real SQL/pins and durable bundle checkpoint boundary acceptance.

These tests deliberately construct actual event counts and publications. They
are expensive; no lowered MAX_PIN_BATCH or database substitutes stand in for
MySQL acceptance. The module gate prevents application config reads when the
isolated test database has not been explicitly configured.
"""

import asyncio
from dataclasses import replace
import json
import os
import threading

import pytest

from swe.agents.memory.chat_checkpoint import CheckpointEvent, CheckpointRecord
from swe.app.conversation_storage.archive_reader import ArchiveReader
from swe.app.conversation_storage.bundle import (
    BundleBlock,
    BundleStore,
    new_operation_key,
)
from swe.app.conversation_storage.errors import ConflictError
from swe.app.conversation_storage.identity import WorkspaceScope
from tests.integrated.conversation_storage.test_schema import (
    NOW,
    insert_chat,
    insert_session,
)

pytestmark = pytest.mark.skipif(
    not (
        os.environ.get("SWE_TEST_DB_HOST")
        and os.environ.get("SWE_TEST_DB_NAME")
    ),
    reason="isolated MySQL not configured; checkpoint acceptance not executed",
)


def payload(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def make_row(bundles, session, chat, first, last, *, record=None):
    operation = new_operation_key()
    blocks = []
    if first is not None:
        events = [
            CheckpointEvent.new(
                sequence=sequence,
                epoch=1,
                type="message_added",
                facts={"message_id": str(sequence), "role": "user"},
                source_refs=(f"message:{sequence}",),
            ).to_dict()
            for sequence in range(first, last + 1)
        ]
        blocks.append(BundleBlock("events", "event", payload(events)))
    if record is not None:
        blocks.append(
            BundleBlock("checkpoint", "checkpoint", payload(record.to_dict())),
        )
    index = bundles.publish(
        operation,
        blocks,
        metadata={"operation_key": operation},
    )
    event = index.blocks.get("events")
    checkpoint = index.blocks.get("checkpoint")
    return {
        "service_unit_key": b"schema-test-service",
        "operation_key": operation.encode(),
        "tenant_key": b"tenant",
        "source_key": b"source",
        "agent_key": b"agent",
        "session_id": session,
        "chat_id": chat,
        "kind": "import",
        "state": "committed",
        "generation": 0,
        "owner_key": b"checkpoint-acceptance",
        "epoch": 1,
        "relative_path": index.relative_path.encode(),
        "format_key": b"swe-bundle-v1",
        "byte_length": index.byte_length,
        "durable_at": NOW,
        "event_first": first,
        "event_last": last,
        "event_offset": event.offset if event else None,
        "event_length": event.length if event else None,
        "checkpoint_revision": record.revision if record else None,
        "checkpoint_base_revision": record.source_revision if record else None,
        "checkpoint_source_high": 0 if record else None,
        "checkpoint_event_watermark": (
            record.applied_event_sequence if record else None
        ),
        "checkpoint_offset": checkpoint.offset if checkpoint else None,
        "checkpoint_length": checkpoint.length if checkpoint else None,
        "created_at": NOW,
        "committed_at": NOW,
    }


async def insert_rows(db, rows):
    # Multi-row SQL avoids thousands of setup transaction/lease round trips;
    # the reader under test still performs real RetentionStore operations.
    columns = tuple(rows[0])
    for start in range(0, len(rows), 128):
        batch = rows[start : start + 128]
        values = ",".join(
            ["(" + ",".join(["%s"] * len(columns)) + ")"] * len(batch),
        )
        await db.execute(
            f"INSERT INTO swe_conversation_publications ({','.join(columns)}) VALUES {values}",
            tuple(row[column] for row in batch for column in columns),
        )


async def setup(db, root, count, *, active="none", events_per_publication=1):
    session = await insert_session(db)
    chat = await insert_chat(db, session, epoch=1, event_high=count)
    bundles = BundleStore(root)
    record = replace(CheckpointRecord.new(chat_id="chat", epoch=1), revision=1)

    def prepare():
        rows = []
        if active == "independent":
            rows.append(
                make_row(bundles, session, chat, None, None, record=record),
            )
        for first in range(1, count + 1, events_per_publication):
            rows.append(
                make_row(
                    bundles,
                    session,
                    chat,
                    first,
                    min(count, first + events_per_publication - 1),
                    record=(
                        record if active == "shared" and first == 1 else None
                    ),
                ),
            )
        return rows

    await insert_rows(db, await asyncio.to_thread(prepare))
    if active != "none":
        await db.execute(
            "UPDATE swe_conversation_chats SET active_checkpoint_publication_id="
            "(SELECT id FROM swe_conversation_publications WHERE chat_id=%s AND checkpoint_revision=1),"
            "checkpoint_revision=1 WHERE id=%s",
            (chat, chat),
        )
    scope = WorkspaceScope(
        "tenant",
        "source",
        "agent",
        service_unit_id="schema-test-service",
    )
    return ArchiveReader(db, scope, bundles), session, chat


async def assert_released(db):
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM swe_conversation_retentions WHERE kind='reader' AND state='active'",
    )
    assert row["n"] == 0
    batches = await db.fetch_all(
        "SELECT retention_key,COUNT(*) AS n FROM swe_conversation_retentions "
        "WHERE kind='reader' GROUP BY retention_key",
    )
    assert all(batch["n"] <= 4096 for batch in batches)


@pytest.mark.parametrize("count", [4095, 4096, 4097, 8193, 16385])
@pytest.mark.parametrize("active", ["none", "independent"])
@pytest.mark.slow
async def test_real_publication_boundaries_are_complete(
    conversation_test_database,
    tmp_path,
    count,
    active,
):
    db = conversation_test_database
    reader, _, _ = await setup(db, tmp_path, count, active=active)
    state = await reader.checkpoint_full("chat")
    assert [event.sequence for event in state.events] == list(
        range(1, count + 1),
    )
    assert state.record.revision == (1 if active != "none" else 0)
    await assert_released(db)


@pytest.mark.parametrize("per_publication", [1, 128])
async def test_shared_active_and_event_publication_crosses_page(
    conversation_test_database,
    tmp_path,
    per_publication,
):
    db = conversation_test_database
    reader, _, _ = await setup(
        db,
        tmp_path,
        8193,
        active="shared",
        events_per_publication=per_publication,
    )
    first = await reader.checkpoint_prefix("chat", 4095)
    assert first.has_more and len(first.events) == 4095
    second = await reader.checkpoint_page(
        "chat",
        cursor=first.next_cursor,
        limit=4096,
    )
    assert second.has_more and len(second.events) == 4096
    third = await reader.checkpoint_page(
        "chat",
        cursor=second.next_cursor,
        limit=4096,
    )
    assert not third.has_more and len(third.events) == 2
    assert [
        event.sequence
        for event in (*first.events, *second.events, *third.events)
    ] == list(range(1, 8194))
    await assert_released(db)


async def test_append_after_capture_is_excluded_from_continuation(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    reader, session, chat = await setup(db, tmp_path, 6)
    first = await reader.checkpoint_page("chat", limit=3)
    rows = await asyncio.to_thread(
        make_row,
        reader.bundles,
        session,
        chat,
        7,
        7,
    )
    await insert_rows(db, [rows])
    await db.execute(
        "UPDATE swe_conversation_chats SET event_high=7 WHERE id=%s",
        (chat,),
    )
    second = await reader.checkpoint_page(
        "chat",
        cursor=first.next_cursor,
        limit=3,
    )
    assert [
        event.sequence for event in (*first.events, *second.events)
    ] == list(range(1, 7))
    assert not second.has_more
    assert len((await reader.checkpoint_full("chat")).events) == 7
    await assert_released(db)


@pytest.mark.parametrize(
    "change",
    ["epoch=2,event_high=0", "checkpoint_revision=checkpoint_revision+1"],
)
async def test_changed_authority_rejects_continuation(
    conversation_test_database,
    tmp_path,
    change,
):
    db = conversation_test_database
    reader, _, chat = await setup(db, tmp_path, 6)
    first = await reader.checkpoint_page("chat", limit=3)
    await db.execute(
        f"UPDATE swe_conversation_chats SET {change} WHERE id=%s",
        (chat,),
    )
    with pytest.raises(ConflictError):
        await reader.checkpoint_page("chat", cursor=first.next_cursor, limit=3)
    await assert_released(db)


async def test_cancel_keeps_real_pins_until_nas_drains_and_gc_refuses(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    from swe.app.chat_sharing.store import ChatShareStore
    from swe.app.conversation_storage.gc import GarbageCollector
    from tests.integrated.conversation_storage.conftest import (
        SQL_ROOT,
        apply_sql_script,
    )

    db = conversation_test_database
    reader, _, chat = await setup(db, tmp_path, 2)
    await ChatShareStore(db).ensure_schema()
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    started, release = threading.Event(), threading.Event()
    original = reader.bundles.read_extent

    def blocked(*args, **kwargs):
        started.set()
        assert release.wait(15), "test NAS gate was not released"
        return original(*args, **kwargs)

    monkeypatch.setattr(reader.bundles, "read_extent", blocked)
    task = asyncio.create_task(reader.checkpoint_full("chat"))
    try:
        assert await asyncio.to_thread(started.wait, 10)
        publication = await db.fetch_one(
            "SELECT id FROM swe_conversation_publications WHERE chat_id=%s ORDER BY event_first LIMIT 1",
            (chat,),
        )
        gc = GarbageCollector(
            db,
            reader.scope,
            reader.bundles,
            physical_enabled=True,
        )
        with pytest.raises(ConflictError, match="retention"):
            await gc.mark(publication["id"])
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        pins = await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_retentions WHERE kind='reader' AND state='active'",
        )
        assert pins["n"] > 0
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    await assert_released(db)

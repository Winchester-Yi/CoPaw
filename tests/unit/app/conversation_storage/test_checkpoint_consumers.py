# -*- coding: utf-8 -*-
"""Consumers must select explicit contracts and exhaust complete verification."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from swe.agents.memory.chat_checkpoint import CheckpointRecord, CheckpointEvent
from swe.app.conversation_storage.archive_runtime import MySQLArchiveStore
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.errors import ConflictError

SCOPE = WorkspaceScope("t", "s", "a", service_unit_id="checkpoint-consumers")


def pages(record, events, finished, *, failure=None):
    async def iterate(*_args, **_kwargs):
        for start in range(0, max(1, len(events)), 3):
            yield SimpleNamespace(
                record=record,
                events=events[start : start + 3],
                current_epoch=1,
            )
        if failure:
            raise failure
        finished.append(True)

    return iterate


async def test_facade_record_is_independent_of_full_or_event_reads():
    store = MySQLArchiveStore(None, SCOPE, None, None)
    record = CheckpointRecord.new(chat_id="chat", epoch=1)
    store.reader = SimpleNamespace(
        checkpoint_record=AsyncMock(return_value=record),
    )
    head = {"epoch": 1, "checkpoint_revision": 0}
    assert await store.read_checkpoint_record("chat", head) is record
    store.reader.checkpoint_record.assert_awaited_once_with("chat", head)


async def test_facade_selects_full_prefix_and_page_contracts():
    store = MySQLArchiveStore(None, SCOPE, None, None)
    store.reader = SimpleNamespace(
        checkpoint_full=AsyncMock(return_value="full"),
        checkpoint_prefix=AsyncMock(return_value="prefix"),
        checkpoint_page=AsyncMock(return_value="page"),
    )
    assert await store.read_checkpoint_state("chat") == "full"
    assert await store.read_checkpoint_prefix("chat", 5) == "prefix"
    assert (
        await store.read_checkpoint_page("chat", cursor="cursor", limit=5)
        == "page"
    )


def source_events(count=8):
    return tuple(
        CheckpointEvent.new(sequence=i, epoch=1, type="updated")
        for i in range(1, count + 1)
    )


@pytest.mark.parametrize("fail_at_end", [False, True])
async def test_projection_keeps_only_five_events_but_exhausts_verification(
    fail_at_end,
):
    store = MySQLArchiveStore(None, SCOPE, None, None)
    record, events, finished = (
        CheckpointRecord.new(chat_id="chat", epoch=1),
        source_events(),
        [],
    )
    store.reader = SimpleNamespace(
        iter_checkpoint_pages=pages(
            record,
            events,
            finished,
            failure=(
                ConflictError("changed after last page")
                if fail_at_end
                else None
            ),
        ),
    )
    if fail_at_end:
        with pytest.raises(ConflictError):
            await store.read_checkpoint_projection("chat")
    else:
        state = await store.read_checkpoint_projection("chat")
        assert state.events == events[-5:] and state.record == record
        assert finished == [True]


@pytest.mark.parametrize(
    "count,fail_at_end",
    [(0, False), (8, False), (8, True)],
)
async def test_migration_uses_all_pages_including_final_validation(
    monkeypatch,
    count,
    fail_at_end,
):
    from swe.app.conversation_storage.migration import verify

    record, events, finished = (
        CheckpointRecord.new(chat_id="chat", epoch=1),
        source_events(count),
        [],
    )
    reader = SimpleNamespace(
        iter_checkpoint_pages=pages(
            record,
            events,
            finished,
            failure=ConflictError("late conflict") if fail_at_end else None,
        ),
    )
    monkeypatch.setattr(verify, "ArchiveReader", lambda *_: reader)
    monkeypatch.setattr(verify, "read_only_adapter", lambda value: value)
    source = {
        "archive": {
            "epochs": [
                {
                    "epoch": 1,
                    "record": record.to_dict(),
                    "events": [e.to_dict() for e in events],
                },
            ],
        },
    }
    if fail_at_end:
        with pytest.raises(ConflictError):
            await verify._verify_checkpoint(None, None, SCOPE, "chat", source)
    else:
        await verify._verify_checkpoint(None, None, SCOPE, "chat", source)
        assert finished == [True]


async def test_migration_rejects_missing_tail(monkeypatch):
    from swe.app.conversation_storage.migration import verify

    record, events = (
        CheckpointRecord.new(chat_id="chat", epoch=1),
        source_events(),
    )
    reader = SimpleNamespace(
        iter_checkpoint_pages=pages(record, events[:-1], []),
    )
    monkeypatch.setattr(verify, "ArchiveReader", lambda *_: reader)
    monkeypatch.setattr(verify, "read_only_adapter", lambda value: value)
    source = {
        "archive": {
            "epochs": [
                {
                    "epoch": 1,
                    "record": record.to_dict(),
                    "events": [e.to_dict() for e in events],
                },
            ],
        },
    }
    with pytest.raises(ValueError, match="checkpoint/event"):
        await verify._verify_checkpoint(None, None, SCOPE, "chat", source)


@pytest.mark.parametrize("fail_at_end", [False, True])
async def test_restore_exhausts_checkpoint_pages(monkeypatch, fail_at_end):
    from swe.app.conversation_storage import (
        archive_reader,
        backup,
        chat_repository,
        history,
        shares,
    )

    record, finished = CheckpointRecord.new(chat_id="chat", epoch=1), []
    reader = SimpleNamespace(
        iter_checkpoint_pages=pages(
            record,
            source_events(),
            finished,
            failure=ConflictError("late conflict") if fail_at_end else None,
        ),
    )
    monkeypatch.setattr(archive_reader, "ArchiveReader", lambda *_: reader)
    monkeypatch.setattr(
        chat_repository,
        "MySQLChatRepository",
        lambda *_: SimpleNamespace(get_chat=AsyncMock(return_value=object())),
    )
    monkeypatch.setattr(
        history,
        "HistoryStore",
        lambda *_args, **_kwargs: SimpleNamespace(
            current=AsyncMock(),
            archive_page=AsyncMock(
                return_value=SimpleNamespace(next_cursor=None),
            ),
        ),
    )
    monkeypatch.setattr(shares, "ShareStorage", Mock())
    document = {
        "scope": SCOPE.values,
        "snapshot": {
            "sessions": [],
            "chats": [
                {"id": 1, "external_chat_key": b"chat", "deleted_at": None},
            ],
            "turns": [],
            "shares": [],
        },
    }
    if fail_at_end:
        with pytest.raises(ConflictError):
            await backup._verify_restored_readers(None, None, document)
    else:
        await backup._verify_restored_readers(None, None, document)
        assert finished == [True]


@pytest.mark.parametrize(
    "stale",
    [None, "high", "revision", "active_publication_id", "epoch"],
)
async def test_compact_reads_only_source_prefix_and_keeps_captured_metadata(
    stale,
):
    from datetime import datetime
    from agentscope.message import Msg
    from swe.app.conversation_storage.archive_runtime import ArchiveCommit

    store = MySQLArchiveStore(None, SCOPE, None, None)
    message = Msg("user", "one", "user")
    record = CheckpointRecord.new(chat_id="chat", epoch=1)
    event = CheckpointEvent.new(
        sequence=1,
        epoch=1,
        type="message_added",
        facts={"message_id": message.id, "role": message.role},
        source_refs=(f"message:{message.id}",),
    )
    context = {
        "id": 2,
        "epoch": 1,
        "event_high": 8193,
        "checkpoint_revision": 0,
        "active_checkpoint_publication_id": None,
    }
    store._context = AsyncMock(return_value=({}, context))
    store._source_rows = AsyncMock(
        return_value=[
            {
                "id": 3,
                "role_key": b"user",
                "source_msg_key": message.id.encode(),
                "source_occurrence_no": 0,
                "kind_key": b"message",
                "task_run_key": None,
                "origin_chat_id": 2,
                "turn_id": None,
                "context_epoch": 1,
                "session_order": 1,
            },
        ],
    )
    captured = {
        "epoch": 1,
        "revision": 0,
        "active_publication_id": None,
        "high": 8193,
    }
    if stale:
        captured[stale] = (captured[stale] or 0) + 1
    store.read_checkpoint_prefix = AsyncMock(
        return_value=SimpleNamespace(
            record=record,
            events=(event,),
            current_epoch=1,
            snapshot=SimpleNamespace(**captured),
        ),
    )
    store.read_checkpoint_state = AsyncMock(
        side_effect=AssertionError("unbounded compact read"),
    )
    store._publish = AsyncMock(
        return_value=ArchiveCommit(
            {},
            {"committed_at": datetime(2026, 11, 5)},
        ),
    )
    if stale:
        with pytest.raises(ConflictError):
            await store.compact(
                "chat",
                [message],
                recovery_state={},
                authority=None,
            )
        store._publish.assert_not_awaited()
    else:
        result = await store.compact(
            "chat",
            [message],
            recovery_state={},
            authority=None,
        )
        assert result.record.applied_event_sequence == 1
    store.read_checkpoint_prefix.assert_awaited_once_with("chat", 1)
    store.read_checkpoint_state.assert_not_awaited()


async def test_degraded_binding_reads_record_then_bounded_projection_tail():
    import asyncio
    from agentscope.memory import InMemoryMemory
    from agentscope.message import Msg

    memory = InMemoryMemory()
    execution = SimpleNamespace(
        mutation_lock=asyncio.Lock(),
        pending_operation=None,
        accept_committed_state=Mock(),
    )
    store = MySQLArchiveStore(None, SCOPE, None, None)
    store.bind_memory(memory, "chat", execution, lambda: {}, None)
    record = CheckpointRecord.new(chat_id="chat", epoch=1)
    store.read_checkpoint_record = AsyncMock(return_value=record)
    store.read_checkpoint_projection = AsyncMock(
        return_value=SimpleNamespace(record=record, events=()),
    )
    store.read_checkpoint_state = AsyncMock(
        side_effect=AssertionError("unbounded degraded read"),
    )
    store.write_active_checkpoint = AsyncMock(
        return_value=SimpleNamespace(recovery_state={}),
    )
    await memory.install_degraded_checkpoint([Msg("user", "one", "user")])
    store.read_checkpoint_record.assert_awaited_once_with("chat")
    store.read_checkpoint_state.assert_not_awaited()
    store.read_checkpoint_projection.assert_awaited_once_with("chat")

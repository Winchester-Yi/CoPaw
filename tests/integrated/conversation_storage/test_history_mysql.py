# -*- coding: utf-8 -*-
import json
from types import SimpleNamespace

import pytest
from agentscope.message import Msg

from swe.app.conversation_storage.bundle import (
    BundleBlock,
    BundleStore,
    new_operation_key,
)
from swe.app.conversation_storage.errors import ConflictError, IntegrityError
from swe.app.conversation_storage.history import HistoryStore
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.runner import api
from tests.integrated.conversation_storage.test_schema import (
    insert_session,
    insert_chat,
    insert_row,
    insert_publication,
    NOW,
)


def source(
    key,
    role="assistant",
    content="text",
    stamp="2026-10-30T00:00:01Z",
):
    msg = Msg(
        "user" if role == "user" else "assistant",
        content,
        role,
        timestamp=stamp,
    )
    msg.id = key
    return msg.to_dict()


async def dataset(db, tmp_path):
    session = await insert_session(db)
    first = await insert_chat(db, session, b"chat-a")
    second = await insert_chat(db, session, b"chat-b", registry_order=2)
    await db.execute(
        "UPDATE swe_conversation_sessions SET history_high=100 WHERE id=%s",
        (session,),
    )
    bundles = BundleStore(tmp_path)
    return (
        session,
        first,
        second,
        HistoryStore(
            db,
            WorkspaceScope(
                "tenant",
                "source",
                "agent",
                service_unit_id="schema-test-service",
            ),
            bundles,
            cursor_secret=b"shared-test-secret",
        ),
    )


async def add_messages(db, store, session, chat, items, *, boundary=False):
    operation = new_operation_key()
    blocks = [
        BundleBlock(
            str(i),
            "message",
            json.dumps(payload, ensure_ascii=False).encode(),
        )
        for i, (_, payload, _) in enumerate(items)
    ]
    index = store.bundles.publish(
        operation,
        blocks,
        metadata={"operation_key": operation},
    )
    values = {
        "state": "committed",
        "session_id": session,
        "chat_id": chat,
        "byte_length": index.byte_length,
        "durable_at": NOW,
        "committed_at": NOW,
        "relative_path": index.relative_path.encode(),
    }
    if boundary:
        values.update(
            boundary_key=b"boundary",
            boundary_order=1,
            archive_first_order=1,
            archive_last_order=max(order for _, _, order in items),
            archive_source_count=len(items),
            archive_display_count=len(items),
            archive_first_source_msg_key=items[0][1]["id"].encode(),
            archive_last_source_msg_key=items[-1][1]["id"].encode(),
        )
    publication = await insert_publication(db, operation.encode(), **values)
    for i, (layer, payload, order) in enumerate(items):
        extent = index.blocks[str(i)]
        await insert_row(
            db,
            "swe_conversation_messages",
            {
                "session_id": session,
                "origin_chat_id": chat if layer == "anchor" else None,
                "archive_chat_id": chat if layer == "archive" else None,
                "layer": layer,
                "session_order": None if layer == "archive" else order,
                "archive_order": order if layer == "archive" else None,
                "source_msg_key": payload["id"].encode(),
                "source_occurrence_no": i,
                "role_key": payload["role"].encode(),
                "kind_key": b"message",
                "publication_id": publication,
                "byte_offset": extent.offset,
                "byte_length": extent.length,
                "archive_publication_id": (
                    publication if layer == "archive" else None
                ),
                "context_epoch": 0,
                "in_context": int(layer == "memory"),
            },
        )
    if boundary:
        await db.execute(
            "UPDATE swe_conversation_chats SET history_high=%s WHERE id=%s",
            (max(order for _, _, order in items), chat),
        )
    return publication, index


async def test_shared_memory_and_chat_anchors_archive_are_independent(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session, a, b, store = await dataset(db, tmp_path)
    await add_messages(
        db,
        store,
        session,
        a,
        [
            ("memory", source("shared", "assistant"), 1),
            ("anchor", source("anchor-a", "user"), 2),
        ],
    )
    await add_messages(
        db,
        store,
        session,
        b,
        [("anchor", source("anchor-b", "user"), 3)],
    )
    await add_messages(
        db,
        store,
        session,
        a,
        [("archive", source("archived-a"), 1)],
        boundary=True,
    )
    first = await store.current("chat-a")
    second = await store.current("chat-b")
    assert [api._message_original_id(m) for m in first.messages] == [
        "shared",
        "anchor-a",
    ]
    assert [api._message_original_id(m) for m in second.messages] == [
        "shared",
        "anchor-b",
    ]
    assert first.archive.has_more and not second.archive.has_more
    traced = await store.tracing_view("chat-a")
    assert traced.chat is None and not traced.archive.has_more
    assert [api._message_original_id(m) for m in traced.messages] == ["shared"]


async def test_archive_limit_counts_sources_and_cursor_keeps_original_high(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session, a, _, store = await dataset(db, tmp_path)
    fragment = [
        {"type": "thinking", "thinking": "x"},
        {"type": "text", "text": "y"},
    ]
    await add_messages(
        db,
        store,
        session,
        a,
        [
            ("archive", source("one"), 1),
            ("archive", source("two", content=fragment), 2),
            ("archive", source("three"), 3),
        ],
        boundary=True,
    )
    page = await store.archive_page("chat-a", limit=2)
    assert len(page.messages) == 3 and page.has_more and page.next_cursor
    assert [api._message_original_id(m) for m in page.messages] == [
        "two",
        "two",
        "three",
    ]
    older = await store.archive_page(
        "chat-a",
        limit=2,
        before=page.next_cursor,
    )
    assert [api._message_original_id(m) for m in older.messages] == ["one"]
    assert not older.has_more
    with pytest.raises(ValueError, match="cursor"):
        await store.archive_page("chat-b", before=page.next_cursor)


async def test_legacy_answer_turn_is_point_range_and_unknown_status_stays_none(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session, a, _, store = await dataset(db, tmp_path)
    await add_messages(
        db,
        store,
        session,
        a,
        [
            ("memory", source("q", "user"), 1),
            ("memory", source("answer"), 2),
            ("memory", source("next", "user"), 3),
            ("memory", source("other"), 4),
        ],
    )
    result = await store.answer_turn(
        "q",
        chat_id="chat-a",
        session_id="wrong-ignored",
        user_id="alice",
    )
    assert [api._message_original_id(m) for m in result.messages] == [
        "q",
        "answer",
    ]
    assert result.turn_status is None
    with pytest.raises(KeyError):
        await store.answer_turn("answer", chat_id="chat-a", user_id="alice")


async def test_import_display_order_preserves_interleaved_error_answer_turn(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session, chat, _, store = await dataset(db, tmp_path)
    from swe.app.conversation_storage.migration.importer import (
        display_order_messages,
        extract_messages,
    )

    messages = display_order_messages(
        extract_messages(
            {
                "agent": {
                    "memory": {
                        "content": [
                            source("q1", "user", stamp="2026-10-30T00:00:01Z"),
                            source("q2", "user", stamp="2026-10-30T00:00:03Z"),
                        ],
                    },
                },
                "model_call_failed_messages": [
                    {
                        "id": "e1",
                        "role": "assistant",
                        "code": "model_call_failed",
                        "message": "failed",
                        "timestamp": "2026-10-30T00:00:02Z",
                    },
                ],
            },
            context_epoch=0,
        ),
    )
    await add_messages(
        db,
        store,
        session,
        chat,
        [
            (item["layer"], item["payload"], order)
            for order, item in enumerate(messages, start=1)
        ],
    )

    result = await store.answer_turn("q1", chat_id="chat-a", user_id="alice")

    assert [
        api._message_original_id(message) for message in result.messages
    ] == [
        "q1",
        "e1",
    ]


async def test_history_rechecks_deletion_after_nas_and_releases_pins(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session, a, _, store = await dataset(db, tmp_path)
    await add_messages(
        db,
        store,
        session,
        a,
        [("memory", source("visible"), 1)],
    )
    original = store._payloads

    async def delete_after_read(snapshot):
        payloads = await original(snapshot)
        await db.execute(
            "UPDATE swe_conversation_chats "
            "SET deleted_at=UTC_TIMESTAMP(6) WHERE id=%s",
            (a,),
        )
        return payloads

    store._payloads = delete_after_read
    with pytest.raises(ConflictError):
        await store.current("chat-a")
    assert not await db.fetch_all(
        "SELECT id FROM swe_conversation_retentions "
        "WHERE kind='reader' AND state='active'",
    )


async def test_numeric_usage_never_reads_nas_and_future_version_is_unavailable(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session, _, _, store = await dataset(db, tmp_path)
    store.bundles.read_extent = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("NAS read"),
    )
    assert (await store.context_usage("chat-a")).available is False
    await db.execute(
        "UPDATE swe_conversation_sessions SET context_usage_text=%s "
        "WHERE id=%s",
        (json.dumps({"schema_version": 99}), session),
    )
    assert (await store.context_usage("chat-a")).available is False


async def test_answer_turn_honors_stored_chat_and_original_status(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session, a, b, store = await dataset(db, tmp_path)
    await add_messages(
        db,
        store,
        session,
        a,
        [
            ("memory", source("question", "user"), 1),
            ("memory", source("response"), 2),
        ],
    )
    await insert_row(
        db,
        "swe_conversation_turns",
        {
            "session_id": session,
            "chat_id": b,
            "question_msg_key": b"question",
            "turn_order": 1,
            "context_epoch": 0,
            "generation": 0,
            "state": "succeeded",
            "history_status": "cancelled",
        },
    )
    with pytest.raises(KeyError):
        await store.answer_turn("question", chat_id="chat-a", user_id="alice")
    result = await store.answer_turn(
        "question",
        session_id="session",
        user_id="alice",
    )
    assert result.chat.id == "chat-b" and result.turn_status == "cancelled"


async def test_history_route_index_and_current_identity_authorization(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session, a, _, store = await dataset(db, tmp_path)
    await add_messages(
        db,
        store,
        session,
        a,
        [("memory", source("visible"), 1)],
    )
    workspace = SimpleNamespace(
        conversation_storage=SimpleNamespace(history=store),
        agent_id="agent",
    )
    manager = SimpleNamespace(get_chat=store.chats.get_chat)
    request = SimpleNamespace(
        state=SimpleNamespace(
            user_id="alice",
            source_id="source",
            agent_id="agent",
        ),
    )
    result = await api.get_chat(
        request,
        "chat-a",
        mgr=manager,
        session=object(),
        workspace=workspace,
    )
    assert [api._message_original_id(m) for m in result.messages] == [
        "visible",
    ]
    request.state.user_id = "other"
    with pytest.raises(Exception) as error:
        await api.get_chat(
            request,
            "chat-a",
            mgr=manager,
            session=object(),
            workspace=workspace,
        )
    assert error.value.status_code == 404


async def test_deleted_chat_after_pin_before_nas_refuses_read(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session, a, _, store = await dataset(db, tmp_path)
    await add_messages(
        db,
        store,
        session,
        a,
        [("memory", source("never-read"), 1)],
    )
    original = store._snapshot

    async def delete_after_snapshot(*args, **kwargs):
        snapshot = await original(*args, **kwargs)
        await db.execute(
            "UPDATE swe_conversation_chats "
            "SET deleted_at=UTC_TIMESTAMP(6) WHERE id=%s",
            (a,),
        )
        return snapshot

    store._snapshot = delete_after_snapshot
    store.bundles.read_extent = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("NAS read after deletion"),
    )
    with pytest.raises(ConflictError):
        await store.current("chat-a")
    assert not await db.fetch_all(
        "SELECT id FROM swe_conversation_retentions "
        "WHERE kind='reader' AND state='active'",
    )


async def test_all_shared_layers_duplicates_and_archive_anchor_suppression(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session, a, _, store = await dataset(db, tmp_path)
    task = {
        "id": "task",
        "role": "assistant",
        "content": "task text",
        "timestamp": "2026-10-30T00:00:02Z",
    }
    error = {
        "id": "error",
        "role": "assistant",
        "code": "model_call_failed",
        "message": "failed",
        "timestamp": "2026-10-30T00:00:03Z",
    }
    await add_messages(
        db,
        store,
        session,
        a,
        [
            ("memory", source("duplicate"), 1),
            ("memory", source("duplicate"), 2),
            ("task", task, 3),
            ("error", error, 4),
            ("anchor", source("archived-anchor", "user"), 5),
        ],
    )
    await add_messages(
        db,
        store,
        session,
        a,
        [("archive", source("archived-anchor", "user"), 1)],
        boundary=True,
    )
    first = await store.current("chat-a")
    second = await store.current("chat-b")
    assert [api._message_original_id(m) for m in first.messages] == [
        "duplicate",
        "duplicate",
        "task",
        "error",
    ]
    assert [api._message_original_id(m) for m in second.messages] == [
        "duplicate",
        "duplicate",
        "task",
        "error",
    ]
    tracing = await store.tracing_view("chat-a")
    assert [api._message_original_id(m) for m in tracing.messages] == [
        "duplicate",
        "duplicate",
        "task",
    ]


async def test_corrupt_publication_scope_or_extent_fails_not_empty(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session, a, _, store = await dataset(db, tmp_path)
    publication, _ = await add_messages(
        db,
        store,
        session,
        a,
        [("memory", source("body"), 1)],
    )
    await db.execute(
        "UPDATE swe_conversation_publications SET source_key=%s WHERE id=%s",
        (b"other", publication),
    )
    with pytest.raises(IntegrityError, match="scope"):
        await store.current("chat-a")
    await db.execute(
        "UPDATE swe_conversation_publications SET source_key=%s WHERE id=%s",
        (b"source", publication),
    )
    await db.execute(
        "UPDATE swe_conversation_messages SET byte_length=999999 "
        "WHERE publication_id=%s",
        (publication,),
    )
    with pytest.raises(IntegrityError, match="extent"):
        await store.current("chat-a")


async def test_numeric_snapshot_stale_only_for_active_chat(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session, a, _, store = await dataset(db, tmp_path)
    snapshot = {
        "schema_version": 1,
        "used_tokens": 10,
        "max_tokens": 100,
        "remaining_tokens": 90,
        "usage_ratio": 0.1,
        "system_context_tokens": 2,
        "tool_definition_tokens": 3,
        "conversation_tokens": 5,
        "governance_threshold_ratio": 0.5,
        "active_threshold_ratio": 0.7,
        "emergency_threshold_ratio": 0.9,
        "status": "normal",
        "estimated": True,
        "as_of": "2026-10-30T00:00:00Z",
    }
    turn = await insert_row(
        db,
        "swe_conversation_turns",
        {
            "session_id": session,
            "chat_id": a,
            "question_msg_key": b"q",
            "turn_order": 1,
            "context_epoch": 0,
            "generation": 1,
            "state": "running",
        },
    )
    await db.execute(
        "UPDATE swe_conversation_sessions SET context_usage_text=%s,"
        "active_turn_id=%s WHERE id=%s",
        (json.dumps(snapshot), turn, session),
    )
    assert (await store.context_usage("chat-a")).stale is True
    assert (await store.context_usage("chat-b")).stale is False


async def test_archived_turn_stays_hidden_but_visible_typed_turn_is_readable(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session, a, _, store = await dataset(db, tmp_path)
    await add_messages(
        db,
        store,
        session,
        a,
        [
            ("memory", source("cold-q", "user"), 1),
            ("memory", source("cold-a"), 2),
        ],
    )
    turn = await insert_row(
        db,
        "swe_conversation_turns",
        {
            "session_id": session,
            "chat_id": a,
            "question_msg_key": b"cold-q",
            "turn_order": 1,
            "context_epoch": 0,
            "generation": 0,
            "state": "succeeded",
            "history_status": "completed",
        },
    )
    await db.execute(
        "UPDATE swe_conversation_messages SET turn_id=%s,"
        "current_visible=0,in_context=0 WHERE session_id=%s",
        (turn, session),
    )
    await add_messages(
        db,
        store,
        session,
        a,
        [
            ("archive", source("cold-q", "user"), 1),
            ("archive", source("cold-a"), 2),
        ],
        boundary=True,
    )
    await db.execute(
        "UPDATE swe_conversation_messages SET turn_id=%s "
        "WHERE archive_chat_id=%s",
        (turn, a),
    )
    with pytest.raises(KeyError):
        await store.answer_turn("cold-q", chat_id="chat-a", user_id="alice")
    await db.execute(
        "UPDATE swe_conversation_messages SET current_visible=1 "
        "WHERE turn_id=%s AND layer='memory'",
        (turn,),
    )
    result = await store.answer_turn(
        "cold-q",
        chat_id="chat-a",
        user_id="alice",
    )
    assert [api._message_original_id(m) for m in result.messages] == [
        "cold-q",
        "cold-a",
    ]
    assert result.turn_status == "completed"

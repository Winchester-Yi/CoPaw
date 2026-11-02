# -*- coding: utf-8 -*-
"""Runtime façade uses existing workspace bundles and SQL occurrence locators."""

import asyncio
from types import SimpleNamespace

from agentscope.message import Msg
import pytest

from swe.app.conversation_storage.archive_runtime import MySQLArchiveStore
from swe.app.conversation_storage.bundle import BundleStore
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.session_adapter import MySQLSessionAdapter
from tests.integrated.conversation_storage.test_schema import (
    insert_chat,
    insert_session,
)


async def setup(db, root):
    session = await insert_session(db)
    chat = await insert_chat(db, session, b"first", epoch=1)
    second = await insert_chat(
        db,
        session,
        b"second",
        registry_order=2,
        epoch=1,
    )
    bundles = BundleStore(root)
    scope = WorkspaceScope(
        "tenant",
        "source",
        "agent",
        service_unit_id="schema-test-service",
    )
    adapter = MySQLSessionAdapter(db, scope, bundles, channel="console")
    store = MySQLArchiveStore(db, scope, bundles, adapter)
    return session, chat, second, store


def source(key, text):
    message = Msg("user", text, "user", timestamp="2026-10-30T00:00:01Z")
    message.id = key
    return message


async def test_runtime_compact_reuses_original_body_extent_and_dependency_pins(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session, chat, other, store = await setup(db, tmp_path)
    first, second = source("q", "question"), source(
        "q",
        "second occurrence with reused ID",
    )
    state = {"revision": 0, "unknown_module": {"keep": ["all"]}}
    for message in (first, second):
        result = await store.append_message(
            "first",
            message,
            recovery_state=state,
            authority=None,
        )
        state = result.recovery_state
    originals = await db.fetch_all(
        "SELECT * FROM swe_conversation_messages ORDER BY session_order",
    )
    result = await store.compact(
        "first",
        [first],
        recovery_state={
            **state,
            "agent": {
                "memory": {
                    "content": [[second.to_dict(), []]],
                    "_compressed_summary": "new",
                },
            },
        },
        authority=None,
    )
    archived = await db.fetch_one(
        "SELECT * FROM swe_conversation_messages WHERE layer='archive'",
    )
    assert (
        archived["publication_id"],
        archived["byte_offset"],
        archived["byte_length"],
    ) == (
        originals[0]["publication_id"],
        originals[0]["byte_offset"],
        originals[0]["byte_length"],
    )
    assert archived["archive_publication_id"] == result.publication["id"]
    pins = await db.fetch_all(
        "SELECT publication_id FROM swe_conversation_retentions WHERE kind='dependency' AND state='active'",
    )
    assert sorted({row["publication_id"] for row in pins}) == sorted(
        {item["publication_id"] for item in originals},
    )
    assert result.recovery_state["unknown_module"] == {"keep": ["all"]}
    assert (
        await db.fetch_one(
            "SELECT history_high FROM swe_conversation_chats WHERE id=%s",
            (other,),
        )
    )["history_high"] == 0
    evidence = await store.recover_evidence(
        chat_id="first",
        epoch=1,
        refs=["message:q"],
    )
    assert [message.content for message in evidence] == ["question"]
    checkpoint = await store.read_checkpoint_state("first")
    assert checkpoint.record.revision == 1
    assert checkpoint.record.applied_event_sequence == 1
    assert [event.sequence for event in checkpoint.events] == [2, 3]
    reset = await store.reset(
        "first",
        recovery_state=result.recovery_state,
        authority=None,
        reason="clear",
    )
    assert reset.state.current_epoch == 2
    assert (
        await store.recover_evidence(
            chat_id="first",
            epoch=1,
            refs=["message:q"],
        )
        == []
    )
    assert (
        await store.recover_evidence(
            chat_id="first",
            epoch=2,
            refs=["message:q"],
        )
        == []
    )
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_messages WHERE layer='archive'",
        )
    )["n"] == 1


async def test_runtime_binding_updates_complete_root_only_after_compact_confirmation(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    _, _, _, store = await setup(db, tmp_path)
    first = source("q", "one")
    result = await store.append_message(
        "first",
        first,
        recovery_state={"revision": 0, "unknown": {"preserve": True}},
        authority=None,
    )
    memory = SimpleNamespace(content=[(first, [])], _compressed_summary="old")
    current = dict(result.recovery_state)

    def accept(state):
        current.clear()
        current.update(state)

    execution = SimpleNamespace(
        accept_committed_state=accept,
        mutation_lock=asyncio.Lock(),
        pending_operation=None,
    )

    async def root():
        return {
            **current,
            "agent": {
                "memory": {
                    "content": [
                        [message.to_dict(), marks]
                        for message, marks in memory.content
                    ],
                    "_compressed_summary": memory._compressed_summary,
                },
            },
        }

    store.bind_memory(memory, "first", execution, root, None)
    await memory.archive_compacted_messages([first], summary="new summary")
    assert memory.content == []
    assert current["agent"]["memory"]["_compressed_summary"] == "new summary"
    assert current["unknown"] == {"preserve": True}
    restored = await store.session_adapter.get_session_state_dict(
        "session",
        "alice",
    )
    assert restored == current


async def test_source_visibility_race_rolls_back_root_archive_and_dependency(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    from swe.app.conversation_storage.errors import IntegrityError
    from swe.app.conversation_storage import publications

    db = conversation_test_database
    session, _, _, store = await setup(db, tmp_path)
    message = source("q", "one")
    result = await store.append_message(
        "first",
        message,
        recovery_state={"revision": 0},
        authority=None,
    )
    snapshot = publications.snapshot_sources

    async def hide_after_snapshot(service, request, plan):
        value = await snapshot(service, request, plan)
        if value.rows:
            await db.execute(
                "UPDATE swe_conversation_messages SET current_visible=0 WHERE id=%s",
                (next(iter(value.rows)),),
            )
        return value

    monkeypatch.setattr(publications, "snapshot_sources", hide_after_snapshot)
    with pytest.raises(IntegrityError, match="changed"):
        await store.compact(
            "first",
            [message],
            recovery_state=result.recovery_state,
            authority=None,
        )
    assert (
        await db.fetch_one(
            "SELECT state_revision FROM swe_conversation_sessions WHERE id=%s",
            (session,),
        )
    )["state_revision"] == 1
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_messages WHERE layer='archive'",
        )
    )["n"] == 0
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_retentions WHERE kind='dependency' AND state='active'",
        )
    )["n"] == 0
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_retentions WHERE kind='reader' AND state='active'",
        )
    )["n"] == 0


async def test_pending_candidate_uses_explicit_key_and_atomic_checkpoint_archive(
    conversation_test_database,
    tmp_path,
):
    from dataclasses import replace
    from swe.agents.memory.chat_checkpoint import PrecompactionCandidate

    db = conversation_test_database
    _, _, _, store = await setup(db, tmp_path)
    message = source("q", "one")
    result = await store.append_message(
        "first",
        message,
        recovery_state={"revision": 0},
        authority=None,
    )
    active = await store.read_checkpoint_state("first")
    candidate = PrecompactionCandidate.new(
        record=replace(
            active.record,
            revision=1,
            source_revision=0,
            applied_event_sequence=1,
        ),
        base_revision=0,
        applied_event_sequence=1,
        source_message_ids=("q",),
    )
    await store.write_pending_candidate("first", candidate)
    result = await store.compact(
        "first",
        [message],
        recovery_state=result.recovery_state,
        authority=None,
        candidate_id=candidate.id,
    )
    assert result.record.revision == 1
    assert result.record.archived_through == result.boundary.id
    current = await store.read_checkpoint_state("first")
    assert current.record == result.record


async def test_runtime_active_owner_generation_fences_late_compact(
    conversation_test_database,
    tmp_path,
):
    from swe.app.conversation_storage.execution import ExecutionAuthority
    from swe.app.conversation_storage.identity import SessionIdentity
    from swe.app.conversation_storage.errors import ConflictError
    from tests.integrated.conversation_storage.test_schema import insert_row

    db = conversation_test_database
    session, chat, _, store = await setup(db, tmp_path)
    message = source("q", "one")
    state = (
        await store.append_message(
            "first",
            message,
            recovery_state={"revision": 0},
            authority=None,
        )
    ).recovery_state
    turn = await insert_row(
        db,
        "swe_conversation_turns",
        {
            "chat_id": chat,
            "session_id": session,
            "question_msg_key": b"active",
            "turn_order": 1,
            "context_epoch": 0,
            "generation": 1,
            "state": "running",
        },
    )
    await db.execute(
        "UPDATE swe_conversation_sessions SET generation=1,active_turn_id=%s,owner_key=%s,lease_until=TIMESTAMPADD(SECOND,60,UTC_TIMESTAMP(6)) WHERE id=%s",
        (turn, b"owner", session),
    )
    authority = ExecutionAuthority(
        SessionIdentity(store.scope, "alice", "console", "session"),
        session,
        chat,
        turn,
        1,
        "owner",
    )
    await db.execute(
        "UPDATE swe_conversation_sessions SET generation=2 WHERE id=%s",
        (session,),
    )
    with pytest.raises(ConflictError, match="authority"):
        await store.compact(
            "first",
            [message],
            recovery_state=state,
            authority=authority,
        )
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_messages WHERE layer='archive'",
        )
    )["n"] == 0
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_retentions WHERE kind='dependency' AND state='active'",
        )
    )["n"] == 0

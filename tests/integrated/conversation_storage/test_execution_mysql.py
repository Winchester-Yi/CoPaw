# -*- coding: utf-8 -*-
"""Session execution authority and atomic publications on a real isolated MySQL."""

import asyncio
import json

import pytest
from agentscope.message import Msg

from swe.app.conversation_storage.bundle import BundleStore, new_operation_key
from swe.app.conversation_storage.errors import (
    ConflictError,
    IntegrityError,
    StorageUnavailable,
    UnknownCommit,
)
from swe.app.conversation_storage.execution import ExecutionStore
from swe.app.conversation_storage.history import HistoryStore
from swe.app.conversation_storage.identity import (
    SessionIdentity,
    WorkspaceScope,
)
from swe.app.conversation_storage.publications import PublicationStore
from swe.app.conversation_storage.retention import RetentionStore
from swe.app.conversation_storage.session_adapter import MySQLSessionAdapter
from tests.integrated.conversation_storage.test_schema import (
    insert_chat,
    insert_session,
)


def identity(user="alice", channel="console"):
    return SessionIdentity(
        WorkspaceScope(
            "tenant",
            "source",
            "agent",
            service_unit_id="schema-test-service",
        ),
        user,
        channel,
        "session",
    )


def store(db, root, owner):
    publications = PublicationStore(
        db,
        BundleStore(root),
        service_unit_id="schema-test-service",
    )
    return ExecutionStore(
        db,
        owner_key=owner,
        lease_seconds=60,
        publications=publications,
        service_unit_id="schema-test-service",
    )


async def seed(db):
    session = await insert_session(db)
    await insert_chat(db, session, b"one", registry_order=1)
    await insert_chat(db, session, b"two", registry_order=2)
    return session


async def admit(service, question="question", chat="one", recovery=None):
    result = await service.reserve_admission(
        identity(),
        chat,
        question,
        operation_key=new_operation_key(),
        runtime_turn_key="turn-" + new_operation_key(),
    )
    if result.created:
        await service.publish_admission(
            result,
            {"id": question, "role": "user", "content": "hello"},
            recovery or {"unknown_module": {"keep": True}},
        )
    return result


@pytest.mark.parametrize(
    "following_chat",
    [None, "one", "two"],
)
async def test_answer_turn_includes_response_after_admission_projects_question(
    conversation_test_database,
    tmp_path,
    following_chat,
):
    db = conversation_test_database
    await seed(db)
    service = store(db, tmp_path, "owner")
    question = Msg("user", "hello", "user")
    question.id = "question"
    answer = Msg("assistant", "reply", "assistant")
    answer.id = "answer"
    question = question.to_dict()
    answer = answer.to_dict()
    state = {"agent": {"memory": {"content": [[question, []]]}}}
    admission = await admit(service, recovery=state)
    await service.start(admission.authority)
    state["agent"]["memory"]["content"].append([answer, []])
    await service.finish(
        admission.authority,
        "completed",
        state,
        operation_key=new_operation_key(),
    )
    if following_chat is not None:
        next_question = Msg("user", "next question", "user")
        next_question.id = "next-question"
        next_answer = Msg("assistant", "next reply", "assistant")
        next_answer.id = "next-answer"
        state["agent"]["memory"]["content"].append(
            [next_question.to_dict(), []],
        )
        following = await admit(
            service,
            question=next_question.id,
            chat=following_chat,
            recovery=state,
        )
        await service.start(following.authority)
        state["agent"]["memory"]["content"].append(
            [next_answer.to_dict(), []],
        )
        await service.finish(
            following.authority,
            "completed",
            state,
            operation_key=new_operation_key(),
        )
    history = HistoryStore(
        db,
        identity().scope,
        service.publications.bundles,
        cursor_secret=b"test",
    )
    result = await history.answer_turn(
        "question",
        chat_id="one",
        user_id="alice",
    )
    from swe.app.runner import api

    assert [api._message_original_id(item) for item in result.messages] == [
        "question",
        "answer",
    ]
    assert result.turn_status == "completed"


async def test_same_session_two_chats_have_one_admission_and_same_question_never_replays(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    await seed(db)
    first, second = store(db, tmp_path, "one-owner"), store(
        db,
        tmp_path,
        "two-owner",
    )
    results = await asyncio.gather(
        first.reserve_admission(
            identity(),
            "one",
            "first",
            operation_key=new_operation_key(),
            runtime_turn_key="runtime-one",
        ),
        second.reserve_admission(
            identity(),
            "two",
            "second",
            operation_key=new_operation_key(),
            runtime_turn_key="runtime-two",
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(value, ConflictError) for value in results) == 1
    admitted = next(
        value for value in results if not isinstance(value, Exception)
    )
    owner = first if admitted.authority.owner_key == "one-owner" else second
    chat = "one" if admitted.authority.owner_key == "one-owner" else "two"
    question = admitted.turn["question_msg_key"].decode()
    duplicate = await owner.reserve_admission(
        identity(),
        chat,
        question,
        operation_key=new_operation_key(),
        runtime_turn_key="different-runtime",
    )
    assert not duplicate.created and duplicate.authority is None
    assert duplicate.turn["id"] == admitted.turn["id"]
    assert (
        await db.fetch_one("SELECT COUNT(*) AS n FROM swe_conversation_turns")
    )["n"] == 1
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_publications",
        )
    )["n"] == 1


async def test_admission_root_message_and_terminal_release_are_atomic(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session_id = await seed(db)
    service = store(db, tmp_path, "owner")
    recovery = {
        "agent": {"memory": {"messages": []}},
        "managed_cron_receipts": {"receipt": True},
        "future_module": {"nested": [1, 2]},
    }
    admitted = await admit(service, recovery=recovery)
    turn = await db.fetch_one(
        "SELECT * FROM swe_conversation_turns WHERE id=%s",
        (admitted.turn["id"],),
    )
    assert (
        turn["state"] == "admitted"
        and turn["admission_publication_id"] is not None
    )
    await service.start(admitted.authority)
    with pytest.raises(ConflictError, match="replay"):
        await service.start(admitted.authority)
    await service.renew(admitted.authority)
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_messages",
        )
    )["n"] == 1
    done = await service.finish(
        admitted.authority,
        "completed",
        recovery,
        operation_key=new_operation_key(),
    )
    assert done["state"] == "committed"
    session = await db.fetch_one(
        "SELECT * FROM swe_conversation_sessions WHERE id=%s",
        (session_id,),
    )
    assert session["active_turn_id"] is None and session["owner_key"] is None
    assert session["state_revision"] == 2
    terminal = await db.fetch_one(
        "SELECT * FROM swe_conversation_turns WHERE id=%s",
        (turn["id"],),
    )
    assert (
        terminal["state"] == "succeeded"
        and terminal["history_status"] == "completed"
    )
    retentions = RetentionStore(db, identity().scope)
    async with retentions.reader(
        [session["recovery_publication_id"]],
    ) as batch:
        publication = batch.publications[0]
        payload = await batch.read(
            BundleStore(tmp_path).read_extent,
            publication["relative_path"].decode(),
            session["recovery_offset"],
            session["recovery_length"],
            expected_length=publication["byte_length"],
        )
        from swe.app.conversation_storage.projection_references import (
            hydrate_projection,
        )

        adapter = MySQLSessionAdapter(
            db,
            identity().scope,
            BundleStore(tmp_path),
            channel="console",
        )
        assert (
            await hydrate_projection(
                adapter,
                json.loads(payload),
                session,
                publication,
                batch,
            )
            == recovery
        )
    assert (await admit(service, question="next-question")).created


async def test_stop_beats_natural_completion_and_stopped_terminal_releases(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    await seed(db)
    service = store(db, tmp_path, "owner")
    admission = await admit(service)
    await service.start(admission.authority)
    await service.request_stop(identity(), "question")
    with pytest.raises(ConflictError, match="Stop"):
        await service.finish(
            admission.authority,
            "completed",
            {},
            operation_key=new_operation_key(),
        )
    await service.finish(
        admission.authority,
        "stopped",
        {},
        operation_key=new_operation_key(),
    )
    stopped = await service.request_stop(identity(), "question")
    assert stopped["state"] == "stopped"


async def test_expired_heartbeat_allows_completion_but_never_automatic_takeover(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session_id = await seed(db)
    service = store(db, tmp_path, "owner")
    admission = await admit(service)
    await db.execute(
        "UPDATE swe_conversation_sessions SET lease_until=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE id=%s",
        (session_id,),
    )
    with pytest.raises(ConflictError, match="busy"):
        await admit(store(db, tmp_path, "other-owner"), question="different")
    duplicate = await admit(store(db, tmp_path, "other-owner"))
    assert not duplicate.created and duplicate.authority is None
    await service.start(admission.authority)
    completed = await service.finish(
        admission.authority,
        "completed",
        {},
        operation_key=new_operation_key(),
    )
    assert completed["state"] == "committed"
    session = await db.fetch_one(
        "SELECT * FROM swe_conversation_sessions WHERE id=%s",
        (session_id,),
    )
    assert session["active_turn_id"] is None and session["owner_key"] is None
    following = await admit(
        store(db, tmp_path, "other-owner"),
        question="following",
    )
    assert (
        following.created
        and following.authority.generation > admission.authority.generation
    )


async def test_session_adapter_pins_complete_root_and_cold_turn_lookup(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    await seed(db)
    service = store(db, tmp_path, "owner")
    state = {
        "future": {"unknown": [1, 2]},
        "turn_states": {
            "question": {
                "status": "completed",
                "message": {
                    "id": "question",
                    "role": "user",
                    "content": "hello",
                },
                "chat_id": "one",
            },
        },
    }
    admission = await admit(service, recovery=state)
    await service.finish(
        admission.authority,
        "completed",
        state,
        operation_key=new_operation_key(),
    )
    adapter = MySQLSessionAdapter(
        db,
        identity().scope,
        BundleStore(tmp_path),
        channel="console",
    )
    loaded = await adapter.get_session_state_dict("session", "alice")
    assert loaded["future"] == state["future"]
    assert loaded["revision"] == 2
    cold = await adapter.get_turn_state("session", "alice", {}, "question")
    assert cold == state["turn_states"]["question"]
    assert (
        await adapter.get_latest_turn_state("session", "alice", {}, "one")
    ) == ("question", cold)
    await adapter.update_session_state(
        "session",
        ["future", "added"],
        True,
        user_id="alice",
    )
    changed = await adapter.get_session_state_dict("session", "alice")
    assert changed["future"] == {"unknown": [1, 2], "added": True}
    assert changed["revision"] == 3
    successive = await adapter.update_session_state(
        "session",
        ["future", "again"],
        True,
        user_id="alice",
    )
    assert successive["revision"] == 4
    with pytest.raises(ConflictError, match="revision"):
        await adapter._save_state(identity(), loaded)
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_retentions WHERE kind='reader' AND state='active'",
        )
    )["n"] == 0


async def test_session_root_rejects_cross_session_pointer_and_changed_locator(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session_id = await seed(db)
    service = store(db, tmp_path, "owner")
    admission = await admit(service)
    await service.finish(
        admission.authority,
        "completed",
        {"private": "one"},
        operation_key=new_operation_key(),
    )
    adapter = MySQLSessionAdapter(
        db,
        identity().scope,
        BundleStore(tmp_path),
        channel="console",
    )
    original = await db.fetch_one(
        "SELECT * FROM swe_conversation_sessions WHERE id=%s",
        (session_id,),
    )
    other = await insert_session(
        db,
        user_key=b"bob",
        external_session_key=b"other",
        recovery_publication_id=original["recovery_publication_id"],
        recovery_offset=original["recovery_offset"],
        recovery_length=original["recovery_length"],
    )
    with pytest.raises(IntegrityError, match="Session"):
        await adapter.get_session_state_dict("other", "bob")
    original_read = adapter.bundles.read_extent

    def changed_locator(*args, **kwargs):
        return original_read(*args, **kwargs)

    original_row = adapter._row
    count = 0

    async def altered_row(*args, **kwargs):
        nonlocal count
        row = await original_row(*args, **kwargs)
        count += 1
        if count == 2:
            await db.execute(
                "UPDATE swe_conversation_sessions SET recovery_offset=recovery_offset+1 WHERE id=%s",
                (session_id,),
            )
            row = await original_row(*args, **kwargs)
        return row

    adapter._row = altered_row
    with pytest.raises(ConflictError, match="changed"):
        await adapter.get_session_state_dict("session", "alice")


async def test_cold_turn_rejects_chat_deleted_during_nas_read(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    await seed(db)
    service = store(db, tmp_path, "owner")
    state = {
        "turn_states": {
            "question": {
                "status": "completed",
                "chat_id": "one",
                "message": {"id": "question", "role": "user"},
            },
        },
    }
    admission = await admit(service, recovery=state)
    await service.finish(
        admission.authority,
        "completed",
        state,
        operation_key=new_operation_key(),
    )
    adapter = MySQLSessionAdapter(
        db,
        identity().scope,
        BundleStore(tmp_path),
        channel="console",
    )
    original = adapter.retentions.pin

    async def delete_after_pin(*args, **kwargs):
        batch = await original(*args, **kwargs)
        await db.execute(
            "UPDATE swe_conversation_chats SET deleted_at=UTC_TIMESTAMP(6) WHERE id=%s",
            (admission.authority.chat_id,),
        )
        return batch

    adapter.retentions.pin = delete_after_pin
    with pytest.raises(ConflictError, match="Chat"):
        await adapter.get_turn_state("session", "alice", {}, "question")


@pytest.mark.parametrize(
    "locked_table",
    ["retentions", "sessions", "chats", "turns"],
)
async def test_renewal_total_budget_includes_real_lock_waits(
    conversation_test_database,
    tmp_path,
    locked_table,
):
    db = conversation_test_database
    await seed(db)
    service = store(db, tmp_path, "owner")
    admission = await admit(service)
    service.lease_seconds = 1
    locked = asyncio.Event()
    release = asyncio.Event()

    async def blocker():
        async with db.transaction():
            await db.fetch_all(
                f"SELECT id FROM swe_conversation_{locked_table} FOR UPDATE",
            )
            locked.set()
            await release.wait()

    task = asyncio.create_task(blocker())
    try:
        await asyncio.wait_for(locked.wait(), 5)
        started = asyncio.get_running_loop().time()
        with pytest.raises(StorageUnavailable, match="heartbeat"):
            await asyncio.wait_for(service.renew(admission.authority), 2)
        assert asyncio.get_running_loop().time() - started < 1
    finally:
        release.set()
        await task


async def test_renewal_allows_expiry_between_select_and_update_mysql(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    db = conversation_test_database
    await seed(db)
    service = store(db, tmp_path, "owner")
    admission = await admit(service)
    # Force expiry below without shrinking the remote SQL budget to one second.
    service.lease_seconds = 30
    associations = service._turn_associations

    async def delay_after_validation(*args):
        rows = await associations(*args)
        # Expiry advances while the authority's rows remain locked.
        await db.execute(
            "UPDATE swe_conversation_sessions SET lease_until=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE id=%s",
            (admission.authority.session_id,),
        )
        return rows

    monkeypatch.setattr(service, "_turn_associations", delay_after_validation)
    await service.renew(admission.authority)
    session = await service._session(admission.authority.identity)
    assert session["owner_key"] == b"owner"


async def test_reserved_admission_can_renew_before_nas_publish_mysql(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    await seed(db)
    service = store(db, tmp_path, "owner")
    admission = await service.reserve_admission(
        identity(),
        "one",
        "question",
        operation_key=new_operation_key(),
        runtime_turn_key="budget-test",
    )
    await service.renew(admission.authority)
    await service.publish_admission(
        admission,
        {"id": "question", "role": "user", "content": "hello"},
        {},
    )
    await service.start(admission.authority)


async def test_expired_heartbeat_can_renew_with_matching_generation_mysql(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session_id = await seed(db)
    service = store(db, tmp_path, "owner")
    admission = await admit(service)
    await db.execute(
        "UPDATE swe_conversation_sessions SET lease_until=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE id=%s",
        (session_id,),
    )
    await service.renew(admission.authority)
    row = await db.fetch_one(
        "SELECT generation,owner_key,lease_until>UTC_TIMESTAMP(6) AS heartbeat_fresh FROM swe_conversation_sessions WHERE id=%s",
        (session_id,),
    )
    assert row["heartbeat_fresh"]
    assert row["generation"] == admission.authority.generation
    assert row["owner_key"] == b"owner"


@pytest.mark.parametrize("method", ["start", "renew", "finish"])
async def test_expired_heartbeat_never_reactivates_fenced_owner_mysql(
    conversation_test_database,
    tmp_path,
    method,
):
    db = conversation_test_database
    session_id = await seed(db)
    service = store(db, tmp_path, "owner")
    admission = await admit(service)
    await db.execute(
        "UPDATE swe_conversation_sessions SET generation=generation+1,owner_key=NULL,lease_until=NULL WHERE id=%s",
        (session_id,),
    )
    with pytest.raises(ConflictError, match="authority"):
        if method == "finish":
            await service.finish(
                admission.authority,
                "completed",
                {},
                operation_key=new_operation_key(),
            )
        else:
            await getattr(service, method)(admission.authority)


async def test_terminal_can_commit_after_heartbeat_expires_during_nas_write_mysql(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    db = conversation_test_database
    session_id = await seed(db)
    service = store(db, tmp_path, "owner")
    admission = await admit(service)
    await service.start(admission.authority)
    write = service.publications.write_bundle

    async def expire_after_nas(*args, **kwargs):
        result = await write(*args, **kwargs)
        await db.execute(
            "UPDATE swe_conversation_sessions SET lease_until=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE id=%s",
            (session_id,),
        )
        return result

    monkeypatch.setattr(service.publications, "write_bundle", expire_after_nas)
    completed = await service.finish(
        admission.authority,
        "completed",
        {},
        operation_key=new_operation_key(),
    )
    assert completed["state"] == "committed"

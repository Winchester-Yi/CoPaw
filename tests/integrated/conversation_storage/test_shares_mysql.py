# -*- coding: utf-8 -*-
import json

import pytest

from swe.app.chat_sharing.store import ChatShareStore
from swe.app.conversation_storage.bundle import (
    BundleBlock,
    BundleStore,
    new_operation_key,
)
from swe.app.conversation_storage.errors import (
    ConflictError,
    StorageUnavailable,
    UnknownCommit,
)
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.retention import RetentionStore
from swe.app.conversation_storage.publications import (
    CommitPlan,
    PublicationRequest,
    PublicationStore,
)
from swe.app.conversation_storage.shares import ShareInstall, ShareStorage
from tests.integrated.conversation_storage.conftest import (
    SQL_ROOT,
    apply_sql_script,
)
from tests.integrated.conversation_storage.test_schema import (
    insert_chat,
    insert_row,
    insert_session,
)
from tests.integrated.conversation_storage.test_publications_mysql import (
    LostCommitDatabase,
)


async def setup(db, root):
    await ChatShareStore(db).ensure_schema()
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    session = await insert_session(db)
    chat = await insert_chat(db, session, b"chat")
    turn = await insert_row(
        db,
        "swe_conversation_turns",
        {
            "chat_id": chat,
            "session_id": session,
            "question_msg_key": b"question",
            "turn_order": 1,
            "context_epoch": 0,
            "generation": 0,
            "state": "succeeded",
            "history_status": "completed",
        },
    )
    publication = await insert_row(
        db,
        "swe_conversation_publications",
        {
            "operation_key": b"old-question-publication",
            "tenant_key": b"tenant",
            "source_key": b"source",
            "agent_key": b"agent",
            "session_id": session,
            "chat_id": chat,
            "kind": "import",
            "state": "committed",
            "generation": 0,
            "owner_key": b"offline",
            "relative_path": b"old-question.bundle",
            "format_key": b"swe-bundle-v1",
            "created_at": "2026-10-30",
        },
    )
    await insert_row(
        db,
        "swe_conversation_messages",
        {
            "session_id": session,
            "origin_chat_id": chat,
            "layer": "memory",
            "session_order": 1,
            "turn_id": turn,
            "source_msg_key": b"question",
            "source_occurrence_no": 0,
            "role_key": b"user",
            "kind_key": b"message",
            "publication_id": publication,
            "byte_offset": 0,
            "byte_length": 2,
            "context_epoch": 0,
            "in_context": 1,
            "current_visible": 1,
        },
    )
    return chat, ShareStorage(
        db,
        BundleStore(root),
        scope=WorkspaceScope("tenant", "source", "agent"),
    )


async def test_permanent_share_survives_chat_deletion_and_counts_access(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    chat, storage = await setup(db, tmp_path)
    payload = {
        "chat_name": "shared",
        "messages": [{"id": "m", "role": "user", "content": "body"}],
    }
    record = await storage.create_snapshot(
        chat_id="chat",
        creator_id="alice",
        tenant_id="tenant",
        payload=payload,
        selected_question_keys=("question",),
    )
    assert record.publication_id and record.byte_offset and record.byte_length
    await db.execute(
        "UPDATE swe_conversation_chats SET deleted_at=UTC_TIMESTAMP(6) WHERE id=%s",
        (chat,),
    )
    anonymous = ShareStorage(db, BundleStore(tmp_path))
    assert await anonymous.get_snapshot(record.token) == payload
    assert (await ChatShareStore(db).get(record.token)).access_count == 1
    await ChatShareStore(db).verify_locator_schema()
    with pytest.raises(KeyError):
        await anonymous.get_snapshot("missing")
    retentions = RetentionStore(db, storage.scope)
    async with db.transaction():
        await retentions.lock_guard()
        await retentions._insert(b"backup-job", None, "backup_fence")
    with pytest.raises(StorageUnavailable):
        await anonymous.get_snapshot(record.token)
    assert (await ChatShareStore(db).get(record.token)).access_count == 1
    await db.execute(
        "UPDATE swe_conversation_retentions SET state='released',released_at=UTC_TIMESTAMP(6) WHERE retention_key=%s AND kind='backup_fence'",
        (b"backup-job",),
    )
    assert await anonymous.get_snapshot(record.token) == payload
    assert (await ChatShareStore(db).get(record.token)).access_count == 2


async def test_legacy_orphan_preserves_token_key_counts_and_uses_publication_scope(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    await setup(db, tmp_path)
    operation = new_operation_key()
    plan = ShareInstall(
        "original-token",
        "deleted-chat",
        "old-creator",
        "other-tenant",
        "opaque/original-key",
        "2026-10-30T00:00:00Z",
        selected_question_keys=("question",),
        access_count=7,
        last_access_at="2026-10-30T01:00:00Z",
    )
    publication = PublicationStore(db, BundleStore(tmp_path))
    request = PublicationRequest(
        operation,
        WorkspaceScope("other-tenant", "", ""),
        "share_import",
        "offline",
        0,
        scope_kind="legacy_share",
    )
    await publication.reserve(request, CommitPlan(share=plan))
    await publication.write_bundle(
        operation,
        [
            BundleBlock(
                "share",
                "share",
                b'{"chat_name":"orphan","messages":[]}',
            ),
        ],
    )
    await publication.commit(operation)
    reader = ShareStorage(
        db,
        BundleStore(tmp_path),
        scope=WorkspaceScope("wrong-current-request", "wrong", "wrong"),
    )
    assert (await reader.get_snapshot("original-token"))[
        "chat_name"
    ] == "orphan"
    record = await ChatShareStore(db).get("original-token")
    assert (
        record.snapshot_key == "opaque/original-key"
        and record.access_count == 8
    )


async def test_lost_share_commit_retains_original_bundle_and_one_token(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    _, storage = await setup(db, tmp_path)
    fault = LostCommitDatabase(db)
    publications = PublicationStore(fault, BundleStore(tmp_path))
    chat = await db.fetch_one("SELECT * FROM swe_conversation_chats")
    operation = new_operation_key()
    plan = ShareInstall(
        "fixed-token",
        "chat",
        "alice",
        "tenant",
        "legacy/key",
        "2026-10-30T00:00:00Z",
        selected_question_keys=("question",),
    )
    request = PublicationRequest(
        operation,
        storage.scope,
        "share",
        "owner",
        0,
        session_id=chat["session_id"],
        chat_id=chat["id"],
    )
    await publications.reserve(request, CommitPlan(share=plan))
    index = await publications.write_bundle(
        operation,
        [BundleBlock("share", "share", b'{"messages":[]}')],
    )
    fault.lose_next_commit = True
    with pytest.raises(UnknownCommit):
        await publications.commit(operation)
    assert (tmp_path / index.relative_path).exists()
    assert (await publications.commit(operation))["state"] == "committed"
    assert (await db.fetch_one("SELECT COUNT(*) AS n FROM swe_chat_shares"))[
        "n"
    ] == 1


async def test_missing_body_and_failed_count_are_unavailable_not_empty(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    _, storage = await setup(db, tmp_path)
    record = await storage.create_snapshot(
        chat_id="chat",
        creator_id="alice",
        tenant_id="tenant",
        payload={"messages": []},
        selected_question_keys=("question",),
    )
    count = storage.store.record_access

    async def failed(_):
        raise ConnectionError("count unavailable")

    storage.store.record_access = failed
    with pytest.raises(StorageUnavailable):
        await storage.get_snapshot(record.token)
    storage.store.record_access = count
    row = await db.fetch_one(
        "SELECT relative_path FROM swe_conversation_publications WHERE id=%s",
        (record.publication_id,),
    )
    (tmp_path / row["relative_path"].decode()).unlink()
    with pytest.raises(StorageUnavailable):
        await storage.get_snapshot(record.token)
    assert (await ChatShareStore(db).get(record.token)).access_count == 0


async def test_token_cannot_be_repointed_to_another_share_same_scope(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    _, storage = await setup(db, tmp_path)
    first = await storage.create_snapshot(
        chat_id="chat",
        creator_id="alice",
        tenant_id="tenant",
        payload={"chat_name": "first", "messages": []},
        selected_question_keys=("question",),
    )
    second = await storage.create_snapshot(
        chat_id="chat",
        creator_id="alice",
        tenant_id="tenant",
        payload={"chat_name": "second", "messages": []},
        selected_question_keys=("question",),
    )
    await db.execute(
        "UPDATE swe_chat_shares SET publication_id=%s,byte_offset=%s,byte_length=%s WHERE token=%s",
        (
            second.publication_id,
            second.byte_offset,
            second.byte_length,
            first.token,
        ),
    )
    with pytest.raises(StorageUnavailable):
        await storage.get_snapshot(first.token)


@pytest.mark.parametrize("changed", ["status", "visibility"])
async def test_share_commit_rejects_selection_changed_after_snapshot(
    conversation_test_database,
    tmp_path,
    changed,
):
    db = conversation_test_database
    chat_id, storage = await setup(db, tmp_path)
    chat = await db.fetch_one(
        "SELECT * FROM swe_conversation_chats WHERE id=%s",
        (chat_id,),
    )
    operation = new_operation_key()
    request = PublicationRequest(
        operation,
        storage.scope,
        "share",
        "owner",
        0,
        session_id=chat["session_id"],
        chat_id=chat_id,
    )
    plan = ShareInstall(
        "selected-token",
        "chat",
        "alice",
        "tenant",
        "legacy/key",
        "2026-10-30T00:00:00Z",
        selected_question_keys=("question",),
    )
    publications = PublicationStore(db, BundleStore(tmp_path))
    await publications.reserve(request, CommitPlan(share=plan))
    await publications.write_bundle(
        operation,
        [BundleBlock("share", "share", b'{"messages":[]}')],
    )
    if changed == "status":
        await db.execute(
            "UPDATE swe_conversation_turns SET history_status='failed',state='failed' WHERE session_id=%s",
            (chat["session_id"],),
        )
    else:
        await db.execute(
            "UPDATE swe_conversation_messages SET current_visible=0 WHERE session_id=%s",
            (chat["session_id"],),
        )
    with pytest.raises(ConflictError):
        await publications.commit(operation)
    assert await ChatShareStore(db).get("selected-token") is None
    assert (await publications.get(operation))["state"] == "prepared"

# -*- coding: utf-8 -*-
"""Real row-lock and unique-key evidence for retention protection."""

import asyncio

import pytest

from swe.app.conversation_storage.errors import ConflictError
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.retention import RetentionStore
from tests.integrated.conversation_storage.test_schema import (
    insert_publication,
)


def retention(db):
    return RetentionStore(
        db,
        WorkspaceScope(
            "tenant",
            "source",
            "agent",
            service_unit_id="schema-test-service",
        ),
    )


async def test_first_scope_guard_creation_race_has_one_identity(
    conversation_test_database,
):
    db = conversation_test_database
    service = retention(db)
    first, second = await asyncio.gather(
        service.ensure_guard(),
        service.ensure_guard(),
    )
    assert first["id"] == second["id"]
    rows = await db.fetch_all("SELECT * FROM swe_conversation_retentions")
    assert len(rows) == 1
    assert rows[0]["guard_marker"] == 1
    assert rows[0]["publication_id"] is None


async def test_pin_waits_for_deleting_mark_then_refuses_target(
    conversation_test_database,
):
    db = conversation_test_database
    publication_id = await insert_publication(db, state="committed")
    service = retention(db)
    locked = asyncio.Event()
    release = asyncio.Event()

    async def mark():
        async with db.transaction():
            await service.lock_guard()
            await db.fetch_one(
                "SELECT * FROM swe_conversation_publications WHERE id=%s FOR UPDATE",
                (publication_id,),
            )
            locked.set()
            await release.wait()
            await db.execute(
                "UPDATE swe_conversation_publications SET gc_state='deleting' WHERE id=%s",
                (publication_id,),
            )

    writer = asyncio.create_task(mark())
    await asyncio.wait_for(locked.wait(), 10)
    reader = asyncio.create_task(service.pin([publication_id], kind="reader"))
    try:
        await asyncio.sleep(0.05)
        assert not reader.done()
        release.set()
        await writer
        with pytest.raises(ConflictError):
            await reader
    finally:
        release.set()
        await asyncio.gather(writer, reader, return_exceptions=True)
    rows = await db.fetch_all(
        "SELECT * FROM swe_conversation_retentions WHERE kind='reader'",
    )
    assert rows == []


async def test_reader_batch_survives_read_and_released_identity_cannot_reactivate(
    conversation_test_database,
):
    db = conversation_test_database
    ids = [
        await insert_publication(
            db,
            f"operation-{number}".encode(),
            state="committed",
        )
        for number in range(3)
    ]
    service = retention(db)
    async with service.reader([ids[2], ids[0], ids[2]]) as batch:
        rows = await db.fetch_all(
            "SELECT * FROM swe_conversation_retentions WHERE kind='reader' AND state='active'",
        )
        assert {row["publication_id"] for row in rows} == {ids[0], ids[2]}
    rows = await db.fetch_all(
        "SELECT * FROM swe_conversation_retentions WHERE kind='reader'",
    )
    assert {row["state"] for row in rows} == {"released"}
    with pytest.raises(ConflictError):
        await service.pin(
            [ids[0], ids[2]],
            kind="reader",
            retention_key=batch.retention_key.decode(),
        )


async def test_dependency_release_requires_physical_source_reclamation(
    conversation_test_database,
):
    db = conversation_test_database
    first = await insert_publication(db, b"first", state="committed")
    second = await insert_publication(db, b"second", state="prepared")
    service = retention(db)
    async with db.transaction():
        await service.add_dependencies(second, [first])
        await db.execute(
            "UPDATE swe_conversation_publications SET state='committed' WHERE id=%s",
            (second,),
        )
    with pytest.raises(ConflictError):
        await service.release_dependencies(second)
    await db.execute(
        "UPDATE swe_conversation_publications SET gc_state='reclaimed' WHERE id=%s",
        (second,),
    )
    await service.release_dependencies(second)
    row = await db.fetch_one(
        "SELECT * FROM swe_conversation_retentions WHERE kind='dependency'",
    )
    assert row["publication_id"] == first
    assert row["state"] == "released"


async def test_backup_fence_blocks_direct_chat_and_execution_writes(
    conversation_test_database,
    tmp_path,
):
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.chat_repository import (
        MySQLChatRepository,
    )
    from swe.app.conversation_storage.execution import ExecutionStore
    from swe.app.conversation_storage.identity import SessionIdentity
    from swe.app.conversation_storage.session_adapter import (
        MySQLSessionAdapter,
    )
    from tests.integrated.conversation_storage.test_schema import (
        insert_chat,
        insert_session,
    )

    db = conversation_test_database
    session = await insert_session(db)
    await insert_chat(db, session)
    service = retention(db)
    await service.install_backup_fence(
        "backup-job",
        "snapshot-1",
    )
    bundles = BundleStore(tmp_path)
    repo = MySQLChatRepository(db, service.scope, bundles)
    with pytest.raises(ConflictError, match="backup fence"):
        await repo.update_title("chat", "new title", expected_revision=0)
    with pytest.raises(ConflictError, match="backup fence"):
        await repo.delete_chats(["chat"])
    execution = ExecutionStore(db, service_unit_id="schema-test-service")
    identity = SessionIdentity(service.scope, "alice", "console", "session")
    with pytest.raises(ConflictError, match="backup fence"):
        await execution.request_stop(identity, "question")
    adapter = MySQLSessionAdapter(
        db,
        service.scope,
        bundles,
        channel="console",
    )
    with pytest.raises(ConflictError, match="backup fence"):
        await adapter._save_state(identity, {"revision": 0})
    await service.release(b"backup-job")
    with pytest.raises(ConflictError, match="backup fence"):
        async with db.transaction():
            await service.assert_writes_allowed()
    await service.release_backup_fence(
        "backup-job",
        "snapshot-1",
    )
    assert await repo.update_title("chat", "new title", expected_revision=0)

# -*- coding: utf-8 -*-
"""Real current-read and NAS evidence for explicitly enabled garbage collection."""

import asyncio
from dataclasses import replace

import pytest

from swe.app.chat_sharing.store import ChatShareStore
from swe.app.conversation_storage.bundle import (
    BundleBlock,
    BundleStore,
    new_operation_key,
)
from swe.app.conversation_storage.errors import ConflictError, IntegrityError
from swe.app.conversation_storage.gc import GarbageCollector, StopProof
from swe.app.conversation_storage.identity import WorkspaceScope
from tests.integrated.conversation_storage.conftest import (
    SQL_ROOT,
    apply_sql_script,
)
from tests.integrated.conversation_storage.test_schema import (
    NOW,
    insert_chat,
    insert_publication,
    insert_row,
    insert_session,
)


async def setup(db, root, *, state="committed", scope_kind="workspace"):
    await ChatShareStore(db).ensure_schema()
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    bundles = BundleStore(root)
    operation = new_operation_key()
    scope = (
        WorkspaceScope(
            "tenant",
            "source",
            "agent",
            service_unit_id="schema-test-service",
        )
        if scope_kind == "workspace"
        else WorkspaceScope(
            "tenant",
            "",
            "",
            service_unit_id="schema-test-service",
        )
    )
    index = bundles.publish(
        operation,
        [BundleBlock("body", "message", b"body")],
        scope=scope,
        scope_kind=scope_kind,
        metadata={"operation_key": operation},
    )
    session = await insert_session(db) if scope_kind == "workspace" else None
    chat = await insert_chat(db, session) if session else None
    publication = await insert_publication(
        db,
        operation.encode(),
        state=state,
        scope_kind=scope_kind,
        source_key=scope.sql_values[2],
        agent_key=scope.sql_values[3],
        session_id=session,
        chat_id=chat,
        relative_path=index.relative_path.encode(),
        byte_length=index.byte_length,
        durable_at=NOW,
    )
    gc = GarbageCollector(
        db,
        scope,
        bundles,
        physical_enabled=True,
        scope_kind=scope_kind,
    )
    return gc, publication, session, chat


async def protect(db, gc, publication, session, chat, kind):
    if kind in ("reader", "backup"):
        await gc.retentions.pin([publication], kind=kind)
    elif kind == "dependency":
        operation = new_operation_key()
        newer = await insert_publication(
            db,
            operation.encode(),
            state="committed",
            relative_path=gc.bundles.path_for(
                operation,
                gc.scope,
            ).encode(),
        )
        await gc.retentions.add_dependencies(newer, [publication])
    elif kind == "root":
        await db.execute(
            "UPDATE swe_conversation_sessions SET recovery_publication_id=%s WHERE id=%s",
            (publication, session),
        )
    elif kind in ("admission", "terminal"):
        await insert_row(
            db,
            "swe_conversation_turns",
            {
                "chat_id": chat,
                "session_id": session,
                "question_msg_key": b"q",
                "turn_order": 1,
                "context_epoch": 0,
                "generation": 0,
                "state": "succeeded",
                (
                    "admission_publication_id"
                    if kind == "admission"
                    else "terminal_publication_id"
                ): publication,
            },
        )
    elif kind in ("checkpoint", "content"):
        field = (
            "active_checkpoint_publication_id"
            if kind == "checkpoint"
            else "content_meta_publication_id"
        )
        await db.execute(
            "UPDATE swe_conversation_chats SET " + field + "=%s WHERE id=%s",
            (publication, chat),
        )
    elif kind == "history":
        await db.execute(
            "UPDATE swe_conversation_publications SET retained_history=1 WHERE id=%s",
            (publication,),
        )
    elif kind == "share":
        await insert_row(
            db,
            "swe_chat_shares",
            {
                "service_unit_key": gc.scope.sql_values[0],
                "token": "permanent",
                "chat_id": "chat",
                "creator_id": "alice",
                "tenant_id": "tenant",
                "snapshot_key": "snapshot",
                "created_at": NOW,
                "publication_id": publication,
                "byte_offset": 1,
                "byte_length": 1,
            },
        )
    elif kind in ("message", "archive", "boundary"):
        values = {
            "session_id": session,
            "layer": (
                "archive" if kind in ("archive", "boundary") else "memory"
            ),
            "session_order": 1 if kind == "message" else None,
            "archive_chat_id": chat if kind != "message" else None,
            "archive_order": 1 if kind != "message" else None,
            "source_occurrence_no": 0,
            "role_key": b"user",
            "kind_key": b"message",
            "publication_id": publication,
            "byte_offset": 1,
            "byte_length": 1,
            "context_epoch": 0,
            "in_context": 0,
            "current_visible": 1,
        }
        if kind == "boundary":
            operation = new_operation_key()
            body = await insert_publication(
                db,
                operation.encode(),
                state="committed",
                relative_path=gc.bundles.path_for(
                    operation,
                    gc.scope,
                ).encode(),
                session_id=session,
                chat_id=chat,
            )
            values["publication_id"] = body
        if kind != "message":
            values["archive_publication_id"] = publication
        await insert_row(db, "swe_conversation_messages", values)


@pytest.mark.parametrize(
    "kind",
    [
        "message",
        "archive",
        "boundary",
        "root",
        "admission",
        "terminal",
        "checkpoint",
        "content",
        "share",
        "history",
        "reader",
        "backup",
        "dependency",
    ],
)
async def test_each_live_reference_prevents_mark_and_reports_reason(
    conversation_test_database,
    tmp_path,
    kind,
):
    db = conversation_test_database
    gc, publication, session, chat = await setup(db, tmp_path)
    await protect(db, gc, publication, session, chat, kind)
    report = await gc.report()
    assert not next(
        item for item in report if item.publication_id == publication
    ).eligible
    with pytest.raises(ConflictError, match="protected"):
        await gc.mark(publication)
    assert (await gc._row(publication))["gc_state"] == "live"
    assert (
        tmp_path / gc.bundles.path_for(report[0].operation_key, gc.scope)
    ).is_file()


async def test_report_only_does_not_mutate_and_explicit_reclaim_preserves_tombstone(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    gc, publication, _, _ = await setup(db, tmp_path)
    reporter = GarbageCollector(db, gc.scope, gc.bundles)
    assert (await reporter.report())[0].eligible
    assert (await gc._row(publication))["gc_state"] == "live"
    await gc.mark(publication)
    row = await gc.reclaim(publication)
    assert row["gc_state"] == "reclaimed"
    assert not (tmp_path / row["relative_path"].decode()).exists()
    assert (await gc.reclaim(publication))["gc_state"] == "reclaimed"
    assert (await gc._row(publication))["operation_key"] == row[
        "operation_key"
    ]


async def test_prepared_and_unverified_aborted_writers_never_reclaim(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    gc, publication, _, _ = await setup(db, tmp_path, state="prepared")
    with pytest.raises(ConflictError, match="terminal"):
        await gc.mark(publication)
    await db.execute(
        "UPDATE swe_conversation_publications SET state='aborted' WHERE id=%s",
        (publication,),
    )
    with pytest.raises(ConflictError, match="evidence"):
        await gc.mark(publication)
    row = await gc._row(publication)
    proof = StopProof(
        row["operation_key"].decode(),
        row["owner_key"].decode(),
        row["generation"],
        "ops:incarnation-drained",
        True,
        True,
        service_unit_id="schema-test-service",
    )
    gc.verify_stopped = lambda value: value.evidence_ref == proof.evidence_ref
    with pytest.raises(ConflictError):
        await gc.mark(publication, proof=replace(proof, generation=99))
    with pytest.raises(ConflictError):
        await gc.mark(publication, proof=replace(proof, io_drained=False))
    await gc.mark(publication, proof=proof)
    assert (await gc.reclaim(publication, proof=proof))[
        "gc_state"
    ] == "reclaimed"


async def test_backup_fence_blocks_mark_and_already_deleting_unlink(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    gc, publication, _, _ = await setup(db, tmp_path)
    await gc.retentions.install_backup_fence("fence", "snapshot")
    with pytest.raises(ConflictError, match="fence"):
        await gc.mark(publication)
    await gc.retentions.release_backup_fence("fence", "snapshot")
    await gc.mark(publication)
    await gc.retentions.install_backup_fence("next", "snapshot2")
    with pytest.raises(ConflictError, match="fence"):
        await gc.reclaim(publication)
    assert (
        tmp_path / (await gc._row(publication))["relative_path"].decode()
    ).is_file()


async def test_unlink_failure_stays_deleting_and_dependencies_release_only_after_success(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    db = conversation_test_database
    gc, publication, session, chat = await setup(db, tmp_path)
    operation = new_operation_key()
    older = await insert_publication(
        db,
        operation.encode(),
        state="committed",
        relative_path=gc.bundles.path_for(
            operation,
            gc.scope,
        ).encode(),
    )
    # Dependencies must point backwards: use a new publication as the GC source.
    operation = new_operation_key()
    index = gc.bundles.publish(
        operation,
        [BundleBlock("body", "message", b"new")],
        scope=gc.scope,
    )
    newer = await insert_publication(
        db,
        operation.encode(),
        state="committed",
        relative_path=index.relative_path.encode(),
        byte_length=index.byte_length,
        session_id=session,
        chat_id=chat,
    )
    await gc.retentions.add_dependencies(newer, [publication, older])
    await gc.mark(newer)
    original = gc.bundles.reclaim

    def failed(key):
        raise OSError("NAS unavailable")

    monkeypatch.setattr(gc.bundles, "reclaim", failed)
    with pytest.raises(OSError):
        await gc.reclaim(newer)
    assert (await gc._row(newer))["gc_state"] == "deleting"
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_retentions WHERE kind='dependency' AND state='active'",
        )
    )["n"] == 2
    monkeypatch.setattr(gc.bundles, "reclaim", original)
    await gc.reclaim(newer)
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_retentions WHERE kind='dependency' AND state='active'",
        )
    )["n"] == 0


async def test_current_read_mark_sees_pin_committed_after_candidate_scan(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    gc, publication, _, _ = await setup(db, tmp_path)
    assert (await gc.report())[0].eligible
    batch = await gc.retentions.pin([publication], kind="reader")
    with pytest.raises(ConflictError, match="retention"):
        await gc.mark(publication)
    await gc.retentions.finish_reader(batch)
    await gc.mark(publication)
    with pytest.raises(ConflictError, match="committed/live"):
        await gc.retentions.pin([publication], kind="reader")


async def test_legacy_share_namespace_is_explicit_and_permanent_share_remains_protected(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    gc, publication, _, _ = await setup(
        db,
        tmp_path,
        scope_kind="legacy_share",
    )
    assert (await gc.report())[0].eligible
    with pytest.raises(IntegrityError, match="scope"):
        await GarbageCollector(
            db,
            WorkspaceScope(
                "tenant",
                "source",
                "agent",
                service_unit_id="schema-test-service",
            ),
            gc.bundles,
            physical_enabled=True,
        ).mark(publication)
    await protect(db, gc, publication, None, None, "share")
    with pytest.raises(ConflictError, match="share"):
        await gc.mark(publication)


async def test_cancelled_reclaim_waits_for_unlink_worker(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    import threading

    db = conversation_test_database
    gc, publication, _, _ = await setup(db, tmp_path)
    await gc.mark(publication)
    started, release = threading.Event(), threading.Event()
    original = gc.bundles.reclaim

    def blocked(operation):
        started.set()
        assert release.wait(5)
        return original(operation)

    monkeypatch.setattr(gc.bundles, "reclaim", blocked)
    task = asyncio.create_task(gc.reclaim(publication))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done(), "GC cancellation must drain its NAS worker"
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert (await gc._row(publication))["gc_state"] == "deleting"
    await gc.reclaim(publication)
    assert (await gc._row(publication))["gc_state"] == "reclaimed"


async def test_aborted_tombstone_rescans_exact_late_file(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    gc, publication, _, _ = await setup(db, tmp_path, state="aborted")
    row = await gc._row(publication)
    proof = StopProof(
        row["operation_key"].decode(),
        row["owner_key"].decode(),
        row["generation"],
        "ops:stopped",
        True,
        True,
        service_unit_id="schema-test-service",
    )
    gc.verify_stopped = lambda value: value == proof
    await gc.mark(publication, proof=proof)
    await gc.reclaim(publication, proof=proof)
    # Simulate an already queued NAS write surfacing after the first unlink.
    operation = proof.operation_key
    gc.bundles.publish(
        operation,
        [BundleBlock("body", "message", b"late")],
        scope=gc.scope,
    )
    other = gc.bundles.publish(
        new_operation_key(),
        [BundleBlock("body", "message", b"keep")],
        scope=gc.scope,
    )
    await gc.reclaim(publication, proof=proof)
    assert not (tmp_path / gc.bundles.path_for(operation, gc.scope)).exists()
    assert (tmp_path / other.relative_path).is_file()
    assert (await gc._row(publication))["gc_state"] == "reclaimed"


async def test_gc_does_not_reverse_session_publication_lock_order(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    db = conversation_test_database
    gc, publication, session, _ = await setup(db, tmp_path)
    await gc.retentions.ensure_guard()
    session_locked, publication_locked = asyncio.Event(), asyncio.Event()
    original = gc._row

    async def observed_row(identity, *, lock=False):
        row = await original(identity, lock=lock)
        if lock:
            publication_locked.set()
        return row

    monkeypatch.setattr(gc, "_row", observed_row)

    async def writer():
        async with db.transaction():
            await db.fetch_one(
                "SELECT id FROM swe_conversation_sessions WHERE id=%s FOR UPDATE",
                (session,),
            )
            session_locked.set()
            await publication_locked.wait()
            row = await db.fetch_one(
                "SELECT gc_state FROM swe_conversation_publications WHERE id=%s FOR UPDATE",
                (publication,),
            )
            assert row["gc_state"] == "deleting"

    writer_task = asyncio.create_task(writer())
    await session_locked.wait()
    try:
        # This checks lock order, not a three-second remote database SLO.
        await asyncio.wait_for(gc.mark(publication), 30)
        await asyncio.wait_for(writer_task, 30)
    finally:
        if not writer_task.done():
            writer_task.cancel()
            await asyncio.gather(writer_task, return_exceptions=True)


async def test_gc_rejects_ambient_snapshot_transaction(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    gc, publication, _, _ = await setup(db, tmp_path)
    with pytest.raises(ConflictError, match="dedicated"):
        async with db.transaction():
            await gc.report()
            await gc.mark(publication)
    assert (await gc._row(publication))["gc_state"] == "live"


@pytest.mark.parametrize("kind", ["reader", "backup"])
async def test_mark_waits_for_concurrent_pin_then_preserves_bundle(
    conversation_test_database,
    tmp_path,
    kind,
):
    db = conversation_test_database
    gc, publication, _, _ = await setup(db, tmp_path)
    pinned, release = asyncio.Event(), asyncio.Event()

    async def pin_writer():
        async with db.transaction():
            batch = await gc.retentions.pin([publication], kind=kind)
            pinned.set()
            await release.wait()
        return batch

    owner = asyncio.create_task(pin_writer())
    await pinned.wait()
    marker = asyncio.create_task(gc.mark(publication))
    try:
        await asyncio.sleep(0.05)
        assert not marker.done()
        release.set()
        batch = await owner
        with pytest.raises(ConflictError, match="retention"):
            await asyncio.wait_for(marker, 3)
        assert (await gc._row(publication))["gc_state"] == "live"
        assert (
            tmp_path
            / BundleStore.path_for_row(
                await gc._row(publication),
            )
        ).is_file()
        await gc.retentions.release(batch.retention_key)
    finally:
        release.set()
        await asyncio.gather(owner, marker, return_exceptions=True)


async def test_unknown_and_scope_dangling_rows_are_quarantined(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    gc, publication, session, _ = await setup(db, tmp_path, state="unknown")
    assert "operation_not_terminal" in (await gc.report())[0].reasons
    with pytest.raises(ConflictError, match="terminal"):
        await gc.mark(publication)
    await db.execute(
        "UPDATE swe_conversation_publications SET state='committed' WHERE id=%s",
        (publication,),
    )
    await db.execute(
        "UPDATE swe_conversation_sessions SET tenant_key=%s WHERE id=%s",
        (b"other", session),
    )
    assert "scope_or_dangling_reference" in (await gc.report())[0].reasons
    with pytest.raises(ConflictError, match="dangling"):
        await gc.mark(publication)


@pytest.mark.parametrize("kind", ["reader", "backup"])
async def test_deleting_mark_wins_concurrent_pin_without_resurrection(
    conversation_test_database,
    tmp_path,
    monkeypatch,
    kind,
):
    db = conversation_test_database
    gc, publication, _, _ = await setup(db, tmp_path)
    locked, release = asyncio.Event(), asyncio.Event()
    original = gc._row

    async def blocked_row(identity, *, lock=False):
        row = await original(identity, lock=lock)
        if lock:
            locked.set()
            await release.wait()
        return row

    monkeypatch.setattr(gc, "_row", blocked_row)
    marker = asyncio.create_task(gc.mark(publication))
    await locked.wait()
    reader = asyncio.create_task(gc.retentions.pin([publication], kind=kind))
    try:
        await asyncio.sleep(0.05)
        assert not reader.done()
        release.set()
        await marker
        with pytest.raises(ConflictError, match="committed/live"):
            await asyncio.wait_for(reader, 3)
        assert (await original(publication))["gc_state"] == "deleting"
        assert not await db.fetch_all(
            "SELECT id FROM swe_conversation_retentions WHERE publication_id=%s AND state='active'",
            (publication,),
        )
    finally:
        release.set()
        await asyncio.gather(marker, reader, return_exceptions=True)


async def test_mark_waits_for_concurrent_backup_fence(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    gc, publication, _, _ = await setup(db, tmp_path)
    fenced, release = asyncio.Event(), asyncio.Event()

    async def backup():
        async with db.transaction():
            await gc.retentions.install_backup_fence(
                "concurrent-fence",
                "snapshot",
            )
            fenced.set()
            await release.wait()

    owner = asyncio.create_task(backup())
    await fenced.wait()
    marker = asyncio.create_task(gc.mark(publication))
    try:
        await asyncio.sleep(0.05)
        assert not marker.done()
        release.set()
        await owner
        with pytest.raises(ConflictError, match="fence"):
            await asyncio.wait_for(marker, 3)
        assert (await gc._row(publication))["gc_state"] == "live"
    finally:
        release.set()
        await asyncio.gather(owner, marker, return_exceptions=True)

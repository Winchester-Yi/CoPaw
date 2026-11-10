# -*- coding: utf-8 -*-
"""Backup/restore and pinned old-reader rollback using isolated schemas only."""

from contextlib import asynccontextmanager
import json
from uuid import uuid4

import pytest

from swe.app.chat_sharing.store import ChatShareStore
from swe.app.conversation_storage.backup import (
    ConversationBackup,
    RestoreEvidence,
    restore_backup,
)
from swe.app.conversation_storage.bundle import BundleStore, new_operation_key
from swe.app.conversation_storage.chat_repository import MySQLChatRepository
from swe.app.conversation_storage.execution import ExecutionStore
from swe.app.conversation_storage.identity import (
    WorkspaceScope,
    SessionIdentity,
)
from swe.app.conversation_storage.migration.exporter import (
    ConversationExporter,
    OldFormatTarget,
)
from swe.app.conversation_storage.publications import PublicationStore
from swe.app.conversation_storage.session_adapter import MySQLSessionAdapter
from swe.app.conversation_storage.shares import ShareStorage
from swe.app.runner.models import ChatSpec
from swe.app.runner.session import SafeJSONSession
from swe.database.connection import DatabaseConnection
from tests.integrated.conversation_storage.conftest import (
    SQL_ROOT,
    apply_sql_script,
)


@asynccontextmanager
async def empty_clone(db):
    schema = "test_conversation_restore_" + uuid4().hex
    await db.execute("CREATE DATABASE " + schema + " CHARACTER SET utf8mb4")
    clone = DatabaseConnection(
        db.config.model_copy(update={"database": schema}),
    )
    await clone.connect()
    try:
        await apply_sql_script(
            clone,
            SQL_ROOT / "conversation_metadata_tables.sql",
        )
        await ChatShareStore(clone).ensure_schema()
        await apply_sql_script(
            clone,
            SQL_ROOT / "conversation_share_publication_extension.sql",
        )
        yield clone
    finally:
        await clone.close()
        await db.execute("DROP DATABASE " + schema)


async def seed_new_data(db, root, scope=None, *, initialize_schema=True):
    root.mkdir(exist_ok=True)
    if initialize_schema:
        await ChatShareStore(db).ensure_schema()
        await apply_sql_script(
            db,
            SQL_ROOT / "conversation_share_publication_extension.sql",
        )
    scope = scope or WorkspaceScope(
        "tenant",
        "source",
        "agent",
        service_unit_id="schema-test-service",
    )
    bundles = BundleStore(root)
    repo = MySQLChatRepository(db, scope, bundles)
    chat = str(uuid4())
    await repo.upsert_chat(
        ChatSpec(
            id=chat,
            session_id="full_session",
            user_id="alice",
            meta={
                "expert_main_binding": {"revision": 1, "expert_id": "expert"},
                "future": {"secret": "retained"},
            },
        ),
    )
    identity = SessionIdentity(scope, "alice", "console", "full_session")
    execution = ExecutionStore(
        db,
        publications=PublicationStore(
            db,
            bundles,
            service_unit_id=scope.service_unit_id,
        ),
        service_unit_id=scope.service_unit_id,
    )
    admission = await execution.reserve_admission(
        identity,
        chat,
        "new-question",
        operation_key=new_operation_key(),
        runtime_turn_key="runtime-" + chat,
    )
    question = {
        "id": "new-question",
        "role": "user",
        "content": "new question",
    }
    state = {
        "schema_version": 2,
        "future_module": {"nested": [1, True]},
        "agent": {"memory": {"content": []}},
        "turn_states": {
            "new-question": {
                "status": "completed",
                "chat_id": chat,
                "message": question,
                "future": {"keep": 2},
            },
        },
    }
    await execution.publish_admission(admission, question, state)
    await execution.start(admission.authority)
    await execution.finish(
        admission.authority,
        "completed",
        state,
        operation_key=new_operation_key(),
    )
    await repo.release_current_expert_binding(chat)
    share = await ShareStorage(db, bundles, scope=scope).create_snapshot(
        chat_id=chat,
        creator_id="alice",
        tenant_id=scope.tenant_id,
        payload={"chat_name": "new-share", "messages": [question]},
        selected_question_keys=("new-question",),
    )
    await ShareStorage(
        db,
        bundles,
        service_unit_id=scope.service_unit_id,
    ).get_snapshot(share.token)
    return scope, bundles, repo, chat, share, state


async def test_new_backup_restore_and_old_reader_roundtrip(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    scope, bundles, repo, chat, share, state = await seed_new_data(
        db,
        tmp_path / "source",
    )
    backup = tmp_path / "backup"
    result = await ConversationBackup(db, bundles, scope).capture(backup)
    assert result["passed"]
    active = await db.fetch_all(
        "SELECT * FROM swe_conversation_retentions WHERE kind='backup' AND state='active'",
    )
    assert active
    assert not await db.fetch_all(
        "SELECT id FROM swe_conversation_retentions WHERE kind='backup_fence' AND state='active'",
    )
    async with empty_clone(db) as clone:
        restored = await restore_backup(
            clone,
            BundleStore(tmp_path / "restored"),
            backup,
            evidence=RestoreEvidence(
                "source-writers-stopped",
                "generated-isolated-clone",
            ),
        )
        assert restored["passed"]
        loaded = await MySQLSessionAdapter(
            clone,
            scope,
            BundleStore(tmp_path / "restored"),
            channel="console",
        ).get_session_state_dict("full_session", "alice")
        assert loaded["future_module"] == state["future_module"]
        assert (
            await ShareStorage(
                clone,
                BundleStore(tmp_path / "restored"),
                service_unit_id="schema-test-service",
            ).get_snapshot(share.token)
        )["chat_name"] == "new-share"
        record = await ChatShareStore(clone).get(share.token)
        assert record.access_count == 2
    destination = tmp_path / "old"
    target = OldFormatTarget("old@sha256:" + "a" * 64, 2, 1)
    exported = ConversationExporter(backup, target).export(destination)
    assert exported["passed"]
    old = SafeJSONSession(str(destination / "sessions"))
    loaded = await old.get_session_state_dict("full_session", "alice", False)
    assert loaded["future_module"] == state["future_module"]
    assert loaded["turn_states"]["new-question"]["future"] == {"keep": 2}
    await old.update_session_state(
        "full_session",
        "continued",
        {"written": True},
        "alice",
    )
    assert (await old.get_session_state_dict("full_session", "alice"))[
        "continued"
    ] == {
        "written": True,
    }
    assert (
        json.loads((destination / "shares.json").read_text())[0][
            "access_count"
        ]
        == 1
    )
    assert (
        json.loads((destination / "chats.json").read_text())["chats"][0][
            "meta"
        ]["expert_main_binding"]["expert_id"]
        is None
    )


async def test_backup_deleted_chat_keeps_share_and_old_tombstone(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    scope, bundles, repo, chat, share, _ = await seed_new_data(
        db,
        tmp_path / "source",
    )
    await repo.delete_chats([chat])
    backup = tmp_path / "backup"
    await ConversationBackup(db, bundles, scope).capture(backup)
    target = OldFormatTarget("old@sha256:" + "a" * 64, 2, 1)
    destination = tmp_path / "old"
    ConversationExporter(backup, target).export(destination)
    assert json.loads((destination / "chats.json").read_text())["chats"] == []
    assert (destination / "dialog" / ".deleted" / chat).exists()
    assert (
        json.loads((destination / "shares.json").read_text())[0]["token"]
        == share.token
    )


async def test_new_compact_and_stop_export_preserve_old_archive_and_complete_root(
    conversation_test_database,
    tmp_path,
):
    from agentscope.message import Msg
    from swe.app.conversation_storage.archive_runtime import MySQLArchiveStore
    from swe.agents.memory.conversation_archive import ConversationArchiveStore
    from swe.app.conversation_storage.migration.legacy import read_archive

    db = conversation_test_database
    scope, bundles, _, chat, _, _ = await seed_new_data(
        db,
        tmp_path / "source",
    )
    adapter = MySQLSessionAdapter(db, scope, bundles, channel="console")
    archive = MySQLArchiveStore(db, scope, bundles, adapter)
    message = Msg(
        "assistant",
        "compact me",
        "assistant",
        timestamp="2026-10-30T00:00:00Z",
    )
    message.id = "new-compact-source"
    state = await adapter.get_session_state_dict("full_session", "alice")
    state["agent"]["memory"]["content"] = [message.to_dict()]
    appended = await archive.append_message(
        chat,
        message,
        recovery_state=state,
        authority=None,
    )
    state = appended.recovery_state
    state["agent"]["memory"]["content"] = []
    compact = await archive.compact(
        chat,
        [message],
        recovery_state=state,
        authority=None,
    )
    execution = ExecutionStore(
        db,
        publications=PublicationStore(
            db,
            bundles,
            service_unit_id="schema-test-service",
        ),
        service_unit_id="schema-test-service",
    )
    identity = SessionIdentity(scope, "alice", "console", "full_session")
    admission = await execution.reserve_admission(
        identity,
        chat,
        "stopped-question",
        operation_key=new_operation_key(),
        runtime_turn_key="runtime-stop",
    )
    question = {
        "id": "stopped-question",
        "role": "user",
        "content": "stop this",
    }
    state = compact.recovery_state
    state["turn_states"]["stopped-question"] = {
        "status": "stopped",
        "chat_id": chat,
        "message": question,
        "stop_reason": "user",
    }
    await execution.publish_admission(admission, question, state)
    await execution.start(admission.authority)
    await execution.request_stop(identity, "stopped-question")
    await execution.finish(
        admission.authority,
        "stopped",
        state,
        operation_key=new_operation_key(),
    )
    backup = tmp_path / "backup"
    await ConversationBackup(db, bundles, scope).capture(backup)
    destination = tmp_path / "old"
    result = ConversationExporter(
        backup,
        OldFormatTarget("old@sha256:" + "b" * 64, 2, 1),
    ).export(destination)
    assert result["passed"] and not result["rollback_ready"]
    old_state = await SafeJSONSession(
        str(destination / "sessions"),
    ).get_session_state_dict("full_session", "alice")
    assert (
        old_state["turn_states"]["stopped-question"]["stop_reason"] == "user"
    )
    legacy = read_archive(destination / "dialog", chat)
    assert legacy["batches"][0]["messages"][0]["id"] == message.id
    store = ConversationArchiveStore(
        destination / "dialog",
        cursor_secret=b"test",
    )
    await store.commit(
        chat,
        [Msg("assistant", "continued old archive", "assistant")],
    )
    assert len(read_archive(destination / "dialog", chat)["batches"]) == 2


async def test_active_backup_restore_requires_original_turn_reconciliation(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    scope, bundles, _, chat, _, state = await seed_new_data(
        db,
        tmp_path / "source",
    )
    execution = ExecutionStore(
        db,
        publications=PublicationStore(
            db,
            bundles,
            service_unit_id="schema-test-service",
        ),
        service_unit_id="schema-test-service",
    )
    identity = SessionIdentity(scope, "alice", "console", "full_session")
    admitted = await execution.reserve_admission(
        identity,
        chat,
        "active-question",
        operation_key=new_operation_key(),
        runtime_turn_key="active-runtime",
    )
    await execution.publish_admission(
        admitted,
        {"id": "active-question", "role": "user", "content": "active"},
        {
            **state,
            "turn_states": {
                **state["turn_states"],
                "active-question": {
                    "status": "running",
                    "chat_id": chat,
                    "message": {
                        "id": "active-question",
                        "role": "user",
                        "content": "active",
                    },
                },
            },
        },
    )
    await execution.start(admitted.authority)
    backup = tmp_path / "backup"
    await ConversationBackup(db, bundles, scope).capture(backup)
    with pytest.raises(ValueError, match="settled"):
        ConversationExporter(
            backup,
            OldFormatTarget("old@sha256:" + "c" * 64, 2, 1),
        ).export(tmp_path / "old")
    async with empty_clone(db) as clone:
        result = await restore_backup(
            clone,
            BundleStore(tmp_path / "restored"),
            backup,
            evidence=RestoreEvidence(
                "confirmed-source-exit",
                "isolated-new-incarnation",
            ),
        )
        row = await clone.fetch_one("SELECT * FROM swe_conversation_sessions")
        turn = await clone.fetch_one(
            "SELECT * FROM swe_conversation_turns WHERE id=%s",
            (admitted.turn["id"],),
        )
        assert row["owner_key"] is None and row["lease_until"] is None
        assert row["generation"] == admitted.authority.generation + 1
        assert (
            row["active_turn_id"] == turn["id"] and turn["state"] == "settling"
        )
        assert any(
            item["action"] == "require_original_operation_reconciliation"
            for item in result["changes"]
        )


async def test_backup_includes_legacy_orphan_and_rejects_nonempty_restore(
    conversation_test_database,
    tmp_path,
):
    from swe.app.conversation_storage.publications import (
        CommitPlan,
        PublicationRequest,
    )
    from swe.app.conversation_storage.shares import ShareInstall
    from swe.app.conversation_storage.bundle import BundleBlock
    from swe.app.conversation_storage.errors import ConflictError

    db = conversation_test_database
    scope, bundles, _, _, _, _ = await seed_new_data(db, tmp_path / "source")
    publications = PublicationStore(
        db,
        bundles,
        service_unit_id="schema-test-service",
    )
    operation = new_operation_key()
    request = PublicationRequest(
        operation,
        WorkspaceScope(
            "tenant",
            "",
            "",
            service_unit_id="schema-test-service",
        ),
        "share_import",
        "offline",
        0,
        scope_kind="legacy_share",
    )
    plan = ShareInstall(
        "orphan-token",
        "old-deleted-chat",
        "former-owner",
        "tenant",
        "opaque-original-key",
        "2026-10-30T00:00:00Z",
        access_count=7,
    )
    await publications.reserve(request, CommitPlan(share=plan))
    await publications.write_bundle(
        operation,
        [
            BundleBlock(
                "share",
                "share",
                json.dumps({"chat_name": "orphan", "messages": []}).encode(),
            ),
        ],
    )
    await publications.commit(operation)
    backup = tmp_path / "backup"
    await ConversationBackup(db, bundles, scope).capture(backup)
    async with empty_clone(db) as clone:
        await restore_backup(
            clone,
            BundleStore(tmp_path / "restored"),
            backup,
            evidence=RestoreEvidence("stopped", "isolated"),
        )
        record = await ChatShareStore(clone).get("orphan-token")
        assert (
            record.access_count == 7
            and record.snapshot_key == "opaque-original-key"
        )
        assert (
            await ShareStorage(
                clone,
                BundleStore(tmp_path / "restored"),
                service_unit_id="schema-test-service",
            ).get_snapshot(record.token)
        )["chat_name"] == "orphan"
        with pytest.raises(ConflictError, match="empty metadata"):
            await restore_backup(
                clone,
                BundleStore(tmp_path / "another"),
                backup,
                evidence=RestoreEvidence("stopped", "isolated"),
            )
    target = OldFormatTarget("old@sha256:" + "d" * 64, 2, 1)
    exported = ConversationExporter(backup, target).export(tmp_path / "old")
    assert exported["shares"] == 2 and not exported["image_verified"]


async def test_verified_backup_callback_and_explicit_release_check_all_members(
    conversation_test_database,
    tmp_path,
):
    from swe.app.conversation_storage.errors import ConflictError

    db = conversation_test_database
    scope, bundles, _, _, _, _ = await seed_new_data(db, tmp_path / "source")
    service = ConversationBackup(db, bundles, scope)
    backup = tmp_path / "backup"

    async def verify_restore(path, backup_id):
        async with empty_clone(db) as clone:
            report = await restore_backup(
                clone,
                BundleStore(tmp_path / "restored"),
                path,
                evidence=RestoreEvidence("source-stopped", "isolated-clone"),
            )
            assert report["backup_id"] == backup_id
            return report

    report = await service.capture(backup, restore_verifier=verify_restore)
    assert report["backup_ready"] and report["restore_verified"]
    members = await db.fetch_all(
        "SELECT * FROM swe_conversation_retentions WHERE kind='backup' AND state='active' ORDER BY id",
    )
    assert members
    identity = members[-1]["id"]
    await db.execute(
        "UPDATE swe_conversation_retentions SET snapshot_ref=%s WHERE id=%s",
        (b"different-job", identity),
    )
    with pytest.raises(ConflictError, match="member identity"):
        await service.release(backup)
    assert len(
        await db.fetch_all(
            "SELECT id FROM swe_conversation_retentions WHERE kind='backup' AND state='active'",
        ),
    ) == len(members)
    await db.execute(
        "UPDATE swe_conversation_retentions SET snapshot_ref=%s WHERE id=%s",
        (str(backup).encode(), identity),
    )
    await service.release(backup)
    assert not await db.fetch_all(
        "SELECT id FROM swe_conversation_retentions WHERE kind='backup' AND state='active'",
    )


async def retained_v3_source(tmp_path):
    from agentscope.message import Msg
    from swe.agents.memory.conversation_archive import ConversationArchiveStore
    from swe.app.conversation_storage.migration.legacy import read_session
    from swe.app.runner.models import ChatsFile

    source = tmp_path / "legacy"
    (source / "sessions").mkdir(parents=True)
    chat = str(uuid4())
    (source / "chats.json").write_text(
        ChatsFile(
            chats=[
                ChatSpec(
                    id=chat,
                    session_id="session",
                    user_id="alice",
                ),
            ],
        ).model_dump_json(),
    )
    old_state = {
        "schema_version": 2,
        "revision": 0,
        "future": {"nested": [True, "old"]},
        "agent": {"memory": {"content": []}},
        "turn_states": {
            f"old-{index}": {
                "status": "completed",
                "chat_id": chat,
                "message": {
                    "id": f"old-{index}",
                    "role": "user",
                    "content": "original" * 13000,
                },
                "extra": {"position": index},
            }
            for index in range(3)
        },
    }
    session_path = source / "sessions" / "alice_session.json"
    session_path.write_text(json.dumps(old_state))
    old = SafeJSONSession(str(source / "sessions"))
    await old.migrate_terminal_turns("session", "alice", dry_run=False)
    await old.update_session_state(
        "session",
        "future.more",
        "original",
        "alice",
    )
    original = read_session(session_path)
    assert original["root"]["cold_turn_count"] > 0
    ledger_bytes = {
        p.relative_to(source): p.read_bytes()
        for p in (source / "sessions" / ".turn-ledger").rglob("*")
        if p.is_file()
    }
    source_archive = ConversationArchiveStore(
        source / "dialog",
        cursor_secret=b"test",
    )
    source_archive.migrate_checkpoint_v2(chat)
    await source_archive.commit(
        chat,
        [Msg("assistant", "old epoch", "assistant")],
    )
    await source_archive.reset_checkpoint_epoch(chat, reason="clear")
    await source_archive.commit(
        chat,
        [Msg("assistant", "old current", "assistant")],
    )
    for path in source.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    source.chmod(0o555)
    return source, chat, session_path, original, ledger_bytes


async def publish_post_import_question_and_compact(db, bundles, scope, chat):
    from agentscope.message import Msg
    from swe.app.conversation_storage.archive_runtime import MySQLArchiveStore

    adapter = MySQLSessionAdapter(db, scope, bundles, channel="console")
    execution = ExecutionStore(
        db,
        publications=PublicationStore(
            db,
            bundles,
            service_unit_id="schema-test-service",
        ),
        service_unit_id="schema-test-service",
    )
    identity = SessionIdentity(scope, "alice", "console", "session")
    admission = await execution.reserve_admission(
        identity,
        chat,
        "new-question",
        operation_key=new_operation_key(),
        runtime_turn_key="new-runtime",
    )
    state = await adapter.get_session_state_dict("session", "alice")
    question = {"id": "new-question", "role": "user", "content": "new"}
    state["turn_states"]["new-question"] = {
        "status": "completed",
        "chat_id": chat,
        "message": question,
        "future": {"retained": True},
    }
    state["future"]["new"] = "after-cutover"
    await execution.publish_admission(admission, question, state)
    await execution.start(admission.authority)
    await execution.finish(
        admission.authority,
        "completed",
        state,
        operation_key=new_operation_key(),
    )
    new_archive = MySQLArchiveStore(db, scope, bundles, adapter)
    archived = Msg("assistant", "after cutover compact", "assistant")
    state = await adapter.get_session_state_dict("session", "alice")
    state["agent"]["memory"]["content"] = [archived.to_dict()]
    appended = await new_archive.append_message(
        chat,
        archived,
        recovery_state=state,
        authority=None,
    )
    state = appended.recovery_state
    state["agent"]["memory"]["content"] = []
    await new_archive.compact(
        chat,
        [archived],
        recovery_state=state,
        authority=None,
    )
    return archived


async def test_retained_v3_ledger_new_write_exports_and_old_reader_continues(
    conversation_test_database,
    tmp_path,
):
    from swe.app.conversation_storage.migration.inventory import (
        WorkspaceMapping,
        inventory,
    )
    from swe.app.conversation_storage.migration.manifest import (
        MigrationManifest,
        SourceBoundary,
    )
    from swe.app.conversation_storage.migration.importer import (
        ConversationImporter,
    )
    from swe.app.conversation_storage.migration.install_records import (
        OfflineTarget,
    )
    from swe.app.conversation_storage.migration.legacy import read_session
    from swe.app.runner.models import ChatsFile
    from agentscope.message import Msg
    from swe.agents.memory.conversation_archive import ConversationArchiveStore
    from swe.app.conversation_storage.archive_runtime import MySQLArchiveStore
    from swe.app.conversation_storage.migration.legacy import read_archive

    db = conversation_test_database
    await ChatShareStore(db).ensure_schema()
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    source, chat, session_path, original, ledger_bytes = (
        await retained_v3_source(tmp_path)
    )
    scope = WorkspaceScope(
        "tenant",
        "source",
        "agent",
        service_unit_id="schema-test-service",
    )
    boundary = SourceBoundary(
        "immutable-source",
        "2026-10-30T00:00:00Z",
        "old@sha256:" + "e" * 64,
        ("writers-stopped", "readonly"),
    )
    inventory_report = inventory([WorkspaceMapping(scope, source)], boundary)
    assert inventory_report["ready"], inventory_report["blockers"]
    manifest = MigrationManifest.create(
        tmp_path / "manifest.json",
        boundary,
        [
            {
                "root": str(source),
                "service_unit_id": scope.service_unit_id,
                "tenant_id": "tenant",
                "source_id": "source",
                "agent_id": "agent",
            },
        ],
    )
    (tmp_path / "nas").mkdir()
    bundles = BundleStore(tmp_path / "nas")
    await ConversationImporter(
        db,
        bundles,
        manifest,
        OfflineTarget(db.config.database, "business", manifest.migration_id),
    ).run(inventory_report)
    archived = await publish_post_import_question_and_compact(
        db,
        bundles,
        scope,
        chat,
    )
    backup = tmp_path / "backup"
    await ConversationBackup(db, bundles, scope).capture(backup)
    destination = tmp_path / "export"
    result = ConversationExporter(
        backup,
        OldFormatTarget(boundary.old_image_digest, 3, 2),
    ).export(destination)
    assert result["passed"] and not result["rollback_ready"]
    exported = read_session(destination / "sessions" / session_path.name)
    assert (
        exported["root"]["session_generation"]
        == original["root"]["session_generation"]
    )
    assert exported["state"]["turn_states"]["new-question"]["future"] == {
        "retained": True,
    }
    assert all(
        (destination / path).read_bytes() == data
        for path, data in ledger_bytes.items()
    )
    from swe.app.runner.turn_ledger import TurnLedger

    latest = TurnLedger(destination / "sessions" / session_path.name).latest(
        exported["root"],
        chat,
    )
    assert latest == (
        "new-question",
        exported["state"]["turn_states"]["new-question"],
    )
    continued = SafeJSONSession(str(destination / "sessions"))
    await continued.update_session_state(
        "session",
        "future.continued",
        True,
        "alice",
    )
    final = read_session(destination / "sessions" / session_path.name)["state"]
    assert (
        final["future"]["new"] == "after-cutover"
        and final["future"]["continued"]
    )
    assert final["turn_states"]["old-0"]["extra"] == {"position": 0}
    legacy_archive = read_archive(destination / "dialog", chat)
    assert len(legacy_archive["epochs"]) == 2
    assert any(
        message["id"] == archived.id
        for batch in legacy_archive["batches"]
        for message in batch["messages"]
    )
    old_archive = ConversationArchiveStore(
        destination / "dialog",
        cursor_secret=b"test",
    )
    await old_archive.commit(
        chat,
        [Msg("assistant", "continued after export", "assistant")],
    )
    assert (
        len(read_archive(destination / "dialog", chat)["batches"])
        == len(legacy_archive["batches"]) + 1
    )


async def test_backup_waits_for_original_prepared_commit_before_scope_fence(
    conversation_test_database,
    tmp_path,
):
    import asyncio
    from swe.app.conversation_storage.bundle import BundleBlock
    from swe.app.conversation_storage.publications import (
        CommitPlan,
        PublicationRequest,
    )

    db = conversation_test_database
    scope, bundles, _, _, _, _ = await seed_new_data(db, tmp_path / "source")
    publications = PublicationStore(
        db,
        bundles,
        service_unit_id="schema-test-service",
    )
    operation = new_operation_key()
    request = PublicationRequest(operation, scope, "backup_probe", "writer", 0)
    await publications.reserve(request, CommitPlan())
    await publications.write_bundle(
        operation,
        [BundleBlock("probe", "probe", b"{}")],
    )
    observed = asyncio.Event()

    class ObservePrepared:
        def __getattr__(self, name):
            return getattr(db, name)

        async def fetch_all(self, query, params=None):
            rows = await db.fetch_all(query, params)
            if (
                query.startswith(
                    "SELECT id FROM swe_conversation_publications WHERE state='prepared'",
                )
                and rows
            ):
                observed.set()
            return rows

    capture = asyncio.create_task(
        ConversationBackup(ObservePrepared(), bundles, scope).capture(
            tmp_path / "backup",
        ),
    )
    try:
        await asyncio.wait_for(observed.wait(), timeout=10)
        assert not capture.done()
        assert not await db.fetch_all(
            "SELECT id FROM swe_conversation_retentions WHERE kind='backup_fence' AND state='active'",
        )
        await publications.commit(operation)
        report = await asyncio.wait_for(capture, timeout=30)
        assert report["passed"]
    finally:
        if not capture.done():
            capture.cancel()
            try:
                await capture
            except asyncio.CancelledError:
                pass


async def test_backup_prepared_timeout_leaves_no_fence_and_original_can_commit(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    from swe.app.conversation_storage import backup
    from swe.app.conversation_storage.bundle import BundleBlock
    from swe.app.conversation_storage.errors import ConflictError
    from swe.app.conversation_storage.publications import (
        CommitPlan,
        PublicationRequest,
    )

    db = conversation_test_database
    scope, bundles, _, _, _, _ = await seed_new_data(db, tmp_path / "source")
    publications = PublicationStore(
        db,
        bundles,
        service_unit_id="schema-test-service",
    )
    operation = new_operation_key()
    await publications.reserve(
        PublicationRequest(operation, scope, "backup_probe", "writer", 0),
        CommitPlan(),
    )
    await publications.write_bundle(
        operation,
        [BundleBlock("probe", "probe", b"{}")],
    )
    monkeypatch.setattr(backup, "BACKUP_SETTLE_SECONDS", 0)
    destination = tmp_path / "timed-out-backup"
    with pytest.raises(ConflictError, match="settle"):
        await ConversationBackup(db, bundles, scope).capture(destination)
    assert not list(destination.iterdir())
    assert not await db.fetch_all(
        "SELECT id FROM swe_conversation_retentions WHERE kind='backup_fence' AND state='active'",
    )
    assert (await publications.get(operation))["state"] == "prepared"
    await publications.commit(operation)
    assert (await publications.get(operation))["state"] == "committed"
    assert (
        await ConversationBackup(db, bundles, scope).capture(
            tmp_path / "recovered-backup",
        )
    )["passed"]


async def test_backup_missing_body_retains_real_sql_pins(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    scope, bundles, _, _, _, _ = await seed_new_data(db, tmp_path / "source")
    row = await db.fetch_one(
        "SELECT id,relative_path FROM swe_conversation_publications "
        "WHERE state='committed' AND gc_state='live' ORDER BY id LIMIT 1",
    )
    path = bundles.root / row["relative_path"].decode()
    body = path.read_bytes()
    destination = tmp_path / "failed-backup"
    path.unlink()
    try:
        with pytest.raises(OSError):
            await ConversationBackup(db, bundles, scope).capture(destination)
        assert (destination / "metadata.json").exists()
        assert not (destination / "report.json").exists()
        assert not await db.fetch_all(
            "SELECT id FROM swe_conversation_retentions WHERE kind='backup_fence' AND state='active'",
        )
        pins = await db.fetch_all(
            "SELECT publication_id,snapshot_ref FROM swe_conversation_retentions "
            "WHERE kind='backup' AND state='active'",
        )
        assert row["id"] in {pin["publication_id"] for pin in pins}
        assert all(
            pin["snapshot_ref"] == str(destination).encode() for pin in pins
        )
    finally:
        path.write_bytes(body)
    assert (
        await ConversationBackup(db, bundles, scope).capture(
            tmp_path / "recovered-backup",
        )
    )["passed"]
    assert await db.fetch_all(
        "SELECT publication_id FROM swe_conversation_retentions "
        "WHERE kind='backup' AND state='active' AND snapshot_ref=%s",
        (str(destination).encode(),),
    )


async def add_collectable_message(
    db,
    bundles,
    session_id,
    chat_id,
    mode,
    order,
):
    from datetime import datetime
    from swe.app.conversation_storage.bundle import BundleBlock
    from tests.integrated.conversation_storage.test_schema import (
        insert_publication,
        insert_row,
    )
    from swe.app.conversation_storage.identity import WorkspaceScope

    operation = new_operation_key()
    message = {
        "id": "collected-" + mode,
        "role": "assistant",
        "content": "obsolete " + mode,
    }
    blocks = [BundleBlock("message", "message", json.dumps(message).encode())]
    if mode == "archive":
        blocks.append(
            BundleBlock("archive", "archive", json.dumps([message]).encode()),
        )
    index = bundles.publish(
        operation,
        blocks,
        scope=WorkspaceScope(
            "tenant",
            "source",
            "agent",
            service_unit_id="schema-test-service",
        ),
        metadata={"operation_key": operation},
    )
    extent = index.blocks["message"]
    fields = {
        "session_id": session_id,
        "chat_id": chat_id,
        "state": "committed",
        "relative_path": index.relative_path.encode(),
        "byte_length": index.byte_length,
        "durable_at": datetime(2026, 10, 30),
    }
    if mode == "archive":
        fields.update(
            archive_offset=index.blocks["archive"].offset,
            archive_length=index.blocks["archive"].length,
            archive_source_count=1,
            archive_display_count=1,
            boundary_key=str(uuid4()).encode(),
            boundary_order=1,
            epoch=1,
        )
    publication = await insert_publication(db, operation.encode(), **fields)
    message_id = await insert_row(
        db,
        "swe_conversation_messages",
        {
            "session_id": session_id,
            "layer": "archive" if mode == "archive" else "memory",
            "session_order": None if mode == "archive" else order,
            "archive_chat_id": chat_id if mode == "archive" else None,
            "archive_order": 1 if mode == "archive" else None,
            "archive_publication_id": (
                publication if mode == "archive" else None
            ),
            "source_occurrence_no": 0,
            "source_msg_key": message["id"].encode(),
            "role_key": b"assistant",
            "kind_key": b"message",
            "publication_id": publication,
            "byte_offset": extent.offset,
            "byte_length": extent.length,
            "context_epoch": 0,
            "in_context": 0,
            "current_visible": 0 if mode == "hidden" else 1,
            "deleted_at": (
                datetime(2026, 10, 30) if mode == "deleted" else None
            ),
        },
    )
    return publication, message_id, index.relative_path


async def test_gc_tombstones_backup_restore_preserve_effective_data_without_missing_bodies(
    conversation_test_database,
    tmp_path,
):
    from swe.app.conversation_storage.gc import GarbageCollector
    from swe.app.conversation_storage.errors import ConflictError
    from swe.app.conversation_storage.migration.legacy import read_archive

    db = conversation_test_database
    scope, bundles, repo, live_chat, share, state = await seed_new_data(
        db,
        tmp_path / "source",
    )
    deleted_chat = str(uuid4())
    await repo.upsert_chat(
        ChatSpec(
            id=deleted_chat,
            session_id="full_session",
            user_id="alice",
            meta={"obsolete": "body"},
        ),
    )
    row = await db.fetch_one(
        "SELECT * FROM swe_conversation_chats WHERE external_chat_key=%s",
        (deleted_chat.encode(),),
    )
    garbage = [
        await add_collectable_message(
            db,
            bundles,
            row["session_id"],
            row["id"],
            mode,
            order,
        )
        for order, mode in enumerate(
            ("hidden", "deleted", "archive"),
            start=100,
        )
    ]
    await repo.delete_chats([deleted_chat])
    garbage.append((row["content_meta_publication_id"], None, None))
    collector = GarbageCollector(db, scope, bundles, physical_enabled=True)
    protected = await db.fetch_one(
        "SELECT recovery_publication_id FROM swe_conversation_sessions WHERE id=%s",
        (row["session_id"],),
    )
    with pytest.raises(ConflictError, match="protected"):
        await collector.mark(protected["recovery_publication_id"])
    for publication, _, relative_path in garbage:
        await collector.mark(publication)
        await collector.reclaim(publication)
        if relative_path:
            assert not (bundles.root / relative_path).exists()
    backup = tmp_path / "backup"
    assert (await ConversationBackup(db, bundles, scope).capture(backup))[
        "passed"
    ]
    async with empty_clone(db) as clone:
        restored_bundles = BundleStore(tmp_path / "restored")
        assert (
            await restore_backup(
                clone,
                restored_bundles,
                backup,
                evidence=RestoreEvidence("stopped", "isolated"),
            )
        )["passed"]
        for publication, message_id, relative_path in garbage:
            restored = await clone.fetch_one(
                "SELECT * FROM swe_conversation_publications WHERE id=%s",
                (publication,),
            )
            assert restored["gc_state"] == "reclaimed"
            assert not (
                restored_bundles.root / restored["relative_path"].decode()
            ).exists()
            if message_id:
                assert await clone.fetch_one(
                    "SELECT * FROM swe_conversation_messages WHERE id=%s",
                    (message_id,),
                ) == await db.fetch_one(
                    "SELECT * FROM swe_conversation_messages WHERE id=%s",
                    (message_id,),
                )
        restored_repo = MySQLChatRepository(clone, scope, restored_bundles)
        assert await restored_repo.get_chat(deleted_chat) is None
        assert (await restored_repo.get_chat(live_chat)).id == live_chat
        restored_state = await MySQLSessionAdapter(
            clone,
            scope,
            restored_bundles,
            channel="console",
        ).get_session_state_dict("full_session", "alice")
        assert restored_state["future_module"] == state["future_module"]
        assert (
            await ShareStorage(
                clone,
                restored_bundles,
                service_unit_id="schema-test-service",
            ).get_snapshot(
                share.token,
            )
        )["chat_name"] == "new-share"
    destination = tmp_path / "old"
    assert ConversationExporter(
        backup,
        OldFormatTarget("old@sha256:" + "f" * 64, 2, 1),
    ).export(destination)["passed"]
    assert read_archive(destination / "dialog", deleted_chat)["deleted"]
    assert [
        chat["id"]
        for chat in json.loads((destination / "chats.json").read_text())[
            "chats"
        ]
    ] == [live_chat]

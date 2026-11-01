# -*- coding: utf-8 -*-
import json
import uuid
import pytest
from copy import deepcopy

from agentscope.message import Msg

from swe.app.chat_sharing.store import ChatShareStore
from swe.app.conversation_storage.bundle import BundleStore
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.history import HistoryStore
from swe.app.conversation_storage.session_adapter import MySQLSessionAdapter
from swe.app.conversation_storage.chat_repository import MySQLChatRepository
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
from swe.app.conversation_storage.migration.verify import verify_import
from swe.app.runner.models import ChatSpec, ChatsFile
from swe.agents.memory.conversation_archive import ConversationArchiveStore
from swe.app.conversation_storage.shares import ShareStorage
from swe.app.runner.session import SafeJSONSession
from tests.integrated.conversation_storage.conftest import (
    SQL_ROOT,
    apply_sql_script,
)


def source_tree(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "sessions").mkdir()
    (source / "dialog").mkdir()
    return source


def duplicate_messages():
    first = Msg("user", "one", "user")
    second = Msg("assistant", "two", "assistant")
    first.id = second.id = "duplicate"
    return first, second


async def prepare_archives(source, spec, source_version):
    deleted = spec.model_copy(
        update={"id": str(uuid.uuid4()), "name": "deleted"},
    )
    (source / "chats.json").write_text(
        ChatsFile(chats=[spec, deleted]).model_dump_json(),
    )
    archive = ConversationArchiveStore(
        source / "dialog",
        cursor_secret=b"test",
    )
    if source_version == 3:
        archive.migrate_checkpoint_v2(spec.id)
    await archive.commit(spec.id, [Msg("assistant", "archived", "assistant")])
    if source_version == 3:
        await archive.reset_checkpoint_epoch(spec.id, reason="clear")
    await archive.delete_chat(deleted.id)


async def reject_tampered_reentry(importer, report, db, manifest):
    from swe.app.conversation_storage.errors import IntegrityError

    changed = deepcopy(report)
    changed["workspaces"][0]["sessions"][0]["legacy"]["state"][
        "future_module"
    ]["nested"][0] = 99
    with pytest.raises(IntegrityError, match="content differs"):
        await importer.run(changed)
    sid = manifest.data["assignments"]["session"]["workspace:0/session:0"][
        "target_id"
    ]
    await db.execute(
        "UPDATE swe_conversation_sessions SET external_session_key=%s WHERE id=%s",
        (b"tampered", sid),
    )
    with pytest.raises(IntegrityError, match="typed row differs"):
        await importer.run(report)
    await db.execute(
        "UPDATE swe_conversation_sessions SET external_session_key=%s WHERE id=%s",
        (b"full_session_id", sid),
    )


@pytest.mark.parametrize(
    ("source_version", "interrupt_messages"),
    [(1, False), (2, False), (3, False), (3, True)],
    ids=["v1", "v2", "v3", "v3-interrupted-message-batch"],
)
# pylint: disable=too-many-statements
async def test_complete_legacy_import_reentry_and_semantics(
    conversation_test_database,
    tmp_path,
    source_version,
    interrupt_messages,
    monkeypatch,
):
    db = conversation_test_database
    await ChatShareStore(db).ensure_schema()
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    source = source_tree(tmp_path)
    chat = str(uuid.uuid4())
    first, second = duplicate_messages()
    spec = ChatSpec(
        id=chat,
        session_id="full_session_id",
        user_id="",
        channel="console",
        meta={
            "scenario_snapshot": {"private": "retain"},
            "expert_main_binding": {"revision": 2, "expert_id": None},
        },
    )
    (source / "chats.json").write_text(
        ChatsFile(chats=[spec]).model_dump_json(),
    )
    state = {
        "schema_version": min(source_version, 2),
        "revision": 4,
        "agent": {"memory": {"content": [first.to_dict(), second.to_dict()]}},
        "future_module": {"nested": [1, {"private": "body"}]},
        "turn_states": {
            "old-question": {
                "status": "stopped",
                "chat_id": chat,
                "message": {
                    "id": "old-question",
                    "role": "user",
                    "content": "old",
                },
            },
        },
    }
    (source / "sessions" / "full_session_id.json").write_text(
        json.dumps(state),
    )
    if source_version == 3:
        await SafeJSONSession(str(source / "sessions")).migrate_terminal_turns(
            "full_session_id",
            dry_run=False,
        )
    await prepare_archives(source, spec, source_version)
    (source / "share.json").write_text(
        json.dumps(
            {"chat_name": "old", "messages": [], "future": "share-module"},
        ),
    )
    shares = [
        {
            "token": "permanent",
            "chat_id": "deleted-original",
            "creator_id": "alice",
            "tenant_id": "tenant",
            "snapshot_key": "original.json",
            "created_at": "2026-10-30T00:00:00Z",
            "access_count": 17,
            "last_access_at": None,
            "snapshot_workspace_index": 0,
            "snapshot_path": "share.json",
        },
    ]
    before = {
        str(p.relative_to(source)): p.read_bytes()
        for p in source.rglob("*")
        if p.is_file()
    }
    for path in source.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    source.chmod(0o555)
    scope = WorkspaceScope("tenant", "source", "agent")
    boundary = SourceBoundary(
        "snapshot",
        "2026-10-30T00:00:00Z",
        "old@sha256:" + "a" * 64,
        ("writers-stopped", "snapshot-readonly"),
    )
    report = inventory(
        [WorkspaceMapping(scope, source)],
        boundary,
        shares=shares,
    )
    assert report["ready"], report["blockers"]
    state = report["workspaces"][0]["sessions"][0]["legacy"]["state"]
    manifest = MigrationManifest.create(
        tmp_path / "manifest.json",
        boundary,
        [
            {
                "root": str(source),
                "tenant_id": "tenant",
                "source_id": "source",
                "agent_id": "agent",
            },
        ],
    )
    (tmp_path / "target").mkdir()
    bundles = BundleStore(tmp_path / "target")
    target = OfflineTarget(
        db.config.database,
        "business",
        manifest.migration_id,
    )
    importer = ConversationImporter(
        db,
        bundles,
        manifest,
        target,
        batch_size=2,
    )
    if interrupt_messages:
        from tests.integrated.conversation_storage.cutover_faults import (
            resume_committed_import,
        )

        await resume_committed_import(
            importer,
            report,
            monkeypatch,
            message_batch=True,
        )
    else:
        await importer.run(report)
    counts = [
        (await db.fetch_one("SELECT COUNT(*) n FROM " + table))["n"]
        for table in (
            "swe_conversation_sessions",
            "swe_conversation_chats",
            "swe_conversation_turns",
            "swe_conversation_messages",
            "swe_conversation_publications",
            "swe_chat_shares",
        )
    ]
    await ConversationImporter(
        db,
        bundles,
        MigrationManifest.load(manifest.path),
        target,
        batch_size=2,
    ).run(report)
    assert counts == [
        (await db.fetch_one("SELECT COUNT(*) n FROM " + table))["n"]
        for table in (
            "swe_conversation_sessions",
            "swe_conversation_chats",
            "swe_conversation_turns",
            "swe_conversation_messages",
            "swe_conversation_publications",
            "swe_chat_shares",
        )
    ]
    result = await verify_import(db, bundles, manifest, report, target)
    assert result["passed"], result["blockers"]
    await reject_tampered_reentry(importer, report, db, manifest)
    record = await db.fetch_one(
        "SELECT access_count,last_access_at FROM swe_chat_shares WHERE token=%s",
        ("permanent",),
    )
    assert record == {"access_count": 17, "last_access_at": None}
    adapter = MySQLSessionAdapter(db, scope, bundles, channel="console")
    assert await adapter.get_session_state_dict("full_session_id", "") == state
    assert (
        await adapter.get_turn_state(
            "full_session_id",
            "",
            state,
            "old-question",
        )
    )["status"] == "stopped"
    assert (
        await MySQLChatRepository(db, scope, bundles).get_chat(chat)
    ).meta == spec.meta
    history = await HistoryStore(
        db,
        scope,
        bundles,
        cursor_secret=b"test",
    ).current(
        chat,
    )
    assert {"duplicate", "old-question"} <= {
        m.metadata.get("original_id", m.id) for m in history.messages
    }
    assert (
        await ShareStorage(db, bundles).get_snapshot("permanent")
        == report["shares"][0]["snapshot"]
    )
    assert (
        await db.fetch_one(
            "SELECT access_count FROM swe_chat_shares WHERE token=%s",
            ("permanent",),
        )
    )["access_count"] == 18
    assert before == {
        str(p.relative_to(source)): p.read_bytes()
        for p in source.rglob("*")
        if p.is_file()
    }

# -*- coding: utf-8 -*-
"""Exercise v3 cutover against a disposable MySQL schema and NAS roots."""

import json

import pytest

from swe.app.chat_sharing.store import ChatShareStore
from swe.app.conversation_storage.migration.discovery import (
    discover_services,
    freeze_discovery,
)
from swe.app.conversation_storage.migration.inventory_v3 import inventory_v3
from swe.app.conversation_storage.migration.manifest import SourceBoundary
from swe.cli.conversation_storage_v3_cmd import _execute
from tests.integrated.conversation_storage.conftest import (
    SQL_ROOT,
    apply_sql_script,
)


@pytest.mark.asyncio
# pylint: disable-next=too-many-statements
async def test_v3_discovery_import_resume_verify_finalize(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    db = conversation_test_database
    await ChatShareStore(db).ensure_schema()
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_migration_control_tables.sql",
    )
    source = tmp_path / "source"
    nas = tmp_path / "nas"
    profiles = {}
    from agentscope.message import Msg
    from swe.app.runner.models import ChatSpec, ChatsFile

    chat_ids = (
        "71b9d38d-8e8e-4f7c-b32d-99d75a2e0491",
        "b62164c2-a066-4ab8-b1b4-9068d817252d",
    )

    for index in range(2):
        agent = f"agent{index}"
        session = f"session{index}"
        workspace = source / "default_SRC" / "workspaces" / agent
        (workspace / "sessions").mkdir(parents=True)
        runtime = nas / "default_SRC" / "workspaces" / agent
        runtime.mkdir(parents=True)
        profiles[agent] = {
            "id": agent,
            "workspace_dir": str(runtime),
            "enabled": True,
        }
        (workspace / "agent.json").write_text(json.dumps({"id": agent}))
        chats = [
            ChatSpec(
                id=chat_ids[index],
                session_id=session,
                user_id="alice",
                channel="console",
            ),
        ]
        (workspace / "chats.json").write_text(
            ChatsFile(chats=chats).model_dump_json(),
        )
        content = [
            Msg(
                "user" if message_index % 2 == 0 else "assistant",
                f"question {index} message {message_index}: " + "x" * 256,
                "user" if message_index % 2 == 0 else "assistant",
            ).to_dict()
            for message_index in range(8 if index == 0 else 1)
        ]
        state = {
            "schema_version": 2,
            "revision": 1,
            "agent": {
                "memory": {
                    "content": content,
                },
            },
        }
        (workspace / "sessions" / f"alice_{session}.json").write_text(
            json.dumps(state),
        )
    (source / "default_SRC" / "config.json").write_text(
        json.dumps({"agents": {"profiles": profiles}}),
    )
    import shutil

    source2 = tmp_path / "source2"
    nas2 = tmp_path / "nas2"
    runtime2 = nas2 / "default_SRC" / "workspaces" / "agent0"
    runtime2.mkdir(parents=True)
    workspace2 = source2 / "default_SRC" / "workspaces" / "agent0"
    shutil.copytree(
        source / "default_SRC" / "workspaces" / "agent0",
        workspace2,
    )
    profile2 = {
        "id": "agent0",
        "workspace_dir": str(runtime2),
        "enabled": True,
    }
    (source2 / "default_SRC" / "config.json").write_text(
        json.dumps({"agents": {"profiles": {"agent0": profile2}}}),
    )
    snapshot = source / "default" / "chat_shares" / "share-token.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(
        json.dumps(
            {
                "chat_name": "shared",
                "messages": [{"role": "user", "content": "shared question"}],
            },
        ),
    )
    orphan_snapshot = (
        source / "retired-tenant" / "chat_shares" / "orphan-token.json"
    )
    orphan_snapshot.parent.mkdir(parents=True)
    orphan_snapshot.write_text(
        json.dumps(
            {
                "chat_name": "orphaned share",
                "messages": [{"role": "user", "content": "orphan body"}],
            },
        ),
    )
    await db.execute(
        "INSERT INTO swe_chat_shares (token,chat_id,creator_id,tenant_id,snapshot_key,created_at,access_count,last_access_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            "share-token",
            chat_ids[0],
            "alice",
            "default",
            "default/chat_shares/share-token.json",
            "2026-11-01 00:00:00",
            17,
            None,
        ),
    )
    await db.execute(
        "INSERT INTO swe_chat_shares (token,chat_id,creator_id,tenant_id,snapshot_key,created_at,access_count,last_access_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            "orphan-token",
            "deleted-chat",
            "former-user",
            "retired-tenant",
            "retired-tenant/chat_shares/orphan-token.json",
            "2026-10-01 12:30:00",
            3,
            "2026-11-01 08:15:00",
        ),
    )
    shares = await db.fetch_all(
        "SELECT token,chat_id,creator_id,tenant_id,snapshot_key,created_at,access_count,last_access_at FROM swe_chat_shares",
    )
    for root in (source, source2):
        for path in root.rglob("*"):
            path.chmod(0o555 if path.is_dir() else 0o444)
        root.chmod(0o555)
    service = {
        "service_unit_id": "TEST-SERVICE",
        "source_root": str(source),
        "config_root": str(source),
        "original_root": str(nas),
        "nas_root": str(nas),
        "snapshot_root": str(source),
    }
    service2 = {
        "service_unit_id": "TEST-SERVICE2",
        "source_root": str(source2),
        "config_root": str(source2),
        "original_root": str(nas2),
        "nas_root": str(nas2),
    }
    report = discover_services([service, service2], shares=shares)
    assert report["ready"], report["blockers"]
    boundary = SourceBoundary(
        "snapshot",
        "2026-11-02T00:00:00Z",
        "old@sha256:" + "a" * 64,
        ("stopped", "readonly"),
    )
    manifest = freeze_discovery(
        report,
        tmp_path / "manifest.json",
        boundary,
        db.config.database,
    )
    inventory = inventory_v3(manifest)
    assert len(inventory["workspaces"]) == 3
    assert sum(len(item["chats"]) for item in inventory["workspaces"]) == 3
    assert sum(len(item["sessions"]) for item in inventory["workspaces"]) == 3
    assert (
        sum(
            len(session["legacy"]["state"]["agent"]["memory"]["content"])
            for workspace in inventory["workspaces"]
            for session in workspace["sessions"]
        )
        == 17
    )
    monkeypatch.setattr(
        "swe.database.config.get_database_config",
        lambda: db.config,
    )
    assert (await _execute("preflight", manifest, inventory, None))["passed"]
    from swe.app.conversation_storage.migration.importer import (
        ParallelWorkspaceImporter,
    )
    from swe.app.conversation_storage.migration.v3_manifest import V3Manifest

    # The internal executor expects the same fixed cutoffs as the CLI spool path.
    inventory = inventory_v3(manifest, begin_sync=True)
    original_finish = ParallelWorkspaceImporter._batch_finished

    async def interrupt_after_commit(self, position):
        await original_finish(self, position)
        raise RuntimeError("simulated stopped migration process")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(
            ParallelWorkspaceImporter,
            "_batch_finished",
            interrupt_after_commit,
        )
        with pytest.raises(RuntimeError, match="simulated stopped"):
            await _execute("import", manifest, inventory, None)
    manifest = V3Manifest.load(manifest.path)
    await _execute("resume", manifest, inventory, None)
    import asyncio
    from click.testing import CliRunner
    from swe.cli.main import cli

    verify = await asyncio.to_thread(
        CliRunner().invoke,
        cli,
        ["conversation-storage", "verify", "--manifest", str(manifest.path)],
    )
    assert verify.exit_code == 0, verify.output
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_service_unit_finalize.sql",
    )
    result = await asyncio.to_thread(
        CliRunner().invoke,
        cli,
        [
            "conversation-storage",
            "finalize",
            "--manifest",
            str(manifest.path),
            "--output-dir",
            str(tmp_path / "output"),
            "--apply",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "output" / "completion.json").is_file()
    from swe.app.conversation_storage.migration.authority import (
        validate_authority,
    )

    runtime_paths = list((tmp_path / "output").glob("*.runtime.json"))
    assert len(runtime_paths) == 2
    for runtime_path in runtime_paths:
        runtime = json.loads(runtime_path.read_text())
        assert (await validate_authority(db, runtime))[
            "migration_id"
        ] == manifest.migration_id
    state = await db.fetch_one(
        "SELECT state FROM swe_conversation_migration_jobs WHERE migration_id=%s",
        (manifest.migration_id.encode(),),
    )
    assert state["state"] == "ready"
    migrated_share = await db.fetch_one(
        "SELECT * FROM swe_chat_shares WHERE token=%s",
        ("share-token",),
    )
    assert migrated_share["access_count"] == 17
    assert migrated_share["service_unit_key"] == b"TEST-SERVICE"
    assert migrated_share["publication_id"] is not None

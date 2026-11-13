# -*- coding: utf-8 -*-
"""Small isolated MySQL proof: A stays online while failed B is repaired."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from swe.app.chat_sharing.store import ChatShareStore
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.migration.discovery import (
    discover_services,
    freeze_discovery,
)
from swe.app.conversation_storage.migration.manifest import SourceBoundary
from swe.app.conversation_storage.runtime import ConversationRuntime
from swe.app.conversation_storage.tenant_admission import TenantNotPublished
from swe.config.context import encode_scope_id
from swe.cli import conversation_storage_tenants as tenant_cli
from tests.integrated.conversation_storage.conftest import (
    SQL_ROOT,
    apply_sql_script,
)


def _frozen_tenant(source, original, tenant):
    from agentscope.message import Msg
    from swe.app.runner.models import ChatSpec, ChatsFile

    folder = encode_scope_id(tenant, "SRC")
    workspace = source / folder / "workspaces" / "agent"
    runtime = original / folder / "workspaces" / "agent"
    (workspace / "sessions").mkdir(parents=True)
    runtime.mkdir(parents=True)
    (runtime / "agent.json").write_text('{"id":"agent"}')
    chat = str(uuid4())
    (workspace / "agent.json").write_text('{"id":"agent"}')
    (workspace / "chats.json").write_text(
        ChatsFile(
            chats=[
                ChatSpec(
                    id=chat,
                    session_id="session",
                    user_id=tenant,
                    channel="console",
                ),
            ],
        ).model_dump_json(),
    )
    (workspace / "sessions" / f"{tenant}_session.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "revision": 1,
                "agent": {
                    "memory": {
                        "content": [Msg("user", "question", "user").to_dict()],
                    },
                },
            },
        ),
    )
    (source / folder / "config.json").write_text(
        json.dumps(
            {
                "agents": {
                    "profiles": {
                        "agent": {
                            "id": "agent",
                            "workspace_dir": str(runtime),
                            "enabled": True,
                        },
                    },
                },
            },
        ),
    )
    import shutil

    shutil.copyfile(
        source / folder / "config.json",
        original / folder / "config.json",
    )
    return chat, runtime


@pytest.mark.asyncio
# One end-to-end scenario keeps publication, online writes and retry in sequence.
# pylint: disable-next=too-many-statements
async def test_tenant_publication_retry_preserves_online_tenant(
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
    source, original, nas = [
        tmp_path / name for name in ("frozen", "runtime", "body")
    ]
    nas.mkdir()
    chat_a, runtime_a = _frozen_tenant(source, original, "alice")
    _chat_b, runtime_b = _frozen_tenant(source, original, "bob")
    snapshot = source / "alice" / "chat_shares" / "token-a.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(
        json.dumps(
            {
                "chat_name": "shared",
                "messages": [
                    {"role": "user", "content": "question"},
                ],
            },
        ),
    )
    await db.execute(
        "INSERT INTO swe_chat_shares (token,chat_id,creator_id,tenant_id,snapshot_key,created_at,access_count,last_access_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            "token-a",
            chat_a,
            "alice",
            "alice",
            "alice/chat_shares/token-a.json",
            "2026-11-01 00:00:00",
            7,
            None,
        ),
    )
    shares = await db.fetch_all(
        "SELECT token,chat_id,creator_id,tenant_id,snapshot_key,created_at,access_count,last_access_at FROM swe_chat_shares",
    )
    for path in source.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    source.chmod(0o555)
    report = discover_services(
        [
            {
                "service_unit_id": "svc",
                "source_root": str(source),
                "config_root": str(source),
                "snapshot_root": str(source),
                "original_root": str(original),
                "nas_root": str(nas),
            },
        ],
        shares=shares,
        tenant_isolation=True,
    )
    assert report["ready"], report["blockers"]
    manifest = freeze_discovery(
        report,
        tmp_path / "manifest.json",
        SourceBoundary(
            "snapshot",
            "2026-11-13T00:00:00Z",
            "old@sha256:" + "a" * 64,
            ("stopped", "readonly"),
        ),
        db.config.database,
    )
    monkeypatch.setattr(
        "swe.database.config.get_database_config",
        lambda: db.config,
    )
    work = tmp_path / "work"
    work.mkdir()
    resources = {
        "workers": 2,
        "io_workers": 2,
        "memory_budget_mib": 128,
        "work_dir": str(work),
        "apply": True,
    }
    real_inventory = tenant_cli._inventory
    failing_b = True
    published_a = False
    parsed = []

    async def inventory(view, spool, options):
        parsed.append(view.key[1])
        if published_a and view.key[1] == "alice":
            raise AssertionError("published A frozen source was re-read")
        if failing_b and view.key[1] == "bob":
            raise tenant_cli.TenantFailure(
                "injected isolated semantic failure",
            )
        return await real_inventory(view, spool, options)

    monkeypatch.setattr(tenant_cli, "_inventory", inventory)
    imported = await tenant_cli.run_tenants(
        "import",
        manifest,
        None,
        **resources,
    )
    assert imported["failed"] == 1
    first = await tenant_cli.run_tenants(
        "finalize",
        manifest,
        tmp_path / "completion",
        **resources,
    )
    assert first["published"] == 1 and first["failed"] == 1
    from swe.app.conversation_storage.migration.exporter import OldFormatTarget
    from swe.app.conversation_storage.migration.tenant_rollback import (
        TenantRollbackEvidence,
        compose_tenant_rollback,
    )

    rollback = await compose_tenant_rollback(
        db,
        manifest,
        tmp_path / "rollback-review",
        OldFormatTarget("old@sha256:" + "a" * 64, 2, 1),
        TenantRollbackEvidence(
            SourceBoundary(
                "rollback-stop",
                "2026-11-13T00:00:00Z",
                "old@sha256:" + "a" * 64,
                ("stop", "drain"),
            ),
            "all-writers-stopped",
            "all-io-drained",
            "current-config-reviewed",
        ),
        current_configs=[
            {
                "service_unit_id": "svc",
                "tenant_id": "alice",
                "source_id": "SRC",
                "config_path": str(
                    original / encode_scope_id("alice", "SRC") / "config.json",
                ),
                "review_ref": "reviewed-current-config",
            },
        ],
    )
    assert rollback["passed"] and rollback["content_verified"]
    assert not rollback["rollback_ready"] and not rollback["image_verified"]
    from pathlib import Path

    rollback_root = Path(rollback["services"]["svc"])
    assert (
        json.loads((rollback_root / "shares.json").read_text())[0][
            "access_count"
        ]
        == 7
    )
    assert (
        rollback_root
        / encode_scope_id("bob", "SRC")
        / "workspaces"
        / "agent"
        / "chats.json"
    ).read_bytes() == (
        source
        / encode_scope_id("bob", "SRC")
        / "workspaces"
        / "agent"
        / "chats.json"
    ).read_bytes()
    published_a = True
    document = {
        "version": 3,
        "service_unit_id": "svc",
        "nas_root": str(nas),
        "migration_id": manifest.migration_id,
    }
    instances = [
        ConversationRuntime(db, document, b"cursor", service_unit_id="svc")
        for _ in range(2)
    ]
    for instance in instances:
        await instance.admit("alice")
        with pytest.raises(TenantNotPublished):
            await instance.admit("bob")
        storage = instance.for_workspace(
            SimpleNamespace(
                workspace_dir=runtime_a,
                agent_id="agent",
                conversation_scope=WorkspaceScope(
                    "alice",
                    "SRC",
                    "agent",
                    service_unit_id="svc",
                ),
            ),
        )
        assert storage.scope.tenant_id == "alice"

    # A's runtime shares the journal allocator. Advance well beyond the old
    # migration ticket head before B allocates its own entity tickets.
    from swe.app.conversation_storage.runtime_ids import runtime_identity
    from swe.app.conversation_storage.message_installs import (
        MessageInstall,
        install_messages,
    )
    from swe.app.conversation_storage.bundle import BundleIndex, BlockExtent

    existing = await db.fetch_one(
        "SELECT * FROM swe_conversation_chats WHERE tenant_key=%s",
        (b"alice",),
    )
    ticket_head = await db.fetch_one(
        "SELECT MAX(id) AS head FROM swe_conversation_migration_entries",
    )
    runtime_db = instances[0].database
    online_ids = []
    for ordinal in range(20):
        identity = await runtime_identity(runtime_db)
        assert identity > ticket_head["head"]
        online_ids.append(identity)
        new_chat = dict(existing)
        new_chat["id"] = identity
        new_chat["external_chat_key"] = f"created-online-{ordinal}".encode()
        new_chat["title"] = "new online conversation"
        columns = tuple(new_chat)
        await runtime_db.insert(
            "INSERT INTO swe_conversation_chats ("
            + ",".join(columns)
            + ") VALUES ("
            + ",".join(["%s"] * len(columns))
            + ")",
            tuple(new_chat.values()),
        )
    assert len(set(online_ids)) == 20
    assert min(online_ids) > ticket_head["head"]

    # Exercise the actual batched message installer (no monkeypatched SQL or
    # fake allocator). Reuse an existing immutable payload as two new retained
    # occurrences, each with its own runtime ticket and Session order.
    session = await db.fetch_one(
        "SELECT * FROM swe_conversation_sessions WHERE id=%s",
        (existing["session_id"],),
    )
    source_message = await db.fetch_one(
        "SELECT * FROM swe_conversation_messages WHERE session_id=%s ORDER BY id LIMIT 1",
        (session["id"],),
    )
    messages = [
        MessageInstall(
            f"online:{ordinal}",
            "memory",
            "user",
            source_msg_key=f"online:{ordinal}",
            context_epoch=session["context_epoch"],
        )
        for ordinal in range(2)
    ]
    index = BundleIndex(
        "existing",
        0,
        {},
        {
            message.block_name: BlockExtent(
                "message",
                source_message["byte_offset"],
                source_message["byte_length"],
            )
            for message in messages
        },
    )
    async with runtime_db.transaction():
        await install_messages(
            runtime_db,
            SimpleNamespace(
                scope=WorkspaceScope(
                    "alice",
                    "SRC",
                    "agent",
                    service_unit_id="svc",
                ),
                session_id=session["id"],
                chat_id=None,
                turn_id=None,
            ),
            messages,
            {"id": source_message["publication_id"]},
            index,
            (session, None, None),
        )
    online_messages = await db.fetch_all(
        "SELECT m.* FROM swe_conversation_messages m JOIN swe_conversation_migration_entries e ON e.id=m.id AND e.entry_kind='runtime_id' WHERE m.session_id=%s ORDER BY m.id",
        (session["id"],),
    )
    assert len(online_messages) == 2
    assert all(row["id"] > max(online_ids) for row in online_messages)
    await db.execute(
        "UPDATE swe_conversation_chats SET title=%s WHERE id=%s",
        ("online title", existing["id"]),
    )
    await db.execute(
        "UPDATE swe_chat_shares SET access_count=access_count+3,last_access_at=%s WHERE token=%s",
        ("2026-11-13 01:00:00", "token-a"),
    )
    before = await db.fetch_all(
        "SELECT * FROM swe_conversation_chats WHERE tenant_key=%s ORDER BY id",
        (b"alice",),
    )
    share_before = await db.fetch_one(
        "SELECT * FROM swe_chat_shares WHERE token=%s",
        ("token-a",),
    )
    failing_b = False
    parsed.clear()
    resumed = await tenant_cli.run_tenants(
        "resume",
        manifest,
        None,
        **resources,
    )
    assert resumed["failed"] == 0
    final = await tenant_cli.run_tenants(
        "finalize",
        manifest,
        tmp_path / "completion",
        **resources,
    )
    assert final["published"] == 2 and final["state"] == "partial_success"
    assert final["pending_schema_finalize"] is True
    assert parsed and set(parsed) == {"bob"}
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_tenant_publication_finalize.sql",
    )
    parsed.clear()
    final = await tenant_cli.run_tenants(
        "finalize",
        manifest,
        tmp_path / "completion",
        **resources,
    )
    assert final["state"] == "ready" and final["published"] == 2
    assert not parsed
    assert (
        await db.fetch_all(
            "SELECT * FROM swe_conversation_chats WHERE tenant_key=%s ORDER BY id",
            (b"alice",),
        )
        == before
    )
    assert (
        await db.fetch_one(
            "SELECT * FROM swe_chat_shares WHERE token=%s",
            ("token-a",),
        )
        == share_before
    )
    assert (
        await db.fetch_all(
            "SELECT m.* FROM swe_conversation_messages m JOIN swe_conversation_migration_entries e ON e.id=m.id AND e.entry_kind='runtime_id' WHERE m.session_id=%s ORDER BY m.id",
            (session["id"],),
        )
        == online_messages
    )
    for instance in instances:
        await instance.admit("bob")
        assert (
            instance.for_workspace(
                SimpleNamespace(
                    workspace_dir=runtime_b,
                    agent_id="agent",
                    conversation_scope=WorkspaceScope(
                        "bob",
                        "SRC",
                        "agent",
                        service_unit_id="svc",
                    ),
                ),
            ).scope.tenant_id
            == "bob"
        )

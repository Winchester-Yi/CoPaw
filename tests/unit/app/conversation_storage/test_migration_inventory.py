# -*- coding: utf-8 -*-
import json
from pathlib import Path
import uuid

import pytest

from swe.app.runner.models import ChatSpec, ChatsFile
from swe.app.runner.session import SafeJSONSession
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.migration.inventory import (
    WorkspaceMapping,
    inventory,
)
from swe.app.conversation_storage.migration.manifest import SourceBoundary


def boundary():
    return SourceBoundary(
        "snapshot",
        "2026-10-30T00:00:00Z",
        "old@sha256:" + "a" * 64,
        ("writers-exited.log", "readonly-mount.log"),
    )


def source(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    (root / "sessions").mkdir()
    (root / "dialog").mkdir()
    chat = ChatSpec(
        id=str(uuid.uuid4()),
        session_id="session_with_parts",
        user_id="user_name",
        channel="console",
        meta={"future": {"body": "retained"}},
    )
    (root / "chats.json").write_text(ChatsFile(chats=[chat]).model_dump_json())
    (root / "sessions" / "user_name_session_with_parts.json").write_text(
        json.dumps({"schema_version": 1, "future_module": {"x": True}}),
    )
    return root, chat


def make_readonly(root):
    for path in root.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def test_inventory_uses_trusted_mapping_and_forward_filename_only(tmp_path):
    root, chat = source(tmp_path)
    make_readonly(root)
    result = inventory(
        [WorkspaceMapping(WorkspaceScope("Tenant", "Source", "Agent"), root)],
        boundary(),
    )
    assert not result["blockers"]
    workspace = result["workspaces"][0]
    assert workspace["sessions"][0]["identity"] == [
        "Tenant",
        "Source",
        "Agent",
        "user_name",
        "console",
        "session_with_parts",
    ]
    assert workspace["sessions"][0]["legacy"]["state"]["future_module"] == {
        "x": True,
    }
    assert workspace["chats"][0]["spec"]["meta"] == chat.meta
    assert len(result["files"]) == 2


def test_inventory_refuses_writable_snapshot_and_unmapped_sessions(tmp_path):
    root, _ = source(tmp_path)
    with pytest.raises(ValueError, match="read.only"):
        inventory(
            [WorkspaceMapping(WorkspaceScope("t", "s", "a"), root)],
            boundary(),
        )
    (root / "sessions" / "orphan_with_underscores.json").write_text("{}")
    make_readonly(root)
    result = inventory(
        [WorkspaceMapping(WorkspaceScope("t", "s", "a"), root)],
        boundary(),
    )
    assert any(
        item["code"] == "unmapped_session" for item in result["blockers"]
    )


def test_inventory_reports_unknown_schema_and_filename_collisions(tmp_path):
    root, chat = source(tmp_path)
    other = chat.model_copy(
        update={
            "id": str(uuid.uuid4()),
            "session_id": "session_with_parts",
            "channel": "cron",
        },
    )
    (root / "chats.json").write_text(
        ChatsFile(chats=[chat, other]).model_dump_json(),
    )
    make_readonly(root)
    result = inventory(
        [WorkspaceMapping(WorkspaceScope("t", "s", "a"), root)],
        boundary(),
    )
    assert any(
        item["code"] == "identity_path_collision"
        for item in result["blockers"]
    )
    root.chmod(0o755)
    (root / "chats.json").chmod(0o644)
    (root / "chats.json").write_text(ChatsFile(chats=[chat]).model_dump_json())
    path = root / "sessions" / "user_name_session_with_parts.json"
    path.chmod(0o644)
    path.write_text('{"schema_version":99}')
    make_readonly(root)
    result = inventory(
        [WorkspaceMapping(WorkspaceScope("t", "s", "a"), root)],
        boundary(),
    )
    assert any(
        item["code"] == "session_invalid" for item in result["blockers"]
    )


def test_inventory_rejects_symlink_and_records_orphan_share(tmp_path):
    root, chat = source(tmp_path)
    (root / "share.json").write_text(
        json.dumps({"messages": [], "future": {"x": 1}}),
    )
    share = {
        "token": "token",
        "chat_id": "deleted",
        "creator_id": "u",
        "tenant_id": "t",
        "snapshot_key": "old.json",
        "created_at": "2026-10-30T00:00:00Z",
        "access_count": 4,
        "last_access_at": None,
        "snapshot_path": "share.json",
        "snapshot_workspace_index": 0,
    }
    make_readonly(root)
    result = inventory(
        [WorkspaceMapping(WorkspaceScope("t", "s", "a"), root)],
        boundary(),
        shares=[share],
    )
    assert result["shares"][0]["scope_kind"] == "legacy_share"
    assert result["shares"][0]["record"]["access_count"] == 4
    assert result["shares"][0]["snapshot"]["future"] == {"x": 1}
    root.chmod(0o755)
    (root / "escape").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        inventory(
            [WorkspaceMapping(WorkspaceScope("t", "s", "a"), root)],
            boundary(),
        )


def test_explicit_paths_disambiguate_full_session_identity(tmp_path):
    root, chat = source(tmp_path)
    other = chat.model_copy(
        update={"id": str(uuid.uuid4()), "channel": "cron"},
    )
    (root / "chats.json").write_text(
        ChatsFile(chats=[chat, other]).model_dump_json(),
    )
    (root / "sessions" / "cron-state.json").write_text(
        json.dumps({"schema_version": 2, "future": "cron"}),
    )
    make_readonly(root)
    mapping = WorkspaceMapping(
        WorkspaceScope("t", "s", "a"),
        root,
        session_paths={
            (
                "user_name",
                "cron",
                "session_with_parts",
            ): "sessions/cron-state.json",
        },
    )
    result = inventory([mapping], boundary())
    assert result["ready"]
    assert len(result["workspaces"][0]["sessions"]) == 2


def test_inventory_refuses_unowned_ledger_generation_and_quarantine(tmp_path):
    root, _ = source(tmp_path)
    directory = root / "sessions" / ".turn-ledger" / str(uuid.uuid4())
    directory.mkdir(parents=True)
    (directory / "metadata.json").write_text(
        json.dumps({"session_file": "unknown.json", "version": 1}),
    )
    (root / "dialog" / "journal-v2-quarantine.json").write_text("{}")
    make_readonly(root)
    result = inventory(
        [WorkspaceMapping(WorkspaceScope("t", "s", "a"), root)],
        boundary(),
    )
    assert {"unmapped_ledger_generation", "source_quarantined"} <= {
        item["code"] for item in result["blockers"]
    }


def test_share_invalid_snapshot_and_duplicate_token_are_blockers(tmp_path):
    root, _ = source(tmp_path)
    (root / "share.json").write_text(json.dumps({"messages": "corrupted"}))
    share = {
        "token": "token",
        "chat_id": "deleted",
        "creator_id": "u",
        "tenant_id": "t",
        "snapshot_key": "old.json",
        "created_at": "2026-10-30T00:00:00Z",
        "access_count": 4,
        "last_access_at": None,
        "snapshot_path": "share.json",
        "snapshot_workspace_index": 0,
    }
    make_readonly(root)
    mapping = WorkspaceMapping(WorkspaceScope("t", "s", "a"), root)
    assert not inventory([mapping], boundary(), shares=[share])["ready"]
    (root / "share.json").chmod(0o644)
    (root / "share.json").write_text(json.dumps({"messages": []}))
    make_readonly(root)
    result = inventory([mapping], boundary(), shares=[share, share])
    assert not result["ready"]
    assert any("duplicate" in item["detail"] for item in result["blockers"])


def test_explicit_session_mapping_can_close_unregistered_session_identity(
    tmp_path,
):
    root, _ = source(tmp_path)
    (root / "sessions" / "orphan_raw_name.json").write_text(
        json.dumps({"schema_version": 2, "module": {"retained": True}}),
    )
    make_readonly(root)
    mapping = WorkspaceMapping(
        WorkspaceScope("t", "s", "a"),
        root,
        session_paths={
            (
                "other_user",
                "cron",
                "explicit_session",
            ): "sessions/orphan_raw_name.json",
        },
    )
    result = inventory([mapping], boundary())
    assert result["ready"]
    extra = next(
        item
        for item in result["workspaces"][0]["sessions"]
        if item["identity"][3] == "other_user"
    )
    assert extra["chat_positions"] == []
    assert extra["legacy"]["state"]["module"] == {"retained": True}


def test_retired_generation_without_committed_root_blocks_import(tmp_path):
    root, _ = source(tmp_path)
    directory = root / "sessions" / ".turn-ledger" / str(uuid.uuid4())
    directory.mkdir(parents=True)
    (directory / "metadata.json").write_text(
        json.dumps(
            {
                "session_file": "user_name_session_with_parts.json",
                "version": 1,
            },
        ),
    )
    make_readonly(root)
    result = inventory(
        [WorkspaceMapping(WorkspaceScope("t", "s", "a"), root)],
        boundary(),
    )
    assert any(
        item["code"] == "retired_generation_root_missing"
        for item in result["blockers"]
    )


@pytest.mark.parametrize(
    "relative",
    [
        ".turn-ledger/unmapped-generation/turns/orphan.json",
        ".turn-ledger/stray.json",
    ],
)
def test_ledger_paths_without_generation_metadata_are_blockers(
    tmp_path,
    relative,
):
    root, _ = source(tmp_path)
    path = root / "sessions" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    make_readonly(root)
    result = inventory(
        [WorkspaceMapping(WorkspaceScope("t", "s", "a"), root)],
        boundary(),
    )
    assert not result["ready"]
    assert any(
        item["code"] == "unmapped_ledger_generation"
        for item in result["blockers"]
    )

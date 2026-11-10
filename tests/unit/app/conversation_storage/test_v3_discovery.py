# -*- coding: utf-8 -*-
import json

import pytest

from swe.app.conversation_storage.migration.manifest import SourceBoundary


def fixture_service(tmp_path):
    source = tmp_path / "source"
    source.mkdir(parents=True)
    tenant = source / "default_SRC"
    workspace = tenant / "workspaces" / "agent"
    workspace.mkdir(parents=True)
    nas = tmp_path / "nas"
    (nas / "default_SRC" / "workspaces" / "agent").mkdir(parents=True)
    (tenant / "config.json").write_text(
        json.dumps(
            {
                "agents": {
                    "profiles": {
                        "agent": {
                            "id": "agent",
                            "workspace_dir": str(
                                nas / "default_SRC" / "workspaces" / "agent",
                            ),
                            "enabled": False,
                        },
                    },
                },
            },
        ),
    )
    (workspace / "agent.json").write_text('{"id": "agent"}')
    (workspace / "chats.json").write_text('{"version": 1, "chats": []}')
    for path in source.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    source.chmod(0o555)
    return {
        "service_unit_id": "SERVICE",
        "source_root": str(source),
        "config_root": str(source),
        "original_root": str(nas),
        "nas_root": str(nas),
    }


def test_discovers_disabled_profile_and_rebases_absolute_path(tmp_path):
    from swe.app.conversation_storage.migration.discovery import (
        discover_services,
    )

    service = fixture_service(tmp_path)
    report = discover_services([service])
    assert report["ready"], report["blockers"]
    assert len(report["workspaces"]) == 1
    item = report["workspaces"][0]
    assert (item["tenant_id"], item["source_id"], item["agent_id"]) == (
        "default",
        "SRC",
        "agent",
    )
    assert item["root"].startswith(service["source_root"])
    assert item["runtime_root"].startswith(service["nas_root"])


def test_new_discovery_avoids_fingerprints_and_keeps_service_authority(
    tmp_path,
    monkeypatch,
):
    from swe.app.conversation_storage.migration import discovery
    from swe.app.conversation_storage.migration.discovery import (
        discover_services,
        freeze_discovery,
    )
    from swe.app.conversation_storage.migration.inventory_v3 import (
        inventory_v3,
    )

    service = fixture_service(tmp_path)
    report = discover_services([service])
    fingerprint_calls = []
    original_fingerprints = discovery.fingerprints

    def count_fingerprints(services):
        fingerprint_calls.append(True)
        return original_fingerprints(services)

    monkeypatch.setattr(discovery, "fingerprints", count_fingerprints)
    boundary = SourceBoundary(
        "snapshot",
        "2026-11-02T00:00:00Z",
        "old@sha256:" + "a" * 64,
        ("stop", "readonly"),
    )
    manifest = freeze_discovery(
        report,
        tmp_path / "manifest.json",
        boundary,
        "business",
    )
    assert fingerprint_calls == []
    assert manifest.data["version"] == 3
    assert inventory_v3(manifest)["ready"]
    assert "tenant_sync_started_at_ns" not in manifest.data
    assert inventory_v3(manifest, begin_sync=True)["ready"]
    registry = (
        __import__("pathlib").Path(report["workspaces"][0]["root"])
        / "chats.json"
    )
    cutoff = manifest.data["tenant_sync_started_at_ns"]["SERVICE\x00default"]
    registry.chmod(0o644)
    registry.write_text("{changed")
    import os

    os.utime(registry, ns=(cutoff + 1, cutoff + 1))
    registry.chmod(0o444)
    result = inventory_v3(manifest)
    assert not result["ready"]
    blocker = result["blockers"][0]
    assert blocker["code"] == "source_modified_after_sync_started"
    assert blocker["scope"] == ["SERVICE", "default"]
    assert blocker["path"] == str(registry)
    assert blocker["mtime_ns"] > blocker["cutoff_ns"]
    assert manifest.data["source_change_blockers"] == [blocker]


def test_unregistered_workspace_is_blocker(tmp_path):
    from swe.app.conversation_storage.migration.discovery import (
        discover_services,
    )

    service = fixture_service(tmp_path)
    root = __import__("pathlib").Path(service["source_root"])
    root.chmod(0o755)
    extra = root / "orphan"
    extra.mkdir()
    (extra / "chats.json").write_text('{"chats": []}')
    (extra / "chats.json").chmod(0o444)
    extra.chmod(0o555)
    root.chmod(0o555)
    report = discover_services([service])
    assert not report["ready"]
    assert any(
        item["code"] == "unregistered_workspace" for item in report["blockers"]
    )


def test_service_without_workspaces_is_retained_for_share_only_job(tmp_path):
    from swe.app.conversation_storage.migration.discovery import (
        discover_services,
    )

    root = tmp_path / "frozen"
    root.mkdir()
    nas = tmp_path / "nas"
    nas.mkdir()
    root.chmod(0o555)
    service = {
        "service_unit_id": "SERVICE",
        "source_root": str(root),
        "config_root": str(root),
        "original_root": str(nas),
        "nas_root": str(nas),
    }
    report = discover_services([service])
    assert report["ready"], report["blockers"]
    assert report["services"][0]["service_unit_id"] == "SERVICE"
    assert report["workspaces"] == []


def test_rejects_another_services_source_overlapping_target_nas(tmp_path):
    from swe.app.conversation_storage.migration.discovery import (
        discover_services,
    )

    first = fixture_service(tmp_path / "one")
    second_root = tmp_path / "two" / "source"
    second_root.mkdir(parents=True)
    first["nas_root"] = str(second_root)
    # Target NAS is a parent of another service's frozen source.
    second_nas = tmp_path / "two" / "nas"
    second_nas.mkdir()
    second = {
        "service_unit_id": "SERVICE2",
        "source_root": str(second_root),
        "config_root": str(second_root),
        "original_root": str(second_nas),
        "nas_root": str(second_nas),
    }
    report = discover_services([first, second])
    assert any(
        item["code"] == "service_invalid" and "overlap" in item["detail"]
        for item in report["blockers"]
    )


def test_source_change_blocker_survives_manifest_reload(tmp_path):
    from pathlib import Path
    from swe.app.conversation_storage.migration.discovery import (
        discover_services,
        freeze_discovery,
    )

    service = fixture_service(tmp_path)
    report = discover_services([service])
    boundary = SourceBoundary(
        "snapshot",
        "2026-11-02T00:00:00Z",
        "old@sha256:" + "a" * 64,
        ("stop", "readonly"),
    )
    manifest_path = tmp_path / "manifest.json"
    manifest = freeze_discovery(report, manifest_path, boundary, "business")
    from swe.app.conversation_storage.migration.freshness import tenant_key

    cutoff = manifest.begin_tenant_sync("SERVICE", "default")
    blocker = {
        "code": "source_modified_after_sync_started",
        "position": report["workspaces"][0]["position"],
        "scope": ["SERVICE", "default"],
        "path": str(Path(service["source_root"])),
        "mtime_ns": cutoff + 1,
        "cutoff_ns": cutoff,
    }
    manifest.record_source_change(blocker)
    from swe.app.conversation_storage.migration.v3_manifest import V3Manifest

    loaded = V3Manifest.load(manifest_path)
    assert (
        loaded.data["tenant_sync_started_at_ns"][
            tenant_key("SERVICE", "default")
        ]
        == cutoff
    )
    assert loaded.data["source_change_blockers"] == [blocker]
    loaded.data["assignments"]["publication"] = {
        "legacy-ticket": {
            "target_id": 1,
            "operation_key": "a" * 64,
            "relative_path": None,
        },
    }
    loaded._persist()
    resumed = V3Manifest.load(manifest_path)
    assert (
        resumed.data["assignments"]["publication"]["legacy-ticket"][
            "relative_path"
        ]
        is None
    )


def test_freeze_rejects_modified_discovery_identity(tmp_path):
    from swe.app.conversation_storage.migration.discovery import (
        discover_services,
        freeze_discovery,
    )

    report = discover_services([fixture_service(tmp_path)])
    report["workspaces"][0]["tenant_id"] = "wrong"
    boundary = SourceBoundary(
        "snapshot",
        "2026-11-02T00:00:00Z",
        "old@sha256:" + "a" * 64,
        ("stop", "readonly"),
    )
    with pytest.raises(ValueError, match="discovery"):
        freeze_discovery(
            report,
            tmp_path / "manifest.json",
            boundary,
            "business",
        )


def test_bound_override_resolves_only_unregistered_candidate(tmp_path):
    from pathlib import Path
    from swe.app.conversation_storage.migration.discovery import (
        discover_services,
        source_key,
    )

    service = fixture_service(tmp_path)
    root = Path(service["source_root"])
    root.chmod(0o755)
    extra = root / "orphan"
    extra.mkdir()
    (extra / "agent.json").write_text('{"id": "retired"}')
    (extra / "chats.json").write_text('{"version": 1, "chats": []}')
    for path in extra.iterdir():
        path.chmod(0o444)
    extra.chmod(0o555)
    root.chmod(0o555)
    override = {
        "position": source_key("workspace", "SERVICE", "orphan"),
        "discovery_version": 1,
        "evidence_refs": ["historical-registration"],
        "reason": "retired profile removed",
        "tenant_id": "default",
        "source_id": "SRC",
        "agent_id": "retired",
    }
    report = discover_services([service], overrides=[override])
    assert report["ready"], report["blockers"]
    assert len(report["workspaces"]) == 2


def test_empty_workspace_without_registry_is_empty_inventory(tmp_path):
    from pathlib import Path
    from swe.app.conversation_storage.migration.discovery import (
        discover_services,
        freeze_discovery,
    )
    from swe.app.conversation_storage.migration.inventory_v3 import (
        inventory_v3,
    )

    service = fixture_service(tmp_path)
    workspace = (
        Path(service["source_root"]) / "default_SRC" / "workspaces" / "agent"
    )
    workspace.chmod(0o755)
    (workspace / "chats.json").unlink()
    workspace.chmod(0o555)
    boundary = SourceBoundary(
        "snapshot",
        "2026-11-02T00:00:00Z",
        "old@sha256:" + "a" * 64,
        ("stop", "readonly"),
    )
    report = discover_services([service])
    manifest = freeze_discovery(
        report,
        tmp_path / "manifest.json",
        boundary,
        "business",
    )
    result = inventory_v3(manifest)
    assert result["ready"], result["blockers"]
    assert result["workspaces"][0]["sessions"] == []


def test_discovery_rejects_registry_that_would_generate_chat_identity(
    tmp_path,
):
    from pathlib import Path
    from swe.app.conversation_storage.migration.discovery import (
        discover_services,
    )

    service = fixture_service(tmp_path)
    registry = (
        Path(service["source_root"])
        / "default_SRC"
        / "workspaces"
        / "agent"
        / "chats.json"
    )
    registry.chmod(0o644)
    registry.write_text(
        json.dumps(
            {"version": 1, "chats": [{"session_id": "sid", "user_id": "u"}]},
        ),
    )
    registry.chmod(0o444)
    report = discover_services([service])
    assert not report["ready"]
    assert any(
        item["code"] == "registry_invalid" for item in report["blockers"]
    )


def test_share_only_service_snapshot_lives_outside_workspace(tmp_path):
    from pathlib import Path
    from swe.app.conversation_storage.migration.discovery import (
        discover_services,
        freeze_discovery,
    )
    from swe.app.conversation_storage.migration.inventory_v3 import (
        inventory_v3,
    )

    source = tmp_path / "frozen"
    source.mkdir()
    snapshot = tmp_path / "snapshots"
    target = snapshot / "tenant" / "chat_shares" / "token.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"chat_name": "old share", "messages": []}')
    nas = tmp_path / "nas"
    nas.mkdir()
    for path in [source, snapshot, *snapshot.rglob("*")]:
        path.chmod(0o555 if path.is_dir() else 0o444)
    service = {
        "service_unit_id": "SERVICE",
        "source_root": str(source),
        "config_root": str(source),
        "nas_root": str(nas),
        "snapshot_root": str(snapshot),
    }
    share = {
        "token": "token",
        "chat_id": "deleted-chat",
        "creator_id": "u",
        "tenant_id": "tenant",
        "snapshot_key": "tenant/chat_shares/token.json",
        "created_at": "2026-11-01T00:00:00+00:00",
        "access_count": 4,
        "last_access_at": None,
    }
    report = discover_services([service], shares=[share])
    assert report["ready"], report["blockers"]
    boundary = SourceBoundary(
        "snapshot",
        "2026-11-02T00:00:00Z",
        "old@sha256:" + "a" * 64,
        ("stop", "readonly"),
    )
    manifest = freeze_discovery(
        report,
        tmp_path / "manifest.json",
        boundary,
        "business",
    )
    result = inventory_v3(manifest, begin_sync=True)
    assert result["ready"], result["blockers"]
    assert result["shares"][0]["scope_kind"] == "legacy_share"
    assert result["shares"][0]["record"]["access_count"] == 4
    cutoff = manifest.data["tenant_sync_started_at_ns"]["SERVICE\x00tenant"]
    import os

    os.utime(target, ns=(cutoff + 1, cutoff + 1))
    result = inventory_v3(manifest)
    assert not result["ready"]
    assert result["blockers"][0]["scope"] == ["SERVICE", "tenant"]
    assert result["blockers"][0]["path"] == str(target)


def test_relative_profile_requires_original_process_directory(tmp_path):
    from pathlib import Path
    from swe.app.conversation_storage.migration.discovery import (
        discover_services,
    )

    service = fixture_service(tmp_path)
    config = Path(service["source_root"]) / "default_SRC" / "config.json"
    value = json.loads(config.read_text())
    value["agents"]["profiles"]["agent"]["workspace_dir"] = "workspaces/agent"
    config.chmod(0o644)
    config.write_text(json.dumps(value))
    config.chmod(0o444)
    report = discover_services([service])
    assert not report["ready"]
    service["original_cwd"] = str(
        Path(service["original_root"]) / "default_SRC",
    )
    report = discover_services([service])
    assert report["ready"], report["blockers"]


def test_invalid_registry_json_is_reported_as_blocker(tmp_path):
    from pathlib import Path
    from swe.app.conversation_storage.migration.discovery import (
        discover_services,
    )

    service = fixture_service(tmp_path)
    registry = (
        Path(service["source_root"])
        / "default_SRC"
        / "workspaces"
        / "agent"
        / "chats.json"
    )
    registry.chmod(0o644)
    registry.write_text("{broken")
    registry.chmod(0o444)
    report = discover_services([service])
    assert not report["ready"]
    assert any(
        item["code"] == "registry_invalid" for item in report["blockers"]
    )


def test_registered_workspace_accepts_evidence_bound_orphan_session_mapping(
    tmp_path,
):
    from pathlib import Path
    from swe.app.conversation_storage.migration.discovery import (
        discover_services,
        source_key,
    )

    service = fixture_service(tmp_path)
    workspace = (
        Path(service["source_root"]) / "default_SRC" / "workspaces" / "agent"
    )
    workspace.chmod(0o755)
    sessions = workspace / "sessions"
    sessions.mkdir()
    (sessions / "legacy-name.json").write_text(
        '{"schema_version": 1, "future_module": {"value": 7}}',
    )
    (sessions / "legacy-name.json").chmod(0o444)
    sessions.chmod(0o555)
    workspace.chmod(0o555)
    override = {
        "position": source_key(
            "workspace",
            "SERVICE",
            "default_SRC/workspaces/agent",
        ),
        "discovery_version": 1,
        "evidence_refs": ["historical-session-identity"],
        "reason": "non-reversible legacy filename",
        "tenant_id": "default",
        "source_id": "SRC",
        "agent_id": "agent",
        "session_paths": [
            {
                "user_id": "u",
                "channel": "console",
                "session_id": "session",
                "path": "sessions/legacy-name.json",
            },
        ],
    }
    report = discover_services([service], overrides=[override])
    assert report["ready"], report["blockers"]
    assert report["workspaces"][0]["counts"]["sessions"] == 1
    override["tenant_id"] = "different"
    result = discover_services([service], overrides=[override])
    assert not result["ready"]
    assert any(
        item["code"] == "override_invalid" for item in result["blockers"]
    )


def test_share_access_count_requires_original_integer(tmp_path):
    from swe.app.conversation_storage.migration.discovery import (
        discover_services,
    )

    service = fixture_service(tmp_path)
    share = {
        "token": "token",
        "chat_id": "chat",
        "creator_id": "u",
        "tenant_id": "tenant",
        "snapshot_key": "tenant/chat_shares/token.json",
        "created_at": "2026-11-01T00:00:00+00:00",
        "access_count": "4",
        "last_access_at": None,
    }
    report = discover_services([service], shares=[share])
    assert not report["ready"]
    assert any("access count" in item["detail"] for item in report["blockers"])


def test_symlink_config_is_rejected_before_any_metadata_read(
    tmp_path,
    monkeypatch,
):
    from pathlib import Path
    from swe.app.conversation_storage.migration import discovery

    service = fixture_service(tmp_path)
    config = Path(service["source_root"]) / "default_SRC" / "config.json"
    config.parent.chmod(0o755)
    config.unlink()
    active = tmp_path / "active-config.json"
    active.write_text("{}")
    config.symlink_to(active)
    config.parent.chmod(0o555)

    def forbidden(_path):
        pytest.fail("unsafe source metadata was read before tree validation")

    monkeypatch.setattr(discovery, "_read_json", forbidden)
    report = discovery.discover_services([service])
    assert not report["ready"]
    assert any(item["code"] == "source_invalid" for item in report["blockers"])

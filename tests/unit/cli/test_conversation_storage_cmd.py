# -*- coding: utf-8 -*-
"""Offline commands default to read-only previews and revalidate their input."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from swe.app.conversation_storage.migration.manifest import (
    MigrationManifest,
    SourceBoundary,
)
from swe.cli.main import cli


@pytest.fixture
def migration(tmp_path):
    source = tmp_path / "frozen"
    source.mkdir()
    (source / "chats.json").write_text('{"chats": []}')
    (source / "chats.json").chmod(0o444)
    source.chmod(0o555)
    nas = tmp_path / "original-nas"
    nas.mkdir()
    boundary = SourceBoundary(
        "final-snapshot",
        "2026-10-30T00:00:00Z",
        "old@sha256:" + "a" * 64,
        ("writers-stopped", "read-only-mount"),
    )
    manifest = MigrationManifest.create(
        tmp_path / "manifest.json",
        boundary,
        [
            {
                "root": str(source),
                "service_unit_id": "schema-test-service",
                "tenant_id": "tenant",
                "source_id": "source",
                "agent_id": "agent",
            },
        ],
    )
    manifest.data["offline"] = {
        "application_schema": "business",
        "nas_roots": {"schema-test-service": str(nas)},
    }
    manifest._persist()
    return manifest


def invoke(manifest, command, *options):
    return CliRunner().invoke(
        cli,
        [
            "conversation-storage",
            command,
            "--manifest",
            str(manifest.path),
            *options,
        ],
    )


@pytest.mark.parametrize(
    "command",
    ["inventory", "plan", "import", "install", "inspect", "export"],
)
def test_preview_does_not_write_or_connect(migration, monkeypatch, command):
    from swe.database.connection import DatabaseConnection

    async def forbidden(*_args):
        pytest.fail("preview opened a database connection")

    monkeypatch.setattr(DatabaseConnection, "connect", forbidden)
    before = {
        str(p): p.read_bytes()
        for p in migration.path.parent.rglob("*")
        if p.is_file()
    }
    result = invoke(migration, command)
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["preview"] is True
    assert before == {
        str(p): p.read_bytes()
        for p in migration.path.parent.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("staging_schema", "business", "generated staging"),
        (
            "nas_roots",
            {"schema-test-service": "/nonexistent-original-root"},
            "existing",
        ),
    ],
)
def test_untrusted_targets_rejected(migration, field, value, expected):
    migration.data["offline"][field] = value
    migration._persist()
    result = invoke(migration, "plan")
    assert result.exit_code != 0
    assert expected in result.output


def test_writable_source_rejected(migration):
    Path(migration.data["workspaces"][0]["root"]).chmod(0o755)
    result = invoke(migration, "import", "--apply")
    assert result.exit_code != 0
    assert "read-only" in result.output


def test_unpinned_old_image_rejected(migration):
    migration.data["source_boundary"]["old_image_digest"] = "old:latest"
    migration._persist()
    result = invoke(migration, "install", "--apply")
    assert result.exit_code != 0
    assert "immutable image digest" in result.output


def test_export_nonempty_destination_rejected_before_writing(migration):
    destination = migration.path.parent / "export"
    destination.mkdir()
    (destination / "keep").write_text("keep")
    migration.data["offline"]["export"] = {"destination": str(destination)}
    migration._persist()
    result = invoke(migration, "export", "--apply")
    assert result.exit_code != 0
    assert "empty" in result.output
    assert (destination / "keep").read_text() == "keep"


def test_readonly_verification_cannot_update_manifest(migration):
    from swe.cli.conversation_storage_cmd import _readonly_manifest

    before = migration.path.read_bytes()
    readonly = _readonly_manifest(migration)
    readonly.set_batch_state("source-artifact:0/chats.json", "verified")
    assert migration.path.read_bytes() == before
    assert migration.data["batches"] == {}


@pytest.mark.parametrize(
    "command",
    [
        ["conversation-storage", "import"],
        ["conversation-storage", "resume"],
    ],
)
def test_parallel_commands_expose_bounded_worker_options(command):
    result = CliRunner().invoke(cli, [*command, "--help"])
    assert result.exit_code == 0, result.output
    assert "--workers INTEGER RANGE" in result.output
    assert "--io-workers INTEGER RANGE" in result.output


def test_missing_fixed_source_boundary_rejected(migration):
    del migration.data["source_boundary"]["snapshot_id"]
    migration._persist()
    result = invoke(migration, "import", "--apply")
    assert result.exit_code != 0
    assert "snapshot_id" in result.output


def test_recovery_preview_requires_exact_ended_owner_manifest_and_never_connects(
    tmp_path,
    monkeypatch,
):
    from swe.database.connection import DatabaseConnection

    async def forbidden(*args):
        pytest.fail("recovery preview connected")

    monkeypatch.setattr(DatabaseConnection, "connect", forbidden)
    nas = tmp_path / "nas"
    nas.mkdir()
    evidence = tmp_path / "owner-exit.json"
    evidence.write_text(
        json.dumps(
            {
                "version": 2,
                "schema": "business",
                "nas_root": str(nas),
                "service_unit_id": "schema-test-service",
                "scope": {
                    "service_unit_id": "schema-test-service",
                    "tenant_id": "tenant",
                    "source_id": "source",
                    "agent_id": "agent",
                },
                "session": {
                    "user_id": "alice",
                    "channel": "console",
                    "session_id": "session",
                },
                "turn_id": 7,
                "owner": {
                    "owner_key": "original-owner-incarnation",
                    "generation": 3,
                    "evidence_ref": "audit/pod-uid-exit-and-drain",
                    "operator": "alice-ops",
                    "owner_stopped": True,
                    "io_drained": True,
                },
            },
        ),
    )
    result = CliRunner().invoke(
        cli,
        ["conversation-storage", "recover", "--evidence", str(evidence)],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["preview"] is True
    result = CliRunner().invoke(
        cli,
        [
            "conversation-storage",
            "recover",
            "--evidence",
            str(evidence),
            "--apply",
        ],
    )
    assert result.exit_code != 0
    assert "confirm-ended-owner" in result.output


def test_multiservice_export_validates_all_ownership_before_output(
    migration,
    monkeypatch,
):
    from swe.cli.conversation_storage_cmd import OfflineJob
    from swe.app.conversation_storage.migration import exporter

    second_nas = migration.path.parent / "second-nas"
    second_nas.mkdir()
    migration.data["workspaces"].append(
        {
            **migration.data["workspaces"][0],
            "service_unit_id": "second-service",
        },
    )
    migration.data["offline"]["nas_roots"]["second-service"] = str(second_nas)
    migration.data["offline"]["export"] = {
        "session_version": 1,
        "checkpoint_version": 1,
        "services": {
            service: {
                "backup_path": service,
                "destination": str(
                    migration.path.parent / (service + "-export"),
                ),
            }
            for service in ("schema-test-service", "second-service")
        },
    }
    migration._persist()
    writes = []

    class Exporter:
        def __init__(self, _path, _target, *, service_unit_id):
            if service_unit_id == "second-service":
                raise ValueError("backup service differs")

        def export(self, destination):
            writes.append(destination)

    monkeypatch.setattr(exporter, "ConversationExporter", Exporter)
    with pytest.raises(ValueError, match="service differs"):
        OfflineJob(migration.path).export()
    assert not writes


@pytest.mark.parametrize("command", ["import", "install"])
def test_legacy_apply_is_denied_before_database_access(
    migration,
    monkeypatch,
    command,
):
    from swe.database.connection import DatabaseConnection

    async def forbidden(*args):
        pytest.fail("legacy write opened database")

    monkeypatch.setattr(DatabaseConnection, "connect", forbidden)
    result = invoke(migration, command, "--apply")
    assert result.exit_code != 0
    assert "legacy v2" in result.output


@pytest.mark.parametrize("marked", [True, False])
def test_offline_factory_manifest_routes_to_offline_preview(migration, marked):
    assert migration.data["version"] == 3
    if marked:
        assert migration.data["workflow"] == "offline"
    else:
        migration.data.pop("workflow", None)
        migration._persist()
    result = invoke(migration, "plan")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["staging_schema"].startswith(
        "conversation_staging_",
    )


@pytest.mark.parametrize(
    "workflow,extra,expected",
    [
        ("unknown", {}, "unsupported migration workflow"),
        ("offline", {"discovery_version": 1}, "conflicts"),
        ("direct", {}, "services"),
    ],
)
def test_invalid_workflow_is_rejected_before_database_access(
    migration,
    monkeypatch,
    workflow,
    extra,
    expected,
):
    from swe.database.connection import DatabaseConnection

    async def forbidden(*_args):
        pytest.fail("invalid workflow connected to database")

    monkeypatch.setattr(DatabaseConnection, "connect", forbidden)
    data = dict(migration.data, workflow=workflow, **extra)
    migration.path.write_text(json.dumps(data))
    result = invoke(migration, "import", "--apply")
    assert result.exit_code != 0
    assert expected in result.output
    assert "Error: 'services'" not in result.output

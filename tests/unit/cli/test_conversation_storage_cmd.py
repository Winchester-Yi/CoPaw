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
                "tenant_id": "tenant",
                "source_id": "source",
                "agent_id": "agent",
            },
        ],
    )
    manifest.data["offline"] = {
        "application_schema": "business",
        "nas_root": str(nas),
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
        ("nas_root", "/nonexistent-original-root", "existing"),
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


def test_missing_fixed_source_boundary_rejected(migration):
    del migration.data["source_boundary"]["snapshot_id"]
    migration._persist()
    result = invoke(migration, "import", "--apply")
    assert result.exit_code != 0
    assert "snapshot_id" in result.output

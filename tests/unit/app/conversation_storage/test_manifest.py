# -*- coding: utf-8 -*-
import json

import pytest

from swe.app.conversation_storage.migration.manifest import (
    MigrationManifest,
    SourceBoundary,
)


def boundary():
    return SourceBoundary(
        "snapshot-1",
        "2026-10-30T00:00:00Z",
        "registry/image@sha256:" + "a" * 64,
        ("writer-exit-report", "snapshot-readonly-report"),
    )


def test_manifest_allocates_and_persists_identity_before_return(tmp_path):
    path = tmp_path / "migration.json"
    manifest = MigrationManifest.create(
        path,
        boundary(),
        [
            {
                "service_unit_id": "schema-test-service",
                "tenant_id": "T",
                "source_id": "S",
                "agent_id": "A",
                "root": "/snapshot/workspace",
            },
        ],
    )
    first = manifest.allocate(
        "session",
        "service:schema-test-service/workspace:0/session:0",
    )
    persisted = json.loads(path.read_text())
    assert (
        persisted["assignments"]["session"][
            "service:schema-test-service/workspace:0/session:0"
        ]
        == first
    )
    assert first["target_id"] == 1
    assert first["relative_path"].endswith(first["operation_key"] + ".bundle")
    again = MigrationManifest.load(path)
    assert again.migration_id == manifest.migration_id
    assert (
        again.allocate(
            "session",
            "service:schema-test-service/workspace:0/session:0",
        )
        == first
    )
    assert (
        again.allocate(
            "session",
            "service:schema-test-service/workspace:0/session:1",
        )["target_id"]
        == 2
    )
    assert (
        "checksum" not in path.read_text()
        and "fingerprint" not in path.read_text()
    )


def test_manifest_freezes_boundary_and_prevents_batch_regression(tmp_path):
    manifest = MigrationManifest.create(
        tmp_path / "migration.json",
        boundary(),
        [],
    )
    manifest.allocate("session", "session:0")
    manifest.set_batch_state("session:0", "importing")
    manifest.set_batch_state("session:0", "committed")
    with pytest.raises(ValueError, match="transition"):
        manifest.set_batch_state("session:0", "planned")
    assert (
        MigrationManifest.load(manifest.path).data["batches"]["session:0"]
        == "committed"
    )


def test_manifest_rejects_unfixed_boundary_and_tampered_mapping(tmp_path):
    with pytest.raises(ValueError, match="image"):
        SourceBoundary("s", "2026-10-30T00:00:00Z", "latest", ("proof",))
    manifest = MigrationManifest.create(
        tmp_path / "migration.json",
        boundary(),
        [],
    )
    manifest.allocate("session", "s")
    value = json.loads(manifest.path.read_text())
    value["assignments"]["session"]["s"]["relative_path"] = "../escape"
    manifest.path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="path"):
        MigrationManifest.load(manifest.path)


def test_concurrent_manifest_instances_reuse_original_allocations(tmp_path):
    original = MigrationManifest.create(
        tmp_path / "migration.json",
        boundary(),
        [],
    )
    second = MigrationManifest.load(original.path)
    first = original.allocate("session", "same")
    assert second.allocate("session", "same") == first
    assert second.allocate("session", "other")["target_id"] == 2
    assert original.allocate("session", "last")["target_id"] == 3


def test_manifest_rejects_changed_workspace_mapping_on_reentry(tmp_path):
    manifest = MigrationManifest.create(
        tmp_path / "migration.json",
        boundary(),
        [
            {
                "root": "/original",
                "service_unit_id": "schema-test-service",
                "tenant_id": "T",
                "source_id": "S",
                "agent_id": "A",
            },
        ],
    )
    value = json.loads(manifest.path.read_text())
    value["workspaces"][0]["root"] = "/different"
    manifest.path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="mapping"):
        manifest.allocate("session", "s")


def test_source_boundary_requires_evidence_collection_not_string():
    with pytest.raises(ValueError, match="evidence"):
        SourceBoundary(
            "s",
            "2026-10-30T00:00:00Z",
            "old@sha256:" + "a" * 64,
            "not-a-list",
        )


def test_manifest_rejects_reused_operation_keys(tmp_path):
    manifest = MigrationManifest.create(
        tmp_path / "migration.json",
        boundary(),
        [],
    )
    first = manifest.allocate("session", "a")
    manifest.allocate("session", "b")
    value = json.loads(manifest.path.read_text())
    value["assignments"]["session"]["b"].update(
        operation_key=first["operation_key"],
        relative_path=first["relative_path"],
    )
    manifest.path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="operation"):
        MigrationManifest.load(manifest.path)


@pytest.mark.parametrize("tamper", ["operation", "target_id", "remove"])
def test_stale_manifest_instance_rejects_changed_or_removed_known_assignment(
    tmp_path,
    tamper,
):
    from swe.app.conversation_storage.bundle import (
        BundleStore,
        new_operation_key,
    )

    manifest = MigrationManifest.create(
        tmp_path / "migration.json",
        boundary(),
        [],
    )
    original = manifest.allocate("session", "known-source")
    value = json.loads(manifest.path.read_text())
    if tamper == "operation":
        operation = new_operation_key()
        value["assignments"]["session"]["known-source"].update(
            operation_key=operation,
            relative_path=BundleStore.path_for(operation),
        )
    elif tamper == "target_id":
        value["assignments"]["session"]["known-source"]["target_id"] = (
            original["target_id"] + 99
        )
    else:
        del value["assignments"]["session"]["known-source"]
    manifest.path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="assignment"):
        manifest.allocate("session", "known-source")

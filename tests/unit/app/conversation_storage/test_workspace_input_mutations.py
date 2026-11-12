# -*- coding: utf-8 -*-
"""Selected workspace input mutations fence the complete inventory pass."""

import json
import os
from pathlib import Path

import pytest

from swe.app.conversation_storage.migration.freshness import tenant_key
from swe.app.conversation_storage.migration.inventory_v3 import (
    inventory_discovery,
)
from swe.app.conversation_storage.migration.source_inputs import (
    safe_input_entries,
)


def _mutate(sessions, artifact, cutoff, mutation):
    sessions.chmod(0o755)
    artifact.chmod(0o644)
    if mutation == "add":
        added = sessions / "added.bin"
        added.write_bytes(b"new")
        added.chmod(0o444)
        os.utime(added, ns=(cutoff + 1, cutoff + 1))
    elif mutation == "delete":
        artifact.unlink()
    elif mutation == "replace":
        replacement = sessions / "replacement.tmp"
        replacement.write_bytes(b"after!")
        replacement.chmod(0o444)
        os.utime(replacement, ns=(cutoff - 1, cutoff - 1))
        replacement.replace(artifact)
    else:
        artifact.write_bytes(b"after!")
        os.utime(artifact, ns=(cutoff + 1, cutoff + 1))
    if artifact.exists():
        artifact.chmod(0o444)
    # Directory entry mutations are observed even if replacement mtime is old.
    stamp = cutoff + 1 if mutation != "modify" else cutoff - 1
    os.utime(sessions, ns=(stamp, stamp))
    sessions.chmod(0o555)


@pytest.mark.parametrize("mutation", ["add", "delete", "replace", "modify"])
def test_selected_workspace_mutation_is_detected_without_business_scan(
    tmp_path,
    monkeypatch,
    mutation,
):
    root = tmp_path / "workspace"
    sessions = root / "sessions"
    sessions.mkdir(parents=True)
    registry = root / "chats.json"
    registry.write_text(json.dumps({"chats": []}))
    artifact = sessions / "retained.bin"
    artifact.write_bytes(b"before")
    business = root / "business"
    business.mkdir()
    (business / "unsafe-link").symlink_to(root, target_is_directory=True)
    cutoff = 2_000_000_000
    for path in (root, sessions, registry, artifact, business):
        os.utime(path, ns=(cutoff - 1, cutoff - 1))
        path.chmod(0o555 if path.is_dir() else 0o444)
    original_iterdir = Path.iterdir

    def selected_only(path):
        if path == business or business in path.parents:
            pytest.fail("enumerated unrelated business tree")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", selected_only)
    data = {
        "workspaces": [
            {
                "service_unit_id": "service",
                "tenant_id": "tenant",
                "source_id": "source",
                "agent_id": "agent",
                "root": str(root),
                "position": "workspace",
            },
        ],
        "shares": [],
        "fingerprints": [],
        "tenant_sync_started_at_ns": {tenant_key("service", "tenant"): cutoff},
    }
    try:
        before = inventory_discovery(data)
        assert before["ready"], before["blockers"]
        before_paths = set(before["workspaces"][0]["source_paths"])
        _mutate(sessions, artifact, cutoff, mutation)
        after = inventory_discovery(data)
        assert not after["ready"]
        blocker = next(
            item
            for item in after["blockers"]
            if item["code"] == "source_modified_after_sync_started"
        )
        assert blocker["path"] == str(
            artifact if mutation == "modify" else sessions,
        )
        after_paths = set(after["workspaces"][0]["source_paths"])
        if mutation == "add":
            assert after_paths - before_paths == {str(sessions / "added.bin")}
        elif mutation == "delete":
            assert before_paths - after_paths == {str(artifact)}
        else:
            assert after_paths == before_paths
        assert not any(str(business) in value for value in after_paths)
    finally:
        root.chmod(0o755)
        sessions.chmod(0o755)
        business.chmod(0o755)


def test_input_root_rejects_symlink_in_ancestor(tmp_path):
    outside = tmp_path / "outside"
    root = outside / "workspace"
    root.mkdir(parents=True)
    (root / "chats.json").write_text("{}")
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="canonical|symlink|unsafe"):
        safe_input_entries(alias / "workspace", ["chats.json"])

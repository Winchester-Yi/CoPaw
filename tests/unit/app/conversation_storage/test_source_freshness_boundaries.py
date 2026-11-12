# -*- coding: utf-8 -*-
"""Shallow evidence and explicit conversation entrances remain separate."""

import os

import pytest

from swe.app.conversation_storage.migration.discovery import _coverage
from swe.app.conversation_storage.migration.freshness import changed_source
from swe.app.conversation_storage.migration.inventory_v3 import (
    inventory_discovery,
)


def test_shallow_parent_does_not_scan_unrelated_descendants(tmp_path):
    parent = tmp_path / "config"
    parent.mkdir()
    evidence = parent / "config.json"
    evidence.write_text("{}")
    unrelated = parent / "tools"
    unrelated.mkdir()
    changed = unrelated / "tool.py"
    changed.write_text("tool")
    for path in (parent, evidence, unrelated):
        os.utime(path, ns=(100, 100))
    os.utime(changed, ns=(300, 300))
    assert changed_source([parent, evidence], 200, recursive=False) is None
    assert changed_source([parent], 200) == (changed, 300)
    # A shallow parent must not hide an explicitly enumerated child.
    os.utime(evidence, ns=(400, 400))
    checked = set()
    assert (
        changed_source([parent], 200, recursive=False, checked=checked) is None
    )
    assert changed_source(
        [evidence],
        200,
        recursive=False,
        checked=checked,
    ) == (evidence, 400)


def test_changed_source_detects_broken_symlink(tmp_path):
    link = tmp_path / "broken.json"
    link.symlink_to(tmp_path / "missing.json")
    assert changed_source([link], 10**18, recursive=False) == (
        link,
        link.lstat().st_mtime_ns,
    )


def test_native_inventory_ignores_nonconversation_tree(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "agent.json").write_text("not conversation content")
    tools = root / "tools"
    tools.mkdir()
    (tools / "sessions").mkdir()
    (tools / "sessions/unrelated.json").write_text("not a session")
    (tools / "external").symlink_to(tmp_path / "outside")
    mapping = {
        "service_unit_id": "service",
        "tenant_id": "tenant",
        "source_id": "source",
        "agent_id": "agent",
        "root": str(root),
        "position": "workspace",
    }
    report = inventory_discovery(
        {"workspaces": [mapping], "shares": [], "fingerprints": []},
    )
    assert report["ready"], report["blockers"]
    assert report["files"] == []
    assert report["workspaces"][0]["source_paths"] == [str(root)]


def test_coverage_prunes_known_workspace_but_finds_unknown_candidate(tmp_path):
    known = tmp_path / "known"
    (known / "tools/example/sessions").mkdir(parents=True)
    unknown = tmp_path / "unknown"
    (unknown / "sessions").mkdir(parents=True)
    report = {"blockers": []}
    _coverage(
        {"source_root": str(tmp_path), "service_unit_id": "service"},
        {known},
        report,
        [],
    )
    assert len(report["blockers"]) == 1
    assert report["blockers"][0]["code"] == "unregistered_workspace"
    assert "unknown" in report["blockers"][0]["position"]


@pytest.mark.parametrize(
    "relative",
    [
        "sessions/orphan.json",
        "sessions/.turn-ledger/generation/metadata.json",
        "dialog/chat/archive.json",
    ],
)
def test_missing_registry_does_not_hide_unmapped_conversation_files(
    tmp_path,
    relative,
):
    root = tmp_path / "workspace"
    path = root / relative
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    for entry in root.rglob("*"):
        entry.chmod(0o555 if entry.is_dir() else 0o444)
    root.chmod(0o555)
    mapping = {
        "service_unit_id": "service",
        "tenant_id": "tenant",
        "source_id": "source",
        "agent_id": "agent",
        "root": str(root),
        "position": "workspace",
    }
    report = inventory_discovery(
        {"workspaces": [mapping], "shares": [], "fingerprints": []},
    )
    assert not report["ready"]
    assert any(
        item["code"] == "registry_invalid" for item in report["blockers"]
    )
    assert relative in {item["path"] for item in report["files"]}


@pytest.mark.parametrize(
    "relative",
    [
        "sessions/.turn-ledger/generation",
        "dialog/unknown-chat",
    ],
)
def test_missing_registry_does_not_hide_empty_conversation_directories(
    tmp_path,
    relative,
):
    root = tmp_path / "workspace"
    (root / relative).mkdir(parents=True)
    for entry in root.rglob("*"):
        entry.chmod(0o555 if entry.is_dir() else 0o444)
    root.chmod(0o555)
    mapping = {
        "service_unit_id": "service",
        "tenant_id": "tenant",
        "source_id": "source",
        "agent_id": "agent",
        "root": str(root),
        "position": "workspace",
    }
    report = inventory_discovery(
        {"workspaces": [mapping], "shares": [], "fingerprints": []},
    )
    assert not report["ready"]
    assert any(
        item["code"] == "registry_invalid" for item in report["blockers"]
    )

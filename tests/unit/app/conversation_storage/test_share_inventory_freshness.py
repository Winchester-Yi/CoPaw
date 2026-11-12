# -*- coding: utf-8 -*-
"""Share freshness scans shared parents once per inventory, never per job."""

import os
from types import SimpleNamespace

import pytest

from swe.app.conversation_storage.migration import inventory_v3 as inventory
from swe.app.conversation_storage.migration.freshness import tenant_key
from swe.app.conversation_storage.migration.spool import InventorySpool
from swe.cli.conversation_storage_v3_cmd import _recheck_spooled_inventory


def _share_data(tmp_path):
    parent = tmp_path / "shares"
    parent.mkdir()
    shares = []
    for index in range(4):
        path = parent / (str(index) + ".json")
        path.write_text('{"messages": []}')
        shares.append(
            {
                "token": str(index),
                "chat_id": "missing",
                "creator_id": "alice",
                "tenant_id": "tenant",
                "snapshot_key": path.name,
                "created_at": "fixed",
                "access_count": 0,
                "last_access_at": None,
                "service_unit_id": "service",
                "position": "share:" + str(index),
                "snapshot_path": str(path),
            },
        )
    cutoff = (
        max(path.stat().st_mtime_ns for path in (parent, *parent.iterdir()))
        + 1000000
    )
    return (
        {
            "workspaces": [],
            "services": [],
            "shares": shares,
            "fingerprints": [],
            "tenant_sync_started_at_ns": {
                tenant_key("service", "tenant"): cutoff,
            },
            "source_boundary": {
                "snapshot_id": "snapshot",
                "captured_at": "2026-11-01T00:00:00Z",
                "old_image_digest": "old@sha256:" + "a" * 64,
                "evidence_refs": ["stopped"],
            },
        },
        parent,
        cutoff,
    )


def _count_freshness(monkeypatch):
    calls = []
    original = inventory.changed_source

    def measured(paths, cutoff, **kwargs):
        calls.extend(paths)
        return original(paths, cutoff, **kwargs)

    monkeypatch.setattr(inventory, "changed_source", measured)
    return calls


def test_share_inventory_deduplicates_parent_but_checks_each_snapshot(
    tmp_path,
    monkeypatch,
):
    data, parent, cutoff = _share_data(tmp_path)
    calls = _count_freshness(monkeypatch)
    report = {"workspaces": [], "shares": [], "blockers": []}
    inventory._inventory_shares(data, report)
    assert not report["blockers"] and len(report["shares"]) == 4
    assert calls.count(str(parent)) == 1
    assert all(
        calls.count(share["snapshot_path"]) == 1 for share in data["shares"]
    )
    # Even when only the directory mtime changes, a new pass must reject it.
    os.utime(parent, ns=(cutoff + 1, cutoff + 1))
    report = {"workspaces": [], "shares": [], "blockers": []}
    inventory._inventory_shares(data, report)
    assert calls.count(str(parent)) == 2
    assert len(report["blockers"]) == 4
    assert all(item["path"] == str(parent) for item in report["blockers"])


@pytest.mark.parametrize("strict_first", [False, True])
def test_same_parent_with_different_cutoffs_keeps_freshness_isolated(
    tmp_path,
    monkeypatch,
    strict_first,
):
    data, parent, cutoff = _share_data(tmp_path)
    shares = data["shares"][:2]
    shares[0]["tenant_id"] = "strict"
    shares[1]["tenant_id"] = "lenient"
    data["shares"] = shares if strict_first else list(reversed(shares))
    data["tenant_sync_started_at_ns"] = {
        tenant_key("service", "strict"): cutoff,
        tenant_key("service", "lenient"): cutoff + 2,
    }
    os.utime(parent, ns=(cutoff + 1, cutoff + 1))
    calls = _count_freshness(monkeypatch)
    report = {"workspaces": [], "shares": [], "blockers": []}
    inventory._inventory_shares(data, report)
    assert calls.count(str(parent)) == 2
    assert len(report["shares"]) == 1
    assert report["shares"][0]["record"]["tenant_id"] == "lenient"
    assert len(report["blockers"]) == 1
    assert report["blockers"][0]["scope"] == ["service", "strict"]
    assert report["blockers"][0]["path"] == str(parent)
    assert report["blockers"][0]["cutoff_ns"] == cutoff


def test_modified_snapshot_takes_priority_over_shared_parent(tmp_path):
    data, parent, cutoff = _share_data(tmp_path)
    snapshot = data["shares"][0]["snapshot_path"]
    os.utime(snapshot, ns=(cutoff + 2, cutoff + 2))
    os.utime(parent, ns=(cutoff + 1, cutoff + 1))
    report = {"workspaces": [], "shares": [], "blockers": []}
    inventory._inventory_shares(data, report)
    assert report["blockers"][0]["path"] == snapshot
    assert all(
        blocker["path"] == str(parent) for blocker in report["blockers"][1:]
    )


@pytest.mark.parametrize("mutation", ["add", "delete", "symlink"])
def test_parent_scan_detects_directory_and_symlink_changes(tmp_path, mutation):
    data, parent, cutoff = _share_data(tmp_path)
    sibling = parent / "sibling.json"
    if mutation == "delete":
        sibling.write_text("old")
        os.utime(sibling, ns=(cutoff - 1, cutoff - 1))
        sibling.unlink()
    elif mutation == "add":
        sibling.write_text("new")
    else:
        sibling = parent / "0.json"
        sibling.unlink()
        sibling.symlink_to(parent / "1.json")
    # Directory mtime detects creation/deletion. A symlink remains forbidden
    # even when its own and the parent's mtimes predate the cutoff.
    stamp = cutoff - 1 if mutation == "symlink" else cutoff + 1
    os.utime(parent, ns=(stamp, stamp))
    if mutation == "symlink":
        os.utime(sibling, ns=(stamp, stamp), follow_symlinks=False)
    report = {"workspaces": [], "shares": [], "blockers": []}
    inventory._inventory_shares(data, report)
    expected_count = 1 if mutation == "symlink" else len(data["shares"])
    assert len(report["blockers"]) == expected_count
    expected = str(sibling) if mutation == "symlink" else str(parent)
    assert all(item["path"] == expected for item in report["blockers"])


async def test_final_spooled_recheck_rescans_parent_without_persisting_blockers(
    tmp_path,
    monkeypatch,
):
    data, parent, cutoff = _share_data(tmp_path)
    calls = _count_freshness(monkeypatch)
    recorded = []
    manifest = SimpleNamespace(data=data, record_source_change=recorded.append)
    with InventorySpool(
        tmp_path,
        max_bytes=1024 * 1024,
        record_limit=65536,
    ) as spool:
        initial = await inventory.inventory_v3_spooled(manifest, spool)
        assert initial["ready"]
        os.utime(parent, ns=(cutoff + 1, cutoff + 1))
        assert not await _recheck_spooled_inventory(
            manifest,
            initial,
            tmp_path,
            disk_limit=1024 * 1024,
            record_limit=65536,
            protected=[],
            workers=2,
            io_workers=2,
            memory_bytes=1024 * 1024,
        )
    assert calls.count(str(parent)) == 2
    assert recorded == []

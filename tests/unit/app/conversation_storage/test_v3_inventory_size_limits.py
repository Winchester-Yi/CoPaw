# -*- coding: utf-8 -*-
"""Reader-size checks also cover workspaces without registries and shares."""

import os

import pytest

from swe.app.conversation_storage.migration import inventory_v3


def workspace(root):
    return {
        "root": str(root),
        "service_unit_id": "svc",
        "tenant_id": "tenant",
        "source_id": "source",
        "agent_id": "agent",
        "position": "workspace-position",
    }


@pytest.mark.parametrize("size", [0, 16, 17])
def test_native_file_outside_conversation_inputs_is_ignored(tmp_path, size):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "native.bin").write_bytes(b"x" * size)
    result = inventory_v3.inventory_discovery(
        {"workspaces": [workspace(root)], "shares": [], "fingerprints": []},
        workspace_offset=41,
    )
    assert result["ready"]
    assert result["files"] == []
    assert result["workspaces"][0]["sessions"] == []
    assert result["workspaces"][0]["chats"] == []
    assert result["blockers"] == []


def test_empty_workspace_remains_valid(tmp_path):
    result = inventory_v3.inventory_discovery(
        {
            "workspaces": [workspace(tmp_path)],
            "shares": [],
            "fingerprints": [],
        },
    )
    assert result["ready"]
    assert result["files"] == result["blockers"] == []


def test_share_snapshot_rejected_before_read(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory_v3, "MAX_READ_BYTES", 16)
    path = tmp_path / "large-share.json"
    path.write_bytes(b"x" * 17)

    def unexpected_read(*_args, **_kwargs):
        pytest.fail("oversized share snapshot must not be read")

    monkeypatch.setattr(type(path), "read_bytes", unexpected_read)
    result = inventory_v3.inventory_discovery(
        {
            "workspaces": [],
            "shares": [
                {"snapshot_path": str(path), "position": "share-position"},
            ],
            "fingerprints": [],
        },
    )
    assert not result["ready"]
    assert result["shares"] == []
    assert result["blockers"] == [
        {
            "code": "share_snapshot_oversized",
            "position": "share-position",
            "detail": "share snapshot exceeds bounded reader size",
        },
    ]


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_share_snapshot_without_cutoff_rejects_unsafe_path_before_read(
    tmp_path,
    monkeypatch,
    kind,
):
    path = tmp_path / "snapshot.json"
    if kind == "symlink":
        target = tmp_path / "target.json"
        target.write_text("{}")
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()

    def unexpected_read(*_args, **_kwargs):
        pytest.fail("unsafe share snapshot must not be read")

    monkeypatch.setattr(type(path), "read_bytes", unexpected_read)
    with pytest.raises(ValueError, match="symlink|unsupported|regular file"):
        inventory_v3.inventory_discovery(
            {
                "workspaces": [],
                "shares": [
                    {"snapshot_path": str(path), "position": "share"},
                ],
                "fingerprints": [],
            },
        )

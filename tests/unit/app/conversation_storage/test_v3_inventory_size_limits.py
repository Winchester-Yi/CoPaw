# -*- coding: utf-8 -*-
"""Reader-size checks also cover workspaces without registries and shares."""

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
def test_native_only_workspace_checks_size_and_keeps_global_index(
    tmp_path,
    monkeypatch,
    size,
):
    monkeypatch.setattr(inventory_v3, "MAX_READ_BYTES", 16)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "native.bin").write_bytes(b"x" * size)
    result = inventory_v3.inventory_discovery(
        {"workspaces": [workspace(root)], "shares": [], "fingerprints": []},
        workspace_offset=41,
    )
    assert result["ready"] is (size <= 16)
    assert result["files"] == [
        {"workspace": 41, "path": "native.bin", "bytes": size},
    ]
    assert result["workspaces"][0]["sessions"] == []
    assert result["workspaces"][0]["chats"] == []
    if size > 16:
        assert result["blockers"][0]["code"] == "source_artifact_oversized"
        assert result["blockers"][0]["position"] == "workspace-position"


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

# -*- coding: utf-8 -*-
"""Configured input entrances share parser and preflight capacity scope."""

import pytest

from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.migration.inventory import WorkspaceMapping
from swe.app.conversation_storage.migration.inventory_parallel import (
    _preflight_workspace,
)
from swe.app.conversation_storage.migration.source_inputs import (
    workspace_files,
)
from swe.app.conversation_storage.migration.spool import MigrationCapacityError


@pytest.mark.parametrize(
    "field,default,configured,is_directory",
    [
        ("registry_path", "chats.json", "reviewed/registry.json", False),
        ("sessions_dir", "sessions", "reviewed/legacy_sessions", True),
        ("dialog_dir", "dialog", "reviewed/legacy_dialog", True),
    ],
)
def test_configured_entrance_is_counted_and_replaces_default(
    tmp_path,
    field,
    default,
    configured,
    is_directory,
):
    chosen = tmp_path / configured
    selected = chosen / "data.bin" if is_directory else chosen
    selected.parent.mkdir(parents=True)
    selected.write_bytes(b"x" * 65)
    ignored = tmp_path / default
    ignored_file = ignored / "oversized.bin" if is_directory else ignored
    ignored_file.parent.mkdir(parents=True, exist_ok=True)
    ignored_file.write_bytes(b"x" * 1024)
    mapping = WorkspaceMapping(
        WorkspaceScope("tenant", "source", "agent", service_unit_id="svc"),
        tmp_path,
        **{field: configured},
    )
    raw = {"root": str(tmp_path), field: configured}
    assert workspace_files(mapping) == [selected]
    assert _preflight_workspace(raw, 2048) == 65
    with pytest.raises(MigrationCapacityError, match="memory allowance"):
        _preflight_workspace(raw, 1024)

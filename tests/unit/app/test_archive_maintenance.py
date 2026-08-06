# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from swe.app.file_governance.archive_maintenance import (
    ARCHIVE_FILES_DIR,
    ARCHIVE_INDEX_FILE,
    PROTECTED_PATHS_FILE,
    archive_old_orphans_for_workspace,
)


def _touch_mtime(path, timestamp: float) -> None:
    os.utime(path, (timestamp, timestamp))


def test_archive_old_orphans_for_workspace_archives_only_old_unprotected_files(
    tmp_path,
) -> None:
    workspace = tmp_path
    old_timestamp = datetime(2026, 6, 25, tzinfo=timezone.utc).timestamp()
    fresh_timestamp = datetime(2026, 7, 2, tzinfo=timezone.utc).timestamp()

    old_file = workspace / "old.txt"
    old_file.write_text("old", encoding="utf-8")
    _touch_mtime(old_file, old_timestamp)

    fresh_file = workspace / "fresh.txt"
    fresh_file.write_text("fresh", encoding="utf-8")
    _touch_mtime(fresh_file, fresh_timestamp)

    keep_file = workspace / "AGENTS.md"
    keep_file.write_text("keep", encoding="utf-8")
    _touch_mtime(keep_file, old_timestamp)

    protected_file = workspace / "protected.txt"
    protected_file.write_text("protected", encoding="utf-8")
    _touch_mtime(protected_file, old_timestamp)
    (workspace / PROTECTED_PATHS_FILE).parent.mkdir(parents=True)
    (workspace / PROTECTED_PATHS_FILE).write_text(
        json.dumps({"version": 1, "paths": [{"path": "protected.txt"}]}),
        encoding="utf-8",
    )

    archived_file = workspace / ARCHIVE_FILES_DIR / "expired-archive"
    archived_file.parent.mkdir(parents=True)
    archived_file.write_text("expired", encoding="utf-8")
    (workspace / ARCHIVE_INDEX_FILE).write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {
                        "id": "expired-archive",
                        "original_path": "previous.txt",
                        "archive_path": f"{ARCHIVE_FILES_DIR}/expired-archive",
                        "size_bytes": 7,
                        "mtime": "2026-06-01T00:00:00Z",
                        "archived_at": "2026-06-01T00:00:00Z",
                        "archived_by": "dream",
                        "archive_reason": "old",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )

    result = archive_old_orphans_for_workspace(
        workspace,
        old_orphan_days=3,
        max_files=10,
        remaining_files=10,
        actor="source_archive_maintenance",
        now=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )

    assert old_file.exists() is False
    assert fresh_file.exists() is True
    assert keep_file.exists() is True
    assert protected_file.exists() is True
    assert archived_file.exists() is True
    assert result.archived_paths == ["old.txt"]
    assert result.archived_size_bytes == 3
    assert result.candidates_count == 1
    index = json.loads((workspace / ARCHIVE_INDEX_FILE).read_text("utf-8"))
    assert [item["original_path"] for item in index["items"]] == [
        "previous.txt",
        "old.txt",
    ]


def test_archive_old_orphans_keeps_system_state_files_and_directories(
    tmp_path,
) -> None:
    old_timestamp = datetime(2026, 6, 25, tzinfo=timezone.utc).timestamp()

    old_file = tmp_path / "old.txt"
    old_file.write_text("old", encoding="utf-8")
    _touch_mtime(old_file, old_timestamp)

    system_jobs = tmp_path / "system_jobs.json"
    system_jobs.write_text("{}", encoding="utf-8")
    _touch_mtime(system_jobs, old_timestamp)

    hook_script = tmp_path / "hooks" / "scripts" / "guard.py"
    hook_script.parent.mkdir(parents=True)
    hook_script.write_text("print('guard')", encoding="utf-8")
    _touch_mtime(hook_script, old_timestamp)

    dialog_manifest = tmp_path / "dialog" / "chat-1" / "manifest.json"
    dialog_manifest.parent.mkdir(parents=True)
    dialog_manifest.write_text('{"boundaries":[]}', encoding="utf-8")
    _touch_mtime(dialog_manifest, old_timestamp)

    result = archive_old_orphans_for_workspace(
        tmp_path,
        old_orphan_days=3,
        max_files=10,
        remaining_files=10,
        actor="source_archive_maintenance",
        now=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )

    assert old_file.exists() is False
    assert system_jobs.exists() is True
    assert hook_script.exists() is True
    assert dialog_manifest.exists() is True
    assert result.archived_paths == ["old.txt"]
    assert result.candidates_count == 1


def test_archive_old_orphans_for_workspace_honors_limits(tmp_path) -> None:
    old_timestamp = datetime(2026, 6, 25, tzinfo=timezone.utc).timestamp()
    for index in range(3):
        path = tmp_path / f"old-{index}.txt"
        path.write_text(str(index), encoding="utf-8")
        _touch_mtime(path, old_timestamp)

    result = archive_old_orphans_for_workspace(
        tmp_path,
        old_orphan_days=3,
        max_files=2,
        remaining_files=1,
        actor="source_archive_maintenance",
        now=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )

    assert len(result.archived_paths) == 1
    assert result.candidates_count == 3
    assert result.skipped_files == 2


def test_archive_old_orphans_for_workspace_skips_index_write_when_no_candidates(
    tmp_path,
) -> None:
    fresh_file = tmp_path / "fresh.txt"
    fresh_file.write_text("fresh", encoding="utf-8")

    result = archive_old_orphans_for_workspace(
        tmp_path,
        old_orphan_days=3,
        max_files=10,
        remaining_files=10,
        actor="source_archive_maintenance",
        now=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )

    assert result.archived_paths == []
    assert (tmp_path / ARCHIVE_INDEX_FILE).exists() is False

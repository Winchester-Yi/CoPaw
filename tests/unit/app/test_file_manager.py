# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import shutil

import pytest

from swe.app import file_manager
from swe.app.file_manager import (
    FILE_MANAGER_PAGE_SIZE,
    FileManagerConflictError,
    FileManagerItemKind,
    FileManagerOutcomeUncertainError,
    FileManagerPathError,
    FileManagerRoot,
    FileManagerService,
    root_capabilities,
)


def _service(workspace: Path) -> FileManagerService:
    return FileManagerService(
        workspace,
        cursor_secret=b"test-file-manager-secret",
    )


def _source_scope_service(
    workspace: Path,
    source_scope_base: Path,
) -> FileManagerService:
    return FileManagerService(
        workspace,
        cursor_secret=b"test-file-manager-secret",
        source_scope_base_dir=source_scope_base,
        source_scope_component="tenant-a",
    )


@pytest.fixture(autouse=True)
def _clear_cursor_secret_cache() -> Generator[None, None, None]:
    file_manager._load_or_create_cursor_secret.cache_clear()
    yield
    file_manager._load_or_create_cursor_secret.cache_clear()


def test_service_factory_uses_configured_cursor_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWE_FILE_MANAGER_CURSOR_SECRET", "configured-secret")

    service = file_manager.get_file_manager_service(tmp_path)

    assert service._cursor_secret == b"configured-secret"


def test_service_factory_cursor_secret_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    secret_path = secret_dir / "file-manager-cursor-secret"
    secret_path.write_bytes(b"x" * 48)
    secret_path.chmod(0o600)
    monkeypatch.delenv("SWE_FILE_MANAGER_CURSOR_SECRET", raising=False)
    monkeypatch.setattr(file_manager, "SECRET_DIR", secret_dir)
    original_read_cursor_secret = file_manager._read_cursor_secret
    read_paths: list[Path] = []

    def track_read_cursor_secret(path: Path) -> bytes:
        read_paths.append(path)
        return original_read_cursor_secret(path)

    monkeypatch.setattr(
        file_manager,
        "_read_cursor_secret",
        track_read_cursor_secret,
    )

    file_manager.get_file_manager_service(tmp_path / "tenant-a")
    file_manager.get_file_manager_service(tmp_path / "tenant-b")

    assert read_paths == [secret_path]


def test_service_factory_persists_a_private_fallback_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_dir = tmp_path / "secrets"
    monkeypatch.delenv("SWE_FILE_MANAGER_CURSOR_SECRET", raising=False)
    monkeypatch.setattr(file_manager, "SECRET_DIR", secret_dir)

    first = file_manager.get_file_manager_service(tmp_path / "tenant-a")
    second = file_manager.get_file_manager_service(tmp_path / "tenant-b")

    secret_file = secret_dir / "file-manager-cursor-secret"
    assert first._cursor_secret == second._cursor_secret
    assert len(first._cursor_secret) >= 32
    assert secret_file.exists()
    assert secret_file.stat().st_mode & 0o077 == 0


def test_service_factory_rejects_an_explicit_empty_cursor_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWE_FILE_MANAGER_CURSOR_SECRET", "")

    with pytest.raises(ValueError, match="must not be empty"):
        file_manager.get_file_manager_service(tmp_path)


def test_working_listing_hides_only_sessions_and_governance(
    tmp_path: Path,
) -> None:
    (tmp_path / "sessions").mkdir()
    (tmp_path / "governance").mkdir()
    (tmp_path / ".env").write_text("visible", encoding="utf-8")
    (tmp_path / "notes").mkdir()

    listing = _service(tmp_path).list_directory(FileManagerRoot.WORKING)

    assert {item.name for item in listing.items} == {"notes", ".env"}


def test_delete_directory_removes_nested_entries_without_following_symlinks(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "reports"
    nested = directory / "weekly"
    outside = tmp_path / "outside"
    nested.mkdir(parents=True)
    outside.mkdir()
    (nested / "report.md").write_text("report", encoding="utf-8")
    (directory / "outside-link").symlink_to(
        outside,
        target_is_directory=True,
    )

    _service(tmp_path).delete_directory("working", "reports")

    assert not directory.exists()
    assert outside.is_dir()


@pytest.mark.parametrize(
    ("root", "path"),
    [
        ("working", ""),
        ("working", "sessions"),
        ("conversation", "folder"),
    ],
)
def test_delete_directory_rejects_root_protected_and_read_only_paths(
    tmp_path: Path,
    root: str,
    path: str,
) -> None:
    (tmp_path / "sessions" / "folder").mkdir(parents=True)

    with pytest.raises(FileManagerPathError):
        _service(tmp_path).delete_directory(root, path)


@pytest.mark.parametrize("path", ["media", "static"])
def test_delete_directory_rejects_top_level_file_manager_backing_dirs(
    tmp_path: Path,
    path: str,
) -> None:
    backing_dir = tmp_path / path
    backing_dir.mkdir()
    (backing_dir / "existing.txt").write_text("kept", encoding="utf-8")

    with pytest.raises(FileManagerPathError):
        _service(tmp_path).delete_directory("working", path)

    assert (backing_dir / "existing.txt").read_text(encoding="utf-8") == "kept"


@pytest.mark.parametrize("path", ["media", "static"])
def test_working_listing_disables_archive_for_top_level_backing_dirs(
    tmp_path: Path,
    path: str,
) -> None:
    (tmp_path / path).mkdir()

    item = _service(tmp_path).list_directory("working").items[0]

    assert item.name == path
    assert item.capabilities.archive is False


def test_source_scope_upload_archives_and_restores_to_tenant_root(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source_scope_base = tmp_path / "source-scope"
    workspace.mkdir()
    source_scope_base.mkdir()
    service = _source_scope_service(workspace, source_scope_base)

    uploaded = service.upload_bytes(
        "source_scope",
        "",
        "report.txt",
        b"root file",
    )
    archived = service.archive_file(
        "source_scope",
        uploaded.path,
        actor="test",
    )

    assert (source_scope_base / "tenant-a").is_dir()
    assert archived.original_path == "source_scope/report.txt"
    assert not (source_scope_base / "tenant-a" / "report.txt").exists()

    restored = service.restore_recycle_item(
        archived.archive_item_id,
        actor="test",
    )

    assert restored.original_path == "source_scope/report.txt"
    assert (source_scope_base / "tenant-a" / "report.txt").read_bytes() == (
        b"root file"
    )


def test_source_scope_rejects_a_symlinked_tenant_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source_scope_base = tmp_path / "source-scope"
    outside = tmp_path / "outside"
    workspace.mkdir()
    source_scope_base.mkdir()
    outside.mkdir()
    (source_scope_base / "tenant-a").symlink_to(
        outside,
        target_is_directory=True,
    )
    service = _source_scope_service(workspace, source_scope_base)

    with pytest.raises(FileManagerPathError, match="Unable to open directory"):
        service.list_directory("source_scope")


def test_listing_breaks_mtime_ties_with_case_insensitive_natural_order(
    tmp_path: Path,
) -> None:
    for name in ("zeta", "Folder10", "folder2"):
        (tmp_path / name).mkdir()
        os.utime(tmp_path / name, ns=(1_700_000_000_000_000_000,) * 2)
    for name in ("item10.txt", "Item2.txt", "alpha.txt", "Folder02"):
        (tmp_path / name).write_text(name, encoding="utf-8")
        os.utime(tmp_path / name, ns=(1_700_000_000_000_000_000,) * 2)

    listing = _service(tmp_path).list_directory(FileManagerRoot.WORKING)

    assert [item.name for item in listing.items] == [
        "alpha.txt",
        "Folder02",
        "folder2",
        "Folder10",
        "Item2.txt",
        "item10.txt",
        "zeta",
    ]
    assert listing.first_child_directory == "folder2"
    assert listing.has_child_directory is True


def test_natural_sort_handles_leading_numeric_and_alphabetic_names(
    tmp_path: Path,
) -> None:
    for name in ("alpha.txt", "10.txt", "2.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")
        os.utime(tmp_path / name, ns=(1_700_000_000_000_000_000,) * 2)

    listing = _service(tmp_path).list_directory(FileManagerRoot.WORKING)

    assert [item.name for item in listing.items] == [
        "2.txt",
        "10.txt",
        "alpha.txt",
    ]


@pytest.mark.parametrize("query", [None, "item"])
def test_listing_sorts_files_and_directories_by_mtime_before_paginating(
    tmp_path: Path,
    query: str | None,
) -> None:
    count = FILE_MANAGER_PAGE_SIZE + 3
    expected = []
    for index in range(count):
        if index in (1, count - 2):
            path = tmp_path / f"item{index:03d}"
            path.mkdir()
        else:
            path = tmp_path / f"item{index:03d}.txt"
            path.write_text("x", encoding="utf-8")
        timestamp = 1_700_000_000_000_000_000 + index * 1_000_000_000
        os.utime(path, ns=(timestamp, timestamp))
        expected.append(path.name)

    service = _service(tmp_path)
    first = service.list_directory("working", query=query)
    # The existing cursor keeps its order even after an item's own mtime changes.
    os.utime(tmp_path / "item001", ns=(1_800_000_000_000_000_000,) * 2)
    second = service.list_directory(
        "working",
        query=query,
        cursor=first.next_cursor,
    )
    assert len(first.items) == FILE_MANAGER_PAGE_SIZE
    assert [item.name for item in first.items + second.items] == expected[::-1]
    assert second.next_cursor is None

    refreshed = service.list_directory("working", query=query)
    assert refreshed.items[0].name == "item001"


def test_listing_reports_no_child_directory_state(tmp_path: Path) -> None:
    (tmp_path / "only-file.txt").write_text("text", encoding="utf-8")

    listing = _service(tmp_path).list_directory(FileManagerRoot.WORKING)

    assert listing.has_child_directory is False
    assert listing.first_child_directory is None


def test_listing_uses_stable_100_item_cursor_pages(tmp_path: Path) -> None:
    for index in range(FILE_MANAGER_PAGE_SIZE + 2):
        (tmp_path / f"item{index:03d}.txt").write_text("x", encoding="utf-8")
        os.utime(
            tmp_path / f"item{index:03d}.txt",
            ns=(1_700_000_000_000_000_000,) * 2,
        )

    service = _service(tmp_path)
    first_page = service.list_directory(FileManagerRoot.WORKING)
    second_page = service.list_directory(
        FileManagerRoot.WORKING,
        cursor=first_page.next_cursor,
    )

    assert len(first_page.items) == FILE_MANAGER_PAGE_SIZE
    assert first_page.next_cursor is not None
    assert [item.name for item in second_page.items] == [
        "item100.txt",
        "item101.txt",
    ]
    assert second_page.next_cursor is None


def test_directory_cursor_reuses_snapshot_and_rejects_changed_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(FILE_MANAGER_PAGE_SIZE + 2):
        (tmp_path / f"item{index:03d}.txt").write_text("x", encoding="utf-8")
        os.utime(
            tmp_path / f"item{index:03d}.txt",
            ns=(1_700_000_000_000_000_000,) * 2,
        )
    service = _service(tmp_path)
    original_candidates = service._directory_candidates
    calls = 0

    def track_candidates(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_candidates(*args, **kwargs)

    monkeypatch.setattr(service, "_directory_candidates", track_candidates)

    first = service.list_directory("working")
    second = service.list_directory("working", cursor=first.next_cursor)

    assert calls == 1
    assert [item.name for item in second.items] == [
        "item100.txt",
        "item101.txt",
    ]

    (tmp_path / "changed.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(FileManagerConflictError, match="refresh and retry"):
        service.list_directory("working", cursor=first.next_cursor)


def test_directory_cursor_uses_the_30_second_snapshot_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(FILE_MANAGER_PAGE_SIZE + 1):
        (tmp_path / f"item{index:03d}.txt").write_text("x", encoding="utf-8")
        os.utime(
            tmp_path / f"item{index:03d}.txt",
            ns=(1_700_000_000_000_000_000,) * 2,
        )
    file_manager._DIRECTORY_SNAPSHOTS.clear()
    clock = [0.0]
    monkeypatch.setattr(file_manager.time, "monotonic", lambda: clock[0])
    try:
        service = _service(tmp_path)
        first = service.list_directory("working")
        assert first.next_cursor is not None

        clock[0] = 29.9
        second = service.list_directory("working", cursor=first.next_cursor)
        assert [item.name for item in second.items] == ["item100.txt"]

        clock[0] = 30.0
        with pytest.raises(
            FileManagerConflictError,
            match="refresh and retry",
        ):
            service.list_directory("working", cursor=first.next_cursor)
    finally:
        file_manager._DIRECTORY_SNAPSHOTS.clear()


def test_snapshot_workspace_quota_retains_sixteen_directories(
    tmp_path: Path,
) -> None:
    file_manager._DIRECTORY_SNAPSHOTS.clear()
    try:
        service = _service(tmp_path)
        cursors: dict[str, str] = {}
        for directory_index in range(17):
            directory = tmp_path / f"dir{directory_index:02d}"
            directory.mkdir()
            for item_index in range(FILE_MANAGER_PAGE_SIZE + 1):
                (directory / f"item{item_index:03d}.txt").write_text(
                    "x",
                    encoding="utf-8",
                )
                os.utime(
                    directory / f"item{item_index:03d}.txt",
                    ns=(1_700_000_000_000_000_000,) * 2,
                )
            listing = service.list_directory(
                "working",
                directory.name,
            )
            assert listing.next_cursor is not None
            cursors[directory.name] = listing.next_cursor

        with pytest.raises(
            FileManagerConflictError,
            match="refresh and retry",
        ):
            service.list_directory("working", "dir00", cursor=cursors["dir00"])
        for directory_index in range(1, 17):
            listing = service.list_directory(
                "working",
                f"dir{directory_index:02d}",
                cursor=cursors[f"dir{directory_index:02d}"],
            )
            assert [item.name for item in listing.items] == ["item100.txt"]
    finally:
        file_manager._DIRECTORY_SNAPSHOTS.clear()


def test_directory_cursor_rejects_deleted_directory(
    tmp_path: Path,
) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    for index in range(FILE_MANAGER_PAGE_SIZE + 1):
        (media_dir / f"item{index:03d}.txt").write_text("x", encoding="utf-8")
    service = _service(tmp_path)
    first = service.list_directory("upload")

    shutil.rmtree(media_dir)

    with pytest.raises(FileManagerConflictError, match="refresh and retry"):
        service.list_directory("upload", cursor=first.next_cursor)


def test_snapshot_workspace_quota_preserves_other_workspace_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        file_manager,
        "_DIRECTORY_SNAPSHOT_WORKSPACE_CAPACITY",
        1,
    )
    file_manager._DIRECTORY_SNAPSHOTS.clear()
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    for workspace in (first_workspace, second_workspace):
        workspace.mkdir()
        for index in range(FILE_MANAGER_PAGE_SIZE + 1):
            (workspace / f"item{index:03d}.txt").write_text(
                "x",
                encoding="utf-8",
            )
            os.utime(
                workspace / f"item{index:03d}.txt",
                ns=(1_700_000_000_000_000_000,) * 2,
            )

    first_service = _service(first_workspace)
    second_service = _service(second_workspace)
    first_page = first_service.list_directory("working")
    second_page = second_service.list_directory("working")
    first_service.list_directory("working", query="item")

    assert (
        second_service.list_directory(
            "working",
            cursor=second_page.next_cursor,
        )
        .items[0]
        .name
        == "item100.txt"
    )
    assert (
        len(
            [
                key
                for key in file_manager._DIRECTORY_SNAPSHOTS
                if key[0] == str(first_workspace.resolve())
            ],
        )
        == 1
    )
    assert first_page.next_cursor is not None


def test_listing_allows_more_than_ten_thousand_items_with_cursor(
    tmp_path: Path,
) -> None:
    for index in range(10_001):
        (tmp_path / f"item{index:05d}.txt").write_text("x", encoding="utf-8")
        os.utime(
            tmp_path / f"item{index:05d}.txt",
            ns=(1_700_000_000_000_000_000,) * 2,
        )

    listing = _service(tmp_path).list_directory(FileManagerRoot.WORKING)

    assert len(listing.items) == FILE_MANAGER_PAGE_SIZE
    assert listing.items[0].name == "item00000.txt"
    assert listing.items[-1].name == "item00099.txt"
    assert listing.next_cursor is not None


def test_cursor_rejects_forgery_and_context_mismatches(tmp_path: Path) -> None:
    for index in range(FILE_MANAGER_PAGE_SIZE + 1):
        (tmp_path / f"item{index:03d}.txt").write_text("x", encoding="utf-8")
        os.utime(
            tmp_path / f"item{index:03d}.txt",
            ns=(1_700_000_000_000_000_000,) * 2,
        )
    (tmp_path / "media").mkdir()
    (tmp_path / "folder").mkdir()
    service = _service(tmp_path)
    cursor = service.list_directory(FileManagerRoot.WORKING).next_cursor
    assert cursor is not None

    payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    payload["last_path"] = "item099.txt"
    forged_cursor = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    ).decode("ascii")

    for root, path, query, candidate in (
        (FileManagerRoot.WORKING, "", None, forged_cursor),
        (FileManagerRoot.UPLOAD, "", None, cursor),
        (FileManagerRoot.WORKING, "folder", None, cursor),
        (FileManagerRoot.WORKING, "", "item", cursor),
        (FileManagerRoot.WORKING, "missing", None, cursor),
    ):
        with pytest.raises(FileManagerPathError):
            service.list_directory(root, path, cursor=candidate, query=query)


def test_missing_root_validates_a_cross_context_cursor_before_returning_empty(
    tmp_path: Path,
) -> None:
    for index in range(FILE_MANAGER_PAGE_SIZE + 1):
        (tmp_path / f"item{index:03d}.txt").write_text("x", encoding="utf-8")
        os.utime(
            tmp_path / f"item{index:03d}.txt",
            ns=(1_700_000_000_000_000_000,) * 2,
        )
    service = _service(tmp_path)
    cursor = service.list_directory(FileManagerRoot.WORKING).next_cursor
    assert cursor is not None

    with pytest.raises(FileManagerPathError):
        service.list_directory(FileManagerRoot.UPLOAD, cursor=cursor)


def test_listing_filters_direct_children_case_insensitively(
    tmp_path: Path,
) -> None:
    (tmp_path / "Report.MD").write_text("report", encoding="utf-8")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "nested-report.md").write_text(
        "nested",
        encoding="utf-8",
    )
    (tmp_path / "other.txt").write_text("other", encoding="utf-8")

    listing = _service(tmp_path).list_directory(
        FileManagerRoot.WORKING,
        query="REpOrT",
    )

    assert {item.name for item in listing.items} == {"reports", "Report.MD"}


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../outside", "folder/../../outside"],
)
def test_rejects_absolute_and_escaping_paths(
    tmp_path: Path,
    path: str,
) -> None:
    with pytest.raises(FileManagerPathError):
        _service(tmp_path).resolve_path(FileManagerRoot.WORKING, path)


def test_rejects_backslash_paths(tmp_path: Path) -> None:
    with pytest.raises(FileManagerPathError):
        _service(tmp_path).resolve_path(
            FileManagerRoot.WORKING,
            r"folder\\file.txt",
        )


def test_rejects_recycle_paths_in_this_service(tmp_path: Path) -> None:
    with pytest.raises(FileManagerPathError):
        _service(tmp_path).resolve_path(
            FileManagerRoot.RECYCLE,
            "archived-file",
        )


@pytest.mark.parametrize(
    "path",
    ["sessions", "sessions/chat.json", "governance/archive"],
)
def test_working_root_rejects_hidden_controlled_paths(
    tmp_path: Path,
    path: str,
) -> None:
    with pytest.raises(FileManagerPathError):
        _service(tmp_path).resolve_path(FileManagerRoot.WORKING, path)


@pytest.mark.parametrize(
    ("root", "directory"),
    [
        (FileManagerRoot.WORKING, ""),
        (FileManagerRoot.UPLOAD, "media"),
        (FileManagerRoot.DOWNLOAD, "static"),
        (FileManagerRoot.CONVERSATION, "sessions"),
    ],
)
def test_resolves_each_normal_root_from_workspace(
    tmp_path: Path,
    root: FileManagerRoot,
    directory: str,
) -> None:
    if directory:
        (tmp_path / directory).mkdir()

    relative_path, resolved_path = _service(tmp_path).resolve_path(root, "")

    assert relative_path == ""
    assert resolved_path == tmp_path / directory


def test_symlink_is_listed_without_capabilities_and_never_followed(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("do not expose", encoding="utf-8")
    (tmp_path / "outside-link").symlink_to(outside)

    service = _service(tmp_path)
    item = service.list_directory(FileManagerRoot.WORKING).items[0]

    assert item.kind == "symlink"
    assert item.capabilities.model_dump() == {
        "browse": False,
        "read": False,
        "upload": False,
        "edit": False,
        "download": False,
        "archive": False,
    }
    with pytest.raises(FileManagerPathError):
        service.resolve_path(FileManagerRoot.WORKING, "outside-link")


def test_conversation_root_is_read_and_download_only(tmp_path: Path) -> None:
    (tmp_path / "sessions").mkdir()

    capabilities = (
        _service(tmp_path)
        .list_directory(
            FileManagerRoot.CONVERSATION,
        )
        .capabilities
    )

    assert capabilities.model_dump() == {
        "browse": True,
        "read": True,
        "upload": False,
        "edit": False,
        "download": True,
        "archive": False,
    }


def test_file_items_do_not_expose_directory_browse_or_upload_actions(
    tmp_path: Path,
) -> None:
    (tmp_path / "document.txt").write_text("text", encoding="utf-8")

    item = _service(tmp_path).list_directory(FileManagerRoot.WORKING).items[0]

    assert item.capabilities.browse is False
    assert item.capabilities.upload is False


@pytest.mark.parametrize(
    "root",
    [
        FileManagerRoot.WORKING,
        FileManagerRoot.UPLOAD,
        FileManagerRoot.DOWNLOAD,
    ],
)
def test_mutable_roots_expose_the_full_directory_capability_set(
    tmp_path: Path,
    root: FileManagerRoot,
) -> None:
    if root is FileManagerRoot.UPLOAD:
        (tmp_path / "media").mkdir()
    if root is FileManagerRoot.DOWNLOAD:
        (tmp_path / "static").mkdir()

    capabilities = _service(tmp_path).list_directory(root).capabilities

    assert capabilities.model_dump() == {
        "browse": True,
        "read": True,
        "upload": True,
        "edit": True,
        "download": True,
        "archive": True,
    }


def test_recycle_root_exposes_only_listing_capability() -> None:
    assert root_capabilities(FileManagerRoot.RECYCLE).model_dump() == {
        "browse": True,
        "read": False,
        "upload": False,
        "edit": False,
        "download": False,
        "archive": False,
    }


def test_text_preview_reads_at_most_one_megabyte_and_marks_large_text_read_only(
    tmp_path: Path,
) -> None:
    full_text = "a" * (1024 * 1024)
    (tmp_path / "large.html").write_text(full_text + "tail", encoding="utf-8")

    preview = _service(tmp_path).read_text_preview(
        FileManagerRoot.WORKING,
        "large.html",
    )

    assert preview.is_text is True
    assert preview.is_truncated is True
    assert preview.editable is False
    assert preview.content == full_text
    assert preview.size_bytes == 1024 * 1024 + 4


def test_small_utf8_text_is_fully_read_and_editable(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("你好，Markdown", encoding="utf-8")

    preview = _service(tmp_path).read_text_preview(
        FileManagerRoot.WORKING,
        "README.md",
    )

    assert preview.content == "你好，Markdown"
    assert preview.is_text is True
    assert preview.is_truncated is False
    assert preview.editable is True


@pytest.mark.parametrize(
    "content",
    [b"text\x07bell", b"text\x1bescape", b"text\x7fdelete"],
)
def test_utf8_files_with_disallowed_control_characters_are_not_editable_text(
    tmp_path: Path,
    content: bytes,
) -> None:
    (tmp_path / "control.txt").write_bytes(content)

    preview = _service(tmp_path).read_text_preview(
        FileManagerRoot.WORKING,
        "control.txt",
    )

    assert preview.is_text is False
    assert preview.content is None
    assert preview.editable is False


def test_large_text_rejects_invalid_utf8_instead_of_ignoring_it(
    tmp_path: Path,
) -> None:
    prefix = b"a" * (1024 * 1024 - 1)
    (tmp_path / "invalid.txt").write_bytes(prefix + b"\xfftail")

    preview = _service(tmp_path).read_text_preview(
        FileManagerRoot.WORKING,
        "invalid.txt",
    )

    assert preview.is_text is False
    assert preview.content is None
    assert preview.editable is False


def test_large_utf8_with_codepoint_started_after_sample_is_still_text(
    tmp_path: Path,
) -> None:
    prefix = b"a" * (1024 * 1024 + 3)
    (tmp_path / "boundary.txt").write_bytes(
        prefix + "😀".encode("utf-8") + b"tail",
    )

    preview = _service(tmp_path).read_text_preview(
        FileManagerRoot.WORKING,
        "boundary.txt",
    )

    assert preview.is_text is True
    assert preview.is_truncated is True
    assert preview.editable is False
    assert preview.content == "a" * (1024 * 1024)


def test_large_invalid_utf8_after_sample_boundary_stays_read_only_text(
    tmp_path: Path,
) -> None:
    prefix = b"a" * (1024 * 1024 + 3)
    (tmp_path / "invalid-boundary.txt").write_bytes(
        prefix + b"\xf0\x28invalid",
    )

    preview = _service(tmp_path).read_text_preview(
        FileManagerRoot.WORKING,
        "invalid-boundary.txt",
    )

    assert preview.is_text is True
    assert preview.content == "a" * (1024 * 1024)


def test_large_text_with_invalid_utf8_after_preview_stays_read_only_text(
    tmp_path: Path,
) -> None:
    (tmp_path / "late-invalid.txt").write_bytes(
        b"a" * (1024 * 1024 + 1024) + b"\xff",
    )

    preview = _service(tmp_path).read_text_preview(
        FileManagerRoot.WORKING,
        "late-invalid.txt",
    )

    assert preview.is_text is True
    assert preview.content == "a" * (1024 * 1024)
    assert preview.editable is False


def test_file_read_rejects_a_symlink_without_following_its_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("private", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(target)

    with pytest.raises(FileManagerPathError):
        _service(tmp_path).read_text_preview(
            FileManagerRoot.WORKING,
            "link.txt",
        )


@pytest.mark.parametrize(
    "operation",
    [
        lambda service: service.list_directory("unknown"),
        lambda service: service.list_directory(
            FileManagerRoot.WORKING,
            "missing",
        ),
        lambda service: service.read_text_preview(
            FileManagerRoot.WORKING,
            "missing.txt",
        ),
    ],
)
def test_unknown_or_missing_paths_raise_stable_file_manager_errors(
    tmp_path: Path,
    operation,
) -> None:
    with pytest.raises(FileManagerPathError):
        operation(_service(tmp_path))


def test_directory_listing_has_no_hard_scan_limit(
    tmp_path: Path,
) -> None:
    for name in ("one.txt", "two.txt", "three.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")
        os.utime(tmp_path / name, ns=(1_700_000_000_000_000_000,) * 2)

    listing = _service(tmp_path).list_directory(FileManagerRoot.WORKING)

    assert [item.name for item in listing.items] == [
        "one.txt",
        "three.txt",
        "two.txt",
    ]


def test_permission_failures_are_translated_to_file_manager_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("swe.app.file_manager.os.open", denied)

    with pytest.raises(FileManagerPathError):
        _service(tmp_path).list_directory(FileManagerRoot.WORKING)


def test_read_snapshot_has_bounded_preview_and_revision_hook(
    tmp_path: Path,
) -> None:
    (tmp_path / "document.md").write_text("content", encoding="utf-8")

    snapshot = _service(tmp_path).read_file_snapshot(
        FileManagerRoot.WORKING,
        "document.md",
    )

    assert snapshot.preview_bytes == b"content"
    assert snapshot.revision == hashlib.sha256(b"content").hexdigest()
    assert snapshot.size_bytes == len(snapshot.preview_bytes)


def test_save_text_requires_matching_revision_and_replaces_safely(
    tmp_path: Path,
) -> None:
    path = tmp_path / "document.md"
    path.write_text("before", encoding="utf-8")
    service = _service(tmp_path)
    revision = service.read_text_preview("working", "document.md").revision

    result = service.save_text(
        "working",
        "document.md",
        "after",
        revision,
    )

    assert path.read_text(encoding="utf-8") == "after"
    assert result.revision != revision
    with pytest.raises(FileManagerConflictError):
        service.save_text("working", "document.md", "lost", revision)
    assert path.read_text(encoding="utf-8") == "after"


def test_save_text_does_not_reread_after_successful_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.md"
    path.write_text("before", encoding="utf-8")
    service = _service(tmp_path)
    revision = service.read_text_preview("working", "document.md").revision
    original_read_text_preview = service.read_text_preview
    preview_calls: list[tuple[object, ...]] = []

    def track_read_text_preview(*args: object, **kwargs: object):
        preview_calls.append(args)
        return original_read_text_preview(*args, **kwargs)

    monkeypatch.setattr(service, "read_text_preview", track_read_text_preview)

    result = service.save_text("working", "document.md", "after", revision)

    assert result.content == "after"
    assert result.revision == hashlib.sha256(b"after").hexdigest()
    assert preview_calls == []


def test_content_revision_rejects_same_stat_content_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "document.md"
    path.write_text("before", encoding="utf-8")
    service = _service(tmp_path)
    revision = service.read_text_preview("working", "document.md").revision
    original_stat = path.stat()

    path.write_text("after!", encoding="utf-8")
    os.utime(
        path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )

    with pytest.raises(FileManagerConflictError):
        service.save_text("working", "document.md", "lost", revision)


def test_archive_rejects_fifo_without_opening_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fifo = tmp_path / "events.pipe"
    os.mkfifo(fifo)
    service = _service(tmp_path)
    original_open = file_manager.os.open

    def fail_if_fifo_open(path, *args, **kwargs):
        if path == "events.pipe":
            raise AssertionError("FIFO must not be opened")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(file_manager.os, "open", fail_if_fifo_open)
    listing = service.list_directory("working")

    assert listing.items[0].kind is FileManagerItemKind.SPECIAL
    assert listing.items[0].capabilities.model_dump() == {
        "browse": False,
        "read": False,
        "upload": False,
        "edit": False,
        "download": False,
        "archive": False,
    }
    with pytest.raises(FileManagerPathError):
        service.archive_file("working", "events.pipe", actor="tester")


@pytest.mark.parametrize("operation", ["archive", "restore", "purge"])
def test_recycle_mutations_restore_visible_state_when_index_save_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    path = tmp_path / "report.txt"
    path.write_text("report", encoding="utf-8")
    service = _service(tmp_path)
    archived = None
    if operation != "archive":
        archived = service.archive_file(
            "working",
            "report.txt",
            actor="tester",
        )

    def fail_index_save(*args, **kwargs):
        raise OSError("index unavailable")

    monkeypatch.setattr(service, "_save_archive_index", fail_index_save)
    with pytest.raises((FileManagerPathError, OSError)):
        if operation == "archive":
            service.archive_file("working", "report.txt", actor="tester")
        elif operation == "restore":
            assert archived is not None
            service.restore_recycle_item(
                archived.archive_item_id,
                actor="tester",
            )
        else:
            assert archived is not None
            service.purge_recycle_item(
                archived.archive_item_id,
                actor="tester",
            )

    if operation == "archive":
        assert path.read_text(encoding="utf-8") == "report"
        assert not (
            tmp_path / "governance" / "archive" / "files"
        ).exists() or not list(
            (tmp_path / "governance" / "archive" / "files").iterdir(),
        )
    elif operation == "restore":
        assert not path.exists()
        assert archived is not None
        assert (
            tmp_path
            / "governance"
            / "archive"
            / "files"
            / archived.archive_item_id
        ).is_file()
    else:
        assert archived is not None
        assert (
            tmp_path
            / "governance"
            / "archive"
            / "files"
            / archived.archive_item_id
        ).is_file()


@pytest.mark.parametrize(
    "root,path,content",
    [
        ("conversation", "chat.txt", "no"),
        ("working", "binary.bin", "no"),
        ("working", "large.txt", "no"),
    ],
)
def test_save_text_rejects_noneditable_file_snapshots(
    tmp_path: Path,
    root: str,
    path: str,
    content: str,
) -> None:
    if root == "conversation":
        (tmp_path / "sessions").mkdir()
        target = tmp_path / "sessions" / path
        target.write_text("chat", encoding="utf-8")
    else:
        target = tmp_path / path
        target.write_bytes(
            (
                b"\x00binary"
                if path.endswith(".bin")
                else b"x" * (1024 * 1024 + 1)
            ),
        )
    service = _service(tmp_path)
    revision = service.read_text_preview(root, path).revision

    with pytest.raises(FileManagerPathError):
        service.save_text(root, path, content, revision)


def test_upload_rejects_collisions_and_never_writes_through_links(
    tmp_path: Path,
) -> None:
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "exists.txt").write_text("old", encoding="utf-8")
    outside = tmp_path.parent / "file-manager-upload-outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "media" / "link.txt").symlink_to(outside)
    service = _service(tmp_path)

    with pytest.raises(FileManagerConflictError):
        service.upload_bytes("upload", "", "exists.txt", b"new")
    with pytest.raises(FileManagerConflictError):
        service.upload_bytes("upload", "", "link.txt", b"new")
    uploaded = service.upload_bytes("upload", "", "fresh.txt", b"fresh")

    assert uploaded.path == "fresh.txt"
    assert (tmp_path / "media" / "fresh.txt").read_bytes() == b"fresh"
    assert outside.read_text(encoding="utf-8") == "outside"


def test_upload_stream_reads_bounded_chunks_and_cleans_up_over_limit(
    tmp_path: Path,
) -> None:
    (tmp_path / "media").mkdir()
    service = _service(tmp_path)

    class RecordingSource(io.BytesIO):
        def __init__(self, data: bytes) -> None:
            super().__init__(data)
            self.read_sizes: list[int] = []

        def read(self, size: int | None = -1) -> bytes:
            assert size is not None and size > 0
            self.read_sizes.append(size)
            return super().read(size)

    source = RecordingSource(b"x" * (file_manager._FILE_READ_CHUNK_BYTES + 1))
    item = service.upload_stream("upload", "", "chunked.bin", source)

    assert (tmp_path / "media" / item.name).read_bytes() == source.getvalue()
    assert set(source.read_sizes) == {file_manager._FILE_READ_CHUNK_BYTES}

    with pytest.raises(FileManagerPathError, match="File too large"):
        service.upload_stream(
            "upload",
            "",
            "too-large.bin",
            io.BytesIO(b"x" * (10 * 1024 * 1024 + 1)),
        )
    assert not (tmp_path / "media" / "too-large.bin").exists()
    assert not list((tmp_path / "media").glob(".too-large.bin.file-manager-*"))


@pytest.mark.parametrize("root", ["conversation", "recycle"])
def test_upload_rejects_read_only_roots(tmp_path: Path, root: str) -> None:
    (tmp_path / "sessions").mkdir()
    with pytest.raises(FileManagerPathError):
        _service(tmp_path).upload_bytes(root, "", "upload.txt", b"body")


def test_archive_restore_and_purge_recycle_items_without_exposing_payload_paths(
    tmp_path: Path,
) -> None:
    path = tmp_path / "MEMORY.md"
    path.write_text("user editable through file manager", encoding="utf-8")
    service = _service(tmp_path)

    archived = service.archive_file("working", "MEMORY.md", actor="tester")
    listing = service.list_directory("recycle")

    assert not path.exists()
    assert listing.items[0].archive_item_id == archived.archive_item_id
    assert listing.items[0].original_path == "MEMORY.md"
    assert listing.items[0].archived_at is not None
    assert "governance/archive/files" not in listing.items[0].model_dump_json()

    (tmp_path / "MEMORY.md").write_text("collision", encoding="utf-8")
    with pytest.raises(FileManagerConflictError):
        service.restore_recycle_item(archived.archive_item_id, actor="tester")
    assert (
        listing.items[0].archive_item_id
        == service.list_directory("recycle").items[0].archive_item_id
    )
    path.unlink()

    restored = service.restore_recycle_item(
        archived.archive_item_id,
        actor="tester",
    )
    assert restored.original_path == "MEMORY.md"
    assert (
        path.read_text(encoding="utf-8")
        == "user editable through file manager"
    )

    second = service.archive_file("working", "MEMORY.md", actor="tester")
    service.purge_recycle_item(second.archive_item_id, actor="tester")
    assert service.list_directory("recycle").items == []


def test_recycle_listing_skips_malformed_archive_index_rows(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    archive_dir = tmp_path / "governance" / "archive"
    archive_dir.mkdir(parents=True)
    archive_item_id = "1" * 32
    (archive_dir / "index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {
                        "id": archive_item_id,
                        "original_path": "reports/valid.txt",
                        "archive_path": (
                            f"{file_manager.ARCHIVE_FILES_DIR}/{archive_item_id}"
                        ),
                        "archived_at": "2026-07-30T00:00:00+00:00",
                        "size_bytes": 12,
                    },
                    {"id": "", "original_path": "bad.txt"},
                    "not-a-record",
                ],
            },
        ),
        encoding="utf-8",
    )

    listing = service.list_directory("recycle")

    assert [item.archive_item_id for item in listing.items] == [
        archive_item_id,
    ]


def test_recycle_listing_pages_through_a_stable_snapshot(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    archive_dir = tmp_path / "governance" / "archive"
    archive_dir.mkdir(parents=True)
    rows = [
        {
            "id": f"{index:032x}",
            "original_path": f"report-{index:03d}.txt",
            "archive_path": (f"{file_manager.ARCHIVE_FILES_DIR}/{index:032x}"),
            "archived_at": (
                f"2026-07-30T00:{index // 60:02d}:{index % 60:02d}+00:00"
            ),
            "size_bytes": index,
        }
        for index in range(FILE_MANAGER_PAGE_SIZE + 1)
    ]
    (archive_dir / "index.json").write_text(
        json.dumps({"version": 1, "items": rows}),
        encoding="utf-8",
    )

    first = service.list_directory("recycle")
    assert first.next_cursor is not None
    second = service.list_directory("recycle", cursor=first.next_cursor)

    assert len(first.items) == FILE_MANAGER_PAGE_SIZE
    assert second.next_cursor is None
    assert {
        item.archive_item_id for item in [*first.items, *second.items]
    } == {row["id"] for row in rows}


@pytest.mark.parametrize("operation", ["archive", "restore", "purge"])
def test_recycle_unlink_then_fsync_failure_keeps_committed_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    path = tmp_path / "report.txt"
    path.write_text("report", encoding="utf-8")
    service = _service(tmp_path)
    archived = None
    if operation != "archive":
        archived = service.archive_file(
            "working",
            "report.txt",
            actor="tester",
        )

    original_unlink = file_manager.os.unlink
    original_fsync = file_manager.os.fsync
    unlinked_target = False

    def track_unlink(name, *args, **kwargs):
        nonlocal unlinked_target
        if name == (
            "report.txt"
            if operation == "archive"
            else archived.archive_item_id
        ):
            unlinked_target = True
        return original_unlink(name, *args, **kwargs)

    def fail_fsync_after_unlink(descriptor):
        if unlinked_target:
            raise OSError("directory durability is uncertain")
        return original_fsync(descriptor)

    monkeypatch.setattr(file_manager.os, "unlink", track_unlink)
    monkeypatch.setattr(file_manager.os, "fsync", fail_fsync_after_unlink)

    with pytest.raises(FileManagerOutcomeUncertainError):
        if operation == "archive":
            service.archive_file("working", "report.txt", actor="tester")
        elif operation == "restore":
            assert archived is not None
            service.restore_recycle_item(
                archived.archive_item_id,
                actor="tester",
            )
        else:
            assert archived is not None
            service.purge_recycle_item(
                archived.archive_item_id,
                actor="tester",
            )

    monkeypatch.setattr(file_manager.os, "fsync", original_fsync)

    if operation == "archive":
        assert not path.exists()
        assert len(service.list_directory("recycle").items) == 1
    elif operation == "restore":
        assert path.read_text(encoding="utf-8") == "report"
        assert service.list_directory("recycle").items == []
    else:
        assert not path.exists()
        assert service.list_directory("recycle").items == []


def test_recycle_index_symlink_is_rejected_without_following_target(
    tmp_path: Path,
) -> None:
    archive_dir = tmp_path / "governance" / "archive"
    archive_dir.mkdir(parents=True)
    outside = tmp_path.parent / "file-manager-outside-index.json"
    outside.write_text('{"version": 1, "items": []}', encoding="utf-8")
    (archive_dir / "index.json").symlink_to(outside)

    with pytest.raises(FileManagerPathError):
        _service(tmp_path).list_directory("recycle")


@pytest.mark.parametrize("operation", ["archive", "restore", "purge"])
def test_restart_recovers_index_publish_fsync_uncertainty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    path = tmp_path / "report.txt"
    path.write_text("report", encoding="utf-8")
    service = _service(tmp_path)
    archived = None
    if operation != "archive":
        archived = service.archive_file(
            "working",
            "report.txt",
            actor="tester",
        )

    original_replace = file_manager.os.replace
    original_fsync = file_manager.os.fsync
    replace_count = 0

    def count_index_publish(*args, **kwargs):
        nonlocal replace_count
        replace_count += 1
        return original_replace(*args, **kwargs)

    def fail_second_index_fsync(descriptor):
        if replace_count >= 2:
            raise OSError("index durability is uncertain")
        return original_fsync(descriptor)

    monkeypatch.setattr(file_manager.os, "replace", count_index_publish)
    monkeypatch.setattr(file_manager.os, "fsync", fail_second_index_fsync)
    with pytest.raises(FileManagerOutcomeUncertainError):
        if operation == "archive":
            service.archive_file("working", "report.txt", actor="tester")
        elif operation == "restore":
            assert archived is not None
            service.restore_recycle_item(
                archived.archive_item_id,
                actor="tester",
            )
        else:
            assert archived is not None
            service.purge_recycle_item(
                archived.archive_item_id,
                actor="tester",
            )

    monkeypatch.setattr(file_manager.os, "fsync", original_fsync)
    recovered = _service(tmp_path).list_directory("recycle")
    if operation == "restore":
        assert path.read_text(encoding="utf-8") == "report"
        assert (
            tmp_path
            / "governance"
            / "archive"
            / "files"
            / archived.archive_item_id
        ).read_text(encoding="utf-8") == "report"
        assert len(recovered.items) == 1
    else:
        assert len(recovered.items) == 1


def test_save_and_upload_publish_fsync_failures_are_outcome_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "note.txt").write_text("before", encoding="utf-8")
    (tmp_path / "media").mkdir()
    service = _service(tmp_path)
    revision = service.read_text_preview("working", "note.txt").revision
    original_replace = file_manager.os.replace
    original_fsync = file_manager.os.fsync
    save_published = False

    def track_replace(*args, **kwargs):
        nonlocal save_published
        save_published = True
        return original_replace(*args, **kwargs)

    def fail_after_save_publish(descriptor):
        if save_published:
            raise OSError("save durability is uncertain")
        return original_fsync(descriptor)

    monkeypatch.setattr(file_manager.os, "replace", track_replace)
    monkeypatch.setattr(file_manager.os, "fsync", fail_after_save_publish)
    with pytest.raises(FileManagerOutcomeUncertainError):
        service.save_text("working", "note.txt", "after", revision)
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "after"

    monkeypatch.setattr(file_manager.os, "fsync", original_fsync)
    original_link = file_manager.os.link
    upload_published = False

    def track_link(*args, **kwargs):
        nonlocal upload_published
        result = original_link(*args, **kwargs)
        upload_published = True
        return result

    def fail_after_upload_publish(descriptor):
        if upload_published:
            raise OSError("upload durability is uncertain")
        return original_fsync(descriptor)

    monkeypatch.setattr(file_manager.os, "link", track_link)
    monkeypatch.setattr(file_manager.os, "fsync", fail_after_upload_publish)
    with pytest.raises(FileManagerOutcomeUncertainError):
        service.upload_bytes("upload", "", "published.txt", b"body")
    assert (tmp_path / "media" / "published.txt").read_bytes() == b"body"


def test_restore_transition_keeps_payload_when_target_was_created_externally(
    tmp_path: Path,
) -> None:
    path = tmp_path / "report.txt"
    path.write_text("report", encoding="utf-8")
    service = _service(tmp_path)
    archived = service.archive_file("working", "report.txt", actor="tester")
    index = service._load_archive_index()
    item = next(
        row for row in index["items"] if row["id"] == archived.archive_item_id
    )
    prepared = dict(index)
    prepared["transition"] = {"operation": "restore", "item": item}
    service._save_archive_index(prepared)
    path.write_text("external replacement", encoding="utf-8")

    recovered = _service(tmp_path).list_directory("recycle")

    assert [row.archive_item_id for row in recovered.items] == [
        archived.archive_item_id,
    ]
    assert (
        tmp_path
        / "governance"
        / "archive"
        / "files"
        / archived.archive_item_id
    ).read_text(encoding="utf-8") == "report"


def test_concurrent_archives_do_not_drop_archive_index_items(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    for index in range(8):
        (tmp_path / f"report-{index}.txt").write_text(
            "report",
            encoding="utf-8",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(
                service.archive_file,
                "working",
                f"report-{index}.txt",
                actor="tester",
            )
            for index in range(8)
        ]
        archived = [future.result() for future in futures]

    listed = service.list_directory("recycle")
    assert {item.archive_item_id for item in listed.items} == {
        item.archive_item_id for item in archived
    }


def test_concurrent_saves_with_one_revision_allow_only_one_winner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "note.txt"
    path.write_text("before", encoding="utf-8")
    service = _service(tmp_path)
    revision = service.read_text_preview("working", "note.txt").revision

    def save(content: str) -> str:
        try:
            service.save_text("working", "note.txt", content, revision)
        except FileManagerConflictError:
            return "conflict"
        return "saved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(save, ("first", "second")))

    assert sorted(outcomes) == ["conflict", "saved"]
    assert path.read_text(encoding="utf-8") in {"first", "second"}

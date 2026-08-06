# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

logger = logging.getLogger(__name__)

KEEP_FILES = {
    "MEMORY.md",
    "AGENTS.md",
    "SOUL.md",
    "PROFILE.md",
    "HEARTBEAT.md",
    "BOOTSTRAP.md",
    "agent.json",
    "chats.json",
    "jobs.json",
    "system_jobs.json",
    "token_usage.json",
    "dream_logs.json",
    "swe_file_metadata.json",
    "skill.json",
}

KEEP_DIRS = {
    "memory",
    "sessions",
    "backup",
    "skills",
    "governance",
    "hooks",
    "dialog",
}

ARCHIVE_FILES_DIR = "governance/archive/files"
ARCHIVE_INDEX_FILE = "governance/archive/index.json"
PROTECTED_PATHS_FILE = "governance/archive/protected_paths.json"


@dataclass(frozen=True)
class OrphanFile:
    filename: str
    size: int
    created_at: str
    modified_at: str
    path: str
    full_path: str


@dataclass(frozen=True)
class WorkspaceArchiveMaintenanceResult:
    archived_items: list[dict[str, Any]] = field(default_factory=list)
    archived_paths: list[str] = field(default_factory=list)
    archived_size_bytes: int = 0
    candidates_count: int = 0
    skipped_files: int = 0
    errors: list[str] = field(default_factory=list)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _load_json_file(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except Exception as exc:
        logger.warning("Failed to load json file %s: %s", path, exc)
        return default


def _archive_index_path(workspace_dir: Path) -> Path:
    return workspace_dir / ARCHIVE_INDEX_FILE


def _protected_paths_path(workspace_dir: Path) -> Path:
    return workspace_dir / PROTECTED_PATHS_FILE


def load_archive_index(workspace_dir: Path) -> dict[str, Any]:
    data = _load_json_file(
        _archive_index_path(workspace_dir),
        {"version": 1, "items": []},
    )
    if not isinstance(data.get("items"), list):
        data["items"] = []
    data["version"] = 1
    return data


def save_archive_index(workspace_dir: Path, data: dict[str, Any]) -> None:
    data["version"] = 1
    if not isinstance(data.get("items"), list):
        data["items"] = []
    _atomic_write_json(_archive_index_path(workspace_dir), data)


def load_protected_paths(workspace_dir: Path) -> dict[str, Any]:
    data = _load_json_file(
        _protected_paths_path(workspace_dir),
        {"version": 1, "paths": []},
    )
    if not isinstance(data.get("paths"), list):
        data["paths"] = []
    data["version"] = 1
    return data


def save_protected_paths(workspace_dir: Path, data: dict[str, Any]) -> None:
    data["version"] = 1
    if not isinstance(data.get("paths"), list):
        data["paths"] = []
    _atomic_write_json(_protected_paths_path(workspace_dir), data)


def normalise_workspace_relative_path(filepath: str) -> str:
    raw_path = Path(filepath)
    if raw_path.is_absolute():
        raise HTTPException(status_code=403, detail="Access denied")
    parts = [part for part in raw_path.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise HTTPException(status_code=403, detail="Access denied")
    if any(part.startswith(".") for part in parts):
        raise HTTPException(status_code=403, detail="Access denied")
    return "/".join(parts)


def is_root_keep_file(relative_path: str) -> bool:
    parts = relative_path.split("/")
    return len(parts) == 1 and parts[0] in KEEP_FILES


def is_keep_dir_path(relative_path: str) -> bool:
    first = relative_path.split("/", 1)[0]
    return first in KEEP_DIRS


def protected_path_set(workspace_dir: Path) -> set[str]:
    data = load_protected_paths(workspace_dir)
    protected: set[str] = set()
    for item in data.get("paths", []):
        if not isinstance(item, dict) or not item.get("path"):
            continue
        try:
            protected.add(
                normalise_workspace_relative_path(str(item["path"])),
            )
        except HTTPException:
            continue
    return protected


def is_protected_path(workspace_dir: Path, relative_path: str) -> bool:
    return relative_path in protected_path_set(workspace_dir)


def resolve_workspace_file(
    workspace_dir: Path,
    filepath: str,
    *,
    allow_protected: bool = False,
) -> tuple[str, Path]:
    relative_path = normalise_workspace_relative_path(filepath)
    if is_root_keep_file(relative_path) or is_keep_dir_path(relative_path):
        raise HTTPException(status_code=403, detail="Protected path")
    if not allow_protected and is_protected_path(workspace_dir, relative_path):
        raise HTTPException(status_code=409, detail="File is protected")
    file_path = workspace_dir / Path(*relative_path.split("/"))
    try:
        file_path.resolve().relative_to(workspace_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    return relative_path, file_path


def scan_orphan_files(workspace_dir: Path) -> list[OrphanFile]:
    orphan_files: list[OrphanFile] = []
    if not workspace_dir.exists():
        return orphan_files

    protected = protected_path_set(workspace_dir)

    def scan_directory(dir_path: Path, relative_base: Path) -> None:
        try:
            for item in dir_path.iterdir():
                if item.name.startswith("."):
                    continue
                if item.is_dir():
                    if item.name in KEEP_DIRS and dir_path == workspace_dir:
                        continue
                    scan_directory(item, relative_base)
                    continue
                if item.is_file() and dir_path == workspace_dir:
                    if item.name in KEEP_FILES:
                        continue
                if not item.is_file():
                    continue
                try:
                    stat = item.stat()
                    relative_path = str(item.relative_to(relative_base))
                    relative_path = relative_path.replace("\\", "/")
                    if relative_path in protected:
                        continue
                    orphan_files.append(
                        OrphanFile(
                            filename=item.name,
                            size=stat.st_size,
                            created_at=datetime.fromtimestamp(
                                stat.st_ctime,
                            ).isoformat(),
                            modified_at=datetime.fromtimestamp(
                                stat.st_mtime,
                            ).isoformat(),
                            path=relative_path,
                            full_path=str(item),
                        ),
                    )
                except Exception as exc:
                    logger.error("Failed to read file %s: %s", item, exc)
        except Exception as exc:
            logger.error("Failed to scan directory %s: %s", dir_path, exc)

    scan_directory(workspace_dir, workspace_dir)
    orphan_files.sort(key=lambda item: item.modified_at, reverse=True)
    return orphan_files


def archive_workspace_files(
    workspace_dir: Path,
    filepaths: list[str],
    *,
    actor: str,
    reason: str,
) -> list[dict[str, Any]]:
    if not filepaths:
        return []
    index = load_archive_index(workspace_dir)
    items = list(index.get("items") or [])
    archived_items: list[dict[str, Any]] = []
    now = _isoformat(_utc_now())
    for filepath in filepaths:
        relative_path, file_path = resolve_workspace_file(
            workspace_dir,
            filepath,
        )
        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        stat = file_path.stat()
        item_id = uuid.uuid4().hex
        archive_relative_path = f"{ARCHIVE_FILES_DIR}/{item_id}"
        archive_file = workspace_dir / archive_relative_path
        archive_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(file_path), str(archive_file))
        item = {
            "id": item_id,
            "original_path": relative_path,
            "archive_path": archive_relative_path,
            "size_bytes": stat.st_size,
            "mtime": _isoformat(
                datetime.fromtimestamp(stat.st_mtime, timezone.utc),
            ),
            "archived_at": now,
            "archived_by": actor,
            "archive_reason": reason,
        }
        items.append(item)
        archived_items.append(item)

    index["items"] = items
    save_archive_index(workspace_dir, index)
    return archived_items


def old_orphan_file_candidates(
    workspace_dir: Path,
    *,
    old_orphan_days: int,
    now: datetime | None = None,
) -> list[str]:
    cutoff = (now or _utc_now()) - timedelta(days=old_orphan_days)
    candidates: list[str] = []
    for orphan_file in scan_orphan_files(workspace_dir):
        file_path = workspace_dir / Path(*orphan_file.path.split("/"))
        try:
            mtime = datetime.fromtimestamp(
                file_path.stat().st_mtime,
                timezone.utc,
            )
        except OSError:
            continue
        if mtime <= cutoff:
            candidates.append(orphan_file.path)
    return candidates


def archive_old_orphans_for_workspace(
    workspace_dir: Path,
    *,
    old_orphan_days: int,
    max_files: int,
    remaining_files: int,
    actor: str,
    now: datetime | None = None,
) -> WorkspaceArchiveMaintenanceResult:
    candidates = old_orphan_file_candidates(
        workspace_dir,
        old_orphan_days=old_orphan_days,
        now=now,
    )
    limit = max(0, min(max_files, remaining_files))
    selected = candidates[:limit]
    archived_items = archive_workspace_files(
        workspace_dir,
        selected,
        actor=actor,
        reason=f"source_archive_maintenance_mtime_{old_orphan_days}_days",
    )
    archived_paths = [
        str(item.get("original_path") or "") for item in archived_items
    ]
    archived_size_bytes = sum(
        int(item.get("size_bytes") or 0) for item in archived_items
    )
    return WorkspaceArchiveMaintenanceResult(
        archived_items=archived_items,
        archived_paths=archived_paths,
        archived_size_bytes=archived_size_bytes,
        candidates_count=len(candidates),
        skipped_files=max(0, len(candidates) - len(selected)),
    )

# -*- coding: utf-8 -*-
"""Skills management: sync skills from code to working_dir."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shutil
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

import frontmatter
from pydantic import BaseModel, Field
from ..constant import env_var_overrides
from ..security.skill_scanner import scan_skill_directory
from .skill_runtime_snapshot import coordinate_workspace_skill_mutation
from ..security.skill_scanner.safe_unpack import safe_unpack_skill_zip
from .utils.file_handling import read_text_file_with_encoding_fallback
from ..utils.fs_text import (
    log_sanitized_fs_text,
    sanitize_fs_text,
)

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None

if fcntl is None and msvcrt is None:  # pragma: no cover
    raise ImportError(
        "No file locking module available (need fcntl or msvcrt)",
    )

logger = logging.getLogger(__name__)

ALL_SKILL_ROUTING_CHANNELS = [
    "console",
    "discord",
    "telegram",
    "dingtalk",
    "feishu",
    "imessage",
    "qq",
    "mattermost",
    "wecom",
    "mqtt",
]

_RegistryResult = TypeVar("_RegistryResult")
_MAX_ZIP_BYTES = 200 * 1024 * 1024
_ZIP_UTF8_FLAG = 0x800
WORKSPACE_SKILL_LAYOUT_VERSION = 2


def _has_cjk_text(text: str) -> bool:
    """判断字符串是否包含常见中日韩文字。"""
    return any(
        "\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff"
        for char in text
    )


def _decode_zip_member_name(info: zipfile.ZipInfo) -> str:
    """兼容未声明 UTF-8 的中文 zip 成员名。"""
    name = info.filename.replace("\\", "/")
    try:
        raw_name = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    if info.flag_bits & _ZIP_UTF8_FLAG and _has_cjk_text(name):
        return name
    for encoding in ("gb18030", "gbk", "big5", "utf-8"):
        try:
            decoded = raw_name.decode(encoding)
        except UnicodeDecodeError:
            continue
        decoded = decoded.replace("\\", "/")
        if decoded != name and _has_cjk_text(decoded):
            return decoded
    return name


def _extract_zip_member(
    zf: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: Path,
) -> None:
    """按已修复的目标路径解压单个 zip 成员。"""
    if info.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with zf.open(info) as src, target.open("wb") as dst:
        shutil.copyfileobj(src, dst)


class SkillInfo(BaseModel):
    """Workspace or hub skill details returned to callers.

    ``name`` is the stable runtime identifier: the directory / manifest key
    used by APIs, sync state, and channel routing. It is intentionally not
    derived from frontmatter because frontmatter can drift while the on-disk
    workspace identity must remain stable.
    """

    name: str
    description: str = ""
    version_text: str = ""
    content: str
    source: str
    references: dict[str, Any] = Field(default_factory=dict)
    scripts: dict[str, Any] = Field(default_factory=dict)


class SkillRequirements(BaseModel):
    """System-managed requirements declared by a skill."""

    require_bins: list[str] = Field(default_factory=list)
    require_envs: list[str] = Field(default_factory=list)


_BUILTIN_SIGNATURES: dict[str, str] = {}
_BUILTIN_SIG_LOCK = threading.Lock()


def _get_builtin_signatures() -> dict[str, str]:
    """Return cached signatures for all packaged builtin skills.

    Computed once on first access; subsequent calls return the same dict.
    Thread-safe: a local dict is built first, then merged in one shot
    so concurrent callers never observe a partially-filled cache.
    """
    if _BUILTIN_SIGNATURES:
        return _BUILTIN_SIGNATURES
    with _BUILTIN_SIG_LOCK:
        if _BUILTIN_SIGNATURES:
            return _BUILTIN_SIGNATURES
        sigs: dict[str, str] = {}
        builtin_dir = get_builtin_skills_dir()
        if builtin_dir.exists():
            for skill_dir in sorted(builtin_dir.iterdir()):
                if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                    sigs[skill_dir.name] = _build_signature(skill_dir)
        _BUILTIN_SIGNATURES.update(sigs)
    return _BUILTIN_SIGNATURES


def get_builtin_skills_dir() -> Path:
    """Return the packaged built-in skill directory."""
    return Path(__file__).parent / "skills"


def get_skill_pool_dir(
    working_dir: Path | None = None,
) -> Path:
    """Return the local shared skill pool directory."""
    if working_dir is not None:
        return Path(working_dir) / "skill_pool"
    from ..constant import WORKING_DIR

    return Path(WORKING_DIR) / "skill_pool"


def get_workspace_skills_dir(workspace_dir: Path) -> Path:
    """Return the workspace skill source directory."""
    preferred = workspace_dir / "skills"
    legacy = workspace_dir / "skill"
    if preferred.exists():
        return preferred
    if legacy.exists():
        try:
            legacy.rename(preferred)
        except OSError:
            return legacy
    return preferred


def resolve_effective_skill_dir(
    workspace_dir: Path,
    skill_name: str,
) -> Path | None:
    """Resolve the actual directory of an enabled skill.

    Prefer the workspace override when it exists, otherwise fall back to the
    packaged builtin skill directory.
    """
    workspace_skill_dir = get_workspace_skills_dir(workspace_dir) / skill_name
    if workspace_skill_dir.exists():
        return workspace_skill_dir

    builtin_skill_dir = get_builtin_skills_dir() / skill_name
    if builtin_skill_dir.exists():
        return builtin_skill_dir

    return None


def get_workspace_skill_manifest_path(workspace_dir: Path) -> Path:
    """Return the workspace skill manifest path."""
    return Path(workspace_dir) / "skill.json"


def get_workspace_disabled_skills_dir(workspace_dir: Path) -> Path:
    """Return the workspace disabled skill directory."""
    return Path(workspace_dir) / ".disabled_skills"


def resolve_workspace_managed_skill_dir(
    workspace_dir: Path,
    skill_name: str,
    *,
    enabled: bool,
) -> Path:
    """Resolve a managed skill directory from its enablement state."""
    root = (
        get_workspace_skills_dir(workspace_dir)
        if enabled
        else get_workspace_disabled_skills_dir(workspace_dir)
    )
    return root / skill_name


def _has_unmanaged_workspace_skill_conflict(
    workspace_dir: Path,
    skill_name: str,
    workspace_manifest: dict[str, Any],
) -> bool:
    """Return whether an unregistered package occupies either managed root."""
    if skill_name in workspace_manifest.get("skills", {}):
        return False
    return any(
        resolve_workspace_managed_skill_dir(
            workspace_dir,
            skill_name,
            enabled=enabled,
        ).exists()
        for enabled in (True, False)
    )


def _existing_workspace_download_result(
    entry: dict[str, Any],
    existing: dict[str, Any],
    final_name: str,
    workspace_identity: dict[str, str],
) -> dict[str, Any]:
    """Return the outcome when a download targets an existing skill."""
    if (
        entry.get("source") == "builtin"
        and existing.get("source") == "builtin"
    ):
        pool_ver = entry.get("version_text", "")
        ws_ver = (existing.get("metadata") or {}).get("version_text", "")
        if pool_ver and ws_ver and pool_ver == ws_ver:
            return {
                "success": True,
                "mode": "unchanged",
                "name": final_name,
                "workspace_id": workspace_identity["workspace_id"],
                "workspace_name": workspace_identity["workspace_name"],
            }
        return {
            "success": False,
            "reason": "builtin_upgrade",
            "workspace_id": workspace_identity["workspace_id"],
            "workspace_name": workspace_identity["workspace_name"],
            "skill_name": final_name,
        }
    return {
        "success": False,
        "reason": "conflict",
        "workspace_id": workspace_identity["workspace_id"],
        "workspace_name": workspace_identity["workspace_name"],
        "suggested_name": suggest_conflict_name(final_name),
    }


def get_workspace_identity(workspace_dir: Path) -> dict[str, str]:
    """Resolve the workspace id together with its display name."""
    workspace_id = workspace_dir.name
    workspace_name = workspace_id
    try:
        from ..config.config import load_agent_config

        workspace_name = load_agent_config(workspace_id).name or workspace_id
    except Exception:
        pass
    return {
        "workspace_id": workspace_id,
        "workspace_name": workspace_name,
    }


def get_pool_skill_manifest_path(
    working_dir: Path | None = None,
) -> Path:
    """Return the shared pool skill manifest path."""
    return get_skill_pool_dir(working_dir=working_dir) / "skill.json"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _get_skill_mtime(skill_dir: Path) -> float:
    """Return max of SKILL.md mtime and skill_dir mtime.

    Uses two cheap ``stat()`` calls — no file content I/O.
    Returns ``0.0`` on any filesystem error.
    """
    try:
        dir_mtime = skill_dir.stat().st_mtime
        skill_md = skill_dir / "SKILL.md"
        md_mtime = skill_md.stat().st_mtime if skill_md.exists() else 0.0
        return max(dir_mtime, md_mtime)
    except OSError:
        return 0.0


def get_skill_freshness_token(skill_dir: Path) -> str:
    """Return a lightweight recursive freshness token for one skill tree.

    The token is a stable digest over non-ignored file paths and cheap stat
    metadata. Directory mtimes are intentionally excluded so ignored
    OS/cache artifacts do not cause false positives by touching parent
    directories, while deletions and renames still change the token.
    """
    if not skill_dir.exists():
        return "v2:missing"

    digest = hashlib.blake2b(digest_size=16)
    digest.update(b"skill-freshness-v2\0")

    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(skill_dir)
        if _is_ignored_skill_path(rel):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(rel.as_posix().encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")

    return f"v2:{digest.hexdigest()}"


def _directory_tree(directory: Path) -> dict[str, Any]:
    """Recursively describe a directory tree for UI display."""
    tree: dict[str, Any] = {}
    if not directory.exists() or not directory.is_dir():
        return tree

    for item in sorted(directory.iterdir()):
        sanitized = sanitize_fs_text(item.name)
        display_name = sanitized.value
        log_sanitized_fs_text(
            logger,
            source=f"skills.directory_tree:{directory}",
            original=item.name,
            sanitized=sanitized,
        )
        if item.is_file():
            tree[display_name] = None
        elif item.is_dir():
            tree[display_name] = _directory_tree(item)

    return tree


def _sanitize_file_name_for_disk(name: str) -> str:
    """Return a filesystem-safe display name for a single path segment."""
    sanitized = sanitize_fs_text(name)
    return sanitized.value or name


def _sanitize_md_name_for_disk(name: str) -> str:
    """Return a sanitized markdown filename preserving the extension."""
    path = Path(name)
    sanitized_name = _sanitize_file_name_for_disk(path.name)
    if sanitized_name.lower().endswith(".md"):
        return sanitized_name
    return f"{sanitized_name}.md"


def _build_unique_file_name(
    name: str,
    existing_names: set[str],
) -> str:
    """Return a unique filename preserving the original extension."""
    path = Path(name)
    stem = path.stem
    suffix = path.suffix
    candidate = name
    if candidate not in existing_names:
        return candidate
    suggested = suggest_conflict_name(
        stem,
        {Path(n).stem for n in existing_names},
    )
    return f"{suggested}{suffix}"


def _merge_existing_metadata(
    existing: dict[str, Any] | None,
    fresh: dict[str, Any],
) -> dict[str, Any]:
    """Merge legacy metadata keys while preferring fresh computed values."""
    merged = dict(existing or {})
    merged.update(fresh)
    return merged


def _rename_skill_dirs_to_utf8_safe(
    root_dir: Path,
    manifest_path: Path,
    default_payload: dict[str, Any],
) -> dict[str, str]:
    """Rename unsafe top-level skill directory names and migrate manifest keys."""
    root_dir.mkdir(parents=True, exist_ok=True)

    if manifest_path.exists():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            payload = default_payload
    else:
        payload = default_payload

    payload.setdefault("skills", {})
    skills = payload["skills"]

    renamed: dict[str, str] = {}
    existing_names = {
        path.name for path in root_dir.iterdir() if path.is_dir()
    }
    changed = False

    for skill_dir in sorted(root_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        sanitized = sanitize_fs_text(skill_dir.name)
        log_sanitized_fs_text(
            logger,
            source=f"skills.rename_root:{root_dir}",
            original=skill_dir.name,
            sanitized=sanitized,
        )
        if not sanitized.changed or sanitized.value == skill_dir.name:
            continue

        target_name = sanitized.value or skill_dir.name
        existing_names.discard(skill_dir.name)
        if target_name in existing_names:
            target_name = suggest_conflict_name(target_name, existing_names)

        skill_dir.rename(root_dir / target_name)
        existing_names.add(target_name)
        renamed[skill_dir.name] = target_name
        changed = True

        if skill_dir.name in skills:
            skills[target_name] = skills.pop(skill_dir.name)

    if changed:
        _write_json_atomic(manifest_path, payload)

    return renamed


def _resolve_child_by_raw_or_sanitized(
    base_dir: Path,
    requested_name: str,
) -> Path | None:
    """Resolve a direct child using either its raw or sanitized name."""
    children = {child.name: child for child in base_dir.iterdir()}
    exact = children.get(requested_name)
    if exact is not None:
        return exact

    for child_name, child in sorted(children.items()):
        sanitized = sanitize_fs_text(child_name)
        if sanitized.value == requested_name:
            return child
    return None


def _resolve_path_by_raw_or_sanitized(
    base_dir: Path,
    relative_path: str,
) -> Path | None:
    """Resolve a relative path using raw or sanitized names per segment."""
    current = base_dir
    for segment in [part for part in relative_path.split("/") if part]:
        current = _resolve_child_by_raw_or_sanitized(current, segment)
        if current is None:
            return None
    return current


def _read_frontmatter(skill_dir: Path) -> Any:
    """Read and parse SKILL.md frontmatter.

    Args:
        skill_dir: Path to skill directory containing SKILL.md

    Returns:
        Parsed frontmatter as dict-like object
    """
    return frontmatter.loads(
        read_text_file_with_encoding_fallback(skill_dir / "SKILL.md"),
    )


def _read_frontmatter_safe(
    skill_dir: Path,
    skill_name: str = "",
) -> dict[str, Any]:
    """Safely read SKILL.md frontmatter with fallback on errors.

    Args:
        skill_dir: Path to skill directory containing SKILL.md
        skill_name: Optional skill name for logging (defaults to dir name)

    Returns:
        Parsed frontmatter dict, or fallback dict with name/description
        on any error (file not found, YAML syntax error, etc.)
    """
    if not skill_name:
        skill_name = skill_dir.name

    try:
        return _read_frontmatter(skill_dir)
    except Exception as e:
        logger.warning(
            f"Failed to read SKILL.md frontmatter for '{skill_name}' "
            f"at {skill_dir}: {e}. Using fallback values.",
        )
        # Return minimal valid frontmatter
        return {"name": skill_name, "description": ""}


def _extract_version(post: Any) -> str:
    metadata = post.get("metadata") or {}
    for value in (
        post.get("version"),
        metadata.get("version"),
        metadata.get("builtin_skill_version"),
    ):
        if value not in (None, ""):
            return str(value)
    return ""


_IGNORED_SKILL_ARTIFACTS = {
    "__pycache__",
    "__MACOSX",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
}


def _is_ignored_skill_path(path: Path) -> bool:
    return bool(_IGNORED_SKILL_ARTIFACTS & set(path.parts))


def _build_signature(skill_dir: Path) -> str:
    """Hash the full skill tree using real file paths and real contents.

    This is the canonical content identity used by pool sync and conflict
    detection. If any file changes, including ``SKILL.md``, the signature
    changes.

    OS/cache artifacts (``__pycache__``, ``.DS_Store``, etc.) are excluded
    so that the signature stays consistent with ``_copy_skill_dir``.
    """
    digest = hashlib.sha256()
    for path in sorted(p for p in skill_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(skill_dir)
        if _is_ignored_skill_path(rel):
            continue
        digest.update(str(rel).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _copy_skill_dir(source: Path, target: Path) -> None:
    """Replace *target* with a copy of *source*.

    We intentionally filter only well-known OS/cache artifacts so skill
    content behaves consistently on macOS, Windows, Linux, and Docker.
    User-authored dotfiles are preserved.
    """
    if target.exists():
        shutil.rmtree(target)

    def _ignore(_dir: str, names: list[str]) -> set[str]:
        return {name for name in names if name in _IGNORED_SKILL_ARTIFACTS}

    shutil.copytree(
        source,
        target,
        ignore=_ignore,
    )


def _move_skill_dir(source: Path, target: Path) -> None:
    """Move one managed skill directory into its authoritative root."""
    target.parent.mkdir(parents=True, exist_ok=True)
    source.replace(target)


def _reconcile_registered_skill_location(
    workspace_dir: Path,
    skill_name: str,
    entry: dict[str, Any],
) -> Path | None:
    """Place a registered skill in the root selected by its manifest state."""
    active = resolve_workspace_managed_skill_dir(
        workspace_dir,
        skill_name,
        enabled=True,
    )
    disabled = resolve_workspace_managed_skill_dir(
        workspace_dir,
        skill_name,
        enabled=False,
    )

    if active.exists() and disabled.exists():
        shutil.rmtree(disabled)
        entry["enabled"] = True

    desired = resolve_workspace_managed_skill_dir(
        workspace_dir,
        skill_name,
        enabled=bool(entry.get("enabled", False)),
    )
    current = active if active.exists() else disabled
    if not current.exists():
        return None
    if current != desired:
        _move_skill_dir(current, desired)
    return desired


def _lock_path_for(json_path: Path) -> Path:
    return json_path.with_name(f".{json_path.name}.lock")


@contextmanager
def _file_write_lock(lock_path: Path) -> Iterator[None]:
    """Serialize manifest mutations across processes."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def _read_json_unlocked(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Malformed JSON in %s, resetting to default", path)
        return json.loads(json.dumps(default))


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    with _file_write_lock(_lock_path_for(path)):
        return _read_json_unlocked(path, default)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temp_path: Path | None = None
    payload = dict(payload)
    payload["version"] = max(
        int(payload.get("version", 0)) + 1,
        int(datetime.now(timezone.utc).timestamp() * 1000),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=path.parent,
            prefix=f".{path.stem}_",
            suffix=path.suffix,
            delete=False,
            encoding="utf-8",
        ) as handle:
            handle.write(json.dumps(payload, indent=2, ensure_ascii=False))
            temp_path = Path(handle.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def _mutate_json(
    path: Path,
    default: dict[str, Any],
    mutator: Callable[[dict[str, Any]], _RegistryResult],
) -> _RegistryResult:
    # Workspace and pool manifests are both named ``skill.json``.  A
    # process-local coordinator prevents a query snapshot from being
    # published while a manifest mutation is still assembling its payload;
    # the existing file lock continues to provide cross-process exclusion.
    # Do not infer manifest kind from the parent directory name: a perfectly
    # valid workspace may itself be named ``skill_pool``.  Pool callers pass
    # the pool schema default, so additionally verify that the path is the
    # canonical pool manifest for its working directory.
    is_pool_manifest = False
    if (
        path.name == "skill.json"
        and default.get("schema_version") == "skill-pool-manifest.v1"
    ):
        expected_pool_path = get_pool_skill_manifest_path(
            working_dir=path.parent.parent,
        )
        is_pool_manifest = path.expanduser().resolve() == (
            expected_pool_path.expanduser().resolve()
        )
    if path.name == "skill.json" and not is_pool_manifest:
        from .skill_runtime_snapshot import workspace_skill_coordinator

        coordination = workspace_skill_coordinator(path.parent)
    else:
        coordination = nullcontext()
    with coordination:
        with _file_write_lock(_lock_path_for(path)):
            payload = _read_json_unlocked(path, default)
            result = mutator(payload)
            if result is not False:
                _write_json_atomic(path, payload)
            return result


def _default_workspace_manifest() -> dict[str, Any]:
    return {
        "schema_version": "workspace-skill-manifest.v1",
        "layout_version": WORKSPACE_SKILL_LAYOUT_VERSION,
        "version": 0,
        "skills": {},
    }


def _require_workspace_layout_v2(
    payload: object,
    workspace_dir: Path,
) -> None:
    """Reject malformed or non-v2 existing workspace manifests."""
    manifest_path = get_workspace_skill_manifest_path(workspace_dir)
    payload = _require_workspace_manifest_object(payload, manifest_path)
    _require_workspace_layout_version(payload, workspace_dir)
    _require_workspace_skill_entries(payload["skills"], manifest_path)


def _require_workspace_manifest_object(
    payload: object,
    manifest_path: Path,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(
            f"Workspace manifest {manifest_path} must contain a JSON object "
            "at the top level.",
        )
    if not isinstance(payload.get("skills"), dict):
        raise ValueError(
            f"Workspace manifest {manifest_path} field 'skills' must be a "
            "JSON object.",
        )
    return payload


def _require_workspace_layout_version(
    payload: dict[str, Any],
    workspace_dir: Path,
) -> None:
    layout_version = payload.get("layout_version")
    if (
        not isinstance(layout_version, int)
        or isinstance(layout_version, bool)
        or layout_version != WORKSPACE_SKILL_LAYOUT_VERSION
    ):
        rendered_version = (
            repr(layout_version)
            if "layout_version" in payload
            else "<missing>"
        )
        raise ValueError(
            f"Workspace {workspace_dir} has unsupported layout_version "
            f"{rendered_version}. Run `skills migrate-layout --check` "
            "then `skills migrate-layout --apply` before using runtime "
            "skill operations.",
        )


def _require_workspace_skill_entries(
    skills: dict[str, Any],
    manifest_path: Path,
) -> None:
    for skill_name, entry in skills.items():
        _require_workspace_skill_name(skill_name, manifest_path)
        _require_workspace_skill_entry(skill_name, entry, manifest_path)


def _require_workspace_skill_name(
    skill_name: object,
    manifest_path: Path,
) -> None:
    if not isinstance(skill_name, str) or not skill_name:
        raise ValueError(
            f"Workspace manifest {manifest_path} skill name "
            f"{skill_name!r} must be a non-empty string.",
        )
    if (
        Path(skill_name).name != skill_name
        or skill_name in {".", ".."}
        or "/" in skill_name
        or "\\" in skill_name
        or "\x00" in skill_name
    ):
        raise ValueError(
            f"Workspace manifest {manifest_path} skill name "
            f"{skill_name!r} must be a safe single path segment.",
        )


def _require_workspace_skill_entry(
    skill_name: str,
    entry: object,
    manifest_path: Path,
) -> None:
    if not isinstance(entry, dict):
        raise ValueError(
            f"Workspace manifest {manifest_path} skill "
            f"{skill_name!r} entry must be a JSON object.",
        )
    if "enabled" in entry and not isinstance(entry["enabled"], bool):
        raise ValueError(
            f"Workspace manifest {manifest_path} skill "
            f"{skill_name!r} field 'enabled' must be a JSON boolean.",
        )


def _read_workspace_manifest_strict_unlocked(
    manifest_path: Path,
) -> dict[str, Any]:
    """Read one workspace manifest without locking or silent recovery."""
    if not manifest_path.exists():
        return _default_workspace_manifest()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    _require_workspace_layout_v2(payload, manifest_path.parent)
    return payload


def _default_pool_manifest() -> dict[str, Any]:
    return {
        "schema_version": "skill-pool-manifest.v1",
        "version": 0,
        "skills": {},
        "builtin_skill_names": [],
    }


def _is_builtin_skill(skill_name: str, builtin_names: list[str]) -> bool:
    """Check if skill name is in builtin list."""
    return skill_name in builtin_names


def _is_pool_builtin_entry(entry: dict[str, Any] | None) -> bool:
    """Return whether one pool manifest entry represents a builtin slot."""
    return bool(entry) and str(entry.get("source", "") or "") == "builtin"


def _classify_pool_skill_source(
    skill_name: str,
    skill_dir: Path,
    existing: dict[str, Any],
    builtin_names: list[str],
) -> tuple[str, bool]:
    """Classify one pool skill against packaged builtins.

    Preserve the manifest's builtin/customized intent when the entry
    already exists. This lets an outdated builtin remain a builtin slot,
    while same-name customized copies stay customized.
    """
    if not _is_builtin_skill(skill_name, builtin_names):
        return "customized", False

    if skill_name not in _get_builtin_signatures():
        return "customized", False

    if existing:
        if _is_pool_builtin_entry(existing):
            return "builtin", False
        return "customized", False

    pool_signature = _build_signature(skill_dir)
    builtin_signature = _get_builtin_signatures().get(skill_name, "")
    if pool_signature == builtin_signature:
        return "builtin", False
    return "customized", False


def _is_hidden(name: str) -> bool:
    return name in {
        "__pycache__",
        "__MACOSX",
        ".DS_Store",
        "Thumbs.db",
        "desktop.ini",
    }


def _extract_and_validate_zip(data: bytes, tmp_dir: Path) -> None:
    """复用统一安全解包边界，保持原 ZIP 导入入口行为稳定."""
    try:
        safe_unpack_skill_zip(
            data,
            tmp_dir,
            max_uncompressed_bytes=_MAX_ZIP_BYTES,
            decode_member_name=_decode_zip_member_name,
        )
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def _safe_child_path(base_dir: Path, relative_name: str) -> Path:
    """Resolve a relative child path and reject traversal / absolute paths."""
    normalized = (relative_name or "").replace("\\", "/").strip()
    if not normalized:
        raise ValueError("Skill file path cannot be empty")
    if normalized.startswith("/"):
        raise ValueError(f"Absolute path not allowed: {relative_name}")

    path = (base_dir / normalized).resolve()
    base_resolved = base_dir.resolve()
    if not path.is_relative_to(base_resolved):
        raise ValueError(
            f"Unsafe path outside skill directory: {relative_name}",
        )
    return path


def _normalize_skill_dir_name(name: str) -> str:
    """Normalize and validate a skill directory name."""
    normalized = str(name or "").strip()
    if not normalized:
        raise ValueError("Skill name cannot be empty")
    if "\x00" in normalized:
        raise ValueError("Skill name cannot contain NUL bytes")
    if normalized in {".", ".."}:
        raise ValueError(f"Invalid skill name: {normalized}")
    if "/" in normalized or "\\" in normalized:
        raise ValueError(
            "Skill name cannot contain path separators",
        )
    return normalized


def _create_files_from_tree(base_dir: Path, tree: dict[str, Any]) -> None:
    for name, value in (tree or {}).items():
        path = _safe_child_path(base_dir, name)
        if isinstance(value, dict):
            path.mkdir(parents=True, exist_ok=True)
            _create_files_from_tree(path, value)
        elif value is None or isinstance(value, str):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value or "", encoding="utf-8")
        else:
            raise ValueError(f"Invalid tree value for {name}: {type(value)}")


def _resolve_skill_name(skill_dir: Path) -> str:
    """Resolve the import-time target name for one concrete skill directory.

    This helper is intentionally import-oriented. Runtime registration inside a
    workspace still keys skills by directory name; we only consult frontmatter
    here so zip imports behave consistently whether a skill is packed at the
    archive root or nested under a folder.
    """
    post = _read_frontmatter_safe(skill_dir)
    name = str(post.get("name") or "").strip()
    if name:
        return name
    return skill_dir.name


def _extract_requirements(post: dict[str, Any]) -> SkillRequirements:
    """Extract requirements from a parsed frontmatter dict."""
    metadata = post.get("metadata") or {}
    if "openclaw" in metadata:
        requires = metadata["openclaw"].get("requires", {})
    elif "copaw" in metadata:
        requires = metadata["copaw"].get("requires", {})
    else:
        requires = metadata.get(
            "requires",
            post.get("requires", {}),
        )

    if isinstance(requires, list):
        return SkillRequirements(require_bins=list(requires), require_envs=[])

    if not isinstance(requires, dict):
        return SkillRequirements()

    return SkillRequirements(
        require_bins=list(requires.get("bins", [])),
        require_envs=list(requires.get("env", [])),
    )


def _stringify_skill_env_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _skill_config_env_var_name(skill_name: str) -> str:
    normalized = [
        char if char.isalnum() else "_"
        for char in str(skill_name or "").upper()
    ]
    return f"COPAW_SKILL_CONFIG_{''.join(normalized).strip('_') or 'DEFAULT'}"


def _build_skill_config_env_overrides(
    skill_name: str,
    config: dict[str, Any],
    require_envs: list[str],
) -> dict[str, str]:
    """Map config keys to env vars based on ``require_envs``.

    Config keys that match a declared ``require_envs`` entry are
    injected as environment variables.  Keys not in ``require_envs``
    are silently skipped (still available via the full JSON var).
    Missing required keys are logged as warnings.
    """
    overrides: dict[str, str] = {}

    normalized_required_envs = [
        str(env_name).strip()
        for env_name in require_envs
        if str(env_name).strip()
    ]

    required_set = set(normalized_required_envs)
    for key, value in config.items():
        if key not in required_set:
            continue
        if value in (None, ""):
            continue
        overrides[key] = _stringify_skill_env_value(value)

    for env_name in normalized_required_envs:
        if env_name not in overrides:
            logger.warning(
                "Skill '%s' requires env '%s' but config does "
                "not provide it",
                skill_name,
                env_name,
            )

    overrides[_skill_config_env_var_name(skill_name)] = json.dumps(
        config,
        ensure_ascii=False,
    )
    return overrides


@contextmanager
def apply_skill_config_env_overrides(
    workspace_dir: Path,
    channel_name: str,
    *,
    snapshot: Any | None = None,
) -> Iterator[None]:
    """Inject effective skill config into request-scoped env overrides.

    Config keys matching ``metadata.requires.env`` entries are exposed via
    ``EnvVarLoader`` for the current agent turn only. The full config is also
    available as ``COPAW_SKILL_CONFIG_<SKILL_NAME>`` within the same scope.
    """
    if snapshot is None:
        manifest = reconcile_workspace_manifest(workspace_dir)
        entries = manifest.get("skills", {})
    else:
        entries = {
            name: {
                "config": dict(skill.config),
                "requirements": dict(skill.requirements),
            }
            for name, skill in snapshot.skills.items()
        }
    overrides: dict[str, str] = {}

    for skill_name in resolve_effective_skills(
        workspace_dir,
        channel_name,
        _snapshot=snapshot,
    ):
        entry = entries.get(skill_name) or {}
        config = entry.get("config") or {}
        if not isinstance(config, dict) or not config:
            continue

        requirements = entry.get("requirements") or {}
        require_envs = requirements.get("require_envs") or []
        for env_key, env_value in _build_skill_config_env_overrides(
            skill_name,
            config,
            list(require_envs),
        ).items():
            if env_key in overrides and overrides[env_key] != env_value:
                logger.warning(
                    "Skipped env override '%s' for skill '%s'",
                    env_key,
                    skill_name,
                )
                continue
            overrides[env_key] = env_value

    with env_var_overrides(overrides):
        yield


def _build_skill_metadata(
    skill_name: str,
    skill_dir: Path,
    *,
    source: str,
    protected: bool = False,
    compute_signature: bool = True,
) -> dict[str, Any]:
    """Build the manifest-facing metadata for one concrete skill directory.

    The metadata is derived from the actual files on disk every time we
    reconcile. That keeps the manifest descriptive rather than authoritative
    for content details.

    Set ``compute_signature=False`` when the caller does not need a content
    hash (e.g. workspace reconcile where signatures are unused).
    """
    post = _read_frontmatter_safe(skill_dir, skill_name)
    requirements = _extract_requirements(post)
    now = _timestamp()
    return {
        "name": skill_name,
        "description": str(post.get("description", "") or ""),
        "version_text": _extract_version(post) or "1.0.0",
        "commit_text": "",
        "signature": _build_signature(skill_dir) if compute_signature else "",
        "source": source,
        "protected": protected,
        "requirements": requirements.model_dump(),
        "updated_at": now,
    }


_TIMESTAMP_SUFFIX_RE = re.compile(r"(-\d{14})+$")


def suggest_conflict_name(
    skill_name: str,
    existing_names: set[str] | None = None,
) -> str:
    """Return a timestamp-suffixed rename suggestion for collisions.

    Strips any previously-appended timestamp suffixes from *skill_name*
    before generating a new one, so names never accumulate multiple
    ``-YYYYMMDDHHMMSS`` tails.  When *existing_names* is provided the
    function iterates (up to 100 attempts) until it finds a candidate
    that is not already taken.
    """
    base = _TIMESTAMP_SUFFIX_RE.sub("", skill_name) or skill_name
    taken = existing_names or set()
    for _ in range(100):
        suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        candidate = f"{base}-{suffix}"
        if candidate not in taken:
            return candidate
        time.sleep(0.01)
    return f"{base}-{suffix}"


class SkillConflictError(RuntimeError):
    """Raised when an import or save operation hits a renameable conflict."""

    def __init__(self, detail: dict[str, Any]):
        super().__init__(str(detail.get("message") or "Skill conflict"))
        self.detail = detail


def _build_import_conflict(
    skill_name: str,
    existing_names: set[str] | None = None,
) -> dict[str, Any]:
    return {
        "reason": "conflict",
        "skill_name": skill_name,
        "suggested_name": suggest_conflict_name(
            skill_name,
            existing_names,
        ),
    }


def list_builtin_import_candidates(
    *,
    working_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """List builtin skills available from packaged source."""
    builtin_dir = get_builtin_skills_dir()
    builtin_sigs = _get_builtin_signatures()
    if not builtin_sigs:
        return []

    manifest = read_skill_pool_manifest(
        reconcile=False,
        working_dir=working_dir,
    )
    pool_skills = manifest.get("skills", {})
    candidates: list[dict[str, Any]] = []

    for skill_name, source_signature in sorted(builtin_sigs.items()):
        post = _read_frontmatter_safe(builtin_dir / skill_name, skill_name)
        current = pool_skills.get(skill_name) or {}
        current_signature = str(current.get("signature", "") or "")
        current_source = str(current.get("source", "") or "")
        status = "missing"
        if current:
            status = (
                "current"
                if current_source == "builtin"
                and current_signature == source_signature
                else "conflict"
            )
        candidates.append(
            {
                "name": skill_name,
                "description": str(post.get("description", "") or ""),
                "version_text": _extract_version(post),
                "current_version_text": str(
                    current.get("version_text", "") or "",
                ),
                "current_source": current_source,
                "status": status,
            },
        )
    return candidates


def import_builtin_skills(
    skill_names: list[str] | None = None,
    *,
    overwrite_conflicts: bool = False,
    working_dir: Path | None = None,
) -> dict[str, list[Any]]:
    """Import selected builtins from packaged source into the local pool."""
    pool_dir = get_skill_pool_dir(working_dir=working_dir)
    pool_dir.mkdir(parents=True, exist_ok=True)

    candidates = {
        item["name"]: item
        for item in list_builtin_import_candidates(working_dir=working_dir)
    }
    selected_names = sorted(skill_names or candidates.keys())

    unknown = [name for name in selected_names if name not in candidates]
    if unknown:
        raise ValueError(
            f"Unknown builtin skill(s): {', '.join(sorted(unknown))}",
        )

    conflicts = [
        {
            "skill_name": name,
            "source_version_text": str(
                candidates[name].get("version_text", "") or "",
            ),
            "current_version_text": str(
                candidates[name].get("current_version_text", "") or "",
            ),
            "current_source": str(
                candidates[name].get("current_source", "") or "",
            ),
        }
        for name in selected_names
        if candidates[name].get("status") == "conflict"
    ]
    if conflicts and not overwrite_conflicts:
        return {
            "imported": [],
            "updated": [],
            "unchanged": [],
            "conflicts": conflicts,
        }

    imported: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    builtin_dir = get_builtin_skills_dir()
    manifest_path = get_pool_skill_manifest_path(working_dir=working_dir)
    manifest_default = _default_pool_manifest()

    builtin_sigs = _get_builtin_signatures()

    def _process(payload: dict[str, Any]) -> dict[str, list[Any]]:
        skills = payload.setdefault("skills", {})
        payload["builtin_skill_names"] = sorted(builtin_sigs.keys())
        for skill_name in selected_names:
            skill_dir = builtin_dir / skill_name
            target = pool_dir / skill_name
            existing = skills.get(skill_name) or {}
            source_signature = builtin_sigs.get(skill_name, "")
            current_signature = (
                _build_signature(target) if target.exists() else ""
            )

            if not target.exists():
                _copy_skill_dir(skill_dir, target)
                imported.append(skill_name)
            elif current_signature != source_signature:
                _copy_skill_dir(skill_dir, target)
                updated.append(skill_name)
            else:
                unchanged.append(skill_name)

            entry = _build_skill_metadata(
                skill_name,
                target,
                source="builtin",
                protected=False,
            )
            if "config" in existing:
                entry["config"] = existing.get("config")
            skills[skill_name] = entry

        return {
            "imported": imported,
            "updated": updated,
            "unchanged": unchanged,
            "conflicts": conflicts,
        }

    return _mutate_json(
        manifest_path,
        manifest_default,
        _process,
    )


def ensure_skill_pool_initialized(
    working_dir: Path | None = None,
) -> bool:
    """Ensure the local skill pool exists and built-ins are synced into it."""
    pool_dir = get_skill_pool_dir(working_dir=working_dir)
    created = False
    if not pool_dir.exists():
        pool_dir.mkdir(parents=True, exist_ok=True)
        created = True

    manifest_path = get_pool_skill_manifest_path(working_dir=working_dir)
    if not manifest_path.exists():
        _write_json_atomic(manifest_path, _default_pool_manifest())
        created = True

    if created:
        import_builtin_skills(working_dir=working_dir)
    return created


def reconcile_pool_manifest(
    working_dir: Path | None = None,
) -> dict[str, Any]:
    """Reconcile shared pool metadata with the filesystem.

    The pool manifest is not treated as the source of truth for content.
    Instead, the pool directory on disk is scanned and metadata is rebuilt
    from the discovered skills. Manifest-only bookkeeping such as ``config``
    is preserved when possible.

    Args:
        working_dir: Target tenant working directory. If None, uses the
            global WORKING_DIR from context.

    Example:
        if a user manually drops ``skill_pool/demo/SKILL.md`` onto disk,
        the next reconcile adds ``demo`` to ``skill_pool/skill.json``.
    """
    pool_dir = get_skill_pool_dir(working_dir=working_dir)
    pool_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = get_pool_skill_manifest_path(working_dir=working_dir)
    if not manifest_path.exists():
        _write_json_atomic(manifest_path, _default_pool_manifest())
    _rename_skill_dirs_to_utf8_safe(
        pool_dir,
        manifest_path,
        _default_pool_manifest(),
    )

    # Clear cached builtin signatures so reconcile always compares
    # against the current packaged builtins on disk.
    with _BUILTIN_SIG_LOCK:
        _BUILTIN_SIGNATURES.clear()
    builtin_sigs = _get_builtin_signatures()
    builtin_names = sorted(builtin_sigs.keys())

    def _update(payload: dict[str, Any]) -> dict[str, Any]:
        payload.setdefault("skills", {})
        payload["builtin_skill_names"] = builtin_names
        skills = payload["skills"]

        discovered = {
            path.name: path
            for path in pool_dir.iterdir()
            if path.is_dir() and (path / "SKILL.md").exists()
        }

        for skill_name, skill_dir in sorted(discovered.items()):
            _scan_skill_dir_or_raise(skill_dir, skill_name)
            existing = skills.get(skill_name, {})
            source, protected = _classify_pool_skill_source(
                skill_name,
                skill_dir,
                existing,
                builtin_names,
            )
            has_config = "config" in existing
            config = existing.get("config") if has_config else None
            skills[skill_name] = _build_skill_metadata(
                skill_name,
                skill_dir,
                source=source,
                protected=protected,
                compute_signature=source == "builtin",
            )
            if has_config:
                skills[skill_name]["config"] = config

        for skill_name in list(skills):
            if skill_name not in discovered:
                skills.pop(skill_name, None)

        return payload

    return _mutate_json(
        manifest_path,
        _default_pool_manifest(),
        _update,
    )


class WorkspaceManifestReconciliationError(RuntimeError):
    """Report a reconciliation failure whose rename rollback also failed."""

    def __init__(
        self,
        reconciliation_error: Exception,
        rollback_errors: tuple[Exception, ...],
    ) -> None:
        super().__init__(
            "Workspace manifest reconciliation and sanitized rename "
            "rollback both failed",
        )
        self.reconciliation_error = reconciliation_error
        self.rollback_errors = rollback_errors


def _mutate_workspace_manifest_strict(
    manifest_path: Path,
    mutator: Callable[[dict[str, Any]], _RegistryResult],
    sanitized_rename_moves: list[tuple[Path, Path]],
) -> _RegistryResult:
    """Strictly mutate a workspace manifest and roll back key renames."""
    with _file_write_lock(_lock_path_for(manifest_path)):
        if manifest_path.exists():
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        else:
            payload = _default_workspace_manifest()

        try:
            result = mutator(payload)
            _write_json_atomic(manifest_path, payload)
            return result
        except Exception as reconcile_error:
            rollback_errors: list[Exception] = []
            for original_path, renamed_path in reversed(
                sanitized_rename_moves,
            ):
                try:
                    _move_skill_dir(renamed_path, original_path)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                raise WorkspaceManifestReconciliationError(
                    reconcile_error,
                    tuple(rollback_errors),
                ) from reconcile_error
            raise


def reconcile_workspace_manifest(workspace_dir: Path) -> dict[str, Any]:
    """Reconcile one workspace manifest with the filesystem.

    The v2 manifest is authoritative for which workspace packages are managed.
    Registered enabled packages live under ``<workspace>/skills``; registered
    disabled packages live under ``<workspace>/.disabled_skills``.

    Behavior summary:
    - Reconcile only registered manifest entries; ignore unmanaged directories.
    - Move each registered package into the root selected by ``enabled``.
    - Prefer the runtime copy if a registered package exists in both roots.
    - Preserve user state such as ``enabled``, ``channels``, and ``config``.
    - Sanitize registered names and refresh metadata from the resolved package.
    - Remove registered entries whose package is missing from both roots.

    Example:
        if registered ``demo_skill`` is disabled, the next reconcile moves it
        from ``skills/demo_skill`` to ``.disabled_skills/demo_skill``.
    """
    from .skill_runtime_snapshot import workspace_skill_coordinator

    workspace_dir = Path(workspace_dir).expanduser().resolve()
    with workspace_skill_coordinator(workspace_dir):
        return _reconcile_workspace_manifest_locked(workspace_dir)


def _reconcile_workspace_manifest_locked(
    workspace_dir: Path,
) -> dict[str, Any]:
    """Reconcile while holding the workspace skill coordinator."""
    manifest_path = get_workspace_skill_manifest_path(workspace_dir)
    sanitized_rename_moves: list[tuple[Path, Path]] = []

    def _update(payload: dict[str, Any]) -> dict[str, Any]:
        _require_workspace_layout_v2(payload, workspace_dir)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        workspace_skills_dir = get_workspace_skills_dir(workspace_dir)
        workspace_skills_dir.mkdir(parents=True, exist_ok=True)
        payload.setdefault("skills", {})
        skills = payload["skills"]
        registered = dict(skills)
        reconciled: dict[str, Any] = {}
        disabled_skills_dir = get_workspace_disabled_skills_dir(workspace_dir)
        occupied_names = set(registered)
        for root in (workspace_skills_dir, disabled_skills_dir):
            if root.exists():
                occupied_names.update(
                    path.name for path in root.iterdir() if path.is_dir()
                )

        for raw_skill_name, raw_entry in sorted(registered.items()):
            skill_name = _normalize_skill_dir_name(raw_skill_name)
            existing = raw_entry or {}
            sanitized = sanitize_fs_text(skill_name)
            log_sanitized_fs_text(
                logger,
                source="skills.reconcile_workspace_manifest",
                original=skill_name,
                sanitized=sanitized,
            )
            resolved_name = sanitized.value or skill_name
            occupied_names.discard(skill_name)
            if resolved_name in occupied_names:
                resolved_name = suggest_conflict_name(
                    resolved_name,
                    occupied_names,
                )

            skill_dir = _reconcile_registered_skill_location(
                workspace_dir,
                skill_name,
                existing,
            )
            if skill_dir is None:
                continue
            if resolved_name != skill_name:
                renamed_dir = skill_dir.with_name(resolved_name)
                _move_skill_dir(skill_dir, renamed_dir)
                sanitized_rename_moves.append((skill_dir, renamed_dir))
                skill_dir = renamed_dir
            occupied_names.add(resolved_name)

            enabled = bool(existing.get("enabled", False))
            channels = existing.get("channels") or ["all"]
            source = existing.get("source", "customized")

            metadata = _build_skill_metadata(
                resolved_name,
                skill_dir,
                source=source,
                protected=False,
                compute_signature=False,
            )
            metadata = _merge_existing_metadata(
                existing.get("metadata"),
                metadata,
            )
            next_entry = {
                "enabled": enabled,
                "channels": channels,
                "source": source,
                "metadata": metadata,
                "requirements": metadata["requirements"],
                "created_at": existing.get("created_at") or _timestamp(),
                "updated_at": _timestamp(),
            }
            if "config" in existing:
                next_entry["config"] = existing.get("config")
            reconciled[resolved_name] = next_entry
            reconciled[resolved_name].pop("sync_to_hub", None)
            reconciled[resolved_name].pop("sync_to_pool", None)

        payload["skills"] = reconciled

        return payload

    result = _mutate_workspace_manifest_strict(
        manifest_path,
        _update,
        sanitized_rename_moves,
    )
    from .skill_runtime_snapshot import invalidate_workspace_skill_snapshot

    invalidate_workspace_skill_snapshot(workspace_dir)
    return result


def list_workspaces(tenant_id: str | None = None) -> list[dict[str, str]]:
    """List configured workspaces with agent names.

    Args:
        tenant_id: Tenant ID. If None, uses current tenant from context.

    Returns:
        List of workspace info dicts with agent_id, agent_name, workspace_dir.
    """
    workspaces: list[dict[str, str]] = []
    try:
        from ..config.utils import load_config, get_tenant_config_path
        from ..config.config import load_agent_config

        config_path = get_tenant_config_path(tenant_id)
        config = load_config(config_path)
        # Only return agents that are still in the configuration
        # This ensures deleted agents are not included
        for agent_id, profile in sorted(config.agents.profiles.items()):
            agent_name = agent_id
            try:
                agent_name = (
                    load_agent_config(
                        agent_id,
                        config_path=config_path,
                    ).name
                    or agent_id
                )
            except Exception:
                pass
            workspaces.append(
                {
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "workspace_dir": str(
                        Path(profile.workspace_dir).expanduser(),
                    ),
                },
            )
    except Exception as exc:
        logger.warning("Failed to load configured workspaces: %s", exc)

    # Note: We intentionally do NOT scan the workspaces/ directory
    # for unlisted workspaces, as those may belong to deleted agents
    # and should not appear in the broadcast list

    return workspaces


def read_skill_manifest(
    workspace_dir: Path,
    *,
    reconcile: bool = True,
) -> dict[str, Any]:
    """Return the workspace skill manifest.

    When *reconcile* is ``True`` (default) the manifest is refreshed
    from disk first.  Pass ``reconcile=False`` in read-only list paths
    to skip the reconciliation and just return the cached JSON.
    """
    if reconcile:
        return reconcile_workspace_manifest(workspace_dir)
    path = get_workspace_skill_manifest_path(workspace_dir)
    return _read_json_unlocked(path, _default_workspace_manifest())


def read_skill_pool_manifest(
    *,
    reconcile: bool = True,
    working_dir: Path | None = None,
) -> dict[str, Any]:
    """Return the pool skill manifest.

    When *reconcile* is ``True`` (default) the manifest is refreshed
    from disk first.  Pass ``reconcile=False`` in read-only list paths
    to skip the expensive reconciliation.

    Args:
        reconcile: Whether to reconcile manifest from disk first.
        working_dir: Target tenant working directory. If None, uses the
            global WORKING_DIR from context.
    """
    if reconcile:
        return reconcile_pool_manifest(working_dir=working_dir)
    path = get_pool_skill_manifest_path(working_dir=working_dir)
    return _read_json_unlocked(path, _default_pool_manifest())


def resolve_effective_skills(
    workspace_dir: Path,
    channel_name: str,
    *,
    _registry: dict | None = None,
    _snapshot: Any | None = None,
) -> list[str]:
    """Resolve enabled workspace skills for one channel."""
    if _snapshot is None:
        from .skill_runtime_snapshot import get_workspace_skill_snapshot

        _snapshot = get_workspace_skill_snapshot(workspace_dir)
    return [
        name
        for name, skill in sorted(_snapshot.skills.items())
        if "all" in skill.channels or channel_name in skill.channels
    ]


def ensure_skills_initialized(workspace_dir: Path) -> None:
    """Ensure workspace manifests exist before runtime use."""
    reconcile_workspace_manifest(workspace_dir)


def get_pool_builtin_sync_status(
    *,
    working_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Compare pool skills against packaged builtins.

    Returns a dict keyed by skill name with sync status for each
    builtin pool skill.

    Status values:
    - ``synced``: pool copy matches the packaged builtin exactly
    - ``outdated``: pool copy differs from the packaged builtin
    """
    builtin_sigs = _get_builtin_signatures()
    if not builtin_sigs:
        return {}

    manifest = _read_json(
        get_pool_skill_manifest_path(working_dir=working_dir),
        _default_pool_manifest(),
    )
    pool_skills = manifest.get("skills", {})
    builtin_dir = get_builtin_skills_dir()

    result: dict[str, dict[str, Any]] = {}
    for name, builtin_sig in builtin_sigs.items():
        pool_entry = pool_skills.get(name)
        if pool_entry is None or not _is_pool_builtin_entry(pool_entry):
            continue
        pool_sig = str(pool_entry.get("signature", ""))
        if pool_sig and pool_sig != builtin_sig:
            post = _read_frontmatter_safe(builtin_dir / name, name)
            result[name] = {
                "sync_status": "outdated",
                "latest_version_text": _extract_version(post),
            }
        else:
            result[name] = {
                "sync_status": "synced",
                "latest_version_text": "",
            }
    return result


def update_single_builtin(
    skill_name: str,
    *,
    working_dir: Path | None = None,
) -> dict[str, Any]:
    """Update one builtin skill in the pool to the latest packaged version."""
    builtin_sigs = _get_builtin_signatures()
    if skill_name not in builtin_sigs:
        raise ValueError(f"'{skill_name}' is not a builtin skill")

    manifest = read_skill_pool_manifest(
        reconcile=False,
        working_dir=working_dir,
    )
    existing = manifest.get("skills", {}).get(skill_name)
    if existing is None or not _is_pool_builtin_entry(existing):
        raise ValueError(
            f"'{skill_name}' is not a builtin pool skill",
        )

    builtin_dir = get_builtin_skills_dir()
    src = builtin_dir / skill_name
    if not src.exists():
        raise ValueError(f"Packaged builtin '{skill_name}' not found")

    pool_dir = get_skill_pool_dir(working_dir=working_dir)
    target = pool_dir / skill_name
    _copy_skill_dir(src, target)

    def _update(payload: dict[str, Any]) -> dict[str, Any]:
        payload.setdefault("skills", {})
        entry = _build_skill_metadata(
            skill_name,
            target,
            source="builtin",
            protected=False,
        )
        if "config" in existing:
            entry["config"] = existing["config"]
        payload["skills"][skill_name] = entry
        return entry

    return _mutate_json(
        get_pool_skill_manifest_path(working_dir=working_dir),
        _default_pool_manifest(),
        _update,
    )


def _read_skill_from_dir(skill_dir: Path, source: str) -> SkillInfo | None:
    if not skill_dir.is_dir():
        return None

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None

    try:
        content = read_text_file_with_encoding_fallback(skill_md)
        description = ""
        post: Any = {}
        try:
            post = frontmatter.loads(content)
            description = str(post.get("description", "") or "")
        except Exception:
            pass

        references = {}
        scripts = {}
        references_dir = skill_dir / "references"
        scripts_dir = skill_dir / "scripts"
        if references_dir.exists():
            references = _directory_tree(references_dir)
        if scripts_dir.exists():
            scripts = _directory_tree(scripts_dir)

        return SkillInfo(
            name=sanitize_fs_text(skill_dir.name).value,
            description=description,
            version_text=_extract_version(post),
            content=content,
            source=source,
            references=references,
            scripts=scripts,
        )
    except Exception as exc:
        logger.error("Failed to read skill %s: %s", skill_dir, exc)
        return None


def _validate_skill_content(content: str) -> tuple[str, str]:
    post = frontmatter.loads(content)
    skill_name = str(post.get("name") or "").strip()
    skill_description = str(post.get("description") or "").strip()
    if not skill_name or not skill_description:
        raise ValueError(
            "SKILL.md must include non-empty frontmatter name and description",
        )
    return skill_name, skill_description


def _import_skill_dir(
    src_dir: Path,
    target_root: Path,
    skill_name: str,
    overwrite: bool,
) -> bool:
    """Import a skill directory to target location.

    Args:
        src_dir: Source skill directory
        target_root: Target root directory
        skill_name: Name of the skill
        overwrite: Whether to overwrite existing skill

    Returns:
        bool: True if import succeeded, False otherwise
    """
    post = _read_frontmatter_safe(src_dir, skill_name)
    if not post.get("name") or not post.get("description"):
        return False

    target_dir = target_root / skill_name
    if target_dir.exists() and not overwrite:
        return False
    _copy_skill_dir(src_dir, target_dir)
    return True


def _write_skill_to_dir(
    skill_dir: Path,
    content: str,
    references: dict[str, Any] | None = None,
    scripts: dict[str, Any] | None = None,
    extra_files: dict[str, Any] | None = None,
) -> None:
    """Write a skill's files into a directory (shared by create flows)."""
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    _create_files_from_tree(skill_dir, extra_files or {})
    if references:
        ref_dir = skill_dir / "references"
        ref_dir.mkdir(parents=True, exist_ok=True)
        _create_files_from_tree(ref_dir, references)
    if scripts:
        script_dir = skill_dir / "scripts"
        script_dir.mkdir(parents=True, exist_ok=True)
        _create_files_from_tree(script_dir, scripts)


def _extract_zip_skills(data: bytes) -> tuple[Path, list[tuple[Path, str]]]:
    """Extract and validate a skill zip.

    Returns ``(tmp_dir, found_skills)``.

    Naming rule:
    - single-skill zips use the skill frontmatter ``name`` when present
    - multi-skill zips apply the same rule per top-level skill directory

    This keeps import results consistent across different zip layouts.
    """
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise ValueError("Uploaded file is not a valid zip archive")
    tmp_dir = Path(tempfile.mkdtemp(prefix="copaw_skill_upload_"))
    _extract_and_validate_zip(data, tmp_dir)
    real_entries = [
        path for path in tmp_dir.iterdir() if not _is_hidden(path.name)
    ]
    extract_root = (
        real_entries[0]
        if len(real_entries) == 1 and real_entries[0].is_dir()
        else tmp_dir
    )
    if (extract_root / "SKILL.md").exists():
        found = [(extract_root, _resolve_skill_name(extract_root))]
    else:
        found = [
            (path, _resolve_skill_name(path))
            for path in sorted(extract_root.iterdir())
            if not _is_hidden(path.name)
            and path.is_dir()
            and (path / "SKILL.md").exists()
        ]
    if not found:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise ValueError("No valid skills found in uploaded zip")
    return tmp_dir, found


def _scan_skill_dir_or_raise(skill_dir: Path, skill_name: str) -> None:
    scan_skill_directory(skill_dir, skill_name=skill_name, block=True)


@contextmanager
def _staged_skill_dir(skill_name: str) -> Iterator[Path]:
    """Create a temporary skill directory used for staged writes."""
    temp_root = Path(
        tempfile.mkdtemp(prefix=f"copaw_skill_stage_{skill_name}_"),
    )
    stage_dir = temp_root / skill_name
    try:
        yield stage_dir
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


class SkillService:
    """Workspace-scoped skill lifecycle service.

    This service owns editable skills inside one workspace, including create,
    zip import, enable/disable, channel routing, config persistence, and file
    access. It treats ``<workspace>/skills`` as the source of truth for skill
    content and ``<workspace>/skill.json`` as the source of truth for runtime
    state such as ``enabled`` and ``channels``.

    Example:
        a user creates ``demo_skill`` in workspace ``a1`` -> files are written
        under ``workspaces/a1/skills/demo_skill`` and metadata/state are
        reconciled into ``workspaces/a1/skill.json``.

        a user enables ``docx`` for the ``discord`` channel only -> the skill
        files stay the same, but the workspace manifest updates ``enabled`` and
        ``channels`` so runtime resolution changes on the next read.
    """

    def __init__(self, workspace_dir: Path):
        self.workspace_dir = Path(workspace_dir).expanduser()
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

    def _manifest(self) -> dict[str, Any]:
        return reconcile_workspace_manifest(self.workspace_dir)

    def _read_manifest(self) -> dict[str, Any]:
        """Read workspace manifest without triggering reconcile."""
        return read_skill_manifest(
            self.workspace_dir,
            reconcile=False,
        )

    def _registered_skill_dir(
        self,
        skill_name: str,
        entry: dict[str, Any],
    ) -> Path:
        """Resolve one registered package from its manifest enablement."""
        return resolve_workspace_managed_skill_dir(
            self.workspace_dir,
            skill_name,
            enabled=bool(entry.get("enabled", False)),
        )

    def list_all_skills(self) -> list[SkillInfo]:
        manifest = self._manifest()
        skills: list[SkillInfo] = []
        for skill_name, entry in sorted(manifest.get("skills", {}).items()):
            skill_dir = self._registered_skill_dir(skill_name, entry)
            source = entry.get("source", "workspace")
            skill = _read_skill_from_dir(skill_dir, source)
            if skill is not None:
                skills.append(skill)
        return skills

    def list_available_skills(self) -> list[SkillInfo]:
        manifest = self._manifest()
        skill_root = get_workspace_skills_dir(self.workspace_dir)
        skills: list[SkillInfo] = []
        for skill_name in resolve_effective_skills(
            self.workspace_dir,
            "console",
        ):
            entry = manifest.get("skills", {}).get(skill_name, {})
            skill = _read_skill_from_dir(
                skill_root / skill_name,
                (
                    "builtin"
                    if entry.get("source", "customized") == "builtin"
                    else "customized"
                ),
            )
            if skill is not None:
                skills.append(skill)
        return skills

    @coordinate_workspace_skill_mutation
    def create_skill(
        self,
        name: str,
        content: str,
        overwrite: bool = False,
        references: dict[str, Any] | None = None,
        scripts: dict[str, Any] | None = None,
        extra_files: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
        enable: bool = False,
    ) -> str | None:
        _validate_skill_content(content)
        skill_name = _normalize_skill_dir_name(name)
        manifest = self._read_manifest()
        existing = manifest.get("skills", {}).get(skill_name)
        active_dir = resolve_workspace_managed_skill_dir(
            self.workspace_dir,
            skill_name,
            enabled=True,
        )
        disabled_dir = resolve_workspace_managed_skill_dir(
            self.workspace_dir,
            skill_name,
            enabled=False,
        )
        if (
            existing is not None
            or active_dir.exists()
            or disabled_dir.exists()
        ) and not overwrite:
            return None
        enabled = (
            bool(existing.get("enabled", False))
            if existing is not None
            else bool(enable)
        )
        skill_dir = resolve_workspace_managed_skill_dir(
            self.workspace_dir,
            skill_name,
            enabled=enabled,
        )
        other_dir = active_dir if skill_dir == disabled_dir else disabled_dir

        with _staged_skill_dir(skill_name) as staged_dir:
            _write_skill_to_dir(
                staged_dir,
                content,
                references,
                scripts,
                extra_files,
            )
            _scan_skill_dir_or_raise(staged_dir, skill_name)
            _copy_skill_dir(staged_dir, skill_dir)
        if other_dir.exists():
            shutil.rmtree(other_dir)

        def _update(payload: dict[str, Any]) -> None:
            payload.setdefault("skills", {})
            entry = payload["skills"].get(skill_name) or {}
            if "source" in entry:
                source = entry["source"]
            elif skill_name in _get_builtin_signatures():
                source = "builtin"
            else:
                source = "customized"
            metadata = _build_skill_metadata(
                skill_name,
                skill_dir,
                source=source,
                protected=False,
            )
            payload["skills"][skill_name] = {
                "enabled": enabled,
                "channels": entry.get("channels") or ["all"],
                "source": metadata["source"],
                "config": (
                    dict(config)
                    if config is not None
                    else dict(entry.get("config") or {})
                ),
                "metadata": metadata,
                "requirements": metadata["requirements"],
                "created_at": entry.get("created_at") or _timestamp(),
                "updated_at": _timestamp(),
            }

        _mutate_json(
            get_workspace_skill_manifest_path(self.workspace_dir),
            _default_workspace_manifest(),
            _update,
        )
        return skill_name

    @coordinate_workspace_skill_mutation
    def replace_workspace_skill_from_dir(
        self,
        *,
        skill_name: str,
        source_dir: Path,
        source: str = "customized",
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        final_name = _normalize_skill_dir_name(skill_name)
        manifest = self._read_manifest()
        if _has_unmanaged_workspace_skill_conflict(
            self.workspace_dir,
            final_name,
            manifest,
        ):
            return {
                "success": False,
                "reason": "conflict",
                "suggested_name": suggest_conflict_name(final_name),
            }
        existing = manifest.get("skills", {}).get(final_name)
        enabled = (
            bool(existing.get("enabled", True))
            if existing is not None
            else True
        )
        target_dir = resolve_workspace_managed_skill_dir(
            self.workspace_dir,
            final_name,
            enabled=enabled,
        )
        other_dir = resolve_workspace_managed_skill_dir(
            self.workspace_dir,
            final_name,
            enabled=not enabled,
        )

        with _staged_skill_dir(final_name) as staged_dir:
            _copy_skill_dir(source_dir, staged_dir)
            _scan_skill_dir_or_raise(staged_dir, final_name)
            _copy_skill_dir(staged_dir, target_dir)
        if other_dir.exists():
            shutil.rmtree(other_dir)

        def _update(payload: dict[str, Any]) -> None:
            payload.setdefault("skills", {})
            entry = payload["skills"].get(final_name) or {}
            metadata = _build_skill_metadata(
                final_name,
                target_dir,
                source=source,
                protected=False,
            )
            payload["skills"][final_name] = {
                "enabled": enabled,
                "channels": entry.get("channels") or ["all"],
                "source": metadata["source"],
                "config": (
                    dict(entry.get("config") or {})
                    if existing is not None
                    else dict(config or {})
                ),
                "metadata": metadata,
                "requirements": metadata["requirements"],
                "created_at": entry.get("created_at") or _timestamp(),
                "updated_at": _timestamp(),
            }

        _mutate_json(
            get_workspace_skill_manifest_path(self.workspace_dir),
            _default_workspace_manifest(),
            _update,
        )
        return {"success": True, "name": final_name}

    @coordinate_workspace_skill_mutation
    def save_skill(
        self,
        *,
        skill_name: str,
        content: str,
        target_name: str | None = None,
        config: dict[str, Any] | None = None,
        references: dict[str, Any] | None = None,
        scripts: dict[str, Any] | None = None,
        extra_files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Edit-in-place or rename-save a workspace skill."""
        final_name = _normalize_skill_dir_name(target_name or skill_name)
        manifest = self._read_manifest()
        old_entry = manifest.get("skills", {}).get(skill_name)
        if old_entry is None:
            return {"success": False, "reason": "not_found"}

        if final_name == skill_name:
            old_sig = (old_entry.get("metadata") or {}).get("signature", "")
            new_config = (
                config if config is not None else old_entry.get("config") or {}
            )
            skill_dir = self._registered_skill_dir(skill_name, old_entry)

            with _staged_skill_dir(skill_name) as staged_dir:
                _write_skill_to_dir(
                    staged_dir,
                    content,
                    references,
                    scripts,
                    extra_files,
                )
                _scan_skill_dir_or_raise(staged_dir, skill_name)
                new_sig = _build_signature(staged_dir)
                content_changed = new_sig != old_sig
                if not content_changed and new_config == (
                    old_entry.get("config") or {}
                ):
                    return {
                        "success": True,
                        "mode": "noop",
                        "name": skill_name,
                    }
                if content_changed:
                    _copy_skill_dir(staged_dir, skill_dir)
            source = (
                old_entry.get("source", "customized")
                if not content_changed
                else "customized"
            )
            metadata = _build_skill_metadata(
                skill_name,
                skill_dir,
                source=source,
                protected=False,
            )

            def _edit(payload: dict[str, Any]) -> None:
                payload.setdefault("skills", {})
                entry = payload["skills"].get(skill_name) or {}
                payload["skills"][skill_name] = {
                    "enabled": bool(entry.get("enabled", False)),
                    "channels": entry.get("channels") or ["all"],
                    "source": metadata["source"],
                    "config": new_config,
                    "metadata": metadata,
                    "requirements": metadata["requirements"],
                    "updated_at": _timestamp(),
                }

            _mutate_json(
                get_workspace_skill_manifest_path(
                    self.workspace_dir,
                ),
                _default_workspace_manifest(),
                _edit,
            )
            return {
                "success": True,
                "mode": "edit",
                "name": skill_name,
            }

        enabled = bool(old_entry.get("enabled", False))
        target_dir = resolve_workspace_managed_skill_dir(
            self.workspace_dir,
            final_name,
            enabled=enabled,
        )
        old_dir = self._registered_skill_dir(skill_name, old_entry)
        active_target = resolve_workspace_managed_skill_dir(
            self.workspace_dir,
            final_name,
            enabled=True,
        )
        disabled_target = resolve_workspace_managed_skill_dir(
            self.workspace_dir,
            final_name,
            enabled=False,
        )
        if active_target.exists() or disabled_target.exists():
            existing: set[str] = set()
            for root in (
                get_workspace_skills_dir(self.workspace_dir),
                get_workspace_disabled_skills_dir(self.workspace_dir),
            ):
                if root.exists():
                    existing.update(
                        path.name for path in root.iterdir() if path.is_dir()
                    )
            return {
                "success": False,
                "reason": "conflict",
                "suggested_name": suggest_conflict_name(
                    final_name,
                    existing,
                ),
            }

        with _staged_skill_dir(final_name) as staged_dir:
            _write_skill_to_dir(
                staged_dir,
                content,
                references,
                scripts,
                extra_files,
            )
            _scan_skill_dir_or_raise(staged_dir, final_name)
            _copy_skill_dir(staged_dir, target_dir)

        old_config = (
            config if config is not None else old_entry.get("config") or {}
        )
        old_channels = old_entry.get("channels") or ["all"]
        source = (
            old_entry.get("source", "customized")
            if final_name == skill_name
            else "customized"
        )
        metadata = _build_skill_metadata(
            final_name,
            target_dir,
            source=source,
            protected=False,
        )

        def _rename_entry(payload: dict[str, Any]) -> None:
            payload.setdefault("skills", {})
            payload["skills"][final_name] = {
                "enabled": enabled,
                "channels": old_channels,
                "source": metadata["source"],
                "config": old_config,
                "metadata": metadata,
                "requirements": metadata["requirements"],
                "created_at": old_entry.get("created_at") or _timestamp(),
                "updated_at": _timestamp(),
            }
            payload["skills"].pop(skill_name, None)

        _mutate_json(
            get_workspace_skill_manifest_path(self.workspace_dir),
            _default_workspace_manifest(),
            _rename_entry,
        )
        if old_dir.exists():
            shutil.rmtree(old_dir)

        return {
            "success": True,
            "mode": "rename",
            "name": final_name,
        }

    def _register_imported_skills(
        self,
        imported: list[str],
        enabled_by_name: dict[str, bool],
        target_by_name: dict[str, Path],
    ) -> None:
        """Atomically refresh manifest entries for copied zip imports."""

        def _register(payload: dict[str, Any]) -> None:
            payload.setdefault("skills", {})
            for skill_name in imported:
                entry = payload["skills"].get(skill_name) or {}
                source = entry.get("source", "customized")
                metadata = _build_skill_metadata(
                    skill_name,
                    target_by_name[skill_name],
                    source=source,
                    protected=False,
                )
                payload["skills"][skill_name] = {
                    "enabled": enabled_by_name[skill_name],
                    "channels": entry.get("channels") or ["all"],
                    "source": metadata["source"],
                    "config": dict(entry.get("config") or {}),
                    "metadata": metadata,
                    "requirements": metadata["requirements"],
                    "created_at": entry.get("created_at") or _timestamp(),
                    "updated_at": _timestamp(),
                }

        _mutate_json(
            get_workspace_skill_manifest_path(self.workspace_dir),
            _default_workspace_manifest(),
            _register,
        )

    @coordinate_workspace_skill_mutation
    def import_from_zip(
        self,
        data: bytes,
        overwrite: bool = False,
        enable: bool = False,
        target_name: str | None = None,
        rename_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        tmp_dir, found = _extract_zip_skills(data)
        renames = rename_map or {}
        try:
            normalized_target = str(target_name or "").strip()
            if normalized_target:
                normalized_target = _normalize_skill_dir_name(
                    normalized_target,
                )
                if len(found) != 1:
                    raise ValueError(
                        "target_name is only supported for "
                        "single-skill zip imports",
                    )
                found = [(found[0][0], normalized_target)]
            found = [
                (d, _normalize_skill_dir_name(renames.get(n, n)))
                for d, n in found
            ]
            manifest = self._read_manifest()
            entries = manifest.get("skills", {})
            existing_on_disk = set(entries)
            for root in (
                get_workspace_skills_dir(self.workspace_dir),
                get_workspace_disabled_skills_dir(self.workspace_dir),
            ):
                if root.exists():
                    existing_on_disk.update(
                        path.name for path in root.iterdir() if path.is_dir()
                    )
            conflicts: list[dict[str, Any]] = []
            planned: list[tuple[Path, str, bool, Path, Path]] = []
            seen_names: set[str] = set()
            for skill_dir, skill_name in found:
                _scan_skill_dir_or_raise(skill_dir, skill_name)
                if skill_name in seen_names:
                    conflicts.append(
                        _build_import_conflict(
                            skill_name,
                            existing_on_disk,
                        ),
                    )
                    continue
                seen_names.add(skill_name)
                active_dir = resolve_workspace_managed_skill_dir(
                    self.workspace_dir,
                    skill_name,
                    enabled=True,
                )
                disabled_dir = resolve_workspace_managed_skill_dir(
                    self.workspace_dir,
                    skill_name,
                    enabled=False,
                )
                existing = entries.get(skill_name)
                exists = (
                    existing is not None
                    or active_dir.exists()
                    or disabled_dir.exists()
                )
                if exists and not overwrite:
                    conflicts.append(
                        _build_import_conflict(
                            skill_name,
                            existing_on_disk,
                        ),
                    )
                    continue
                enabled = (
                    bool(existing.get("enabled", False))
                    if existing is not None
                    else bool(enable)
                )
                target_dir = active_dir if enabled else disabled_dir
                other_dir = disabled_dir if enabled else active_dir
                planned.append(
                    (
                        skill_dir,
                        skill_name,
                        enabled,
                        target_dir,
                        other_dir,
                    ),
                )
            if conflicts:
                return {
                    "imported": [],
                    "count": 0,
                    "enabled": False,
                    "conflicts": conflicts,
                }
            imported: list[str] = []
            enabled_by_name: dict[str, bool] = {}
            target_by_name: dict[str, Path] = {}
            for (
                skill_dir,
                skill_name,
                enabled,
                target_dir,
                other_dir,
            ) in planned:
                target_dir.parent.mkdir(parents=True, exist_ok=True)
                if _import_skill_dir(
                    skill_dir,
                    target_dir.parent,
                    skill_name,
                    True,
                ):
                    imported.append(skill_name)
                    enabled_by_name[skill_name] = enabled
                    target_by_name[skill_name] = target_dir
                    if other_dir.exists():
                        shutil.rmtree(other_dir)

            if imported:
                self._register_imported_skills(
                    imported,
                    enabled_by_name,
                    target_by_name,
                )

            return {
                "imported": imported,
                "count": len(imported),
                "enabled": bool(imported)
                and all(enabled_by_name[name] for name in imported),
                "conflicts": conflicts,
            }
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @coordinate_workspace_skill_mutation
    def enable_skill(
        self,
        name: str,
        target_workspaces: list[str] | None = None,
    ) -> dict[str, Any]:
        # Enabling a skill only flips manifest state after a fresh scan of the
        # current on-disk skill directory.
        #
        # Example:
        # if ``skills/docx`` was edited after creation and now violates scan
        # policy, enable returns a scan failure instead of trusting old state.
        skill_name = str(name or "")
        if (
            target_workspaces
            and self.workspace_dir.name not in target_workspaces
        ):
            return {
                "success": False,
                "updated_workspaces": [],
                "failed": target_workspaces,
                "reason": "workspace_mismatch",
            }

        manifest_path = get_workspace_skill_manifest_path(self.workspace_dir)
        with _file_write_lock(_lock_path_for(manifest_path)):
            payload = _read_workspace_manifest_strict_unlocked(manifest_path)
            entry = payload.get("skills", {}).get(skill_name)
            if entry is None:
                return {
                    "success": False,
                    "updated_workspaces": [],
                    "failed": [self.workspace_dir.name],
                    "reason": "not_found",
                }
            skill_dir = self._registered_skill_dir(skill_name, entry)
            if not skill_dir.exists():
                return {
                    "success": False,
                    "updated_workspaces": [],
                    "failed": [self.workspace_dir.name],
                    "reason": "not_found",
                }
            _scan_skill_dir_or_raise(skill_dir, skill_name)

            entry["enabled"] = True
            entry.setdefault("channels", ["all"])
            entry["updated_at"] = _timestamp()
            _write_json_atomic(manifest_path, payload)

            active_dir = resolve_workspace_managed_skill_dir(
                self.workspace_dir,
                skill_name,
                enabled=True,
            )
            if skill_dir != active_dir:
                try:
                    _move_skill_dir(skill_dir, active_dir)
                except OSError:
                    return {
                        "success": False,
                        "updated_workspaces": [],
                        "failed": [self.workspace_dir.name],
                        "reason": "move_failed",
                    }

        return {
            "success": True,
            "updated_workspaces": [self.workspace_dir.name],
            "failed": [],
            "reason": None,
        }

    @coordinate_workspace_skill_mutation
    def disable_skill(self, name: str) -> dict[str, Any]:
        skill_name = str(name or "")
        manifest_path = get_workspace_skill_manifest_path(self.workspace_dir)
        with _file_write_lock(_lock_path_for(manifest_path)):
            payload = _read_workspace_manifest_strict_unlocked(manifest_path)
            entry = payload.get("skills", {}).get(skill_name)
            if entry is None:
                return {"success": False, "updated_workspaces": []}

            skill_dir = self._registered_skill_dir(skill_name, entry)
            if not skill_dir.exists():
                return {"success": False, "updated_workspaces": []}
            disabled_dir = resolve_workspace_managed_skill_dir(
                self.workspace_dir,
                skill_name,
                enabled=False,
            )
            if skill_dir != disabled_dir:
                if disabled_dir.exists():
                    shutil.rmtree(disabled_dir)
                _move_skill_dir(skill_dir, disabled_dir)

            entry["enabled"] = False
            entry["updated_at"] = _timestamp()
            _write_json_atomic(manifest_path, payload)

        return {
            "success": True,
            "updated_workspaces": [self.workspace_dir.name],
        }

    @coordinate_workspace_skill_mutation
    def set_skill_channels(
        self,
        name: str,
        channels: list[str] | None,
    ) -> bool:
        """Update one workspace skill's channel scope."""
        skill_name = str(name or "")
        manifest_path = get_workspace_skill_manifest_path(self.workspace_dir)
        normalized = channels or ["all"]

        def _update(payload: dict[str, Any]) -> bool:
            entry = payload.get("skills", {}).get(skill_name)
            if entry is None:
                return False
            entry["channels"] = normalized
            entry["updated_at"] = _timestamp()
            return True

        updated = _mutate_json(
            manifest_path,
            _default_workspace_manifest(),
            _update,
        )
        return updated

    @coordinate_workspace_skill_mutation
    def delete_skill(self, name: str) -> bool:
        skill_name = str(name or "")
        manifest = self._read_manifest()
        entry = manifest.get("skills", {}).get(skill_name)
        if entry is None or entry.get("enabled", False):
            return False

        skill_dir = self._registered_skill_dir(skill_name, entry)
        if skill_dir.exists():
            shutil.rmtree(skill_dir)

        def _update(payload: dict[str, Any]) -> None:
            payload.get("skills", {}).pop(skill_name, None)

        _mutate_json(
            get_workspace_skill_manifest_path(self.workspace_dir),
            _default_workspace_manifest(),
            _update,
        )
        return True

    def load_skill_file(
        self,
        skill_name: str,
        file_path: str,
        source: str,
    ) -> str | None:
        del source
        normalized = file_path.replace("\\", "/")
        is_relative = ".." not in normalized and not normalized.startswith("/")
        is_supported = normalized.startswith(
            "references/",
        ) or normalized.startswith(
            "scripts/",
        )
        if not is_relative or not is_supported:
            return None

        manifest = self._read_manifest()
        manifest_skills = manifest.get("skills", {})
        resolved_skill_name = skill_name
        if resolved_skill_name not in manifest_skills:
            for manifest_name in manifest_skills:
                sanitized = sanitize_fs_text(manifest_name)
                if sanitized.value == skill_name:
                    resolved_skill_name = manifest_name
                    break
        if resolved_skill_name not in manifest_skills:
            return None

        workspace_base_dir = self._registered_skill_dir(
            resolved_skill_name,
            manifest_skills[resolved_skill_name],
        )
        if not workspace_base_dir.exists():
            return None

        full_path = _resolve_path_by_raw_or_sanitized(
            workspace_base_dir,
            normalized,
        )
        if (
            full_path is None
            or not full_path.exists()
            or not full_path.is_file()
        ):
            return None
        return read_text_file_with_encoding_fallback(full_path)


class SkillPoolService:
    """Shared skill-pool lifecycle service.

    This service manages reusable skills in the local shared pool
    ``WORKING_DIR/skill_pool``. It supports creating pool-native skills,
    importing zips, syncing packaged builtins, uploading skills from a
    workspace into the pool, and downloading pool skills back into one or more
    workspaces.

    The pool is intentionally separate from any single workspace: it is the
    place for shared reuse, conflict detection, and builtin version management.

    Example:
        uploading ``demo_skill`` from workspace ``a1`` stores a shared copy in
        ``skill_pool/demo_skill`` and records the workspace-to-pool linkage in
        the workspace manifest.

        downloading pool skill ``shared_docx`` into workspace ``b1`` creates
        ``workspaces/b1/skills/shared_docx`` and marks its sync state against
        the pool entry.
    """

    def __init__(
        self,
        *,
        working_dir: Path | None = None,
    ):
        self.working_dir = working_dir
        ensure_skill_pool_initialized(working_dir=self.working_dir)

    def list_all_skills(self) -> list[SkillInfo]:
        manifest = read_skill_pool_manifest(
            reconcile=False,
            working_dir=self.working_dir,
        )
        pool_dir = get_skill_pool_dir(working_dir=self.working_dir)
        skills: list[SkillInfo] = []
        for skill_name, entry in sorted(manifest.get("skills", {}).items()):
            skill = _read_skill_from_dir(
                pool_dir / skill_name,
                entry.get("source", "customized"),
            )
            if skill is not None:
                skills.append(skill)
        return skills

    def create_skill(
        self,
        name: str,
        content: str,
        references: dict[str, Any] | None = None,
        scripts: dict[str, Any] | None = None,
        extra_files: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
        overwrite: bool = True,
    ) -> str | None:
        _validate_skill_content(content)
        skill_name = _normalize_skill_dir_name(name)
        pool_dir = get_skill_pool_dir(working_dir=self.working_dir)
        skill_dir = pool_dir / skill_name
        manifest = read_skill_pool_manifest(
            reconcile=False,
            working_dir=self.working_dir,
        )
        existing = manifest.get("skills", {}).get(skill_name)
        # 同名技能且 overwrite=False 时拒绝
        if (existing is not None or skill_dir.exists()) and not overwrite:
            return None

        # 覆盖时保留原技能的 config 和 protected
        existing_config = existing.get("config") if existing else None
        existing_protected = (
            existing.get("protected", False) if existing else False
        )

        with _staged_skill_dir(skill_name) as staged_dir:
            _write_skill_to_dir(
                staged_dir,
                content,
                references,
                scripts,
                extra_files,
            )
            _scan_skill_dir_or_raise(staged_dir, skill_name)
            _copy_skill_dir(staged_dir, skill_dir)

        def _update(payload: dict[str, Any]) -> None:
            payload.setdefault("skills", {})
            entry = _build_skill_metadata(
                skill_name,
                skill_dir,
                source="customized",
                protected=existing_protected,
            )
            # 优先使用传入 config，其次保留原池 config
            if config is not None:
                entry["config"] = dict(config)
            elif existing_config:
                entry["config"] = existing_config
            payload["skills"][skill_name] = entry

        _mutate_json(
            get_pool_skill_manifest_path(working_dir=self.working_dir),
            _default_pool_manifest(),
            _update,
        )
        return skill_name

    def import_from_zip(
        self,
        data: bytes,
        overwrite: bool = True,
        target_name: str | None = None,
        rename_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        pool_dir = get_skill_pool_dir(working_dir=self.working_dir)
        tmp_dir, found = _extract_zip_skills(data)
        renames = rename_map or {}
        try:
            normalized_target = str(target_name or "").strip()
            if normalized_target:
                normalized_target = _normalize_skill_dir_name(
                    normalized_target,
                )
                if len(found) != 1:
                    raise ValueError(
                        "target_name is only supported for "
                        "single-skill zip imports",
                    )
                found = [(found[0][0], normalized_target)]
            found = [
                (d, _normalize_skill_dir_name(renames.get(n, n)))
                for d, n in found
            ]
            manifest = read_skill_pool_manifest(
                reconcile=False,
                working_dir=self.working_dir,
            )
            existing_pool_names = (
                set(
                    manifest.get("skills", {}).keys(),
                )
                | {p.name for p in pool_dir.iterdir() if p.is_dir()}
                if pool_dir.exists()
                else set(
                    manifest.get("skills", {}).keys(),
                )
            )
            for skill_dir, skill_name in found:
                _scan_skill_dir_or_raise(skill_dir, skill_name)
            conflicts: list[dict[str, Any]] = []
            planned: list[tuple[Path, str]] = []
            seen_names: set[str] = set()
            for skill_dir, skill_name in found:
                if skill_name in seen_names:
                    conflicts.append(
                        _build_import_conflict(
                            skill_name,
                            existing_pool_names,
                        ),
                    )
                    continue
                seen_names.add(skill_name)
                existing = manifest.get("skills", {}).get(
                    skill_name,
                )
                occupied = (
                    existing is not None or (pool_dir / skill_name).exists()
                )
                # overwrite=False 时任何冲突均拒绝
                if occupied and not overwrite:
                    conflicts.append(
                        _build_import_conflict(
                            skill_name,
                            existing_pool_names,
                        ),
                    )
                    continue
                planned.append((skill_dir, skill_name))
            imported: list[str] = []
            for skill_dir, skill_name in planned:
                if _import_skill_dir(
                    skill_dir,
                    pool_dir,
                    skill_name,
                    True,
                ):
                    imported.append(skill_name)

            if imported:

                def _update(payload: dict[str, Any]) -> None:
                    payload.setdefault("skills", {})
                    for name in imported:
                        # 覆盖时保留原技能的 config 和 protected
                        old_entry = manifest.get("skills", {}).get(name)
                        old_config = (
                            old_entry.get("config") if old_entry else None
                        )
                        old_protected = (
                            old_entry.get("protected", False)
                            if old_entry
                            else False
                        )
                        entry = _build_skill_metadata(
                            name,
                            pool_dir / name,
                            source="customized",
                            protected=old_protected,
                        )
                        if old_config:
                            entry["config"] = old_config
                        payload["skills"][name] = entry

                _mutate_json(
                    get_pool_skill_manifest_path(
                        working_dir=self.working_dir,
                    ),
                    _default_pool_manifest(),
                    _update,
                )
            return {
                "imported": imported,
                "count": len(imported),
                "conflicts": conflicts,
            }
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def delete_skill(self, name: str) -> bool:
        skill_name = str(name or "")
        manifest = read_skill_pool_manifest(
            reconcile=False,
            working_dir=self.working_dir,
        )
        entry = manifest.get("skills", {}).get(skill_name)
        if entry is None:
            return False

        skill_dir = (
            get_skill_pool_dir(working_dir=self.working_dir) / skill_name
        )
        if skill_dir.exists():
            shutil.rmtree(skill_dir)

        def _update(payload: dict[str, Any]) -> None:
            payload.get("skills", {}).pop(skill_name, None)

        _mutate_json(
            get_pool_skill_manifest_path(working_dir=self.working_dir),
            _default_pool_manifest(),
            _update,
        )
        return True

    def get_edit_target_name(
        self,
        skill_name: str,
        *,
        target_name: str | None = None,
    ) -> dict[str, Any]:
        manifest = read_skill_pool_manifest(
            reconcile=False,
            working_dir=self.working_dir,
        )
        entry = manifest.get("skills", {}).get(skill_name)
        if entry is None:
            return {"success": False, "reason": "not_found"}

        pool_names = set(manifest.get("skills", {}).keys())
        normalized_target = _normalize_skill_dir_name(
            target_name or skill_name,
        )
        if normalized_target == skill_name:
            return {
                "success": True,
                "mode": "edit",
                "name": skill_name,
            }

        existing = manifest.get("skills", {}).get(normalized_target)
        if existing is not None:
            return {
                "success": False,
                "reason": "conflict",
                "mode": "rename",
                "suggested_name": suggest_conflict_name(
                    normalized_target,
                    pool_names,
                ),
            }
        return {
            "success": True,
            "mode": "rename",
            "name": normalized_target,
        }

    def save_pool_skill(
        self,
        *,
        skill_name: str,
        content: str,
        references: dict[str, Any] | None = None,
        scripts: dict[str, Any] | None = None,
        extra_files: dict[str, Any] | None = None,
        target_name: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _validate_skill_content(content)
        manifest = read_skill_pool_manifest(
            reconcile=False,
            working_dir=self.working_dir,
        )
        entry = manifest.get("skills", {}).get(skill_name)
        if entry is None:
            return {"success": False, "reason": "not_found"}

        edit_target = self.get_edit_target_name(
            skill_name,
            target_name=target_name,
        )
        if not edit_target.get("success"):
            return edit_target

        final_name = str(edit_target["name"])
        is_rename = (
            str(edit_target["mode"]) == "rename" and final_name != skill_name
        )
        keep_original = _is_pool_builtin_entry(entry) and is_rename
        skill_dir = (
            get_skill_pool_dir(working_dir=self.working_dir) / final_name
        )
        old_skill_dir = (
            get_skill_pool_dir(working_dir=self.working_dir) / skill_name
        )
        old_sig = str(entry.get("signature", ""))
        new_config = (
            config if config is not None else entry.get("config") or {}
        )

        with _staged_skill_dir(final_name) as staged_dir:
            _write_skill_to_dir(
                staged_dir,
                content,
                references,
                scripts,
                extra_files,
            )
            _scan_skill_dir_or_raise(staged_dir, final_name)
            new_sig = _build_signature(staged_dir)
            content_changed = new_sig != old_sig

            if (
                not is_rename
                and not content_changed
                and new_config == (entry.get("config") or {})
            ):
                return {
                    "success": True,
                    "mode": "noop",
                    "name": skill_name,
                }

            if not is_rename and _is_pool_builtin_entry(entry):
                return {
                    "success": False,
                    "reason": "conflict",
                    "mode": "rename",
                    "suggested_name": suggest_conflict_name(
                        skill_name,
                        set(manifest.get("skills", {}).keys()),
                    ),
                }

            if is_rename or content_changed:
                _copy_skill_dir(staged_dir, skill_dir)

        if is_rename and not keep_original and old_skill_dir.exists():
            shutil.rmtree(old_skill_dir)

        next_entry = _build_skill_metadata(
            final_name,
            skill_dir,
            source="customized",
            protected=False,
        )
        next_entry["config"] = new_config

        def _update(payload: dict[str, Any]) -> None:
            payload.setdefault("skills", {})
            payload["skills"][final_name] = next_entry
            if is_rename and not keep_original:
                payload["skills"].pop(skill_name, None)

        _mutate_json(
            get_pool_skill_manifest_path(working_dir=self.working_dir),
            _default_pool_manifest(),
            _update,
        )
        return {
            "success": True,
            "mode": str(edit_target["mode"]),
            "name": final_name,
        }

    def upload_from_workspace(
        self,
        workspace_dir: Path,
        skill_name: str,
        *,
        target_name: str | None = None,
        overwrite: bool = True,
    ) -> dict[str, Any]:
        source_dir = get_workspace_skills_dir(workspace_dir) / skill_name
        if not source_dir.exists():
            return {"success": False, "reason": "not_found"}

        final_name = _normalize_skill_dir_name(target_name or skill_name)
        target_dir = (
            get_skill_pool_dir(working_dir=self.working_dir) / final_name
        )
        manifest = read_skill_pool_manifest(
            reconcile=False,
            working_dir=self.working_dir,
        )
        existing = manifest.get("skills", {}).get(final_name)
        if existing is not None and not overwrite:
            return {
                "success": False,
                "reason": "conflict",
                "suggested_name": suggest_conflict_name(
                    final_name,
                ),
            }

        # 覆盖时保留原技能的 config 和 protected
        existing_config = existing.get("config") if existing else None
        existing_protected = (
            existing.get("protected", False) if existing else False
        )

        with _staged_skill_dir(final_name) as staged_dir:
            _copy_skill_dir(source_dir, staged_dir)
            _scan_skill_dir_or_raise(staged_dir, final_name)
            _copy_skill_dir(staged_dir, target_dir)

        ws_manifest = _read_json(
            get_workspace_skill_manifest_path(workspace_dir),
            _default_workspace_manifest(),
        )
        workspace_entry = ws_manifest.get("skills", {}).get(skill_name, {})
        ws_config = workspace_entry.get("config") or {}

        def _update(payload: dict[str, Any]) -> None:
            payload.setdefault("skills", {})
            entry = _build_skill_metadata(
                final_name,
                target_dir,
                source="customized",
                protected=existing_protected,
            )
            # 优先使用工作区 config，其次保留原池 config
            if ws_config:
                entry["config"] = ws_config
            elif existing_config:
                entry["config"] = existing_config
            payload["skills"][final_name] = entry

        _mutate_json(
            get_pool_skill_manifest_path(working_dir=self.working_dir),
            _default_pool_manifest(),
            _update,
        )

        return {"success": True, "name": final_name}

    def replace_pool_skill_from_dir(
        self,
        *,
        skill_name: str,
        source_dir: Path,
        source: str = "customized",
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        final_name = _normalize_skill_dir_name(skill_name)
        target_dir = (
            get_skill_pool_dir(working_dir=self.working_dir) / final_name
        )

        with _staged_skill_dir(final_name) as staged_dir:
            _copy_skill_dir(source_dir, staged_dir)
            _scan_skill_dir_or_raise(staged_dir, final_name)
            _copy_skill_dir(staged_dir, target_dir)

        def _update(payload: dict[str, Any]) -> None:
            payload.setdefault("skills", {})
            entry = _build_skill_metadata(
                final_name,
                target_dir,
                source=source,
                protected=False,
                compute_signature=source == "builtin",
            )
            if config is not None:
                entry["config"] = dict(config)
            payload["skills"][final_name] = entry

        _mutate_json(
            get_pool_skill_manifest_path(working_dir=self.working_dir),
            _default_pool_manifest(),
            _update,
        )

        return {"success": True, "name": final_name}

    def _workspace_download_preflight(
        self,
        skill_name: str,
        workspace_dir: Path,
        *,
        target_name: str | None,
        overwrite: bool,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        manifest = read_skill_pool_manifest(
            reconcile=False,
            working_dir=self.working_dir,
        )
        entry = manifest.get("skills", {}).get(skill_name)
        if entry is None:
            return None, {"success": False, "reason": "not_found"}

        final_name = _normalize_skill_dir_name(target_name or skill_name)
        workspace_manifest = read_skill_manifest(
            workspace_dir,
            reconcile=False,
        )
        existing = workspace_manifest.get("skills", {}).get(final_name)
        workspace_identity = get_workspace_identity(workspace_dir)
        if _has_unmanaged_workspace_skill_conflict(
            workspace_dir,
            final_name,
            workspace_manifest,
        ):
            return None, {
                "success": False,
                "reason": "conflict",
                "workspace_id": workspace_identity["workspace_id"],
                "workspace_name": workspace_identity["workspace_name"],
                "suggested_name": suggest_conflict_name(final_name),
            }
        if existing is not None and not overwrite:
            return None, _existing_workspace_download_result(
                entry,
                existing,
                final_name,
                workspace_identity,
            )
        return {
            "entry": entry,
            "existing": existing,
            "final_name": final_name,
            "workspace_identity": workspace_identity,
        }, None

    @staticmethod
    def _install_pool_skill_in_workspace(
        source_dir: Path,
        target_dir: Path,
        other_dir: Path,
        final_name: str,
    ) -> None:
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        with _staged_skill_dir(final_name) as staged_dir:
            _copy_skill_dir(source_dir, staged_dir)
            _scan_skill_dir_or_raise(staged_dir, final_name)
            _copy_skill_dir(staged_dir, target_dir)
        if other_dir.exists():
            shutil.rmtree(other_dir)

    @staticmethod
    def _workspace_download_settings(
        entry: dict[str, Any],
        existing: dict[str, Any] | None,
    ) -> tuple[bool, list[str], dict[str, Any], str]:
        if existing is None:
            return True, ["all"], dict(entry.get("config") or {}), _timestamp()
        return (
            bool(existing.get("enabled", True)),
            existing.get("channels") or ["all"],
            dict(existing.get("config") or {}),
            existing.get("created_at") or _timestamp(),
        )

    @staticmethod
    def _write_workspace_download_manifest(
        workspace_dir: Path,
        final_name: str,
        target_dir: Path,
        entry: dict[str, Any],
        *,
        enabled: bool,
        channels: list[str],
        config: dict[str, Any],
        created_at: str,
    ) -> None:
        def _update(payload: dict[str, Any]) -> None:
            payload.setdefault("skills", {})
            metadata = _build_skill_metadata(
                final_name,
                target_dir,
                source=(
                    "builtin"
                    if entry.get("source") == "builtin"
                    else "customized"
                ),
                protected=False,
            )
            payload["skills"][final_name] = {
                "enabled": enabled,
                "channels": channels,
                "source": metadata["source"],
                "config": config,
                "metadata": metadata,
                "requirements": metadata["requirements"],
                "created_at": created_at,
                "updated_at": _timestamp(),
            }

        _mutate_json(
            get_workspace_skill_manifest_path(workspace_dir),
            _default_workspace_manifest(),
            _update,
        )

    @coordinate_workspace_skill_mutation
    def download_to_workspace(
        self,
        skill_name: str,
        workspace_dir: Path,
        *,
        target_name: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        context, result = self._workspace_download_preflight(
            skill_name,
            workspace_dir,
            target_name=target_name,
            overwrite=overwrite,
        )
        if result is not None:
            return result

        assert context is not None
        entry = context["entry"]
        existing = context["existing"]
        final_name = context["final_name"]
        workspace_identity = context["workspace_identity"]
        settings = self._workspace_download_settings(
            entry,
            existing,
        )
        enabled, channels, config, created_at = settings
        source_dir = (
            get_skill_pool_dir(working_dir=self.working_dir) / skill_name
        )
        target_dir = resolve_workspace_managed_skill_dir(
            workspace_dir,
            final_name,
            enabled=enabled,
        )
        other_dir = resolve_workspace_managed_skill_dir(
            workspace_dir,
            final_name,
            enabled=not enabled,
        )
        self._install_pool_skill_in_workspace(
            source_dir,
            target_dir,
            other_dir,
            final_name,
        )
        self._write_workspace_download_manifest(
            workspace_dir,
            final_name,
            target_dir,
            entry,
            enabled=enabled,
            channels=channels,
            config=config,
            created_at=created_at,
        )
        return {
            "success": True,
            "name": final_name,
            "workspace_id": workspace_identity["workspace_id"],
            "workspace_name": workspace_identity["workspace_name"],
        }

    def preflight_download_to_workspace(
        self,
        skill_name: str,
        workspace_dir: Path,
        *,
        target_name: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        context, result = self._workspace_download_preflight(
            skill_name,
            workspace_dir,
            target_name=target_name,
            overwrite=overwrite,
        )
        if result is not None:
            return result

        assert context is not None
        return {
            "success": True,
            "workspace_id": context["workspace_identity"]["workspace_id"],
            "workspace_name": context["workspace_identity"]["workspace_name"],
            "name": context["final_name"],
        }

# -*- coding: utf-8 -*-
"""Process-local, immutable snapshots for workspace skill resolution."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import wraps
import logging
from pathlib import Path
from threading import RLock
import time
from types import MappingProxyType
from typing import Any, Mapping

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManifestStat:
    mtime_ns: int
    size: int
    inode: int


@dataclass(frozen=True)
class SkillRuntimeSnapshot:
    directory: Path
    metadata: Mapping[str, Any]
    content_signature: str
    freshness_token: str
    runtime_profile: Any
    config: Mapping[str, Any]
    requirements: Mapping[str, Any]
    channels: tuple[str, ...]


@dataclass(frozen=True)
class WorkspaceSkillSnapshot:
    workspace_dir: Path
    generation: int
    manifest_stat: ManifestStat
    skills: Mapping[str, SkillRuntimeSnapshot]


_LOCK = RLock()
_CACHE: dict[Path, WorkspaceSkillSnapshot] = {}
_GENERATION = 0
_COORDINATORS: dict[Path, RLock] = {}


@contextmanager
def workspace_skill_coordinator(workspace_dir: Path):
    """Serialize workspace skill mutations and snapshot publication.

    The lock is process-local (the manifest file lock remains the
    cross-process guard).  Keeping it separate from ``_LOCK`` ensures a
    reconcile/scan cannot race a local manifest mutation while a snapshot is
    being assembled.
    """
    key = Path(workspace_dir).expanduser().resolve()
    with _LOCK:
        coordinator = _COORDINATORS.setdefault(key, RLock())
    with coordinator:
        yield


def coordinate_workspace_skill_mutation(func):
    """Wrap a workspace skill mutation in the process-local coordinator."""

    @wraps(func)
    def wrapped(self, *args, **kwargs):
        workspace_dir = getattr(self, "workspace_dir", None)
        if workspace_dir is None:
            workspace_dir = kwargs.get("workspace_dir")
        if workspace_dir is None and len(args) >= 2:
            workspace_dir = args[1]
        if workspace_dir is None:
            return func(self, *args, **kwargs)
        with workspace_skill_coordinator(workspace_dir):
            return func(self, *args, **kwargs)

    return wrapped


def _stat(path: Path) -> ManifestStat:
    try:
        value = path.stat()
    except OSError:
        return ManifestStat(0, 0, 0)
    return ManifestStat(value.st_mtime_ns, value.st_size, value.st_ino)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze(item) for key, item in value.items()},
        )
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _fresh(snapshot: WorkspaceSkillSnapshot, manifest_path: Path) -> bool:
    if snapshot.manifest_stat != _stat(manifest_path):
        return False
    from .skills_manager import get_skill_freshness_token

    return all(
        get_skill_freshness_token(skill.directory) == skill.freshness_token
        for skill in snapshot.skills.values()
    )


# pylint: disable-next=too-many-statements
def get_workspace_skill_snapshot(
    workspace_dir: Path,
    *,
    reconcile: bool = True,
    fail_closed: bool = False,
    _scan_direct: bool = False,
    _retry: int = 0,
) -> WorkspaceSkillSnapshot:
    """Return a cached workspace snapshot, reconciling only on invalidation."""
    global _GENERATION
    capture_started_at = time.monotonic()
    workspace_dir = workspace_dir.expanduser().resolve()
    from .skills_manager import (
        _build_signature,
        _build_skill_metadata,
        get_workspace_skill_manifest_path,
        get_skill_freshness_token,
        read_skill_manifest,
        resolve_workspace_managed_skill_dir,
    )

    manifest_path = get_workspace_skill_manifest_path(workspace_dir)
    with workspace_skill_coordinator(workspace_dir):
        with _LOCK:
            previous = _CACHE.get(workspace_dir)
            if previous is not None and _fresh(previous, manifest_path):
                logger.debug(
                    "skill_manifest_cache_hit=true skill_count=%d "
                    "runtime_skill_snapshot_generation=%d",
                    len(previous.skills),
                    previous.generation,
                )
                return previous
        reconcile_started_at = time.monotonic()
        manifest_available = True
        try:
            manifest = read_skill_manifest(workspace_dir, reconcile=reconcile)
        except Exception as exc:  # noqa: BLE001
            if not fail_closed:
                raise
            # Query startup is fail-closed: an unreadable or malformed
            # manifest must not prevent the query from running, and must not
            # allow any workspace skill whose state cannot be confirmed.
            logger.warning(
                "Workspace skill manifest unavailable; continuing without "
                "workspace skills: %s",
                exc,
            )
            manifest_available = False
            manifest = {"skills": {}}
        logger.debug(
            "skill_manifest_reconcile_ms=%.1f skill_manifest_cache_hit=false",
            (time.monotonic() - reconcile_started_at) * 1000,
        )
        manifest_stat_before = _stat(manifest_path)
        entries = manifest.get("skills", {})
        skills: dict[str, SkillRuntimeSnapshot] = {}
        parse_started_at = time.monotonic()
        for name, entry in sorted(entries.items()):
            if not isinstance(entry, dict) or not entry.get("enabled", False):
                continue
            try:
                directory = resolve_workspace_managed_skill_dir(
                    workspace_dir,
                    name,
                    enabled=True,
                )
                if not directory.is_dir():
                    continue
                signature = _build_signature(directory)
                freshness = get_skill_freshness_token(directory)
                metadata = entry.get("metadata")
                if not isinstance(metadata, dict) or not metadata.get(
                    "description",
                ):
                    metadata = _build_skill_metadata(
                        name,
                        directory,
                        source=str(entry.get("source", "customized")),
                        compute_signature=False,
                    )
                from .skill_runtime_profile import build_skill_runtime_profile
                from ..security.skill_scanner import (
                    SkillScanError,
                    _get_scan_mode,
                    is_skill_whitelisted,
                    scan_skill_directory,
                )

                scan_mode = _get_scan_mode()
                try:
                    scan_result = scan_skill_directory(
                        directory,
                        skill_name=name,
                        # Let the scanner apply the configured block/warn/off
                        # policy.  Forcing ``block=True`` here made warn mode
                        # silently drop otherwise loadable skills.
                        block=None,
                        _direct=_scan_direct,
                    )
                    if (
                        scan_result is None
                        and scan_mode != "off"
                        and not is_skill_whitelisted(name, directory)
                    ):
                        logger.warning(
                            "Workspace skill '%s' excluded because scan did not complete",
                            name,
                        )
                        continue
                except SkillScanError:
                    if scan_mode == "block":
                        logger.warning(
                            "Workspace skill '%s' excluded after security scan",
                            name,
                        )
                        continue
                    # A scanner implementation may still raise while the
                    # effective policy is warn (for example during a config
                    # transition).  Warn mode is explicitly non-blocking.
                    logger.warning(
                        "Workspace skill '%s' has scanner findings; "
                        "continuing because scan mode is warn",
                        name,
                    )

                skills[name] = SkillRuntimeSnapshot(
                    directory=directory.resolve(),
                    metadata=_freeze(dict(metadata)),
                    content_signature=signature,
                    freshness_token=freshness,
                    runtime_profile=build_skill_runtime_profile(
                        directory.resolve(),
                        name,
                    ),
                    config=_freeze(dict(entry.get("config") or {})),
                    requirements=_freeze(
                        dict(entry.get("requirements") or {}),
                    ),
                    channels=tuple(entry.get("channels") or ("all",)),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Workspace skill '%s' excluded from snapshot: %s",
                    name,
                    exc,
                )
        manifest_stat_after = _stat(manifest_path)
        skills_still_current = all(
            get_skill_freshness_token(skill.directory) == skill.freshness_token
            for skill in skills.values()
        )
        if _retry < 1 and (
            manifest_stat_before != manifest_stat_after
            or not skills_still_current
        ):
            logger.info(
                "Workspace skill snapshot changed during capture; retrying",
            )
            return get_workspace_skill_snapshot(
                workspace_dir,
                reconcile=reconcile,
                fail_closed=fail_closed,
                _scan_direct=_scan_direct,
                _retry=_retry + 1,
            )
        with _LOCK:
            _GENERATION += 1
            snapshot = WorkspaceSkillSnapshot(
                workspace_dir=workspace_dir,
                generation=_GENERATION,
                manifest_stat=manifest_stat_after,
                skills=MappingProxyType(skills),
            )
            if manifest_available:
                _CACHE[workspace_dir] = snapshot
            logger.debug(
                "skill_md_parse_ms=%.1f skill_count=%d "
                "runtime_skill_snapshot_generation=%d "
                "snapshot_capture_ms=%.1f",
                (time.monotonic() - parse_started_at) * 1000,
                len(skills),
                snapshot.generation,
                (time.monotonic() - capture_started_at) * 1000,
            )
            return snapshot


def invalidate_workspace_skill_snapshot(workspace_dir: Path) -> None:
    with _LOCK:
        _CACHE.pop(workspace_dir.expanduser().resolve(), None)


async def get_workspace_skill_snapshot_async(
    workspace_dir: Path,
    *,
    reconcile: bool = True,
) -> WorkspaceSkillSnapshot:
    """Build or fetch a snapshot without blocking the event loop."""
    from ..security.skill_scanner import _get_scan_executor

    executor, _slot = _get_scan_executor()
    future = executor.submit(
        get_workspace_skill_snapshot,
        workspace_dir,
        reconcile=reconcile,
        fail_closed=True,
        _scan_direct=True,
    )
    return await asyncio.wrap_future(future)


def _filter_changed_skills(
    snapshot: WorkspaceSkillSnapshot,
) -> WorkspaceSkillSnapshot:
    """Drop skills whose content changed after the snapshot was captured."""
    from .skills_manager import _build_signature, get_skill_freshness_token

    valid: dict[str, SkillRuntimeSnapshot] = {}
    changed = False
    for name, skill in snapshot.skills.items():
        # The recursive stat token is the cheap admission check. Hash the
        # directory only when metadata/size/timestamps changed.
        current_freshness = get_skill_freshness_token(skill.directory)
        if current_freshness == skill.freshness_token:
            valid[name] = skill
            continue
        if _build_signature(skill.directory) == skill.content_signature:
            # A metadata-only touch is still the same content, but update the
            # token so subsequent query admissions stay on the stat-only path.
            valid[name] = replace(skill, freshness_token=current_freshness)
            changed = True
    if not changed and len(valid) == len(snapshot.skills):
        return snapshot
    return WorkspaceSkillSnapshot(
        workspace_dir=snapshot.workspace_dir,
        generation=snapshot.generation,
        manifest_stat=snapshot.manifest_stat,
        skills=MappingProxyType(valid),
    )


async def validate_workspace_skill_snapshot(
    snapshot: WorkspaceSkillSnapshot,
) -> WorkspaceSkillSnapshot:
    """Recheck content signatures off-loop immediately before consumption."""
    return await asyncio.to_thread(_filter_changed_skills, snapshot)

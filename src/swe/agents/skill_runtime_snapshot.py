# -*- coding: utf-8 -*-
"""Process-local, immutable snapshots for workspace skill resolution."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass
from functools import partial, wraps
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
    manifest_available: bool = True
    unconfirmed_skills: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _CacheEntry:
    snapshot: WorkspaceSkillSnapshot
    expires_at: float


SNAPSHOT_TTL_SECONDS = 600
REFRESH_FAILURE_COOLDOWN_SECONDS = 30
PARTIAL_REFRESH_RETRY_SECONDS = 180


_LOCK = RLock()
_CACHE: dict[Path, _CacheEntry] = {}
_GENERATION = 0
_COORDINATORS: dict[Path, RLock] = {}
_INFLIGHT: dict[Path, Future[WorkspaceSkillSnapshot]] = {}
_REFRESH_EXECUTOR = ThreadPoolExecutor(thread_name_prefix="skill-freshness")


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
    except FileNotFoundError:
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

    try:
        return all(
            get_skill_freshness_token(skill.directory) == skill.freshness_token
            for skill in snapshot.skills.values()
        )
    except Exception:  # noqa: BLE001
        # Rebuild so an unreadable skill does not discard confirmed peers.
        return False


def _read_workspace_skill_manifest(
    workspace_dir: Path,
    *,
    reconcile: bool,
    fail_closed: bool,
) -> tuple[dict[str, Any], bool]:
    from .skills_manager import read_skill_manifest

    try:
        return read_skill_manifest(workspace_dir, reconcile=reconcile), True
    except Exception as exc:  # noqa: BLE001
        if not fail_closed:
            raise
        logger.warning(
            "Workspace skill manifest unavailable; continuing without "
            "workspace skills: %s",
            exc,
        )
        return {"skills": {}}, False


def _cached_workspace_skill_snapshot(
    workspace_dir: Path,
) -> WorkspaceSkillSnapshot | None:
    with _LOCK:
        entry = _CACHE.get(workspace_dir)
        if entry is None or time.monotonic() >= entry.expires_at:
            return None
        previous = entry.snapshot
        logger.debug(
            "skill_manifest_cache_hit=true skill_count=%d "
            "runtime_skill_snapshot_generation=%d",
            len(previous.skills),
            previous.generation,
        )
        return previous


def _build_workspace_skill_runtime_snapshot(
    workspace_dir: Path,
    name: str,
    entry: dict[str, Any],
) -> SkillRuntimeSnapshot | None:
    from .skills_manager import (
        _build_signature,
        _build_skill_metadata,
        get_skill_freshness_token,
        resolve_workspace_managed_skill_dir,
    )

    directory = resolve_workspace_managed_skill_dir(
        workspace_dir,
        name,
        enabled=True,
    )
    if not directory.is_dir():
        return None
    freshness = get_skill_freshness_token(directory)
    signature = _build_signature(directory)
    metadata = entry.get("metadata")
    if not isinstance(metadata, dict) or not metadata.get("description"):
        metadata = _build_skill_metadata(
            name,
            directory,
            source=str(entry.get("source", "customized")),
            compute_signature=False,
        )
    from .skill_runtime_profile import build_skill_runtime_profile

    return SkillRuntimeSnapshot(
        directory=directory.resolve(),
        metadata=_freeze(dict(metadata)),
        content_signature=signature,
        freshness_token=freshness,
        runtime_profile=build_skill_runtime_profile(
            directory.resolve(),
            name,
        ),
        config=_freeze(dict(entry.get("config") or {})),
        requirements=_freeze(dict(entry.get("requirements") or {})),
        channels=tuple(entry.get("channels") or ("all",)),
    )


def _build_workspace_skill_runtime_snapshots(
    workspace_dir: Path,
    entries: Any,
) -> tuple[dict[str, SkillRuntimeSnapshot], set[str]]:
    skills: dict[str, SkillRuntimeSnapshot] = {}
    unconfirmed: set[str] = set()
    for name, entry in sorted(entries.items()):
        if not isinstance(entry, dict) or not entry.get("enabled", False):
            continue
        try:
            skill = _build_workspace_skill_runtime_snapshot(
                workspace_dir,
                name,
                entry,
            )
            if skill is not None:
                skills[name] = skill
            else:
                unconfirmed.add(name)
        except Exception as exc:  # noqa: BLE001
            unconfirmed.add(name)
            logger.warning(
                "Workspace skill '%s' excluded from snapshot: %s",
                name,
                exc,
            )
    return skills, unconfirmed


def _publish_workspace_skill_snapshot(
    workspace_dir: Path,
    manifest_stat: ManifestStat,
    skills: dict[str, SkillRuntimeSnapshot],
    *,
    manifest_available: bool,
    unconfirmed_skills: set[str],
    capture_started_at: float,
) -> WorkspaceSkillSnapshot:
    global _GENERATION
    with _LOCK:
        _GENERATION += 1
        snapshot = WorkspaceSkillSnapshot(
            workspace_dir=workspace_dir,
            generation=_GENERATION,
            manifest_stat=manifest_stat,
            skills=MappingProxyType(skills),
            manifest_available=manifest_available,
            unconfirmed_skills=frozenset(unconfirmed_skills),
        )
        _cache_snapshot(snapshot)
        logger.debug(
            "skill_count=%d "
            "runtime_skill_snapshot_generation=%d "
            "snapshot_capture_ms=%.1f",
            len(skills),
            snapshot.generation,
            (time.monotonic() - capture_started_at) * 1000,
        )
        return snapshot


def _cache_snapshot(snapshot: WorkspaceSkillSnapshot) -> None:
    """Publish a result with a fixed window measured from completion."""
    if not snapshot.manifest_available:
        seconds = REFRESH_FAILURE_COOLDOWN_SECONDS
        outcome = "failure"
    elif snapshot.unconfirmed_skills:
        seconds = PARTIAL_REFRESH_RETRY_SECONDS
        outcome = "partial"
    else:
        seconds = SNAPSHOT_TTL_SECONDS
        outcome = "success"
    with _LOCK:
        _CACHE[snapshot.workspace_dir] = _CacheEntry(
            snapshot,
            time.monotonic() + seconds,
        )
    logger.debug(
        "skill_snapshot_refresh_outcome=%s skill_count=%d "
        "unconfirmed_skill_count=%d refresh_window_seconds=%d",
        outcome,
        len(snapshot.skills),
        len(snapshot.unconfirmed_skills),
        seconds,
    )


def _capture_workspace_skill_snapshot(
    workspace_dir: Path,
    manifest_path: Path,
    *,
    reconcile: bool,
) -> WorkspaceSkillSnapshot:
    """Bound capture retries and never publish an unstable skill as confirmed."""
    from .skills_manager import get_skill_freshness_token

    capture_started_at = time.monotonic()
    for attempt in range(2):
        manifest, available = _read_workspace_skill_manifest(
            workspace_dir,
            reconcile=reconcile,
            fail_closed=False,
        )
        before = _stat(manifest_path)
        skills, unconfirmed = _build_workspace_skill_runtime_snapshots(
            workspace_dir,
            manifest.get("skills", {}),
        )
        after = _stat(manifest_path)
        if before != after:
            if attempt == 0:
                continue
            raise RuntimeError(
                "Workspace skill manifest changed during capture",
            )
        unstable: set[str] = set()
        for name, skill in skills.items():
            try:
                if (
                    get_skill_freshness_token(skill.directory)
                    != skill.freshness_token
                ):
                    unstable.add(name)
            except Exception:  # noqa: BLE001
                unstable.add(name)
        if unstable and attempt == 0:
            continue
        for name in unstable:
            skills.pop(name)
        unconfirmed.update(unstable)
        return _publish_workspace_skill_snapshot(
            workspace_dir,
            after,
            skills,
            manifest_available=available,
            unconfirmed_skills=unconfirmed,
            capture_started_at=capture_started_at,
        )
    raise AssertionError("unreachable capture state")


def get_workspace_skill_snapshot(
    workspace_dir: Path,
    *,
    reconcile: bool = True,
    fail_closed: bool = False,
) -> WorkspaceSkillSnapshot:
    """Observe workspace skills once per fixed freshness window."""
    capture_started_at = time.monotonic()
    workspace_dir = workspace_dir.expanduser().resolve()
    from .skills_manager import (
        get_workspace_skill_manifest_path,
    )

    cached = _cached_workspace_skill_snapshot(workspace_dir)
    if cached is not None:
        return cached
    manifest_path = get_workspace_skill_manifest_path(workspace_dir)
    with workspace_skill_coordinator(workspace_dir):
        cached = _cached_workspace_skill_snapshot(workspace_dir)
        if cached is not None:
            return cached
        with _LOCK:
            previous = _CACHE.get(workspace_dir)
        try:
            if (
                previous is not None
                and previous.snapshot.manifest_available
                and not previous.snapshot.unconfirmed_skills
                and _fresh(previous.snapshot, manifest_path)
            ):
                _cache_snapshot(previous.snapshot)
                return previous.snapshot
            return _capture_workspace_skill_snapshot(
                workspace_dir,
                manifest_path,
                reconcile=reconcile,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Workspace skill refresh failed: %s", exc)
            failed = _publish_workspace_skill_snapshot(
                workspace_dir,
                ManifestStat(0, 0, 0),
                {},
                manifest_available=False,
                unconfirmed_skills=set(),
                capture_started_at=capture_started_at,
            )
            if not fail_closed:
                raise
            return failed


def invalidate_workspace_skill_snapshot(workspace_dir: Path) -> None:
    workspace_dir = workspace_dir.expanduser().resolve()
    with workspace_skill_coordinator(workspace_dir):
        with _LOCK:
            _CACHE.pop(workspace_dir, None)


async def get_workspace_skill_snapshot_async(
    workspace_dir: Path,
    *,
    reconcile: bool = True,
) -> WorkspaceSkillSnapshot:
    """Build or fetch a snapshot without blocking the event loop."""
    workspace_dir = workspace_dir.expanduser().resolve()
    cached = _cached_workspace_skill_snapshot(workspace_dir)
    if cached is not None:
        return cached
    with _LOCK:
        cached = _cached_workspace_skill_snapshot(workspace_dir)
        if cached is not None:
            return cached
        future = _INFLIGHT.get(workspace_dir)
        if future is None:
            # Preserve request context, while making queued work independent
            # of the lifetime of the event loop that requested it.
            refresh = partial(
                get_workspace_skill_snapshot,
                workspace_dir,
                reconcile=reconcile,
                fail_closed=True,
            )
            future = _REFRESH_EXECUTOR.submit(copy_context().run, refresh)
            _INFLIGHT[workspace_dir] = future
            future.add_done_callback(
                partial(_forget_shared_refresh, workspace_dir),
            )
    # A concurrent Future can be awaited from different event loops. Only
    # its owner occupies a worker; cancellation of a waiter is isolated.
    return await asyncio.shield(asyncio.wrap_future(future))


def _forget_shared_refresh(
    workspace_dir: Path,
    future: Future[WorkspaceSkillSnapshot],
) -> None:
    with _LOCK:
        if _INFLIGHT.get(workspace_dir) is future:
            _INFLIGHT.pop(workspace_dir)

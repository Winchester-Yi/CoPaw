# -*- coding: utf-8 -*-
"""
Skill security scanner for SWE.

Scans skills for security threats before they are activated or installed.

Architecture
~~~~~~~~~~~~

The scanner follows a lightweight, extensible design:

* **BaseAnalyzer** - abstract interface every analyzer must implement.
* **PatternAnalyzer** - YAML regex-signature matching (fast, line-based).
* **SkillScanner** - orchestrator that runs registered analyzers and
  aggregates findings into a :class:`ScanResult`.

This branch intentionally ships the baseline pattern analyzer only.
Additional analyzers can be plugged in later without changing the
orchestrator.

Quick start::

    from swe.security.skill_scanner import SkillScanner

    scanner = SkillScanner()
    result = scanner.scan_skill("/path/to/skill_directory")
    if not result.is_safe:
        print(f"Blocked: {result.max_severity.value} findings detected")
"""

from __future__ import annotations

import asyncio
from concurrent import futures
import hashlib
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .history import BlockedSkillRecord, SkillScanHistoryRecorder
from .models import (
    Finding,
    ScanResult,
    Severity,
    SkillFile,
    ThreatCategory,
)
from .scan_policy import ScanPolicy
from .analyzers import BaseAnalyzer
from .analyzers.ast_behavior_analyzer import AstBehaviorAnalyzer
from .analyzers.package_analyzer import PackageAnalyzer
from .analyzers.pattern_analyzer import PatternAnalyzer
from .scanner import SkillScanner

logger = logging.getLogger(__name__)

__all__ = [
    "BaseAnalyzer",
    "BlockedSkillRecord",
    "AstBehaviorAnalyzer",
    "Finding",
    "PackageAnalyzer",
    "PatternAnalyzer",
    "ScanPolicy",
    "ScanResult",
    "Severity",
    "SkillFile",
    "SkillScanner",
    "SkillScanError",
    "ThreatCategory",
    "compute_skill_content_hash",
    "install_skill_scan_history_recorder",
    "is_skill_whitelisted",
    "scan_skill_directory",
    "scan_skill_directory_async",
]

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


_VALID_MODES = {"block", "warn", "off"}


def _load_scanner_config() -> Any:
    """Load SkillScannerConfig from the app config (lazy import)."""
    try:
        from ...config import load_config

        return load_config().security.skill_scanner
    except Exception:
        return None


def _get_scan_mode(cfg: Any = None) -> str:
    """Return the effective scan mode: ``block``, ``warn``, or ``off``.

    Priority: env ``SWE_SKILL_SCAN_MODE`` > config > default ``warn``.
    """
    env = os.environ.get("SWE_SKILL_SCAN_MODE")
    if env is not None:
        val = env.lower().strip()
        if val in _VALID_MODES:
            return val
    if cfg is None:
        cfg = _load_scanner_config()
    return cfg.mode if cfg is not None else "block"


def _scan_timeout(cfg: Any = None) -> float:
    if cfg is None:
        cfg = _load_scanner_config()
    return float(cfg.timeout) if cfg is not None else 30.0


# ---------------------------------------------------------------------------
# Content hash
# ---------------------------------------------------------------------------


def compute_skill_content_hash(skill_dir: Path) -> str:
    """SHA-256 hash of all regular file contents in *skill_dir* (sorted)."""
    h = hashlib.sha256()
    try:
        for p in sorted(skill_dir.rglob("*")):
            if p.is_file() and not p.is_symlink():
                try:
                    h.update(p.read_bytes())
                except OSError:
                    pass
    except OSError:
        pass
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Whitelist helpers
# ---------------------------------------------------------------------------


def is_skill_whitelisted(
    skill_name: str,
    skill_dir: Path | None = None,
    *,
    cfg: Any = None,
) -> bool:
    """Return True if *skill_name* is on the whitelist.

    When a whitelist entry has a non-empty ``content_hash``, the hash must
    match the current directory contents for the entry to apply.
    """
    if cfg is None:
        cfg = _load_scanner_config()
    if cfg is None:
        return False
    for entry in cfg.whitelist:
        if entry.skill_name != skill_name:
            continue
        if not entry.content_hash:
            return True
        if skill_dir is not None:
            current_hash = compute_skill_content_hash(skill_dir)
            if current_hash == entry.content_hash:
                return True
        else:
            return True
    return False


# ---------------------------------------------------------------------------
# Blocked history persistence
# ---------------------------------------------------------------------------

_history_recorder: SkillScanHistoryRecorder | None = None


def install_skill_scan_history_recorder(
    recorder: SkillScanHistoryRecorder | None,
) -> None:
    """Install the application-scoped database history recorder."""
    global _history_recorder
    _history_recorder = recorder


def _finding_to_dict(f: Finding) -> dict[str, Any]:
    return {
        "severity": f.severity.value,
        "title": f.title,
        "description": f.description,
        "file_path": f.file_path,
        "line_number": f.line_number,
        "rule_id": f.rule_id,
        "analyzer": f.analyzer,
    }


def _record_blocked_skill(
    result: ScanResult,
    skill_dir: Path,
    *,
    action: str = "blocked",
    source_id: str = "",
    user_id: str = "",
    bbk_id: str = "",
) -> None:
    """Submit a scan alert to the database-backed history recorder."""
    record = BlockedSkillRecord(
        skill_name=result.skill_name,
        blocked_at=datetime.now(timezone.utc).isoformat(),
        max_severity=result.max_severity.value,
        findings=[_finding_to_dict(f) for f in result.findings],
        content_hash=compute_skill_content_hash(skill_dir),
        action=action,
        source_id=source_id,
        user_id=user_id,
        bbk_id=bbk_id,
    )
    recorder = _history_recorder
    if recorder is None:
        logger.error(
            "Skill scan history recorder is unavailable; record %s was dropped",
            record.id,
        )
        return
    try:
        accepted = recorder.submit(record)
    except Exception as exc:
        logger.error(
            "Failed to submit skill scan history record %s: %s",
            record.id,
            exc,
        )
        return
    if not accepted:
        logger.error(
            "Skill scan history recorder rejected record %s",
            record.id,
        )


# ---------------------------------------------------------------------------
# Lazy singleton (thread-safe)
# ---------------------------------------------------------------------------

_scanner_instance: SkillScanner | None = None
_scanner_lock = threading.Lock()
_scan_executor: futures.ThreadPoolExecutor | None = None
_scan_executor_workers: int | None = None
_scan_executor_slots: threading.BoundedSemaphore | None = None
_scan_executor_pid: int | None = None
_scan_executor_lock = threading.Lock()


def _get_scanner() -> SkillScanner:
    """Return a lazily-initialised :class:`SkillScanner` singleton."""
    global _scanner_instance
    if _scanner_instance is None:
        with _scanner_lock:
            if _scanner_instance is None:
                _scanner_instance = SkillScanner()
    return _scanner_instance


def _configured_scan_executor_workers() -> int:
    raw_value = os.environ.get("SWE_SKILL_SCAN_EXECUTOR_WORKERS")
    if raw_value:
        try:
            return max(1, int(raw_value))
        except ValueError:
            logger.warning(
                "Invalid SWE_SKILL_SCAN_EXECUTOR_WORKERS=%r; using 4",
                raw_value,
            )
    return 4


def _get_scan_executor() -> tuple[
    futures.ThreadPoolExecutor,
    threading.BoundedSemaphore,
]:
    """Return a shared, bounded executor for blocking scan work."""
    global _scan_executor, _scan_executor_pid, _scan_executor_slots
    global _scan_executor_workers
    workers = _configured_scan_executor_workers()
    current_pid = os.getpid()
    executor_stale = (
        _scan_executor is None
        or _scan_executor_workers != workers
        or _scan_executor_pid != current_pid
    )
    if executor_stale:
        with _scan_executor_lock:
            executor_stale = (
                _scan_executor is None
                or _scan_executor_workers != workers
                or _scan_executor_pid != current_pid
            )
            if executor_stale:
                if (
                    _scan_executor is not None
                    and _scan_executor_pid == current_pid
                ):
                    _scan_executor.shutdown(
                        wait=False,
                        cancel_futures=True,
                    )
                _scan_executor = futures.ThreadPoolExecutor(  # pylint: disable=consider-using-with
                    max_workers=workers,
                )
                _scan_executor_workers = workers
                _scan_executor_pid = current_pid
                _scan_executor_slots = threading.BoundedSemaphore(workers)
    assert _scan_executor is not None
    assert _scan_executor_slots is not None
    return _scan_executor, _scan_executor_slots


def _scan_with_slot_release(
    scanner: SkillScanner,
    resolved: Path,
    *,
    skill_name: str | None,
    slot: threading.BoundedSemaphore,
    queued_at: float | None = None,
) -> ScanResult:
    scan_started_at = time.monotonic()
    try:
        return scanner.scan_skill(resolved, skill_name=skill_name)
    finally:
        try:
            logger.debug(
                "skill_scan_queue_ms=%.1f skill_scan_ms=%.1f skill_name=%s",
                (
                    max(
                        0.0,
                        (scan_started_at - queued_at) * 1000,
                    )
                    if queued_at is not None
                    else 0.0
                ),
                (time.monotonic() - scan_started_at) * 1000,
                skill_name or resolved.name,
            )
        finally:
            slot.release()


# ---------------------------------------------------------------------------
# Scan result cache (mtime-based)
# ---------------------------------------------------------------------------

_MAX_CACHE_ENTRIES = 64
_scan_cache: dict[str, tuple[str, str, ScanResult]] = {}
_cache_lock = threading.Lock()


def _get_tree_stat_token(skill_dir: Path) -> str:
    """Return a cheap recursive path/stat fingerprint for cache probing."""
    digest = hashlib.blake2b(digest_size=16)
    try:
        for path in sorted(skill_dir.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            digest.update(path.relative_to(skill_dir).as_posix().encode())
            digest.update(str(stat.st_mtime_ns).encode())
            digest.update(str(stat.st_size).encode())
    except OSError:
        return "missing"
    return digest.hexdigest()


def _get_cached_result(
    skill_dir: Path,
) -> ScanResult | None:
    """Return a cached ScanResult if the directory hasn't changed."""
    key = str(skill_dir)
    with _cache_lock:
        entry = _scan_cache.get(key)
    if entry is None:
        return None
    cached_stat, cached_hash, cached_result = entry
    current_stat = _get_tree_stat_token(skill_dir)
    if current_stat == cached_stat:
        logger.debug(
            "Returning cached scan result for '%s'",
            cached_result.skill_name,
        )
        return cached_result
    current_hash = compute_skill_content_hash(skill_dir)
    if current_hash == cached_hash:
        with _cache_lock:
            _scan_cache[key] = (current_stat, cached_hash, cached_result)
        return cached_result
    return None


def _store_cached_result(
    skill_dir: Path,
    result: ScanResult,
) -> None:
    """Store a scan result in the cache (LRU eviction)."""
    key = str(skill_dir)
    stat_token = _get_tree_stat_token(skill_dir)
    content_hash = compute_skill_content_hash(skill_dir)
    with _cache_lock:
        _scan_cache.pop(key, None)
        _scan_cache[key] = (stat_token, content_hash, result)
        while len(_scan_cache) > _MAX_CACHE_ENTRIES:
            oldest = next(iter(_scan_cache))
            del _scan_cache[oldest]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _format_finding_location(f: Finding) -> str:
    if f.line_number is not None:
        return f"({f.file_path}:{f.line_number})"
    return f"({f.file_path})"


class SkillScanError(Exception):
    """Raised when a skill fails a security scan and blocking is enabled."""

    def __init__(self, result: ScanResult) -> None:
        self.result = result
        findings_summary = "; ".join(
            f"[{f.severity.value}] {f.title} " f"{_format_finding_location(f)}"
            for f in result.findings[:5]
        )
        truncated = (
            f" (and {len(result.findings) - 5} more)"
            if len(result.findings) > 5
            else ""
        )
        super().__init__(
            f"Security scan of skill '{result.skill_name}' found "
            f"{len(result.findings)} issue(s) "
            f"(max severity: {result.max_severity.value}): "
            f"{findings_summary}{truncated}",
        )


def scan_skill_directory(
    skill_dir: str | Path,
    *,
    skill_name: str | None = None,
    block: bool | None = None,
    timeout: float | None = None,
    _direct: bool = False,
    _cache_result: bool = True,
    source_id: str = "",
    user_id: str = "",
    bbk_id: str = "",
) -> ScanResult | None:
    """Scan a skill directory and optionally block on unsafe results.

    Parameters
    ----------
    skill_dir:
        Path to the skill directory to scan.
    skill_name:
        Human-readable name (falls back to directory name).
    block:
        Whether to raise :class:`SkillScanError` when the scan finds
        CRITICAL/HIGH issues.  *None* means use the configured mode
        (``block`` mode → True, ``warn`` mode → False).
    timeout:
        Maximum seconds to wait for the scan to complete before
        giving up and returning ``None``.  *None* reads from config.

    Returns
    -------
    ScanResult or None
        ``None`` when scanning is disabled, whitelisted, or timed out.

    Raises
    ------
    SkillScanError
        When blocking is enabled and the skill is deemed unsafe.
    """
    cfg = _load_scanner_config()
    mode = _get_scan_mode(cfg)
    if mode == "off":
        return None

    resolved = Path(skill_dir).resolve()
    effective_name = skill_name or resolved.name

    if is_skill_whitelisted(effective_name, resolved, cfg=cfg):
        logger.debug(
            "Skill '%s' is whitelisted, skipping scan",
            effective_name,
        )
        return None

    effective_timeout = timeout if timeout is not None else _scan_timeout(cfg)

    cached = _get_cached_result(resolved)
    if cached is not None:
        result = cached
        logger.debug(
            "skill_scan_queue_ms=0.0 skill_scan_ms=0.0 "
            "skill_scan_cache_hit=true skill_name=%s",
            effective_name,
        )
    else:
        scanner = _get_scanner()
        deadline = time.monotonic() + effective_timeout
        queued_at = time.monotonic()
        executor, slot = _get_scan_executor()
        # Slot acquisition is released by _scan_with_slot_release.
        # pylint: disable-next=consider-using-with
        if not slot.acquire(timeout=effective_timeout):
            logger.warning(
                "Security scan of skill '%s' timed out after %.0fs "
                "(waiting for scan executor)",
                effective_name,
                effective_timeout,
            )
            return None
        if _direct:
            result = _scan_with_slot_release(
                scanner,
                resolved,
                skill_name=skill_name,
                slot=slot,
                queued_at=queued_at,
            )
        else:
            try:
                future = executor.submit(
                    _scan_with_slot_release,
                    scanner,
                    resolved,
                    skill_name=skill_name,
                    slot=slot,
                    queued_at=queued_at,
                )
            except Exception:
                slot.release()
                raise

            future.add_done_callback(
                lambda completed: (
                    slot.release() if completed.cancelled() else None
                ),
            )
            remaining_timeout = max(0.0, deadline - time.monotonic())
            try:
                result = future.result(timeout=remaining_timeout)
            except futures.TimeoutError:
                logger.warning(
                    "Security scan of skill '%s' timed out after %.0fs",
                    effective_name,
                    effective_timeout,
                )
                future.cancel()
                return None

        if _cache_result:
            _store_cached_result(resolved, result)

    if not result.is_safe:
        should_block = block if block is not None else (mode == "block")
        if should_block:
            _record_blocked_skill(
                result,
                resolved,
                action="blocked",
                source_id=source_id,
                user_id=user_id,
                bbk_id=bbk_id,
            )
            raise SkillScanError(result)
        _record_blocked_skill(
            result,
            resolved,
            action="warned",
            source_id=source_id,
            user_id=user_id,
            bbk_id=bbk_id,
        )
        logger.warning(
            "Skill '%s' has %d security finding(s) (max severity: %s) "
            "but blocking is disabled – proceeding anyway.",
            result.skill_name,
            len(result.findings),
            result.max_severity.value,
        )

    return result


async def scan_skill_directory_async(
    skill_dir: str | Path,
    *,
    skill_name: str | None = None,
    block: bool | None = None,
    timeout: float | None = None,
) -> ScanResult | None:
    """Await the scanner on its bounded executor without nested pools."""
    loop = asyncio.get_running_loop()
    executor, _slot = _get_scan_executor()
    future = executor.submit(
        scan_skill_directory,
        skill_dir,
        skill_name=skill_name,
        block=block,
        timeout=timeout,
        _direct=True,
    )
    wrapped = asyncio.wrap_future(future, loop=loop)
    try:
        if timeout is None:
            return await wrapped
        return await asyncio.wait_for(wrapped, timeout=timeout)
    except asyncio.CancelledError:
        future.cancel()
        raise

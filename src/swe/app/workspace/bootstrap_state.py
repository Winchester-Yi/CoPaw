# -*- coding: utf-8 -*-
"""Strict tenant bootstrap readiness and persistence helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from uuid import uuid4

from ...config.config import AgentProfileConfig, Config

_REQUIRED_WORKSPACE_FILES = (
    "AGENTS.md",
    "HEARTBEAT.md",
    "MEMORY.md",
    "PROFILE.md",
    "SOUL.md",
)
_REQUIRED_WORKSPACE_DIRECTORIES = ("sessions", "memory")
_REQUIRED_WORKSPACE_JSON_FILES = (
    "jobs.json",
    "token_usage.json",
)


class TenantBootstrapUnavailable(RuntimeError):
    """Bootstrap cannot safely make the tenant available right now."""

    retry_after_seconds = 2


class SourceTemplateUnavailable(TenantBootstrapUnavailable):
    """The explicitly provisioned source template is unavailable."""


class BootstrapRecoveryFailure(TenantBootstrapUnavailable):
    """Recovery left the tenant short of strict bootstrap readiness."""


@dataclass(frozen=True)
class BootstrapReadiness:
    """Non-mutating result of strict tenant bootstrap validation."""

    ready: bool
    missing_paths: tuple[Path, ...]
    invalid_json_paths: tuple[Path, ...]
    reason: str


def inspect_bootstrap_readiness(
    tenant_dir: Path,
    *,
    expected_tenant_dir: Path | None = None,
) -> BootstrapReadiness:
    """Validate bootstrap artifacts without tolerant parsing or mutation."""
    from ..conversation_storage.runtime import (
        METADATA_AUTHORITY,
        storage_authority,
    )

    tenant_dir = Path(tenant_dir)
    workspace_dir = tenant_dir / "workspaces" / "default"
    expected_workspace_dir = (
        Path(expected_tenant_dir) / "workspaces" / "default"
        if expected_tenant_dir is not None
        else workspace_dir
    )
    missing_paths: list[Path] = []
    invalid_json_paths: list[Path] = []

    _check_default_workspace_config(
        tenant_dir,
        expected_workspace_dir,
        missing_paths,
        invalid_json_paths,
    )

    _require_directory(workspace_dir, missing_paths)
    for directory_name in _REQUIRED_WORKSPACE_DIRECTORIES:
        _require_directory(workspace_dir / directory_name, missing_paths)
    for file_name in _REQUIRED_WORKSPACE_FILES:
        _require_file(workspace_dir / file_name, missing_paths)

    agent_path = workspace_dir / "agent.json"
    agent_payload = _read_json_object(
        agent_path,
        missing_paths,
        invalid_json_paths,
    )
    if agent_payload is not None:
        try:
            agent = AgentProfileConfig.model_validate(agent_payload)
            if agent.id != "default" or not _same_path(
                agent.workspace_dir,
                expected_workspace_dir,
            ):
                invalid_json_paths.append(agent_path)
        except Exception:
            invalid_json_paths.append(agent_path)

    json_files = _REQUIRED_WORKSPACE_JSON_FILES
    if storage_authority() != METADATA_AUTHORITY:
        json_files = ("chats.json", *json_files)
    for file_name in json_files:
        _read_json_object(
            workspace_dir / file_name,
            missing_paths,
            invalid_json_paths,
        )

    missing = tuple(dict.fromkeys(missing_paths))
    invalid = tuple(dict.fromkeys(invalid_json_paths))
    if invalid:
        reason = "invalid_json"
    elif missing:
        reason = "missing_artifact"
    else:
        reason = "ready"
    return BootstrapReadiness(
        ready=not missing and not invalid,
        missing_paths=missing,
        invalid_json_paths=invalid,
        reason=reason,
    )


def _check_default_workspace_config(
    tenant_dir: Path,
    expected_workspace_dir: Path,
    missing_paths: list[Path],
    invalid_json_paths: list[Path],
) -> None:
    config_path = tenant_dir / "config.json"
    config_payload = _read_json_object(
        config_path,
        missing_paths,
        invalid_json_paths,
    )
    if config_payload is not None:
        try:
            config = Config.model_validate(config_payload)
            profile = config.agents.profiles.get("default")
            if (
                profile is None
                or profile.id != "default"
                or not profile.enabled
                or not _same_path(
                    profile.workspace_dir,
                    expected_workspace_dir,
                )
            ):
                invalid_json_paths.append(config_path)
        except Exception:
            invalid_json_paths.append(config_path)


def write_bootstrap_json(path: Path, payload: dict[str, Any]) -> None:
    """Durably publish a bootstrap-owned JSON file in its target directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    finally:
        temp_path.unlink(missing_ok=True)


def write_bootstrap_ready_marker(tenant_dir: Path) -> None:
    """Write a diagnostic marker after strict readiness has passed."""
    write_bootstrap_json(
        Path(tenant_dir) / ".bootstrap.ready",
        {
            "version": 1,
            "ready_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def move_to_recovery_backup(path: Path) -> Path:
    """Move one explicitly identified invalid JSON artifact to a backup."""
    path = Path(path)
    backup_path = path.with_name(f"{path.name}.{uuid4().hex}.bak")
    path.replace(backup_path)
    return backup_path


def _read_json_object(
    path: Path,
    missing_paths: list[Path],
    invalid_json_paths: list[Path],
) -> dict[str, Any] | None:
    if not path.is_file():
        missing_paths.append(path)
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        invalid_json_paths.append(path)
        return None
    if not isinstance(payload, dict):
        invalid_json_paths.append(path)
        return None
    return payload


def _require_file(path: Path, missing_paths: list[Path]) -> None:
    if not path.is_file():
        missing_paths.append(path)


def _require_directory(path: Path, missing_paths: list[Path]) -> None:
    if not path.is_dir():
        missing_paths.append(path)


def _same_path(value: str, expected: Path) -> bool:
    try:
        return Path(value).expanduser().resolve() == expected.resolve()
    except OSError:
        return False


def _fsync_directory(path: Path) -> None:
    directory_descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)

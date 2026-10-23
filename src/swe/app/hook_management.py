# -*- coding: utf-8 -*-
"""Default Agent Profile Hook configuration management."""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Awaitable, Callable

from pydantic import TypeAdapter, ValidationError

from ..agents.hook_runtime.executor import execute_handler
from ..agents.hook_runtime.models import (
    HookConfig,
    HookContext,
    HookHandlerConfig,
    HookHandlerResult,
    validate_handler_event,
)
from ..security.skill_scanner import SkillScanError, scan_skill_directory

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows compatibility path
    fcntl = None

logger = logging.getLogger(__name__)

ALLOWED_SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".zsh"}
MAX_SCRIPT_BYTES = 1 * 1024 * 1024
MAX_UPLOAD_FILES = 20

_HANDLER_ADAPTER = TypeAdapter(HookHandlerConfig)
_CONFIGURATION_THREAD_LOCK = threading.RLock()


class HookManagementConflict(Exception):
    """Raised when a client saves against an obsolete Hook revision."""


class HookManagementValidationError(ValueError):
    """Raised when Hook-console constraints reject a draft."""


@dataclass(frozen=True)
class HookAuditActor:
    """Request actor identity retained in Hook-management audit logs."""

    user_id: str | None
    tenant_id: str | None


@dataclass(frozen=True)
class HookConfigurationSnapshot:
    """A default profile Hook draft plus its optimistic-lock revision."""

    hooks: dict[str, Any]
    revision: str


@dataclass(frozen=True)
class UploadFilePayload:
    """One uploaded Hook-script file held in memory by the HTTP boundary."""

    filename: str
    content: bytes


@dataclass(frozen=True)
class HookScriptFailure:
    """A file-level upload failure that does not fail a whole batch."""

    filename: str
    reason: str


@dataclass(frozen=True)
class HookScriptUploadResult:
    """Independent outcomes for one batch of default-profile scripts."""

    accepted: tuple[str, ...] = ()
    warned: tuple[str, ...] = ()
    failed: tuple[HookScriptFailure, ...] = ()

    @property
    def accepted_names(self) -> list[str]:
        return list(self.accepted)


@dataclass(frozen=True)
class HookManualTestResult:
    """Bounded, redacted output from executing one draft Handler."""

    handler_result: HookHandlerResult
    redacted_summary: dict[str, Any]


@dataclass(frozen=True)
class HookDistributionScript:
    """A source-controlled script selected for a Hook distribution."""

    filename: str
    content: bytes
    sha256: str


@dataclass(frozen=True)
class HookDistributionPayload:
    """The latest saved source groups and their controlled script files."""

    matcher_group_ids: tuple[str, ...]
    groups_by_event: dict[str, list[dict[str, Any]]]
    scripts: tuple[HookDistributionScript, ...]
    revision: str


@dataclass(frozen=True)
class HookDistributionTargetResult:
    """Metadata about one successfully updated target Hook configuration."""

    matcher_group_ids: tuple[str, ...]
    script_names: tuple[str, ...]
    revision: str


@dataclass(frozen=True)
class _FileBackup:
    path: Path
    content: bytes | None
    mode: int | None


class HookManagementService:
    """Own Hook-console persistence for a tenant's default profile workspace."""

    def __init__(self, workspace_dir: Path, *, tenant_id: str | None) -> None:
        self._workspace_dir = Path(workspace_dir)
        self._tenant_id = tenant_id

    @property
    def _agent_config_path(self) -> Path:
        return self._workspace_dir / "agent.json"

    @property
    def _script_root(self) -> Path:
        return self._workspace_dir / "hooks" / "scripts"

    def get_configuration(self) -> HookConfigurationSnapshot:
        """Return the validated Hook configuration stored for the default profile."""
        hooks = self._load_hooks()
        return HookConfigurationSnapshot(
            hooks=hooks,
            revision=self._revision_for(hooks),
        )

    def save_configuration(
        self,
        *,
        hooks: dict[str, Any],
        expected_revision: str,
        actor: HookAuditActor,
    ) -> HookConfigurationSnapshot:
        """Validate and persist a Hook draft when its revision is current."""
        with self._configuration_lock():
            current = self.get_configuration()
            if expected_revision != current.revision:
                raise HookManagementConflict(
                    "hook configuration revision is stale",
                )

            normalized_hooks = self._validate_hooks(hooks)
            agent_config = self._load_agent_config()
            agent_config["hooks"] = normalized_hooks
            self._write_agent_config(agent_config)

        snapshot = HookConfigurationSnapshot(
            hooks=normalized_hooks,
            revision=self._revision_for(normalized_hooks),
        )
        self._emit_audit(
            event="configuration_saved",
            actor=actor,
            revision=snapshot.revision,
        )
        removed_group_ids, removed_handler_ids = self._removed_hook_ids(
            current.hooks,
            normalized_hooks,
        )
        if removed_group_ids or removed_handler_ids:
            self._emit_audit(
                event="configuration_removed",
                actor=actor,
                revision=snapshot.revision,
                details={
                    "removed_group_ids": removed_group_ids,
                    "removed_handler_ids": removed_handler_ids,
                },
            )
        return snapshot

    def upload_scripts(
        self,
        *,
        files: list[UploadFilePayload],
        overwrite_names: set[str],
        actor: HookAuditActor,
    ) -> HookScriptUploadResult:
        """Upload independent script files into the controlled library root."""
        if len(files) > MAX_UPLOAD_FILES:
            raise HookManagementValidationError(
                f"a batch may contain at most {MAX_UPLOAD_FILES} files",
            )

        with self._configuration_lock():
            return self._upload_scripts_locked(
                files=files,
                overwrite_names=overwrite_names,
                actor=actor,
            )

    def _upload_scripts_locked(
        self,
        *,
        files: list[UploadFilePayload],
        overwrite_names: set[str],
        actor: HookAuditActor,
    ) -> HookScriptUploadResult:
        script_root = self._ensure_script_root()
        accepted: list[str] = []
        warned: list[str] = []
        failed: list[HookScriptFailure] = []
        seen_names: set[str] = set()

        for file in files:
            try:
                self._validate_upload(file, seen_names)
                target = script_root / file.filename
                if target.is_symlink():
                    raise HookManagementValidationError(
                        "script target must not be a symbolic link",
                    )
                if target.exists() and file.filename not in overwrite_names:
                    raise HookManagementConflict(
                        f"script already exists: {file.filename}",
                    )

                old_hash = (
                    self._sha256_file(target) if target.exists() else None
                )
                scan_outcome = self._scan_upload(file)
                self._atomic_write(target, file.content)
                new_hash = hashlib.sha256(file.content).hexdigest()
                accepted.append(file.filename)
                if scan_outcome == "warning":
                    warned.append(file.filename)
                self._emit_audit(
                    event=(
                        "script_replaced"
                        if old_hash is not None
                        else "script_uploaded"
                    ),
                    actor=actor,
                    revision=self.get_configuration().revision,
                    details={
                        "filename": file.filename,
                        "old_sha256": old_hash,
                        "new_sha256": new_hash,
                        "scan_outcome": scan_outcome,
                    },
                )
            except (
                HookManagementValidationError,
                HookManagementConflict,
                OSError,
            ) as exc:
                failed.append(HookScriptFailure(file.filename, str(exc)))
            finally:
                seen_names.add(file.filename)

        return HookScriptUploadResult(
            accepted=tuple(accepted),
            warned=tuple(warned),
            failed=tuple(failed),
        )

    def list_scripts(self) -> list[dict[str, Any]]:
        """List only controlled script-library metadata, never file bodies."""
        if not self._script_root.exists():
            return []
        script_root = self._ensure_script_root()
        return [
            {
                "filename": path.name,
                "size": path.stat().st_size,
                "sha256": self._sha256_file(path),
            }
            for path in sorted(script_root.iterdir())
            if path.is_file()
            and not path.is_symlink()
            and path.suffix.lower() in ALLOWED_SCRIPT_SUFFIXES
        ]

    async def manual_test(
        self,
        *,
        handler: dict[str, Any],
        context: HookContext,
        actor: HookAuditActor,
        source_id: str | None = None,
    ) -> HookManualTestResult:
        """Run exactly one unsaved Handler with real external side effects."""
        revision = self.get_configuration().revision
        started_at = time.monotonic()
        self._emit_audit(
            event="manual_test_requested",
            actor=actor,
            revision=revision,
        )
        try:
            normalized_handler = self._parse_handler(
                handler,
                event_name=context.hook_event_name,
            )
            if (
                normalized_handler.output_append
                and not (context.assistant_response or "").strip()
            ):
                raise HookManagementValidationError(
                    "outputAppend requires nonempty context.assistant_response",
                )
            normalized_context = context.model_copy(
                update={
                    "tenant_id": self._tenant_id or context.tenant_id,
                    "effective_tenant_id": self._tenant_id
                    or context.effective_tenant_id,
                    "user_id": actor.user_id or context.user_id,
                    "agent_id": "default",
                    "workspace_dir": str(self._workspace_dir),
                    "source_id": source_id,
                },
            )
            handler_result = await execute_handler(
                normalized_handler,
                normalized_context,
                workspace_dir=self._workspace_dir,
            )
            handler_result.output_transform = (
                normalized_handler.output_transform
            )
        except Exception:
            self._emit_audit(
                event="manual_test_failed",
                actor=actor,
                revision=revision,
            )
            raise

        result = HookManualTestResult(
            handler_result=handler_result,
            redacted_summary=self._redacted_summary(
                handler_result,
                handler=normalized_handler,
                duration_ms=round((time.monotonic() - started_at) * 1000),
            ),
        )
        self._emit_audit(
            event=(
                "manual_test_failed"
                if handler_result.failed
                else "manual_test_completed"
            ),
            actor=actor,
            revision=revision,
            details={
                "duration_ms": round((time.monotonic() - started_at) * 1000),
                "result": result.redacted_summary,
            },
        )
        return result

    async def distribute_to_target(
        self,
        *,
        target: "HookManagementService",
        matcher_group_ids: list[str],
        actor: HookAuditActor,
        activate: Callable[[], Awaitable[None]],
    ) -> HookDistributionTargetResult:
        """Apply selected source groups to one target as a single transaction."""
        payload = self.prepare_distribution(matcher_group_ids)
        return await self.distribute_payload_to_target(
            payload=payload,
            target=target,
            actor=actor,
            activate=activate,
        )

    def prepare_distribution(
        self,
        matcher_group_ids: list[str],
    ) -> HookDistributionPayload:
        """Build the latest saved source payload before any target is changed."""
        return self._build_distribution_payload(matcher_group_ids)

    async def distribute_payload_to_target(
        self,
        *,
        payload: HookDistributionPayload,
        target: "HookManagementService",
        actor: HookAuditActor,
        activate: Callable[[], Awaitable[None]],
        target_tenant_id: str | None = None,
        bootstrapped: bool = False,
    ) -> HookDistributionTargetResult:
        """Apply a prevalidated source payload to one target transactionally."""
        return await target._apply_distribution_payload(
            payload=payload,
            source_tenant_id=self._tenant_id,
            actor=actor,
            activate=activate,
            target_tenant_id=target_tenant_id,
            bootstrapped=bootstrapped,
        )

    def emit_distribution_failure(
        self,
        *,
        payload: HookDistributionPayload,
        actor: HookAuditActor,
        target_tenant_id: str,
        bootstrapped: bool,
        error: str,
    ) -> None:
        """Audit a target failure that occurred before its service existed."""
        self._emit_audit(
            event="distribution_failed",
            actor=actor,
            revision=payload.revision,
            details=self._distribution_audit_details(
                payload=payload,
                source_tenant_id=self._tenant_id,
                target_tenant_id=target_tenant_id,
                bootstrapped=bootstrapped,
                error=error,
            ),
        )

    def _build_distribution_payload(
        self,
        matcher_group_ids: list[str],
    ) -> HookDistributionPayload:
        selected_ids = self._normalize_distribution_group_ids(
            matcher_group_ids,
        )
        with self._configuration_lock():
            snapshot = self.get_configuration()
            groups_by_id: dict[str, tuple[str, dict[str, Any]]] = {}
            for event, groups in snapshot.hooks.get("events", {}).items():
                for group in groups:
                    groups_by_id[group["id"]] = (event, group)

            missing_ids = [
                group_id
                for group_id in selected_ids
                if group_id not in groups_by_id
            ]
            if missing_ids:
                raise HookManagementValidationError(
                    "selected matcher group no longer exists: "
                    + ", ".join(missing_ids),
                )

            groups_by_event: dict[str, list[dict[str, Any]]] = {}
            selected_groups: list[dict[str, Any]] = []
            for group_id in selected_ids:
                event, group = groups_by_id[group_id]
                copied_group = copy.deepcopy(group)
                groups_by_event.setdefault(event, []).append(copied_group)
                selected_groups.append(copied_group)
            scripts = self._distribution_scripts(selected_groups)

        return HookDistributionPayload(
            matcher_group_ids=selected_ids,
            groups_by_event=groups_by_event,
            scripts=scripts,
            revision=snapshot.revision,
        )

    async def _apply_distribution_payload(
        self,
        *,
        payload: HookDistributionPayload,
        source_tenant_id: str | None,
        actor: HookAuditActor,
        activate: Callable[[], Awaitable[None]],
        target_tenant_id: str | None,
        bootstrapped: bool,
    ) -> HookDistributionTargetResult:
        script_transfers: dict[str, str] = {}
        with self._configuration_lock():
            try:
                agent_config = self._load_agent_config()
                target_hooks = self._validate_hooks(
                    agent_config.get("hooks", {}),
                )
                merged_hooks = self._merge_distribution_groups(
                    target_hooks,
                    payload,
                )
                script_backups, script_transfers = (
                    self._prepare_script_distribution(payload)
                )
                agent_backup = _FileBackup(
                    path=self._agent_config_path,
                    content=self._agent_config_path.read_bytes(),
                    mode=self._agent_config_path.stat().st_mode,
                )

                try:
                    self._write_distribution_scripts(payload)
                    normalized_hooks = self._validate_hooks(merged_hooks)
                    agent_config["hooks"] = normalized_hooks
                    self._write_agent_config(agent_config)
                    await activate()
                except Exception as exc:
                    rollback_error = await self._rollback_distribution(
                        agent_backup,
                        script_backups,
                        activate,
                    )
                    if rollback_error:
                        raise RuntimeError(
                            f"{exc}; rollback failed: {rollback_error}",
                        ) from exc
                    raise
            except Exception as exc:
                self._emit_audit(
                    event="distribution_failed",
                    actor=actor,
                    revision=payload.revision,
                    details={
                        **self._distribution_audit_details(
                            payload=payload,
                            source_tenant_id=source_tenant_id,
                            target_tenant_id=target_tenant_id,
                            bootstrapped=bootstrapped,
                            script_transfers=script_transfers,
                            error=str(exc),
                        ),
                    },
                )
                raise

        result = HookDistributionTargetResult(
            matcher_group_ids=payload.matcher_group_ids,
            script_names=tuple(script.filename for script in payload.scripts),
            revision=self.get_configuration().revision,
        )
        self._emit_audit(
            event="distribution_applied",
            actor=actor,
            revision=result.revision,
            details={
                **self._distribution_audit_details(
                    payload=payload,
                    source_tenant_id=source_tenant_id,
                    target_tenant_id=target_tenant_id,
                    bootstrapped=bootstrapped,
                    script_transfers=script_transfers,
                ),
            },
        )
        return result

    @staticmethod
    def _distribution_audit_details(
        *,
        payload: HookDistributionPayload,
        source_tenant_id: str | None,
        target_tenant_id: str | None,
        bootstrapped: bool,
        script_transfers: dict[str, str] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        details: dict[str, Any] = {
            "source_tenant_id": source_tenant_id,
            "target_tenant_id": target_tenant_id,
            "bootstrapped": bootstrapped,
            "matcher_group_ids": list(payload.matcher_group_ids),
            "script_digests": {
                script.filename: script.sha256 for script in payload.scripts
            },
            "script_transfers": script_transfers or {},
        }
        if error is not None:
            details["error"] = error
        return details

    @staticmethod
    def _normalize_distribution_group_ids(
        matcher_group_ids: list[str],
    ) -> tuple[str, ...]:
        normalized_ids = tuple(
            group_id.strip()
            for group_id in matcher_group_ids
            if isinstance(group_id, str) and group_id.strip()
        )
        if not normalized_ids:
            raise HookManagementValidationError(
                "at least one matcher group must be selected",
            )
        if len(set(normalized_ids)) != len(normalized_ids):
            raise HookManagementValidationError(
                "matcher group ids must be unique",
            )
        return normalized_ids

    def _distribution_scripts(
        self,
        groups: list[dict[str, Any]],
    ) -> tuple[HookDistributionScript, ...]:
        filenames: set[str] = set()
        for group in groups:
            for handler in group.get("hooks", []):
                if handler.get("type") != "command":
                    continue
                for argument in handler.get("argv", []):
                    path = Path(str(argument))
                    if path.parts[:2] == ("hooks", "scripts"):
                        filenames.add(path.name)

        script_root = self._ensure_script_root()
        scripts: list[HookDistributionScript] = []
        for filename in sorted(filenames):
            path = script_root / filename
            if path.is_symlink() or not path.is_file():
                raise HookManagementValidationError(
                    f"source script is not in the controlled library: {filename}",
                )
            content = path.read_bytes()
            scripts.append(
                HookDistributionScript(
                    filename=filename,
                    content=content,
                    sha256=hashlib.sha256(content).hexdigest(),
                ),
            )
        return tuple(scripts)

    def _merge_distribution_groups(
        self,
        target_hooks: dict[str, Any],
        payload: HookDistributionPayload,
    ) -> dict[str, Any]:
        merged_hooks = copy.deepcopy(target_hooks)
        merged_hooks["enabled"] = True
        selected_groups = {
            group["id"]: group
            for groups in payload.groups_by_event.values()
            for group in groups
        }
        selected_ids_by_event = {
            event: {group["id"] for group in groups}
            for event, groups in payload.groups_by_event.items()
        }
        selected_ids = set(selected_groups)
        events = merged_hooks.setdefault("events", {})
        for event, groups in list(events.items()):
            merged_groups: list[dict[str, Any]] = []
            for group in groups:
                replacement = selected_groups.get(group["id"])
                if replacement is not None:
                    if group["id"] in selected_ids_by_event.get(event, set()):
                        merged_groups.append(copy.deepcopy(replacement))
                    selected_ids.discard(group["id"])
                    continue
                merged_groups.append(group)
            events[event] = merged_groups
            if not events[event]:
                del events[event]
        for event, groups in payload.groups_by_event.items():
            events.setdefault(event, []).extend(
                copy.deepcopy(group)
                for group in groups
                if group["id"] in selected_ids
            )
        return merged_hooks

    def _prepare_script_distribution(
        self,
        payload: HookDistributionPayload,
    ) -> tuple[tuple[_FileBackup, ...], dict[str, str]]:
        script_root = self._script_library_root(create=False)
        retained_references = self._script_references(
            self._load_hooks(),
            excluded_group_ids=set(payload.matcher_group_ids),
        )
        backups: list[_FileBackup] = []
        transfers: dict[str, str] = {}
        for script in payload.scripts:
            target = script_root / script.filename
            if target.is_symlink():
                raise HookManagementValidationError(
                    "target script must not be a symbolic link",
                )
            if target.is_file():
                existing = target.read_bytes()
                if (
                    existing != script.content
                    and script.filename in retained_references
                ):
                    raise HookManagementConflict(
                        "target retained matcher groups reference a conflicting "
                        f"script: {script.filename}",
                    )
                if existing == script.content:
                    transfers[script.filename] = "unchanged"
                    continue
                transfers[script.filename] = "replaced"
                backups.append(
                    _FileBackup(
                        path=target,
                        content=existing,
                        mode=target.stat().st_mode,
                    ),
                )
            elif target.exists():
                raise HookManagementValidationError(
                    "target script must be a regular file",
                )
            else:
                transfers[script.filename] = "created"
                backups.append(_FileBackup(target, None, None))
        return tuple(backups), transfers

    @staticmethod
    def _script_references(
        hooks: dict[str, Any],
        *,
        excluded_group_ids: set[str],
    ) -> set[str]:
        references: set[str] = set()
        for groups in hooks.get("events", {}).values():
            for group in groups:
                if group["id"] in excluded_group_ids:
                    continue
                for handler in group.get("hooks", []):
                    if handler.get("type") != "command":
                        continue
                    for argument in handler.get("argv", []):
                        path = Path(str(argument))
                        if path.parts[:2] == ("hooks", "scripts"):
                            references.add(path.name)
        return references

    def _write_distribution_scripts(
        self,
        payload: HookDistributionPayload,
    ) -> None:
        if not payload.scripts:
            return
        script_root = self._ensure_script_root()
        for script in payload.scripts:
            target = script_root / script.filename
            if not target.exists() or target.read_bytes() != script.content:
                self._atomic_write(target, script.content)

    async def _rollback_distribution(
        self,
        agent_backup: _FileBackup,
        script_backups: tuple[_FileBackup, ...],
        activate: Callable[[], Awaitable[None]],
    ) -> str | None:
        try:
            for backup in reversed(script_backups):
                if backup.content is None:
                    backup.path.unlink(missing_ok=True)
                else:
                    self._atomic_write(backup.path, backup.content)
                    if backup.mode is not None:
                        os.chmod(backup.path, backup.mode)
            if agent_backup.content is not None:
                self._atomic_write(agent_backup.path, agent_backup.content)
                if agent_backup.mode is not None:
                    os.chmod(agent_backup.path, agent_backup.mode)
            await activate()
        except Exception as exc:
            return str(exc)
        return None

    def _load_agent_config(self) -> dict[str, Any]:
        try:
            with self._agent_config_path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except FileNotFoundError as exc:
            raise HookManagementValidationError(
                "default agent configuration does not exist",
            ) from exc
        except json.JSONDecodeError as exc:
            raise HookManagementValidationError(
                "default agent configuration is invalid JSON",
            ) from exc

        if not isinstance(data, dict):
            raise HookManagementValidationError(
                "default agent configuration must be an object",
            )
        return data

    def _load_hooks(self) -> dict[str, Any]:
        agent_config = self._load_agent_config()
        raw_hooks = agent_config.get("hooks", {})
        if not isinstance(raw_hooks, dict):
            raise HookManagementValidationError(
                "agent hooks must be an object",
            )
        return self._validate_hooks(raw_hooks)

    def _validate_hooks(self, hooks: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(hooks, dict):
            raise HookManagementValidationError("hooks must be an object")

        self._reject_command_strings(hooks)
        try:
            config = HookConfig.model_validate(hooks)
        except (ValidationError, ValueError) as exc:
            raise HookManagementValidationError(str(exc)) from exc

        self._validate_unique_ids(config)
        normalized = config.model_dump(mode="json", by_alias=True)
        self._normalize_script_references(normalized)
        return normalized

    def _parse_handler(
        self,
        handler: dict[str, Any],
        *,
        event_name,
    ) -> HookHandlerConfig:
        if not isinstance(handler, dict):
            raise HookManagementValidationError("handler must be an object")
        self._reject_command_strings(
            {"events": {"PreToolUse": [{"hooks": [handler]}]}},
        )
        try:
            parsed = _HANDLER_ADAPTER.validate_python(handler)
            validate_handler_event(parsed, event_name)
        except (ValidationError, ValueError) as exc:
            raise HookManagementValidationError(str(exc)) from exc
        normalized = parsed.model_dump(mode="json", by_alias=True)
        self._normalize_script_references(
            {"events": {"PreToolUse": [{"hooks": [normalized]}]}},
        )
        return _HANDLER_ADAPTER.validate_python(normalized)

    @staticmethod
    def _reject_command_strings(hooks: dict[str, Any]) -> None:
        events = hooks.get("events", {})
        if not isinstance(events, dict):
            return
        for groups in events.values():
            if not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, dict):
                    continue
                for handler in group.get("hooks", []):
                    if (
                        isinstance(handler, dict)
                        and handler.get("type") == "command"
                        and str(handler.get("command", "")).strip()
                    ):
                        raise HookManagementValidationError(
                            "command handler command strings are not supported; use argv",
                        )

    def _normalize_script_references(self, hooks: dict[str, Any]) -> None:
        events = hooks.get("events", {})
        if not isinstance(events, dict):
            return
        for groups in events.values():
            if not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, dict):
                    continue
                for handler in group.get("hooks", []):
                    if not isinstance(handler, dict):
                        continue
                    if handler.get("type") != "command":
                        continue
                    argv = handler.get("argv", [])
                    if not isinstance(argv, list):
                        continue
                    normalized_argv = [
                        self._normalize_script_argument(item, index)
                        for index, item in enumerate(argv)
                    ]
                    has_controlled_script = any(
                        Path(value).parts[:2] == ("hooks", "scripts")
                        for value in normalized_argv
                    )
                    if (
                        has_controlled_script
                        and str(handler.get("cwd", "")).strip()
                    ):
                        raise HookManagementValidationError(
                            "script handlers must not set cwd",
                        )
                    handler["argv"] = normalized_argv

    def _normalize_script_argument(self, argument: Any, index: int) -> str:
        if not isinstance(argument, str):
            raise HookManagementValidationError("argv entries must be strings")
        if argument.startswith("-"):
            return argument

        candidate = Path(argument)
        is_script = candidate.suffix.lower() in ALLOWED_SCRIPT_SUFFIXES
        is_path_like = "/" in argument or "\\" in argument
        if not is_script:
            if index == 0 and is_path_like:
                raise HookManagementValidationError(
                    "argv executable paths must be a bare executable name",
                )
            return argument
        if candidate.is_absolute() or ".." in candidate.parts:
            raise HookManagementValidationError(
                "script arguments must stay inside hooks/scripts",
            )

        canonical = Path("hooks") / "scripts" / candidate.name
        if candidate != Path(candidate.name) and candidate != canonical:
            raise HookManagementValidationError(
                "script arguments must use hooks/scripts/<filename>",
            )
        script_root = self._ensure_script_root()
        script_path = script_root / candidate.name
        if script_path.is_symlink():
            raise HookManagementValidationError(
                "script must not be a symbolic link",
            )
        if not script_path.is_file():
            raise HookManagementValidationError(
                f"script is not in the controlled library: {candidate.name}",
            )
        try:
            script_path.resolve(strict=True).relative_to(script_root)
        except ValueError as exc:
            raise HookManagementValidationError(
                "script is outside the controlled library",
            ) from exc
        return canonical.as_posix()

    @staticmethod
    def _validate_unique_ids(config: HookConfig) -> None:
        group_ids: set[str] = set()
        handler_ids: set[str] = set()
        for groups in config.events.values():
            for group in groups:
                if not group.id.strip():
                    raise HookManagementValidationError(
                        "matcher group id must not be blank",
                    )
                if group.id in group_ids:
                    raise HookManagementValidationError(
                        f"duplicate matcher group id: {group.id}",
                    )
                group_ids.add(group.id)
                for handler in group.hooks:
                    if not handler.id.strip():
                        raise HookManagementValidationError(
                            "handler id must not be blank",
                        )
                    if handler.id in handler_ids:
                        raise HookManagementValidationError(
                            f"duplicate handler id: {handler.id}",
                        )
                    handler_ids.add(handler.id)

    def _write_agent_config(self, agent_config: dict[str, Any]) -> None:
        encoded = (
            json.dumps(agent_config, ensure_ascii=False, indent=2) + "\n"
        ).encode(
            "utf-8",
        )
        self._atomic_write(self._agent_config_path, encoded)

    @staticmethod
    def _validate_upload(
        file: UploadFilePayload,
        seen_names: set[str],
    ) -> None:
        filename = file.filename
        if not filename or Path(filename).name != filename:
            raise HookManagementValidationError(
                "script filename must not contain a path",
            )
        if filename in seen_names:
            raise HookManagementValidationError(
                f"duplicate filename in batch: {filename}",
            )
        if Path(filename).suffix.lower() not in ALLOWED_SCRIPT_SUFFIXES:
            raise HookManagementValidationError("unsupported script suffix")
        if len(file.content) > MAX_SCRIPT_BYTES:
            raise HookManagementValidationError(
                f"script exceeds {MAX_SCRIPT_BYTES} byte limit",
            )
        if b"\x00" in file.content:
            raise HookManagementValidationError(
                "script content must be UTF-8 text",
            )
        try:
            file.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HookManagementValidationError(
                "script content must be UTF-8 text",
            ) from exc

    def _scan_upload(self, file: UploadFilePayload) -> str:
        with tempfile.TemporaryDirectory(
            dir=self._ensure_script_root().parent,
        ) as stage:
            stage_dir = Path(stage)
            (stage_dir / file.filename).write_bytes(file.content)
            try:
                result = scan_skill_directory(
                    stage_dir,
                    skill_name=file.filename,
                )
            except SkillScanError as exc:
                raise HookManagementValidationError(
                    f"script scan blocked upload: {exc}",
                ) from exc
        if result is not None and not result.is_safe:
            return "warning"
        return "clean" if result is not None else "off"

    @staticmethod
    def _atomic_write(target: Path, content: bytes) -> None:
        with tempfile.NamedTemporaryFile(
            dir=target.parent,
            delete=False,
        ) as staged:
            staged.write(content)
            staged_path = Path(staged.name)
        staged_path.replace(target)
        os.chmod(target, 0o700)

    def _ensure_script_root(self) -> Path:
        return self._script_library_root(create=True)

    def _script_library_root(self, *, create: bool) -> Path:
        workspace_root = self._workspace_dir.resolve()
        hooks_dir = self._script_root.parent
        script_root = self._script_root
        for path in (hooks_dir, script_root):
            if path.is_symlink():
                raise HookManagementValidationError(
                    "script library must not be a symbolic link",
                )
        if create:
            hooks_dir.mkdir(parents=True, exist_ok=True)
            script_root.mkdir(exist_ok=True)
        elif script_root.exists() and not script_root.is_dir():
            raise HookManagementValidationError(
                "script library must be a directory",
            )
        resolved_root = script_root.resolve()
        try:
            resolved_root.relative_to(workspace_root)
        except ValueError as exc:
            raise HookManagementValidationError(
                "script library is outside the default workspace",
            ) from exc
        return resolved_root

    @contextmanager
    def _configuration_lock(self):
        self._workspace_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._workspace_dir / ".hook-management.lock"
        if not _CONFIGURATION_THREAD_LOCK.acquire(blocking=False):
            raise HookManagementConflict("hook configuration is busy")
        try:
            with lock_path.open("a+", encoding="utf-8") as lock_file:
                if fcntl is not None:
                    try:
                        fcntl.flock(
                            lock_file.fileno(),
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                        )
                    except BlockingIOError as exc:
                        raise HookManagementConflict(
                            "hook configuration is busy",
                        ) from exc
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            _CONFIGURATION_THREAD_LOCK.release()

    @staticmethod
    def _sha256_file(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _revision_for(hooks: dict[str, Any]) -> str:
        canonical = json.dumps(
            hooks,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _removed_hook_ids(
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> tuple[list[str], list[str]]:
        def collect(hooks: dict[str, Any]) -> tuple[set[str], set[str]]:
            group_ids: set[str] = set()
            handler_ids: set[str] = set()
            for groups in hooks.get("events", {}).values():
                for group in groups:
                    group_ids.add(group["id"])
                    handler_ids.update(
                        handler["id"] for handler in group["hooks"]
                    )
            return group_ids, handler_ids

        before_groups, before_handlers = collect(before)
        after_groups, after_handlers = collect(after)
        return (
            sorted(before_groups - after_groups),
            sorted(before_handlers - after_handlers),
        )

    def _emit_audit(
        self,
        *,
        event: str,
        actor: HookAuditActor,
        revision: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Record operational metadata without making logs a write dependency."""
        try:
            extra = {
                "event": event,
                "actor_user_id": actor.user_id,
                "actor_tenant_id": actor.tenant_id,
                "tenant_id": self._tenant_id,
                "agent_id": "default",
                "configuration_revision": revision,
            }
            if details:
                extra.update(
                    {
                        key: (
                            json.dumps(
                                value,
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                            if isinstance(value, (dict, list))
                            else value
                        )
                        for key, value in details.items()
                    },
                )
            logger.info("agent_hook.audit", extra=extra)
        except (
            Exception
        ):  # pragma: no cover - logging failures are best effort
            pass

    @staticmethod
    def _redacted_summary(
        result: HookHandlerResult,
        *,
        handler: HookHandlerConfig,
        duration_ms: int,
    ) -> dict[str, Any]:
        return {
            "handler_id": result.handler_id,
            "decision": str(result.decision.value),
            "failed": result.failed,
            "failure_type": result.failure_type,
            "status": "failed" if result.failed else "completed",
            "output_transform": result.output_transform,
            "replacement_applied": result.replacement_text is not None,
            "replacement_length": (
                len(result.replacement_text)
                if result.replacement_text is not None
                else 0
            ),
            "output_append": handler.output_append,
            "append_applied": result.append_text is not None,
            "append_bytes": len((result.append_text or "").encode("utf-8")),
            "effective_fail_policy": (
                "allow" if handler.output_append else handler.fail_policy
            ),
            "duration_ms": duration_ms,
        }

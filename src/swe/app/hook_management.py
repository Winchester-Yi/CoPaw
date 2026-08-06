# -*- coding: utf-8 -*-
"""Default Agent Profile Hook configuration management."""

from __future__ import annotations

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
from typing import Any

from pydantic import TypeAdapter, ValidationError

from ..agents.hook_runtime.executor import execute_handler
from ..agents.hook_runtime.models import (
    HookConfig,
    HookContext,
    HookHandlerConfig,
    HookHandlerResult,
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
class HookScriptDiagnostic:
    """One invalid script reference retained for Hook-console repair."""

    event: str
    group_id: str
    handler_id: str
    argument: str
    reason: str


@dataclass(frozen=True)
class HookConfigurationSnapshot:
    """A default profile Hook draft plus its optimistic-lock revision."""

    hooks: dict[str, Any]
    revision: str
    diagnostics: tuple[HookScriptDiagnostic, ...] = ()


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
        """Return the Hook configuration and repairable script diagnostics."""
        hooks, diagnostics = self._load_hooks()
        return HookConfigurationSnapshot(
            hooks=hooks,
            revision=self._revision_for(hooks),
            diagnostics=diagnostics,
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
            normalized_handler = self._parse_handler(handler)
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
        except Exception:
            self._emit_audit(
                event="manual_test_failed",
                actor=actor,
                revision=revision,
            )
            raise

        result = HookManualTestResult(
            handler_result=handler_result,
            redacted_summary=self._redacted_summary(handler_result),
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

    def _load_hooks(
        self,
    ) -> tuple[dict[str, Any], tuple[HookScriptDiagnostic, ...]]:
        agent_config = self._load_agent_config()
        raw_hooks = agent_config.get("hooks", {})
        if not isinstance(raw_hooks, dict):
            raise HookManagementValidationError(
                "agent hooks must be an object",
            )
        hooks = self._normalize_hook_shape(raw_hooks)
        return hooks, self._normalize_script_references_for_load(hooks)

    def _normalize_hook_shape(self, hooks: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(hooks, dict):
            raise HookManagementValidationError("hooks must be an object")
        self._reject_command_strings(hooks)
        try:
            config = HookConfig.model_validate(hooks)
        except ValidationError as exc:
            raise HookManagementValidationError(str(exc)) from exc
        self._validate_unique_ids(config)
        return config.model_dump(mode="json", by_alias=True)

    def _validate_hooks(self, hooks: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_hook_shape(hooks)
        self._normalize_script_references(normalized)
        return normalized

    def _parse_handler(self, handler: dict[str, Any]) -> HookHandlerConfig:
        if not isinstance(handler, dict):
            raise HookManagementValidationError("handler must be an object")
        self._reject_command_strings(
            {"events": {"PreToolUse": [{"hooks": [handler]}]}},
        )
        try:
            parsed = _HANDLER_ADAPTER.validate_python(handler)
        except ValidationError as exc:
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

    def _normalize_script_references_for_load(
        self,
        hooks: dict[str, Any],
    ) -> tuple[HookScriptDiagnostic, ...]:
        diagnostics: list[HookScriptDiagnostic] = []
        events = hooks.get("events", {})
        if not isinstance(events, dict):
            return ()
        for event, groups in events.items():
            if not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, dict):
                    continue
                group_id = str(group.get("id", ""))
                for handler in group.get("hooks", []):
                    if (
                        not isinstance(handler, dict)
                        or handler.get("type") != "command"
                    ):
                        continue
                    argv = handler.get("argv", [])
                    if not isinstance(argv, list):
                        continue
                    handler_id = str(handler.get("id", ""))
                    normalized_argv = []
                    for index, argument in enumerate(argv):
                        try:
                            normalized_argv.append(
                                self._normalize_script_argument(
                                    argument,
                                    index,
                                ),
                            )
                        except HookManagementValidationError as exc:
                            normalized_argv.append(argument)
                            diagnostics.append(
                                HookScriptDiagnostic(
                                    event=str(event),
                                    group_id=group_id,
                                    handler_id=handler_id,
                                    argument=str(argument),
                                    reason=str(exc),
                                ),
                            )
                    handler["argv"] = normalized_argv
        return tuple(diagnostics)

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
        workspace_root = self._workspace_dir.resolve()
        hooks_dir = self._script_root.parent
        script_root = self._script_root
        for path in (hooks_dir, script_root):
            if path.is_symlink():
                raise HookManagementValidationError(
                    "script library must not be a symbolic link",
                )
        hooks_dir.mkdir(parents=True, exist_ok=True)
        script_root.mkdir(exist_ok=True)
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
        with _CONFIGURATION_THREAD_LOCK:
            with lock_path.open("a+", encoding="utf-8") as lock_file:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

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
    def _redacted_summary(result: HookHandlerResult) -> dict[str, Any]:
        return {
            "handler_id": result.handler_id,
            "decision": str(result.decision.value),
            "failed": result.failed,
            "failure_type": result.failure_type,
            "status": "failed" if result.failed else "completed",
        }

# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable, Collection
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import (
    CommandHookHandlerConfig,
    HookConfig,
    HookMatcherGroupConfig,
    HookSessionState,
    HttpHookHandlerConfig,
    LoadedSkillHookSource,
)

logger = logging.getLogger(__name__)


ApprovedHttpUrls = Collection[str] | Callable[[str], bool] | None


class SkillHookLoadError(ValueError):
    """Raised when a skill-owned hook file fails validation."""


def load_skill_hooks_for_session(
    *,
    skill_name: str,
    skill_root: Path,
    workspace_dir: Path,
    session_state: HookSessionState,
    approved_http_urls: ApprovedHttpUrls = None,
) -> HookSessionState:
    """Load one skill's hooks/hooks.json into session hook state."""
    resolved_workspace = _resolve_existing_dir(workspace_dir, "workspace")
    resolved_skill_root = _resolve_existing_dir(skill_root, "skill root")
    _ensure_under(resolved_skill_root, resolved_workspace, "skill root")

    source_id = f"skill:{skill_name}"
    if any(
        source.source_id == source_id
        for source in session_state.loaded_skill_sources
    ):
        return session_state

    hook_file = resolved_skill_root / "hooks" / "hooks.json"
    if not hook_file.is_file():
        return session_state

    hook_config = _read_hook_config(hook_file)
    if not hook_config.enabled:
        return session_state

    namespaced = _namespace_and_validate_config(
        hook_config,
        skill_name=skill_name,
        skill_root=resolved_skill_root,
        workspace_dir=resolved_workspace,
        approved_http_urls=approved_http_urls,
    )
    source = LoadedSkillHookSource(
        source_id=source_id,
        skill_name=skill_name,
        skill_root=str(resolved_skill_root),
        source_path=str(hook_file),
        hook_config=namespaced,
        loaded_at=datetime.now(timezone.utc),
        metadata={"format": "hooks.json"},
    )
    return HookSessionState(
        loaded_skill_sources=[
            *session_state.loaded_skill_sources,
            source,
        ],
        entries=session_state.entries,
        once_executed=session_state.once_executed,
    )


def _read_hook_config(hook_file: Path) -> HookConfig:
    try:
        data = json.loads(hook_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SkillHookLoadError(f"invalid skill hooks JSON: {exc}") from exc
    except OSError as exc:
        raise SkillHookLoadError(
            f"failed to read skill hooks file: {exc}",
        ) from exc
    try:
        return HookConfig.model_validate(data)
    except ValidationError as exc:
        raise SkillHookLoadError(f"invalid skill hook config: {exc}") from exc


def _namespace_and_validate_config(
    hook_config: HookConfig,
    *,
    skill_name: str,
    skill_root: Path,
    workspace_dir: Path,
    approved_http_urls: ApprovedHttpUrls,
) -> HookConfig:
    namespace = f"skill:{skill_name}:"
    events: dict[Any, list[HookMatcherGroupConfig]] = {}
    for event_name, groups in hook_config.events.items():
        namespaced_groups: list[HookMatcherGroupConfig] = []
        for group_index, group in enumerate(groups):
            group_data = group.model_dump(mode="json", by_alias=True)
            original_group_id = group.id or f"group-{group_index}"
            group_data["id"] = _namespace_id(namespace, original_group_id)
            group_data["hooks"] = [
                _normalize_handler(
                    handler,
                    namespace=namespace,
                    skill_root=skill_root,
                    workspace_dir=workspace_dir,
                    approved_http_urls=approved_http_urls,
                )
                for handler in group.hooks
            ]
            namespaced_groups.append(
                HookMatcherGroupConfig.model_validate(group_data),
            )
        events[event_name] = namespaced_groups
    return HookConfig(enabled=True, events=events)


def _normalize_handler(
    handler: Any,
    *,
    namespace: str,
    skill_root: Path,
    workspace_dir: Path,
    approved_http_urls: ApprovedHttpUrls,
) -> dict[str, Any]:
    data = copy.deepcopy(handler.model_dump(mode="json", by_alias=True))
    data["id"] = _namespace_id(namespace, handler.id)
    if isinstance(handler, CommandHookHandlerConfig):
        _normalize_command_handler(data, skill_root, workspace_dir)
    elif isinstance(handler, HttpHookHandlerConfig):
        _validate_http_handler(data, approved_http_urls)
    return data


def _normalize_command_handler(
    data: dict[str, Any],
    skill_root: Path,
    workspace_dir: Path,
) -> None:
    if data.get("command"):
        raise SkillHookLoadError(
            "skill hook command handlers must not use shell command strings",
        )
    argv = data.get("argv") or []
    if not isinstance(argv, list) or not argv:
        raise SkillHookLoadError(
            "skill hook command handler requires an argv script argument",
        )
    if data.get("env"):
        raise SkillHookLoadError(
            "skill hook command handlers must not define literal env values",
        )

    script_index = _find_single_script_arg(argv, skill_root)
    script_path = _resolve_script_path(argv[script_index], skill_root)
    _ensure_under(script_path, skill_root / "scripts", "script path")
    _ensure_under(script_path, workspace_dir, "script path")
    if not script_path.exists():
        raise SkillHookLoadError("skill hook script path does not exist")
    if not script_path.is_file():
        raise SkillHookLoadError(
            "skill hook script path must be a regular file",
        )

    normalized_argv = list(argv)
    normalized_argv[script_index] = str(script_path)
    data["argv"] = normalized_argv

    cwd = data.get("cwd") or ""
    if cwd:
        cwd_path = Path(cwd).expanduser()
        if not cwd_path.is_absolute():
            cwd_path = skill_root / cwd_path
        try:
            resolved_cwd = cwd_path.resolve(strict=True)
        except OSError as exc:
            raise SkillHookLoadError("skill hook cwd does not exist") from exc
        if not resolved_cwd.is_dir():
            raise SkillHookLoadError("skill hook cwd must be a directory")
        _ensure_under(resolved_cwd, skill_root, "cwd")
        _ensure_under(resolved_cwd, workspace_dir, "cwd")
        data["cwd"] = str(resolved_cwd)
    else:
        data["cwd"] = str(skill_root)


def _find_single_script_arg(argv: list[str], skill_root: Path) -> int:
    scripts_root = skill_root / "scripts"
    candidates: list[int] = []
    for index, item in enumerate(argv):
        if not isinstance(item, str) or not _looks_like_path(item):
            continue
        candidate = Path(item).expanduser()
        if not candidate.is_absolute():
            candidate = skill_root / candidate
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(scripts_root)
        except ValueError as exc:
            raise SkillHookLoadError(
                "skill hook script path is outside skill scripts",
            ) from exc
        candidates.append(index)
    if not candidates:
        raise SkillHookLoadError(
            "skill hook command handler requires exactly one script argument",
        )
    if len(candidates) > 1:
        raise SkillHookLoadError(
            "skill hook command handler has multiple script arguments",
        )
    return candidates[0]


def _resolve_script_path(raw_path: str, skill_root: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = skill_root / candidate
    try:
        return candidate.resolve(strict=True)
    except OSError:
        return candidate.resolve(strict=False)


def _validate_http_handler(
    data: dict[str, Any],
    approved_http_urls: ApprovedHttpUrls,
) -> None:
    if data.get("headers"):
        raise SkillHookLoadError(
            "skill hook HTTP handlers must not define literal headers",
        )
    if data.get("allowedEnvVars"):
        raise SkillHookLoadError(
            "skill hook HTTP handlers must not define allowedEnvVars",
        )
    url = str(data.get("url") or "")
    if not _is_http_url_approved(url, approved_http_urls):
        raise SkillHookLoadError(
            f"skill hook HTTP endpoint is not approved: {url}",
        )


def _is_http_url_approved(
    url: str,
    approved_http_urls: ApprovedHttpUrls,
) -> bool:
    if callable(approved_http_urls):
        return bool(approved_http_urls(url))
    # Skill-owned HTTP hooks are now allowed by default. Keep the callable
    # branch for custom validators used by internal callers and tests.
    return True


def _namespace_id(namespace: str, raw_id: str) -> str:
    if raw_id.startswith(namespace):
        return raw_id
    return f"{namespace}{raw_id}"


def _looks_like_path(value: str) -> bool:
    return (
        "/" in value
        or "\\" in value
        or value.startswith(".")
        or Path(value).expanduser().is_absolute()
    )


def _resolve_existing_dir(path: Path, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise SkillHookLoadError(f"{label} does not exist") from exc
    if not resolved.is_dir():
        raise SkillHookLoadError(f"{label} must be a directory")
    return resolved


def _ensure_under(path: Path, root: Path, label: str) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise SkillHookLoadError(f"{label} is outside skill scripts") from exc

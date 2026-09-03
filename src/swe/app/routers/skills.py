# -*- coding: utf-8 -*-
"""Workspace and skill-pool APIs."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ...agents.skills_hub import (
    SkillImportCancelled,
    search_hub_skills,
    import_pool_skill_from_hub,
    install_skill_from_hub,
)
from ...agents.skills_manager import (
    SkillConflictError,
    SkillPoolService,
    SkillInfo,
    SkillService,
    _has_unmanaged_workspace_skill_conflict,
    _default_pool_manifest,
    _default_workspace_manifest,
    _get_skill_mtime,
    _mutate_json,
    _normalize_skill_dir_name,
    _read_skill_from_dir,
    get_pool_builtin_sync_status,
    get_pool_skill_manifest_path,
    get_skill_pool_dir,
    get_workspace_skill_manifest_path,
    resolve_workspace_managed_skill_dir,
    import_builtin_skills,
    list_builtin_import_candidates,
    list_workspaces,
    read_skill_pool_manifest,
    read_skill_manifest,
    reconcile_pool_manifest,
    reconcile_workspace_manifest,
    resolve_effective_skills,
    suggest_conflict_name,
    update_single_builtin,
)
from ...config.utils import (
    get_tenant_request_working_dir,
    list_logical_tenant_ids,
)
from ...config.context import resolve_request_effective_tenant_id
from ...security.skill_scanner import SkillScanError
from ..utils import schedule_agent_reload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["skills"])
_POOL_SKILL_DOWNLOAD_LOCK = asyncio.Lock()


def _scan_error_payload(exc: SkillScanError) -> dict[str, Any]:
    """Normalize scanner exceptions into a stable API payload.

    Example response body:
        {
            "type": "security_scan_failed",
            "skill_name": "blocked_skill",
            "max_severity": "high",
            "findings": [...]
        }
    """
    result = exc.result
    return {
        "type": "security_scan_failed",
        "detail": str(exc),
        "skill_name": result.skill_name,
        "max_severity": result.max_severity.value,
        "findings": [
            {
                "severity": f.severity.value,
                "title": f.title,
                "description": f.description,
                "file_path": f.file_path,
                "line_number": f.line_number,
                "rule_id": f.rule_id,
            }
            for f in result.findings
        ],
    }


def _scan_error_response(exc: SkillScanError) -> JSONResponse:
    """Build the historical 422 response shape used by skill endpoints.

    We intentionally return a real HTTP 422 response object here so callers
    and tests observe the same behavior as before the skill-pool refactor.
    """
    return JSONResponse(
        status_code=422,
        content=_scan_error_payload(exc),
    )


class SkillSpec(SkillInfo):
    enabled: bool = False
    channels: list[str] = Field(default_factory=lambda: ["all"])
    config: dict[str, Any] = Field(default_factory=dict)
    last_updated: str = ""


class EffectiveSkillSpec(BaseModel):
    """Safe skill metadata used by the Console chat composer."""

    name: str
    description: str = ""


class PoolSkillSpec(SkillInfo):
    protected: bool = False
    commit_text: str = ""
    sync_status: str = ""
    latest_version_text: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    last_updated: str = ""


class WorkspaceSkillSummary(BaseModel):
    agent_id: str
    agent_name: str = ""
    workspace_dir: str
    skills: list[SkillSpec] = Field(default_factory=list)


class HubSkillSpec(BaseModel):
    slug: str
    name: str
    description: str = ""
    version: str = ""
    source_url: str = ""


class BuiltinImportSpec(BaseModel):
    name: str
    description: str = ""
    version_text: str = ""
    current_version_text: str = ""
    current_source: str = ""
    status: str = ""


class ImportBuiltinRequest(BaseModel):
    skill_names: list[str] = Field(default_factory=list)
    overwrite_conflicts: bool = False


class CreateSkillRequest(BaseModel):
    name: str
    content: str
    overwrite: bool = True
    references: dict[str, Any] | None = None
    scripts: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    enable: bool = True


class UploadToPoolRequest(BaseModel):
    workspace_id: str
    skill_name: str
    new_name: str | None = None
    overwrite: bool = True


class PoolDownloadTarget(BaseModel):
    workspace_id: str
    target_name: str | None = None


class DownloadFromPoolRequest(BaseModel):
    skill_name: str
    targets: list[PoolDownloadTarget] = Field(default_factory=list)
    all_workspaces: bool = False
    overwrite: bool = False


class BroadcastTenantListResponse(BaseModel):
    tenant_ids: list[str] = Field(default_factory=list)


class BroadcastDefaultAgentsRequest(BaseModel):
    skill_names: list[str] = Field(default_factory=list)
    target_tenant_ids: list[str] = Field(default_factory=list)
    overwrite: bool = False


class BroadcastDefaultAgentTenantResult(BaseModel):
    tenant_id: str
    success: bool
    bootstrapped: bool = False
    pool_updated: list[str] = Field(default_factory=list)
    default_agent_updated: list[str] = Field(default_factory=list)
    error: str = ""


class BroadcastDefaultAgentsResponse(BaseModel):
    results: list[BroadcastDefaultAgentTenantResult] = Field(
        default_factory=list,
    )


class SkillConfigRequest(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)


class SavePoolSkillRequest(CreateSkillRequest):
    source_name: str | None = None


class SaveSkillRequest(CreateSkillRequest):
    source_name: str | None = None


class HubInstallRequest(BaseModel):
    bundle_url: str = Field(..., description="Skill URL")
    version: str = Field(default="", description="Optional version tag")
    enable: bool = Field(default=True, description="Enable after import")
    target_name: str = Field(default="", description="Optional renamed skill")
    overwrite: bool = Field(
        default=False,
        description="Overwrite existing workspace skill",
    )


class HubInstallTaskStatus(str, Enum):
    PENDING = "pending"
    IMPORTING = "importing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HubInstallTask(BaseModel):
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    bundle_url: str
    version: str = ""
    enable: bool = True
    overwrite: bool = False
    status: HubInstallTaskStatus = HubInstallTaskStatus.PENDING
    error: str | None = None
    result: dict[str, Any] | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


_hub_install_tasks: dict[str, HubInstallTask] = {}
_hub_install_runtime_tasks: dict[str, asyncio.Task] = {}
_hub_install_cancel_events: dict[str, threading.Event] = {}
_hub_install_lock = asyncio.Lock()

_ALLOWED_ZIP_TYPES = {
    "application/zip",
    "application/x-zip-compressed",
    "application/octet-stream",
}
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB


def _request_tenant_id(request: Request) -> str | None:
    return getattr(request.state, "tenant_id", None)


def _request_effective_tenant_id(request: Request) -> str | None:
    return resolve_request_effective_tenant_id(
        _request_tenant_id(request),
        _request_source_id(request),
        getattr(request.state, "scope_id", None),
    )


def _request_tenant_working_dir(request: Request) -> Path:
    return get_tenant_request_working_dir(_request_tenant_id(request))


def _request_source_id(request: Request) -> str | None:
    return getattr(request.state, "source_id", None)


def _workspace_dir_for_agent(
    agent_id: str,
    *,
    tenant_id: str | None = None,
) -> Path:
    for workspace in list_workspaces(tenant_id=tenant_id):
        if workspace["agent_id"] == agent_id:
            return Path(workspace["workspace_dir"])
    raise HTTPException(
        status_code=404,
        detail=f"Workspace '{agent_id}' not found",
    )


def _snapshot_workspace_skill(
    workspace_dir: Path,
    skill_name: str,
) -> dict[str, Any]:
    manifest = read_skill_manifest(workspace_dir)
    entry = manifest.get("skills", {}).get(skill_name)
    backup_dir: Path | None = None
    if entry is not None:
        skill_dir = resolve_workspace_managed_skill_dir(
            workspace_dir,
            skill_name,
            enabled=bool(entry.get("enabled", False)),
        )
    else:
        skill_dir = None
    if skill_dir is not None and skill_dir.exists():
        backup_root = Path(
            tempfile.mkdtemp(prefix=f"swe_skill_rollback_{skill_name}_"),
        )
        backup_dir = backup_root / skill_name
        shutil.copytree(skill_dir, backup_dir)
    return {
        "workspace_dir": workspace_dir,
        "skill_name": skill_name,
        "entry": copy.deepcopy(entry) if entry is not None else None,
        "backup_dir": backup_dir,
    }


def _restore_workspace_skill(snapshot: dict[str, Any]) -> None:
    from ...agents.skill_runtime_snapshot import workspace_skill_coordinator

    workspace_dir = Path(snapshot["workspace_dir"])
    skill_name = str(snapshot["skill_name"])
    backup_dir = snapshot.get("backup_dir")
    entry = snapshot.get("entry")
    active_dir = resolve_workspace_managed_skill_dir(
        workspace_dir,
        skill_name,
        enabled=True,
    )
    hidden_dir = resolve_workspace_managed_skill_dir(
        workspace_dir,
        skill_name,
        enabled=False,
    )

    def _restore(payload: dict[str, Any]) -> None:
        payload.setdefault("skills", {})
        if entry is None:
            payload["skills"].pop(skill_name, None)
            return
        payload["skills"][skill_name] = copy.deepcopy(entry)

    with workspace_skill_coordinator(workspace_dir):
        for skill_dir in (active_dir, hidden_dir):
            if skill_dir.exists():
                shutil.rmtree(skill_dir)
        if (
            entry is not None
            and backup_dir is not None
            and Path(backup_dir).exists()
        ):
            restore_dir = resolve_workspace_managed_skill_dir(
                workspace_dir,
                skill_name,
                enabled=bool(entry.get("enabled", False)),
            )
            restore_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(Path(backup_dir), restore_dir)
        _mutate_json(
            get_workspace_skill_manifest_path(workspace_dir),
            _default_workspace_manifest(),
            _restore,
        )
        reconcile_workspace_manifest(workspace_dir)
    if backup_dir is not None:
        shutil.rmtree(Path(backup_dir).parent, ignore_errors=True)


def _validate_target_tenant_id(tenant_id: str) -> str:
    tenant_id = str(tenant_id or "").strip()
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if len(tenant_id) > 256:
        raise ValueError(f"Invalid tenant ID format: {tenant_id}")
    if ".." in tenant_id or "/" in tenant_id or "\\" in tenant_id:
        raise ValueError(f"Invalid tenant ID format: {tenant_id}")
    if any(ord(c) < 32 for c in tenant_id):
        raise ValueError(f"Invalid tenant ID format: {tenant_id}")
    return tenant_id


def _read_pool_skill_entry(
    *,
    working_dir: Path,
    skill_name: str,
) -> tuple[dict[str, Any], Path]:
    manifest = read_skill_pool_manifest(
        reconcile=False,
        working_dir=working_dir,
    )
    entry = manifest.get("skills", {}).get(skill_name)
    if entry is None:
        raise ValueError(f"Pool skill '{skill_name}' not found")
    source_dir = get_skill_pool_dir(working_dir=working_dir) / skill_name
    if not source_dir.exists():
        raise ValueError(f"Pool skill '{skill_name}' content is missing")
    return entry, source_dir


async def _broadcast_skills_to_tenant(
    *,
    source_working_dir: Path,
    target_tenant_id: str,
    skill_names: list[str],
    source_id: str | None,
    tenant_workspace_pool: Any,
) -> BroadcastDefaultAgentTenantResult:
    from ..workspace.tenant_initializer import TenantInitializer

    initializer = TenantInitializer(
        source_working_dir.parent,
        target_tenant_id,
        source_id=source_id,
    )
    was_bootstrapped = initializer.has_seeded_bootstrap()
    if not was_bootstrapped:
        await tenant_workspace_pool.ensure_bootstrap(
            target_tenant_id,
            source_id=source_id,
        )
    target_working_dir = initializer.tenant_dir
    workspace_dir = target_working_dir / "workspaces" / "default"
    workspace_service = SkillService(workspace_dir)
    workspace_manifest = await asyncio.to_thread(
        read_skill_manifest,
        workspace_dir,
        reconcile=False,
    )
    for skill_name in skill_names:
        final_name = _normalize_skill_dir_name(skill_name)
        if _has_unmanaged_workspace_skill_conflict(
            workspace_dir,
            final_name,
            workspace_manifest,
        ):
            raise ValueError(
                f"Workspace skill '{final_name}' conflicts with "
                "unmanaged content",
            )

    pool_service = SkillPoolService(working_dir=target_working_dir)
    pool_updated: list[str] = []
    default_updated: list[str] = []
    for skill_name in skill_names:
        entry, source_dir = await asyncio.to_thread(
            _read_pool_skill_entry,
            working_dir=source_working_dir,
            skill_name=skill_name,
        )
        source = str(entry.get("source") or "customized")
        config = entry.get("config") or {}
        await asyncio.to_thread(
            pool_service.replace_pool_skill_from_dir,
            skill_name=skill_name,
            source_dir=source_dir,
            source=source,
            config=config,
        )
        pool_updated.append(skill_name)
        await asyncio.to_thread(
            workspace_service.replace_workspace_skill_from_dir,
            skill_name=skill_name,
            source_dir=source_dir,
            source=source,
            config=config,
        )
        default_updated.append(skill_name)

    return BroadcastDefaultAgentTenantResult(
        tenant_id=target_tenant_id,
        success=True,
        bootstrapped=not was_bootstrapped,
        pool_updated=pool_updated,
        default_agent_updated=default_updated,
    )


async def _broadcast_default_agents(
    *,
    source_working_dir: Path,
    target_tenant_ids: list[str],
    skill_names: list[str],
    source_id: str | None,
    tenant_workspace_pool: Any,
) -> BroadcastDefaultAgentsResponse:
    results: list[BroadcastDefaultAgentTenantResult] = []
    for tenant_id in target_tenant_ids:
        try:
            validated_tenant_id = _validate_target_tenant_id(tenant_id)
            result = await _broadcast_skills_to_tenant(
                source_working_dir=source_working_dir,
                target_tenant_id=validated_tenant_id,
                skill_names=skill_names,
                source_id=source_id,
                tenant_workspace_pool=tenant_workspace_pool,
            )
            results.append(result)
        except Exception as exc:
            results.append(
                BroadcastDefaultAgentTenantResult(
                    tenant_id=str(tenant_id),
                    success=False,
                    error=str(exc),
                ),
            )
    return BroadcastDefaultAgentsResponse(results=results)


async def _ensure_source_pool_skills_exist(
    *,
    working_dir: Path,
    skill_names: list[str],
) -> None:
    if not skill_names:
        raise HTTPException(status_code=400, detail="No skill names provided")
    for skill_name in skill_names:
        try:
            await asyncio.to_thread(
                _read_pool_skill_entry,
                working_dir=working_dir,
                skill_name=skill_name,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _request_workspace_dir(request: Request) -> Path:
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    return Path(workspace.workspace_dir)


async def _request_workspace(request: Request) -> Any:
    from ..agent_context import get_agent_for_request

    return await get_agent_for_request(request)


def _schedule_workspace_reload(request: Request, workspace: Any) -> None:
    schedule_agent_reload(
        request,
        workspace.agent_id,
        tenant_id=getattr(workspace, "tenant_id", None)
        or _request_tenant_id(request),
    )


async def _hub_task_set_status(
    task_id: str,
    status: HubInstallTaskStatus,
    *,
    error: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    async with _hub_install_lock:
        task = _hub_install_tasks.get(task_id)
        if task is None:
            return
        task.status = status
        task.updated_at = time.time()
        if error is not None:
            task.error = error
        if result is not None:
            task.result = result


async def _hub_task_get(task_id: str) -> HubInstallTask | None:
    async with _hub_install_lock:
        return _hub_install_tasks.get(task_id)


async def _hub_task_register_runtime(task_id: str, task: asyncio.Task) -> None:
    async with _hub_install_lock:
        _hub_install_runtime_tasks[task_id] = task


async def _hub_task_pop_runtime(task_id: str) -> asyncio.Task | None:
    async with _hub_install_lock:
        return _hub_install_runtime_tasks.pop(task_id, None)


async def _read_validated_zip_upload(file: UploadFile) -> bytes:
    if file.content_type and file.content_type not in _ALLOWED_ZIP_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                "Expected a zip file, "
                f"got content-type: {file.content_type}"
            ),
        )

    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"File too large ({len(data) // (1024 * 1024)} MB). "
                f"Maximum is {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
            ),
        )
    return data


def _cleanup_imported_skill(workspace_dir: Path, skill_name: str) -> None:
    if not skill_name:
        return
    try:
        skill_service = SkillService(workspace_dir)
        skill_service.disable_skill(skill_name)
        skill_service.delete_skill(skill_name)
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "Cleanup after cancelled import failed for '%s': %s",
            skill_name,
            exc,
        )


async def _run_hub_install_task(
    *,
    task_id: str,
    workspace_dir: Path,
    body: HubInstallRequest,
    cancel_event: threading.Event,
) -> None:
    await _hub_task_set_status(task_id, HubInstallTaskStatus.IMPORTING)
    imported_skill_name: str | None = None
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: install_skill_from_hub(
                workspace_dir=workspace_dir,
                bundle_url=body.bundle_url,
                version=body.version,
                enable=body.enable,
                target_name=body.target_name,
                overwrite=body.overwrite,
                cancel_checker=cancel_event.is_set,
            ),
        )
        imported_skill_name = result.name
        if cancel_event.is_set():
            await asyncio.to_thread(
                _cleanup_imported_skill,
                workspace_dir,
                result.name,
            )
            await _hub_task_set_status(
                task_id,
                HubInstallTaskStatus.CANCELLED,
                result={
                    "installed": False,
                    "name": result.name,
                    "enabled": False,
                    "source_url": result.source_url,
                },
            )
            return
        await _hub_task_set_status(
            task_id,
            HubInstallTaskStatus.COMPLETED,
            result={
                "installed": True,
                "name": result.name,
                "enabled": result.enabled,
                "source_url": result.source_url,
            },
        )
    except SkillImportCancelled:
        if imported_skill_name:
            await asyncio.to_thread(
                _cleanup_imported_skill,
                workspace_dir,
                imported_skill_name,
            )
        await _hub_task_set_status(task_id, HubInstallTaskStatus.CANCELLED)
    except SkillScanError as exc:
        await _hub_task_set_status(
            task_id,
            HubInstallTaskStatus.FAILED,
            error=str(exc),
            result=_scan_error_payload(exc),
        )
    except ValueError as exc:
        await _hub_task_set_status(
            task_id,
            HubInstallTaskStatus.FAILED,
            error=str(exc),
        )
    except SkillConflictError as exc:
        await _hub_task_set_status(
            task_id,
            HubInstallTaskStatus.FAILED,
            error=str(exc),
            result=exc.detail,
        )
    except RuntimeError as exc:
        await _hub_task_set_status(
            task_id,
            HubInstallTaskStatus.FAILED,
            error=str(exc),
        )
    except Exception as exc:  # pragma: no cover
        await _hub_task_set_status(
            task_id,
            HubInstallTaskStatus.FAILED,
            error=f"Skill hub import failed: {exc}",
        )
    finally:
        await _hub_task_pop_runtime(task_id)


def _mtime_to_iso(mtime: float) -> str:
    """Convert a POSIX mtime float to an ISO-8601 UTC string."""
    if not mtime:
        return ""
    return (
        datetime.fromtimestamp(mtime, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _build_workspace_skill_specs(workspace_dir: Path) -> list[SkillSpec]:
    manifest = read_skill_manifest(workspace_dir, reconcile=False)
    entries = manifest.get("skills", {})
    specs: list[SkillSpec] = []
    for skill_name, entry in sorted(entries.items()):
        source = entry.get("source", "customized")
        skill_dir = resolve_workspace_managed_skill_dir(
            workspace_dir,
            skill_name,
            enabled=bool(entry.get("enabled", False)),
        )
        skill = _read_skill_from_dir(skill_dir, source)
        if skill is None:
            continue
        mtime = _get_skill_mtime(skill_dir)
        specs.append(
            SkillSpec(
                **skill.model_dump(),
                enabled=entry.get("enabled", False),
                channels=entry.get("channels") or ["all"],
                config=entry.get("config") or {},
                last_updated=_mtime_to_iso(mtime),
            ),
        )
    return specs


def _build_pool_skill_specs(
    *,
    working_dir: Path,
) -> list[PoolSkillSpec]:
    manifest = read_skill_pool_manifest(
        reconcile=False,
        working_dir=working_dir,
    )
    entries = manifest.get("skills", {})
    pool_dir = get_skill_pool_dir(working_dir=working_dir)
    sync_info = get_pool_builtin_sync_status(working_dir=working_dir)
    specs: list[PoolSkillSpec] = []
    for skill_name, entry in sorted(entries.items()):
        source = entry.get("source", "customized")
        skill_dir = pool_dir / skill_name
        skill = _read_skill_from_dir(skill_dir, source)
        if skill is None:
            continue
        info = sync_info.get(skill_name, {})
        mtime = _get_skill_mtime(skill_dir)
        specs.append(
            PoolSkillSpec(
                **skill.model_dump(exclude={"version_text"}),
                protected=bool(entry.get("protected", False)),
                version_text=str(entry.get("version_text", "") or ""),
                commit_text=str(entry.get("commit_text", "") or ""),
                sync_status=str(info.get("sync_status", "") or ""),
                latest_version_text=str(
                    info.get("latest_version_text", "") or "",
                ),
                config=entry.get("config") or {},
                last_updated=_mtime_to_iso(mtime),
            ),
        )
    return specs


@router.get("")
async def list_skills(request: Request) -> list[SkillSpec]:
    import asyncio

    workspace_dir = await _request_workspace_dir(request)
    return await asyncio.to_thread(_build_workspace_skill_specs, workspace_dir)


@router.get("/effective")
async def list_effective_skills(
    request: Request,
) -> list[EffectiveSkillSpec]:
    """Return only skills currently available to the Console runtime."""
    import asyncio

    workspace_dir = await _request_workspace_dir(request)
    effective_names = set(
        await asyncio.to_thread(
            resolve_effective_skills,
            workspace_dir,
            "console",
        ),
    )
    specs = await asyncio.to_thread(
        _build_workspace_skill_specs,
        workspace_dir,
    )
    return [
        EffectiveSkillSpec(name=skill.name, description=skill.description)
        for skill in specs
        if skill.name in effective_names
    ]


@router.post("/refresh")
async def refresh_skills(request: Request) -> list[SkillSpec]:
    """Force reconcile and return updated workspace skill list."""
    import asyncio

    workspace_dir = await _request_workspace_dir(request)
    await asyncio.to_thread(reconcile_workspace_manifest, workspace_dir)
    return await asyncio.to_thread(_build_workspace_skill_specs, workspace_dir)


@router.get("/hub/search")
async def search_hub(
    q: str = "",
    limit: int = 20,
) -> list[HubSkillSpec]:
    results = await asyncio.to_thread(search_hub_skills, q, limit=limit)
    return [
        HubSkillSpec(
            slug=item.slug,
            name=item.name,
            description=item.description,
            version=item.version,
            source_url=item.source_url,
        )
        for item in results
    ]


@router.get("/workspaces")
async def list_workspace_skill_sources(
    request: Request,
) -> list[WorkspaceSkillSummary]:
    summaries: list[WorkspaceSkillSummary] = []
    workspaces = await asyncio.to_thread(
        list_workspaces,
        tenant_id=_request_effective_tenant_id(request),
    )
    for workspace in workspaces:
        workspace_dir = Path(workspace["workspace_dir"])
        skills = await asyncio.to_thread(
            _build_workspace_skill_specs,
            workspace_dir,
        )
        summaries.append(
            WorkspaceSkillSummary(
                agent_id=workspace["agent_id"],
                agent_name=workspace.get("agent_name", ""),
                workspace_dir=str(workspace_dir),
                skills=skills,
            ),
        )
    return summaries


@router.post("/hub/install/start", response_model=HubInstallTask)
async def start_install_from_hub(
    request_body: HubInstallRequest,
    request: Request,
) -> HubInstallTask:
    workspace_dir = await _request_workspace_dir(request)
    task = HubInstallTask(
        bundle_url=request_body.bundle_url,
        version=request_body.version,
        enable=request_body.enable,
        overwrite=request_body.overwrite,
    )
    cancel_event = threading.Event()
    async with _hub_install_lock:
        _hub_install_tasks[task.task_id] = task
        _hub_install_cancel_events[task.task_id] = cancel_event

    runtime_task = asyncio.create_task(
        _run_hub_install_task(
            task_id=task.task_id,
            workspace_dir=workspace_dir,
            body=request_body,
            cancel_event=cancel_event,
        ),
        name=f"skill-hub-install-{task.task_id}",
    )
    await _hub_task_register_runtime(task.task_id, runtime_task)
    return task


@router.get("/hub/install/status/{task_id}", response_model=HubInstallTask)
async def get_hub_install_status(task_id: str) -> HubInstallTask:
    task = await _hub_task_get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="install task not found")
    return task


@router.post("/hub/install/cancel/{task_id}")
async def cancel_hub_install(task_id: str) -> dict[str, Any]:
    async with _hub_install_lock:
        task = _hub_install_tasks.get(task_id)
        if task is None:
            raise HTTPException(
                status_code=404,
                detail="install task not found",
            )
        if task.status in (
            HubInstallTaskStatus.COMPLETED,
            HubInstallTaskStatus.FAILED,
            HubInstallTaskStatus.CANCELLED,
        ):
            return {"task_id": task_id, "status": task.status.value}
        cancel_event = _hub_install_cancel_events.get(task_id)
        if cancel_event is not None:
            cancel_event.set()
        task.status = HubInstallTaskStatus.CANCELLED
        task.updated_at = time.time()
    return {"task_id": task_id, "status": "cancelled"}


@router.get("/pool")
async def list_pool_skills(
    request: Request,
) -> list[PoolSkillSpec]:
    return await asyncio.to_thread(
        _build_pool_skill_specs,
        working_dir=_request_tenant_working_dir(request),
    )


@router.post("/pool/refresh")
async def refresh_pool_skills(
    request: Request,
) -> list[PoolSkillSpec]:
    """Force reconcile and return updated pool skill list."""
    working_dir = _request_tenant_working_dir(request)
    await asyncio.to_thread(reconcile_pool_manifest, working_dir=working_dir)
    return await asyncio.to_thread(
        _build_pool_skill_specs,
        working_dir=working_dir,
    )


@router.get("/pool/builtin-sources")
async def list_pool_builtin_sources(
    request: Request,
) -> list[BuiltinImportSpec]:
    items = await asyncio.to_thread(
        list_builtin_import_candidates,
        working_dir=_request_tenant_working_dir(request),
    )
    return [BuiltinImportSpec(**item) for item in items]


@router.post("")
async def create_skill(
    request: Request,
    body: CreateSkillRequest,
) -> dict[str, Any]:
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    workspace_dir = Path(workspace.workspace_dir)
    try:
        created = await asyncio.to_thread(
            SkillService(workspace_dir).create_skill,
            name=body.name,
            content=body.content,
            overwrite=body.overwrite,
            references=body.references,
            scripts=body.scripts,
            config=body.config,
            enable=body.enable,
        )
    except SkillScanError as exc:
        return _scan_error_response(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not created:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "conflict",
                "suggested_name": suggest_conflict_name(body.name),
            },
        )
    if body.enable:
        _schedule_workspace_reload(request, workspace)
    return {"created": True, "name": created}


@router.post("/upload")
async def upload_skill_zip(
    request: Request,
    file: UploadFile = File(...),
    enable: bool = True,
    overwrite: bool = False,
    target_name: str = "",
    rename_map: str = "",
) -> dict[str, Any]:
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    workspace_dir = Path(workspace.workspace_dir)
    data = await _read_validated_zip_upload(file)
    parsed_rename: dict[str, str] | None = None
    if rename_map.strip():
        try:
            parsed_rename = json.loads(rename_map)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="rename_map must be valid JSON",
            ) from exc
        if not isinstance(parsed_rename, dict):
            raise HTTPException(
                status_code=400,
                detail="rename_map must be a JSON object",
            )
    try:
        result = await asyncio.to_thread(
            SkillService(workspace_dir).import_from_zip,
            data=data,
            overwrite=overwrite,
            enable=enable,
            target_name=target_name,
            rename_map=parsed_rename,
        )
    except SkillScanError as exc:
        return _scan_error_response(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.get("conflicts"):
        raise HTTPException(status_code=409, detail=result)
    if enable and result.get("count", 0) > 0:
        _schedule_workspace_reload(request, workspace)
    return result


@router.post("/pool/create")
async def create_pool_skill(
    request: Request,
    body: CreateSkillRequest,
) -> dict[str, Any]:
    try:
        created = await asyncio.to_thread(
            SkillPoolService(
                working_dir=_request_tenant_working_dir(request),
            ).create_skill,
            name=body.name,
            content=body.content,
            references=body.references,
            scripts=body.scripts,
            config=body.config,
            overwrite=body.overwrite,
        )
    except SkillScanError as exc:
        return _scan_error_response(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not created:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "conflict",
                "suggested_name": suggest_conflict_name(body.name),
            },
        )
    return {"created": True, "name": created}


@router.put("/pool/save")
async def save_pool_skill(
    request: Request,
    body: SavePoolSkillRequest,
) -> dict[str, Any]:
    """Edit or save-as a pool skill depending on the target name.

    Example:
    - editing a normal shared skill in place -> ``mode="edit"``
    - saving any skill under a new name -> ``mode="rename"``
    - editing a builtin in place -> conflict with suggestion
    - customizing a builtin -> save under a new name
    """
    service = SkillPoolService(
        working_dir=_request_tenant_working_dir(request),
    )
    try:
        result = await asyncio.to_thread(
            service.save_pool_skill,
            skill_name=body.source_name or body.name,
            target_name=body.name,
            content=body.content,
            references=body.references,
            scripts=body.scripts,
            config=body.config,
        )
    except SkillScanError as exc:
        return _scan_error_response(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result.get("success"):
        reason = result.get("reason")
        status = 404 if reason == "not_found" else 409
        raise HTTPException(status_code=status, detail=result)
    return result


@router.post("/pool/upload-zip")
async def upload_skill_pool_zip(
    request: Request,
    file: UploadFile = File(...),
    overwrite: bool = True,
    target_name: str = "",
    rename_map: str = "",
) -> dict[str, Any]:
    data = await _read_validated_zip_upload(file)
    parsed_rename: dict[str, str] | None = None
    if rename_map.strip():
        try:
            parsed_rename = json.loads(rename_map)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="rename_map must be valid JSON",
            ) from exc
        if not isinstance(parsed_rename, dict):
            raise HTTPException(
                status_code=400,
                detail="rename_map must be a JSON object",
            )
    try:
        result = await asyncio.to_thread(
            SkillPoolService(
                working_dir=_request_tenant_working_dir(request),
            ).import_from_zip,
            data=data,
            overwrite=overwrite,
            target_name=target_name,
            rename_map=parsed_rename,
        )
    except SkillScanError as exc:
        return _scan_error_response(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.get("conflicts"):
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/pool/import")
async def import_skill_pool_from_hub(
    request: Request,
    body: HubInstallRequest,
) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(
            import_pool_skill_from_hub,
            bundle_url=body.bundle_url,
            version=body.version,
            target_name=body.target_name,
            working_dir=_request_tenant_working_dir(request),
        )
    except SkillScanError as exc:
        return _scan_error_response(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SkillConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "installed": True,
        "name": result.name,
        "enabled": False,
        "source_url": result.source_url,
    }


@router.post("/pool/upload")
async def upload_workspace_skill_to_pool(
    request: Request,
    body: UploadToPoolRequest,
) -> dict[str, Any]:
    tenant_id = _request_effective_tenant_id(request)
    workspace_dir = _workspace_dir_for_agent(
        body.workspace_id,
        tenant_id=tenant_id,
    )
    try:
        result = await asyncio.to_thread(
            SkillPoolService(
                working_dir=_request_tenant_working_dir(request),
            ).upload_from_workspace,
            workspace_dir=workspace_dir,
            skill_name=body.skill_name,
            target_name=body.new_name,
            overwrite=body.overwrite,
        )
    except SkillScanError as exc:
        return _scan_error_response(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result.get("success"):
        status = 404 if result.get("reason") == "not_found" else 409
        raise HTTPException(status_code=status, detail=result)
    return result


def _preflight_download_conflicts(
    hub_service: SkillPoolService,
    targets: list[PoolDownloadTarget],
    skill_name: str,
    overwrite: bool,
    *,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Check all targets for conflicts before downloading."""
    conflicts: list[dict[str, Any]] = []
    for target in targets:
        workspace_dir = _workspace_dir_for_agent(
            target.workspace_id,
            tenant_id=tenant_id,
        )
        result = hub_service.preflight_download_to_workspace(
            skill_name=skill_name,
            workspace_dir=workspace_dir,
            target_name=target.target_name,
            overwrite=overwrite,
        )
        if not result.get("success"):
            conflicts.append(result)
    return conflicts


def _resolve_and_preflight(
    body: DownloadFromPoolRequest,
    *,
    tenant_id: str | None = None,
    working_dir: Path,
) -> tuple[list[PoolDownloadTarget], SkillPoolService]:
    """Resolve targets and reject if any conflicts exist."""
    targets = list(body.targets)
    if body.all_workspaces:
        targets = [
            PoolDownloadTarget(workspace_id=workspace["agent_id"])
            for workspace in list_workspaces(tenant_id=tenant_id)
        ]
    if not targets:
        raise HTTPException(
            status_code=400,
            detail="No workspace targets provided",
        )
    hub_service = SkillPoolService(working_dir=working_dir)
    try:
        conflicts = _preflight_download_conflicts(
            hub_service,
            targets,
            body.skill_name,
            body.overwrite,
            tenant_id=tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "downloaded": [],
                "conflicts": conflicts,
            },
        )
    return targets, hub_service


def _build_download_plan(
    targets: list[PoolDownloadTarget],
    skill_name: str,
    *,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build execution plan with rollback snapshots."""
    plan: list[dict[str, Any]] = []
    for target in targets:
        workspace_dir = _workspace_dir_for_agent(
            target.workspace_id,
            tenant_id=tenant_id,
        )
        snapshot = _snapshot_workspace_skill(
            workspace_dir,
            str(target.target_name or skill_name),
        )
        plan.append(
            {
                "workspace_id": target.workspace_id,
                "workspace_dir": workspace_dir,
                "target_name": target.target_name,
                "snapshot": snapshot,
            },
        )
    return plan


def _download_pool_skill_transaction(
    body: DownloadFromPoolRequest,
    *,
    tenant_id: str | None,
    working_dir: Path,
) -> dict[str, Any]:
    targets, hub_service = _resolve_and_preflight(
        body,
        tenant_id=tenant_id,
        working_dir=working_dir,
    )

    execution_plan = _build_download_plan(
        targets,
        body.skill_name,
        tenant_id=tenant_id,
    )

    downloaded: list[dict[str, str]] = []
    try:
        for plan in execution_plan:
            result = hub_service.download_to_workspace(
                skill_name=body.skill_name,
                workspace_dir=plan["workspace_dir"],
                target_name=plan["target_name"],
                overwrite=body.overwrite,
            )
            if not result.get("success"):
                for rollback in reversed(execution_plan):
                    _restore_workspace_skill(rollback["snapshot"])
                raise HTTPException(
                    status_code=409,
                    detail={
                        "downloaded": [],
                        "conflicts": [result],
                    },
                )
            downloaded.append(
                {
                    "workspace_id": str(plan["workspace_id"]),
                    "workspace_name": str(
                        result.get("workspace_name", "") or "",
                    ),
                    "name": str(result.get("name", "")),
                },
            )
    except HTTPException:
        raise
    except SkillScanError as exc:
        for rollback in reversed(execution_plan):
            _restore_workspace_skill(rollback["snapshot"])
        return _scan_error_response(exc)
    except Exception:
        for rollback in reversed(execution_plan):
            _restore_workspace_skill(rollback["snapshot"])
        raise
    finally:
        for plan in execution_plan:
            backup_dir = plan["snapshot"].get("backup_dir")
            if backup_dir is not None:
                shutil.rmtree(
                    Path(backup_dir).parent,
                    ignore_errors=True,
                )

    return {"downloaded": downloaded}


@router.post("/pool/download")
async def download_pool_skill_to_workspaces(
    request: Request,
    body: DownloadFromPoolRequest,
) -> dict[str, Any]:
    """Download one pool skill into one or more workspaces.

    All-or-nothing: if any target conflicts, reject everything.
    """
    tenant_id = _request_effective_tenant_id(request)
    working_dir = _request_tenant_working_dir(request)
    async with _POOL_SKILL_DOWNLOAD_LOCK:
        return await asyncio.to_thread(
            _download_pool_skill_transaction,
            body,
            tenant_id=tenant_id,
            working_dir=working_dir,
        )


@router.post("/pool/import-builtin")
async def import_pool_builtins(
    request: Request,
    body: ImportBuiltinRequest,
) -> dict[str, Any]:
    result = await asyncio.to_thread(
        import_builtin_skills,
        body.skill_names,
        overwrite_conflicts=body.overwrite_conflicts,
        working_dir=_request_tenant_working_dir(request),
    )
    if result.get("conflicts") and not body.overwrite_conflicts:
        raise HTTPException(status_code=409, detail=result)
    return result


@router.get(
    "/pool/broadcast/tenants",
    response_model=BroadcastTenantListResponse,
)
async def list_broadcast_tenants(
    request: Request,
) -> BroadcastTenantListResponse:
    return BroadcastTenantListResponse(
        tenant_ids=await list_logical_tenant_ids(
            _request_source_id(request),
            source_filter=True,
            include_templates=True,
        ),
    )


@router.post(
    "/pool/broadcast/default-agents",
    response_model=BroadcastDefaultAgentsResponse,
)
async def broadcast_pool_skills_to_default_agents(
    request: Request,
    body: BroadcastDefaultAgentsRequest,
) -> BroadcastDefaultAgentsResponse:
    if not body.overwrite:
        raise HTTPException(
            status_code=400,
            detail="overwrite=true is required for broadcast",
        )
    if not body.target_tenant_ids:
        raise HTTPException(
            status_code=400,
            detail="No target tenant IDs provided",
        )

    source_working_dir = _request_tenant_working_dir(request)
    source_id = _request_source_id(request)
    await _ensure_source_pool_skills_exist(
        working_dir=source_working_dir,
        skill_names=body.skill_names,
    )
    tenant_workspace_pool = getattr(
        getattr(getattr(request, "app", None), "state", request.state),
        "tenant_workspace_pool",
        None,
    )
    if tenant_workspace_pool is None:
        raise HTTPException(
            status_code=503,
            detail="Tenant pool not available",
        )
    return await _broadcast_default_agents(
        source_working_dir=source_working_dir,
        target_tenant_ids=body.target_tenant_ids,
        skill_names=body.skill_names,
        tenant_workspace_pool=tenant_workspace_pool,
        source_id=source_id,
    )


@router.post("/pool/{skill_name}/update-builtin")
async def update_pool_builtin(
    skill_name: str,
    request: Request,
) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            update_single_builtin,
            skill_name,
            working_dir=_request_tenant_working_dir(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/pool/{skill_name}")
async def delete_pool_skill(
    skill_name: str,
    request: Request,
) -> dict[str, Any]:
    deleted = await asyncio.to_thread(
        SkillPoolService(
            working_dir=_request_tenant_working_dir(request),
        ).delete_skill,
        skill_name,
    )
    if not deleted:
        raise HTTPException(
            status_code=409,
            detail="Skill pool entry cannot be deleted",
        )
    return {"deleted": True}


@router.get("/pool/{skill_name}/config")
async def get_pool_skill_config(
    skill_name: str,
    request: Request,
) -> dict[str, Any]:
    manifest = await asyncio.to_thread(
        read_skill_pool_manifest,
        working_dir=_request_tenant_working_dir(request),
    )
    entry = manifest.get("skills", {}).get(skill_name)
    if entry is None:
        raise HTTPException(status_code=404, detail="Pool skill not found")
    return {"config": entry.get("config", {})}


@router.put("/pool/{skill_name}/config")
async def update_pool_skill_config(
    skill_name: str,
    body: SkillConfigRequest,
    request: Request,
) -> dict[str, Any]:
    manifest_path = get_pool_skill_manifest_path(
        working_dir=_request_tenant_working_dir(request),
    )

    def _update(payload: dict[str, Any]) -> bool:
        entry = payload.get("skills", {}).get(skill_name)
        if entry is None:
            return False
        entry["config"] = dict(body.config)
        return True

    updated = await asyncio.to_thread(
        _mutate_json,
        manifest_path,
        _default_pool_manifest(),
        _update,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Pool skill not found")
    return {"updated": True}


@router.delete("/pool/{skill_name}/config")
async def delete_pool_skill_config(
    skill_name: str,
    request: Request,
) -> dict[str, Any]:
    manifest_path = get_pool_skill_manifest_path(
        working_dir=_request_tenant_working_dir(request),
    )

    def _update(payload: dict[str, Any]) -> bool:
        entry = payload.get("skills", {}).get(skill_name)
        if entry is None:
            return False
        entry.pop("config", None)
        return True

    updated = await asyncio.to_thread(
        _mutate_json,
        manifest_path,
        _default_pool_manifest(),
        _update,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Pool skill not found")
    return {"cleared": True}


@router.post("/batch-delete")
async def batch_delete_skills(
    request: Request,
    skills: list[str],
) -> dict[str, Any]:
    """Auto-disable then delete each skill. Per-skill results."""
    workspace_dir = await _request_workspace_dir(request)
    service = SkillService(workspace_dir)
    results: dict[str, Any] = {}
    for skill_name in skills:
        try:
            await asyncio.to_thread(service.disable_skill, skill_name)
            deleted = await asyncio.to_thread(service.delete_skill, skill_name)
            results[skill_name] = {
                "success": deleted,
                "reason": None if deleted else "delete_failed",
            }
        except Exception as exc:
            results[skill_name] = {
                "success": False,
                "reason": str(exc),
            }
    return {"results": results}


@router.post("/pool/batch-delete")
async def batch_delete_pool_skills(
    request: Request,
    skills: list[str],
) -> dict[str, Any]:
    """Delete multiple pool skills. Per-skill results."""
    service = SkillPoolService(
        working_dir=_request_tenant_working_dir(request),
    )
    results: dict[str, Any] = {}
    for skill_name in skills:
        try:
            deleted = await asyncio.to_thread(service.delete_skill, skill_name)
            results[skill_name] = {
                "success": deleted,
                "reason": None if deleted else "delete_failed",
            }
        except Exception as exc:
            results[skill_name] = {
                "success": False,
                "reason": str(exc),
            }
    return {"results": results}


@router.post("/batch-disable")
async def batch_disable_skills(
    request: Request,
    skills: list[str],
) -> dict[str, Any]:
    workspace = await _request_workspace(request)
    workspace_dir = Path(workspace.workspace_dir)
    service = SkillService(workspace_dir)
    results = dict(
        zip(
            skills,
            await asyncio.gather(
                *(
                    asyncio.to_thread(service.disable_skill, skill)
                    for skill in skills
                ),
            ),
        ),
    )
    if any(result.get("success") for result in results.values()):
        _schedule_workspace_reload(request, workspace)
    return {"results": results}


@router.post("/batch-enable")
async def batch_enable_skills(
    request: Request,
    skills: list[str],
) -> dict[str, Any]:
    """Enable each requested skill independently and collect per-skill results.

    Example:
        enabling ``["ok_skill", "blocked_skill"]`` returns success for the
        first item and ``reason="security_scan_failed"`` for the second,
        rather than aborting the entire batch.
    """
    workspace = await _request_workspace(request)
    workspace_dir = Path(workspace.workspace_dir)
    service = SkillService(workspace_dir)
    results: dict[str, Any] = {}
    for skill in skills:
        try:
            results[skill] = await asyncio.to_thread(
                service.enable_skill,
                skill,
            )
        except SkillScanError as exc:
            results[skill] = {
                "success": False,
                "reason": "security_scan_failed",
                "detail": _scan_error_payload(exc),
            }
    if any(result.get("success") for result in results.values()):
        _schedule_workspace_reload(request, workspace)
    return {"results": results}


@router.post("/{skill_name}/disable")
async def disable_skill(
    request: Request,
    skill_name: str,
) -> dict[str, Any]:
    workspace = await _request_workspace(request)
    workspace_dir = Path(workspace.workspace_dir)
    result = await asyncio.to_thread(
        SkillService(workspace_dir).disable_skill,
        skill_name,
    )
    if not result.get("success"):
        raise HTTPException(status_code=404, detail="Skill not found")
    _schedule_workspace_reload(request, workspace)
    return {"disabled": True, **result}


@router.post("/{skill_name}/enable")
async def enable_skill(
    request: Request,
    skill_name: str,
) -> dict[str, Any]:
    """Enable one workspace skill after a fresh scan."""
    workspace = await _request_workspace(request)
    workspace_dir = Path(workspace.workspace_dir)
    try:
        result = await asyncio.to_thread(
            SkillService(workspace_dir).enable_skill,
            skill_name,
        )
    except SkillScanError as exc:
        return _scan_error_response(exc)
    if not result.get("success"):
        raise HTTPException(
            status_code=404,
            detail=result.get("reason", "Skill not found"),
        )
    _schedule_workspace_reload(request, workspace)
    return {"enabled": True, **result}


@router.delete("/{skill_name}")
async def delete_skill(
    request: Request,
    skill_name: str,
) -> dict[str, Any]:
    workspace_dir = await _request_workspace_dir(request)
    service = SkillService(workspace_dir)
    await asyncio.to_thread(service.disable_skill, skill_name)
    deleted = await asyncio.to_thread(service.delete_skill, skill_name)
    if not deleted:
        raise HTTPException(
            status_code=409,
            detail="Only disabled workspace skills can be deleted",
        )
    return {"deleted": True}


@router.get("/{skill_name}/files/{source}/{file_path:path}")
async def load_skill_file(
    request: Request,
    skill_name: str,
    source: str,
    file_path: str,
) -> dict[str, Any]:
    workspace_dir = await _request_workspace_dir(request)
    content = await asyncio.to_thread(
        SkillService(workspace_dir).load_skill_file,
        skill_name=skill_name,
        file_path=file_path,
        source=source,
    )
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")
    return {"content": content}


@router.put("/save")
async def save_workspace_skill(
    request: Request,
    body: SaveSkillRequest,
) -> dict[str, Any]:
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    workspace_dir = Path(workspace.workspace_dir)
    try:
        result = await asyncio.to_thread(
            SkillService(workspace_dir).save_skill,
            skill_name=body.source_name or body.name,
            content=body.content,
            target_name=body.name if body.source_name else None,
            config=body.config,
            references=body.references,
            scripts=body.scripts,
        )
    except SkillScanError as exc:
        return _scan_error_response(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result.get("success"):
        if result.get("reason") == "conflict":
            raise HTTPException(status_code=409, detail=result)
        raise HTTPException(status_code=404, detail="Skill not found")
    if result.get("mode") != "noop":
        _schedule_workspace_reload(request, workspace)
    return result


@router.put("/{skill_name}/channels")
async def update_skill_channels_endpoint(
    request: Request,
    skill_name: str,
    channels: list[str],
) -> dict[str, Any]:
    from ..agent_context import get_agent_for_request

    workspace = await get_agent_for_request(request)
    workspace_dir = Path(workspace.workspace_dir)
    updated = await asyncio.to_thread(
        SkillService(workspace_dir).set_skill_channels,
        skill_name,
        channels,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Skill not found")
    _schedule_workspace_reload(request, workspace)
    return {"updated": True, "channels": channels}


@router.get("/{skill_name}/config")
async def get_skill_config_endpoint(
    request: Request,
    skill_name: str,
) -> dict[str, Any]:
    workspace_dir = await _request_workspace_dir(request)
    manifest = await asyncio.to_thread(read_skill_manifest, workspace_dir)
    entry = manifest.get("skills", {}).get(skill_name)
    if entry is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"config": entry.get("config", {})}


@router.put("/{skill_name}/config")
async def update_skill_config_endpoint(
    request: Request,
    skill_name: str,
    body: SkillConfigRequest,
) -> dict[str, Any]:
    workspace_dir = await _request_workspace_dir(request)
    manifest_path = get_workspace_skill_manifest_path(workspace_dir)

    def _update(payload: dict[str, Any]) -> bool:
        entry = payload.get("skills", {}).get(skill_name)
        if entry is None:
            return False
        entry["config"] = dict(body.config)
        return True

    updated = await asyncio.to_thread(
        _mutate_json,
        manifest_path,
        _default_workspace_manifest(),
        _update,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"updated": True}


@router.delete("/{skill_name}/config")
async def delete_skill_config_endpoint(
    request: Request,
    skill_name: str,
) -> dict[str, Any]:
    workspace_dir = await _request_workspace_dir(request)
    manifest_path = get_workspace_skill_manifest_path(workspace_dir)

    def _update(payload: dict[str, Any]) -> bool:
        entry = payload.get("skills", {}).get(skill_name)
        if entry is None:
            return False
        entry.pop("config", None)
        return True

    updated = await asyncio.to_thread(
        _mutate_json,
        manifest_path,
        _default_workspace_manifest(),
        _update,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"cleared": True}

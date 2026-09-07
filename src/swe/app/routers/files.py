# -*- coding: utf-8 -*-
"""File-related HTTP routes."""

from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import tempfile

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse

from ...config.context import (
    encode_scope_id,
    is_valid_identity_value,
)
from ...constant import WORKING_DIR
from ..agent_context import resolve_file_manager_workspace_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["files"])


class WorkspaceFileCopyTarget(BaseModel):
    """One tenant-scoped copy destination."""

    model_config = ConfigDict(populate_by_name=False, extra="forbid")

    tenant_id: str = Field(alias="tenantId")
    source_id: str = Field(alias="sourceId")

    @property
    def scope_id(self) -> str:
        """Return the server-derived canonical target scope."""
        return encode_scope_id(self.tenant_id, self.source_id)


class WorkspaceFileDistributionRequest(BaseModel):
    """Request to copy one current-workspace file to several users."""

    model_config = ConfigDict(populate_by_name=False, extra="forbid")

    source_path: str = Field(alias="sourcePath", min_length=1)
    targets: list[WorkspaceFileCopyTarget] = Field(min_length=1)
    target_path: str = Field(alias="targetPath", min_length=1)


class WorkspaceFileCopyResult(BaseModel):
    """Outcome for one requested target."""

    model_config = ConfigDict(populate_by_name=True)

    tenant_id: str = Field(alias="tenantId")
    source_id: str = Field(alias="sourceId")
    scope_id: str = Field(alias="scopeId")
    success: bool
    error: str = ""


class WorkspaceFileDistributionResponse(BaseModel):
    """Batch copy result."""

    model_config = ConfigDict(populate_by_name=True)

    source_path: str = Field(alias="sourcePath")
    target_path: str = Field(alias="targetPath")
    agent_id: str = Field(alias="agentId")
    results: list[WorkspaceFileCopyResult] = Field(default_factory=list)


def _validate_relative_file_path(value: str, field_name: str) -> str:
    """Validate one portable, workspace-relative file path."""
    normalized = str(value or "").strip()
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(normalized)
    invalid_format = (
        not normalized
        or "\\" in normalized
        or ":" in normalized
        or any(ord(char) < 32 for char in normalized)
    )
    escapes_workspace = (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or ".." in posix_path.parts
        or posix_path in {PurePosixPath("."), PurePosixPath("/")}
    )
    if invalid_format or escapes_workspace:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must be a relative path within a workspace",
        )
    return posix_path.as_posix()


def _canonical_targets(
    targets: list[WorkspaceFileCopyTarget],
) -> list[WorkspaceFileCopyTarget]:
    """Validate target identities before any copy starts."""
    normalized_targets: list[WorkspaceFileCopyTarget] = []
    seen_scope_ids: set[str] = set()
    for target in targets:
        tenant_id = target.tenant_id.strip()
        source_id = target.source_id.strip()
        if not is_valid_identity_value(tenant_id):
            raise HTTPException(
                status_code=422,
                detail="Invalid target tenantId",
            )
        if not is_valid_identity_value(source_id):
            raise HTTPException(
                status_code=422,
                detail="Invalid target sourceId",
            )
        scope_id = encode_scope_id(tenant_id, source_id)
        if scope_id in seen_scope_ids:
            raise HTTPException(
                status_code=422,
                detail="Target scopes must be unique",
            )
        seen_scope_ids.add(scope_id)
        normalized_targets.append(
            target.model_copy(
                update={
                    "tenant_id": tenant_id,
                    "source_id": source_id,
                },
            ),
        )
    return normalized_targets


def _path_within_root(
    root: Path,
    relative_path: str,
    *,
    escape_error: str,
) -> tuple[Path, Path]:
    """Return unresolved and resolved candidates inside a resolved root."""
    root = root.resolve()
    candidate = root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(escape_error) from exc
    return candidate, resolved


def _resolve_source_file(workspace_dir: Path, source_path: str) -> Path:
    candidate, resolved = _path_within_root(
        workspace_dir,
        source_path,
        escape_error="Source path escapes the agent workspace",
    )
    if candidate.is_symlink():
        raise HTTPException(
            status_code=422,
            detail="Source path must be a regular file, not a symbolic link",
        )
    if not candidate.exists():
        raise HTTPException(status_code=404, detail="Source file not found")
    if not resolved.is_file():
        raise HTTPException(
            status_code=422,
            detail="Source path must reference a regular file",
        )
    return resolved


def _target_workspace_dir(scope_id: str, agent_id: str) -> Path:
    working_root = Path(WORKING_DIR).resolve()
    workspaces_root = (working_root / scope_id / "workspaces").resolve(
        strict=False,
    )
    try:
        workspaces_root.relative_to(working_root)
    except ValueError as exc:
        raise ValueError("Target scope directory is unavailable") from exc

    workspace_dir = (workspaces_root / agent_id).resolve(strict=False)
    try:
        workspace_dir.relative_to(workspaces_root)
    except ValueError as exc:
        raise ValueError("Target agent workspace is unavailable") from exc
    if not workspace_dir.is_dir():
        raise ValueError("Target agent workspace does not exist")
    return workspace_dir


def _copy_to_target(
    source_file: Path,
    target: WorkspaceFileCopyTarget,
    agent_id: str,
    target_path: str,
) -> WorkspaceFileCopyResult:
    try:
        workspace_dir = _target_workspace_dir(target.scope_id, agent_id)
        candidate, resolved = _path_within_root(
            workspace_dir,
            target_path,
            escape_error="Target path escapes the agent workspace",
        )
        if candidate.is_symlink():
            raise ValueError("Target path must not be a symbolic link")
        if candidate.exists() and not resolved.is_file():
            raise ValueError("Target path must reference a regular file")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".workspace-file-copy-",
            dir=resolved.parent,
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            shutil.copy2(source_file, temporary_path)
            os.replace(temporary_path, resolved)
        finally:
            temporary_path.unlink(missing_ok=True)
        return WorkspaceFileCopyResult(
            tenant_id=target.tenant_id,
            source_id=target.source_id,
            scope_id=target.scope_id,
            success=True,
        )
    except ValueError as exc:
        return WorkspaceFileCopyResult(
            tenant_id=target.tenant_id,
            source_id=target.source_id,
            scope_id=target.scope_id,
            success=False,
            error=str(exc),
        )
    except OSError:
        logger.exception(
            "Workspace file distribution failed for tenant=%s scope=%s",
            target.tenant_id,
            target.scope_id,
        )
        return WorkspaceFileCopyResult(
            tenant_id=target.tenant_id,
            source_id=target.source_id,
            scope_id=target.scope_id,
            success=False,
            error="Failed to copy file",
        )


@router.api_route(
    "/preview/{filepath:path}",
    methods=["GET", "HEAD"],
    summary="Preview file",
)
async def preview_file(
    filepath: str,
):
    """Preview file."""
    path = Path(filepath)
    if not path.is_absolute():
        path = Path("/" + filepath)
    path = path.resolve()
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, filename=path.name)


@router.post(
    "/distribute",
    response_model=WorkspaceFileDistributionResponse,
    summary="Distribute a workspace file",
)
async def distribute_workspace_file(
    payload: WorkspaceFileDistributionRequest,
    request: Request,
) -> WorkspaceFileDistributionResponse:
    """Copy a current Agent workspace file to matching target scopes."""
    source_path = _validate_relative_file_path(
        payload.source_path,
        "source_path",
    )
    target_path = _validate_relative_file_path(
        payload.target_path,
        "target_path",
    )
    targets = _canonical_targets(payload.targets)
    workspace_dir = await resolve_file_manager_workspace_dir(request)
    try:
        source_file = _resolve_source_file(workspace_dir, source_path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    agent_id = workspace_dir.resolve().name
    results: list[WorkspaceFileCopyResult] = []
    for target in targets:
        results.append(
            await run_in_threadpool(
                _copy_to_target,
                source_file,
                target,
                agent_id,
                target_path,
            ),
        )
    return WorkspaceFileDistributionResponse(
        source_path=source_path,
        target_path=target_path,
        agent_id=agent_id,
        results=results,
    )

# -*- coding: utf-8 -*-
"""HTTP boundary for Default Agent Profile Hook management."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Annotated

from fastapi import (
    APIRouter,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from pydantic import BaseModel, ConfigDict, Field

from ...agents.hook_runtime.models import HookContext
from ...config.config import AgentProfileRef
from ...config.context import resolve_request_effective_tenant_id
from ...config.utils import get_tenant_config_path_strict, load_config
from ..hook_management import (
    HookAuditActor,
    HookConfigurationSnapshot,
    HookManagementConflict,
    HookManagementService,
    HookManagementValidationError,
    MAX_UPLOAD_FILES,
    MAX_SCRIPT_BYTES,
    UploadFilePayload,
)
from ..utils import schedule_agent_reload

router = APIRouter(prefix="/hook-management", tags=["hook-management"])


class HookScriptDiagnosticResponse(BaseModel):
    event: str
    group_id: str
    handler_id: str
    argument: str
    reason: str


class HookConfigurationResponse(BaseModel):
    hooks: dict[str, Any]
    revision: str
    diagnostics: list[HookScriptDiagnosticResponse] = Field(
        default_factory=list,
    )


class HookConfigurationUpdate(BaseModel):
    hooks: dict[str, Any]


class HookManualTestRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    confirm_real_execution: bool = Field(alias="confirmRealExecution")
    handler: dict[str, Any]
    context: HookContext


def _effective_tenant_id(request: Request) -> str | None:
    return resolve_request_effective_tenant_id(
        getattr(request.state, "tenant_id", None),
        getattr(request.state, "source_id", None),
        getattr(request.state, "scope_id", None),
    )


def _actor_for_request(request: Request) -> HookAuditActor:
    return HookAuditActor(
        user_id=getattr(request.state, "user_id", None),
        tenant_id=_effective_tenant_id(request),
    )


def _service_for_request(request: Request) -> HookManagementService:
    tenant_id = _effective_tenant_id(request)
    config = load_config(get_tenant_config_path_strict(tenant_id))
    profile: AgentProfileRef | None = config.agents.profiles.get("default")
    if profile is None:
        raise HTTPException(
            status_code=404,
            detail="Default Agent Profile not found",
        )
    return HookManagementService(
        Path(profile.workspace_dir).expanduser(),
        tenant_id=tenant_id,
    )


def _configuration_response(
    snapshot: HookConfigurationSnapshot,
) -> HookConfigurationResponse:
    return HookConfigurationResponse(
        hooks=snapshot.hooks,
        revision=snapshot.revision,
        diagnostics=[
            HookScriptDiagnosticResponse(**diagnostic.__dict__)
            for diagnostic in snapshot.diagnostics
        ],
    )


@router.get("/configuration", response_model=HookConfigurationResponse)
async def get_configuration(request: Request) -> HookConfigurationResponse:
    try:
        return _configuration_response(
            _service_for_request(request).get_configuration(),
        )
    except HookManagementValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/configuration", response_model=HookConfigurationResponse)
async def put_configuration(
    payload: HookConfigurationUpdate,
    request: Request,
    if_match: Annotated[str, Header(alias="If-Match")],
) -> HookConfigurationResponse:
    try:
        snapshot = _service_for_request(request).save_configuration(
            hooks=payload.hooks,
            expected_revision=if_match,
            actor=_actor_for_request(request),
        )
    except HookManagementConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HookManagementValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    schedule_agent_reload(
        request,
        "default",
        tenant_id=_effective_tenant_id(request),
    )
    return _configuration_response(snapshot)


@router.get("/scripts")
async def list_scripts(request: Request) -> list[dict[str, Any]]:
    return _service_for_request(request).list_scripts()


@router.post("/scripts")
async def upload_scripts(
    request: Request,
    files: list[UploadFile] = File(...),
    overwrite: Annotated[str, Form()] = "[]",
) -> dict[str, Any]:
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(
            status_code=422,
            detail=f"a batch may contain at most {MAX_UPLOAD_FILES} files",
        )
    try:
        parsed_overwrite = json.loads(overwrite)
        if not isinstance(parsed_overwrite, list) or not all(
            isinstance(name, str) for name in parsed_overwrite
        ):
            raise ValueError("overwrite must be a string list")
        overwrite_names = set(parsed_overwrite)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=400,
            detail="invalid overwrite list",
        ) from exc

    payloads = [
        UploadFilePayload(
            file.filename or "",
            await file.read(MAX_SCRIPT_BYTES + 1),
        )
        for file in files
    ]
    try:
        result = _service_for_request(request).upload_scripts(
            files=payloads,
            overwrite_names=overwrite_names,
            actor=_actor_for_request(request),
        )
    except HookManagementValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "accepted": result.accepted_names,
        "warned": list(result.warned),
        "failed": [failure.__dict__ for failure in result.failed],
    }


@router.post("/manual-test")
async def manual_test(
    payload: HookManualTestRequest,
    request: Request,
) -> dict[str, Any]:
    if not payload.confirm_real_execution:
        raise HTTPException(
            status_code=400,
            detail="confirmRealExecution must be true",
        )
    try:
        result = await _service_for_request(request).manual_test(
            handler=payload.handler,
            context=payload.context,
            actor=_actor_for_request(request),
            source_id=getattr(request.state, "source_id", None),
        )
    except HookManagementValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "redacted_summary": result.redacted_summary,
    }

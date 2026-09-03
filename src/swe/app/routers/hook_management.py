# -*- coding: utf-8 -*-
"""HTTP boundary for Default Agent Profile Hook management."""

from __future__ import annotations

import asyncio
import json
from functools import partial
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
from ...config.context import (
    decode_scope_id,
    is_valid_identity_value,
    resolve_request_effective_tenant_id,
)
from ...config.utils import (
    get_tenant_config_path_strict,
    get_tenant_request_working_dir,
    list_logical_tenant_ids,
    load_config,
)
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
from ..workspace.tenant_initializer import TenantInitializer

router = APIRouter(prefix="/hook-management", tags=["hook-management"])


class HookConfigurationResponse(BaseModel):
    hooks: dict[str, Any]
    revision: str


class HookConfigurationUpdate(BaseModel):
    hooks: dict[str, Any]


class HookManualTestRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    confirm_real_execution: bool = Field(alias="confirmRealExecution")
    handler: dict[str, Any]
    context: HookContext


class HookDistributionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    matcher_group_ids: list[str] = Field(alias="matcherGroupIds")
    target_tenant_ids: list[str] = Field(alias="targetTenantIds")


class HookDistributionTenantResult(BaseModel):
    tenant_id: str
    success: bool
    bootstrapped: bool = False
    matcher_group_ids: list[str] = Field(default_factory=list)
    script_names: list[str] = Field(default_factory=list)
    error: str = ""


class HookDistributionResponse(BaseModel):
    source_revision: str
    results: list[HookDistributionTenantResult] = Field(default_factory=list)


class HookDistributionTenantListResponse(BaseModel):
    tenant_ids: list[str] = Field(default_factory=list)


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
    return _service_for_tenant(tenant_id)


def _service_for_tenant(tenant_id: str | None) -> HookManagementService:
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


def _request_source_id(request: Request) -> str | None:
    return getattr(request.state, "source_id", None)


def _validate_target_tenant_id(tenant_id: str) -> str:
    normalized = str(tenant_id or "").strip()
    if normalized.startswith(("default_", "scope.v1.")):
        raise ValueError(f"invalid target tenant id: {tenant_id}")
    try:
        decode_scope_id(normalized)
    except ValueError:
        pass
    else:
        raise ValueError(f"invalid target tenant id: {tenant_id}")
    if not is_valid_identity_value(normalized):
        raise ValueError(f"invalid target tenant id: {tenant_id}")
    return normalized


def _get_multi_agent_manager(request: Request) -> Any:
    manager = getattr(request.app.state, "multi_agent_manager", None)
    if manager is None:
        raise RuntimeError("MultiAgentManager not initialized")
    return manager


async def _reload_target_default_agent(
    manager: Any,
    target_tenant_id: str,
) -> None:
    await manager.reload_agent("default", tenant_id=target_tenant_id)


async def _ensure_target_hook_service(
    request: Request,
    target_tenant_id: str,
) -> tuple[HookManagementService, str, bool]:
    initializer = TenantInitializer(
        get_tenant_request_working_dir(
            getattr(request.state, "tenant_id", None),
        ).parent,
        target_tenant_id,
        source_id=_request_source_id(request),
    )
    was_bootstrapped = initializer.has_seeded_bootstrap()
    if not was_bootstrapped:
        pool = getattr(request.app.state, "tenant_workspace_pool", None)
        if pool is None:
            raise HTTPException(
                status_code=503,
                detail="Tenant pool not available",
            )
        await pool.ensure_bootstrap(
            target_tenant_id,
            source_id=_request_source_id(request),
        )
    effective_target_tenant_id = getattr(
        initializer,
        "effective_tenant_id",
        target_tenant_id,
    )
    return (
        _service_for_tenant(effective_target_tenant_id),
        effective_target_tenant_id,
        not was_bootstrapped,
    )


def _configuration_response(
    snapshot: HookConfigurationSnapshot,
) -> HookConfigurationResponse:
    return HookConfigurationResponse(
        hooks=snapshot.hooks,
        revision=snapshot.revision,
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
        result = await asyncio.to_thread(
            _service_for_request(request).upload_scripts,
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


@router.get(
    "/distribution/tenants",
    response_model=HookDistributionTenantListResponse,
)
async def list_distribution_tenants(
    request: Request,
) -> HookDistributionTenantListResponse:
    return HookDistributionTenantListResponse(
        tenant_ids=await list_logical_tenant_ids(
            _request_source_id(request),
            source_filter=True,
            include_templates=True,
        ),
    )


@router.post(
    "/distribute/default-agents",
    response_model=HookDistributionResponse,
)
async def distribute_to_default_agents(
    payload: HookDistributionRequest,
    request: Request,
) -> HookDistributionResponse:
    if not payload.target_tenant_ids:
        raise HTTPException(
            status_code=400,
            detail="at least one target tenant must be selected",
        )
    source_tenant_id = getattr(request.state, "tenant_id", None)
    try:
        target_tenant_ids = [
            _validate_target_tenant_id(tenant_id)
            for tenant_id in payload.target_tenant_ids
        ]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if len(set(target_tenant_ids)) != len(target_tenant_ids):
        raise HTTPException(
            status_code=422,
            detail="target tenant ids must be unique",
        )
    if source_tenant_id in target_tenant_ids:
        raise HTTPException(
            status_code=400,
            detail="current tenant cannot be a distribution target",
        )

    source_service = _service_for_request(request)
    try:
        distribution = source_service.prepare_distribution(
            payload.matcher_group_ids,
        )
    except HookManagementConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HookManagementValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    actor = _actor_for_request(request)
    manager = _get_multi_agent_manager(request)
    results: list[HookDistributionTenantResult] = []
    for target_tenant_id in target_tenant_ids:
        bootstrapped = False
        target_service: HookManagementService | None = None
        try:
            target_service, effective_target_tenant_id, bootstrapped = (
                await _ensure_target_hook_service(request, target_tenant_id)
            )

            result = await source_service.distribute_payload_to_target(
                payload=distribution,
                target=target_service,
                actor=actor,
                activate=partial(
                    _reload_target_default_agent,
                    manager,
                    effective_target_tenant_id,
                ),
                target_tenant_id=target_tenant_id,
                bootstrapped=bootstrapped,
            )
            results.append(
                HookDistributionTenantResult(
                    tenant_id=target_tenant_id,
                    success=True,
                    bootstrapped=bootstrapped,
                    matcher_group_ids=list(result.matcher_group_ids),
                    script_names=list(result.script_names),
                ),
            )
        except Exception as exc:
            if target_service is None:
                source_service.emit_distribution_failure(
                    payload=distribution,
                    actor=actor,
                    target_tenant_id=target_tenant_id,
                    bootstrapped=bootstrapped,
                    error=str(exc),
                )
            results.append(
                HookDistributionTenantResult(
                    tenant_id=target_tenant_id,
                    success=False,
                    bootstrapped=bootstrapped,
                    error=str(exc),
                ),
            )

    return HookDistributionResponse(
        source_revision=distribution.revision,
        results=results,
    )

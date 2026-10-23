# -*- coding: utf-8 -*-
"""Source-scoped management and effective-metadata API for source tools."""

from __future__ import annotations

from typing import Any
from pathlib import Path
import uuid

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from swe.config.context import is_valid_identity_value

from .models import (
    SourceToolAuditEvent,
    SourceToolDraft,
    SourceToolMetadata,
    SourceToolVersion,
)
from .service import (
    SourceToolConflict,
    SourceToolSafetyError,
    SourceToolService,
)
from .validation import MAX_SOURCE_TOOL_BYTES, SourceToolValidationError

router = APIRouter(prefix="/source-tools", tags=["source-tools"])
_MANAGER_ROLES = frozenset({"manager", "admin"})


class SourceToolDraftResponse(BaseModel):
    """Script-free manager draft response."""

    name: str
    description: str
    json_schema: dict[str, Any]
    required_env: list[str]
    content_digest: str
    created_at: float
    created_by: str | None
    status: str = "draft"


class SourceToolVersionResponse(SourceToolDraftResponse):
    """Script-free published-version response."""

    version: int


class SourceToolMetadataResponse(BaseModel):
    """Script-free effective tool metadata for tenants."""

    name: str
    version: int
    description: str
    json_schema: dict[str, Any]
    required_env: list[str]
    content_digest: str
    active: bool
    origin: str


class SourceToolAuditResponse(BaseModel):
    """Metadata-only lifecycle audit response."""

    event: str
    source_id: str
    tool_name: str
    actor: str | None
    timestamp: float
    version: int | None = None
    content_digest: str | None = None


class SourceToolManualTestRequest(BaseModel):
    """Explicit, real execution request for an unpublished draft."""

    confirmed: bool = False
    arguments: dict[str, Any] = Field(default_factory=dict)


class SourceToolManualTestResponse(BaseModel):
    """The normal ToolResponse payload produced by a guarded draft call."""

    output: Any


def _get_service(request: Request) -> SourceToolService:
    service = getattr(request.app.state, "source_tool_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Source tool service unavailable",
        )
    return service


def _source_id(request: Request) -> str:
    source_id = getattr(request.state, "source_id", None)
    if not source_id or not is_valid_identity_value(source_id):
        raise HTTPException(
            status_code=400,
            detail="Source context missing or invalid",
        )
    return source_id


def _actor(request: Request) -> str:
    role = request.headers.get("X-User-Role", "").strip().lower()
    if role not in _MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="Manager role required")
    return (
        getattr(request.state, "user", None)
        or request.headers.get("X-User-Id")
        or "unknown"
    )


def _draft_response(value: SourceToolDraft) -> SourceToolDraftResponse:
    return SourceToolDraftResponse(
        name=value.name,
        description=value.description,
        json_schema=value.json_schema,
        required_env=list(value.required_env),
        content_digest=value.content_digest,
        created_at=value.created_at,
        created_by=value.created_by,
    )


def _version_response(value: SourceToolVersion) -> SourceToolVersionResponse:
    return SourceToolVersionResponse(
        name=value.name,
        version=value.version,
        description=value.description,
        json_schema=value.json_schema,
        required_env=list(value.required_env),
        content_digest=value.content_digest,
        created_at=value.created_at,
        created_by=value.created_by,
    )


def _metadata_response(
    value: SourceToolMetadata,
) -> SourceToolMetadataResponse:
    return SourceToolMetadataResponse(
        name=value.name,
        version=value.version,
        description=value.description,
        json_schema=value.json_schema,
        required_env=list(value.required_env),
        content_digest=value.content_digest,
        active=value.active,
        origin=value.origin,
    )


def _audit_response(value: SourceToolAuditEvent) -> SourceToolAuditResponse:
    return SourceToolAuditResponse(**value.to_dict())


@router.get("/effective", response_model=list[SourceToolMetadataResponse])
async def effective_source_tools(
    request: Request,
) -> list[SourceToolMetadataResponse]:
    """List active script-free tools for the current source."""
    return [
        _metadata_response(value)
        for value in _get_service(request).list_metadata(_source_id(request))
    ]


@router.get("/drafts", response_model=list[SourceToolDraftResponse])
async def list_drafts(request: Request) -> list[SourceToolDraftResponse]:
    """List current-source drafts for source managers."""
    _actor(request)
    return [
        _draft_response(value)
        for value in _get_service(request).list_drafts(_source_id(request))
    ]


@router.post("/drafts", response_model=SourceToolDraftResponse)
async def create_draft(
    request: Request,
    file: UploadFile = File(...),
    replace_draft: bool = Form(default=False),
) -> SourceToolDraftResponse:
    """Upload, statically validate, and safety-scan one source-tool draft."""
    actor = _actor(request)
    try:
        content = await file.read(MAX_SOURCE_TOOL_BYTES + 1)
        if len(content) > MAX_SOURCE_TOOL_BYTES:
            raise SourceToolValidationError("script exceeds the 1 MiB limit")
        draft = _get_service(request).create_draft(
            _source_id(request),
            content,
            actor=actor,
            replace_draft=replace_draft,
        )
    except SourceToolConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (SourceToolValidationError, SourceToolSafetyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _draft_response(draft)


@router.delete("/drafts/{tool_name}", status_code=204)
async def discard_draft(tool_name: str, request: Request) -> None:
    """Discard an unpublished draft, retaining only its audit metadata."""
    actor = _actor(request)
    try:
        _get_service(request).discard_draft(
            _source_id(request),
            tool_name,
            actor=actor,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/drafts/{tool_name}/download")
async def download_draft(tool_name: str, request: Request) -> dict[str, str]:
    """Return source-manager draft content; never include it in list responses."""
    _actor(request)
    try:
        return {
            "content": _get_service(request)
            .download_draft(_source_id(request), tool_name)
            .decode("utf-8"),
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/drafts/{tool_name}/publish",
    response_model=SourceToolVersionResponse,
)
async def publish_draft(
    tool_name: str,
    request: Request,
    confirm_replace: bool = Form(default=False),
) -> SourceToolVersionResponse:
    """Publish a draft, requiring confirmation when it replaces an active tool."""
    actor = _actor(request)
    try:
        published = _get_service(request).publish(
            _source_id(request),
            tool_name,
            actor=actor,
            confirm_replace=confirm_replace,
        )
    except SourceToolConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _version_response(published)


@router.post(
    "/drafts/{tool_name}/manual-test",
    response_model=SourceToolManualTestResponse,
)
async def manual_test_draft(
    tool_name: str,
    payload: SourceToolManualTestRequest,
    request: Request,
) -> SourceToolManualTestResponse:
    """Execute a draft through the selected Agent's ordinary guard path."""
    actor = _actor(request)
    if not payload.confirmed:
        raise HTTPException(
            status_code=400,
            detail="Manual test requires explicit side-effect confirmation",
        )
    source_id = _source_id(request)
    service = _get_service(request)
    try:
        draft = service.get_draft(source_id, tool_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        from jsonschema import (
            Draft202012Validator,
            SchemaError,
            ValidationError,
        )

        validator = Draft202012Validator(draft.json_schema)
        validator.validate(payload.arguments)
    except (SchemaError, ValidationError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Manual test arguments violate tool JSON schema: {exc.message}",
        ) from exc

    from swe.agents.react_agent import SWEAgent
    from swe.agents.model_factory import prepare_model_config_async
    from swe.agents.prompt import (
        PromptConfig,
        bind_prepared_system_prompt,
        prepare_system_prompt_async,
    )
    from swe.agents.skill_runtime_snapshot import (
        get_workspace_skill_snapshot_async,
    )
    from swe.app.agent_context import get_agent_and_config_for_request
    from swe.config.config import BuiltinToolConfig
    from swe.constant import WORKING_DIR

    workspace, agent_config = await get_agent_and_config_for_request(request)
    test_config = agent_config.model_copy(deep=True)
    if test_config.tools is None:
        raise HTTPException(
            status_code=500,
            detail="Agent tool config unavailable",
        )
    tool_config = test_config.tools.builtin_tools.get(tool_name)
    if tool_config is None:
        test_config.tools.builtin_tools[tool_name] = BuiltinToolConfig(
            name=tool_name,
            enabled=True,
            description=draft.description,
        )
    else:
        tool_config.enabled = True

    from .models import SourceToolVersion

    draft_version = SourceToolVersion(
        name=draft.name,
        version=0,
        description=draft.description,
        json_schema=draft.json_schema,
        required_env=draft.required_env,
        content_digest=draft.content_digest,
        script=draft.script,
        created_at=draft.created_at,
        created_by=draft.created_by,
    )
    tool_call_id = "source-draft-test-" + uuid.uuid4().hex
    prepared_model = await prepare_model_config_async(
        agent_id=workspace.agent_id,
        tenant_id=workspace.tenant_id,
    )
    workspace_dir = Path(workspace.workspace_dir or WORKING_DIR)
    skill_snapshot = await get_workspace_skill_snapshot_async(workspace_dir)
    prompt_files = test_config.system_prompt_files
    prepared_prompt = await prepare_system_prompt_async(
        workspace_dir,
        list(
            (
                PromptConfig.DEFAULT_FILES
                if prompt_files is None
                else prompt_files
            ),
        ),
        workspace.agent_id,
        bool(getattr(test_config.heartbeat, "enabled", False)),
    )
    with bind_prepared_system_prompt(prepared_prompt):
        agent = SWEAgent(
            agent_config=test_config,
            prepared_model_config=prepared_model,
            workspace_skill_snapshot=skill_snapshot,
            request_context={
                "session_id": "source-draft-test-" + uuid.uuid4().hex,
                "user_id": actor,
                "channel": "console",
                "agent_id": workspace.agent_id,
                "tenant_id": workspace.tenant_id or "",
                "source_id": source_id,
            },
            workspace_dir=workspace.workspace_dir,
            task_tracker=getattr(workspace, "_task_tracker", None),
            source_tool_versions=(draft_version,),
        )
    try:
        await agent._acting_impl(
            {
                "type": "tool_use",
                "id": tool_call_id,
                "name": tool_name,
                "input": payload.arguments,
            },
        )
        output = agent.memory.content[-1][0].content[0]["output"]
    except Exception as exc:  # noqa: BLE001
        service.record_manual_test(
            source_id=source_id,
            tool=draft_version,
            actor=actor,
            tenant_id=workspace.tenant_id,
            agent_id=workspace.agent_id,
            result="failed",
        )
        raise HTTPException(
            status_code=500,
            detail="Manual test failed",
        ) from exc
    service.record_manual_test(
        source_id=source_id,
        tool=draft_version,
        actor=actor,
        tenant_id=workspace.tenant_id,
        agent_id=workspace.agent_id,
        result="completed",
    )
    return SourceToolManualTestResponse(output=output)


@router.post("/active/{tool_name}/deactivate", status_code=204)
async def deactivate_source_tool(tool_name: str, request: Request) -> None:
    """Deactivate one active version without deleting its published history."""
    actor = _actor(request)
    try:
        _get_service(request).deactivate(
            _source_id(request),
            tool_name,
            actor=actor,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/history/{tool_name}",
    response_model=list[SourceToolVersionResponse],
)
async def source_tool_history(
    tool_name: str,
    request: Request,
) -> list[SourceToolVersionResponse]:
    """List immutable source-tool versions for source managers."""
    _actor(request)
    try:
        records = _get_service(request).history(_source_id(request), tool_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [_version_response(value) for value in records]


@router.get("/history/{tool_name}/{version}/download")
async def download_version(
    tool_name: str,
    version: int,
    request: Request,
) -> dict[str, str]:
    """Return controlled script content for a published version."""
    _actor(request)
    try:
        return {
            "content": _get_service(request)
            .download_version(_source_id(request), tool_name, version)
            .decode("utf-8"),
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/audit", response_model=list[SourceToolAuditResponse])
async def source_tool_audit(
    request: Request,
) -> list[SourceToolAuditResponse]:
    """List metadata-only source-tool lifecycle audit for managers."""
    _actor(request)
    return [
        _audit_response(value)
        for value in _get_service(request).audit(_source_id(request))
    ]

# -*- coding: utf-8 -*-
"""Agent-owned SubAgent expert configuration APIs."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ...app.subagents import (
    AgentOwnedDefinitionConflict,
    AgentOwnedDefinitionPackage,
    AgentOwnedDefinitionRepository,
    builtin_definition_provider,
)
from ...runtime_workers import run_runtime_state_work

router = APIRouter(prefix="/experts", tags=["experts"])


class ExpertPayload(BaseModel):
    """Managed fields accepted from the expert configuration form."""

    model_config = ConfigDict(extra="ignore")

    name: str
    description: str
    instruction: str
    trigger_keywords: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    mcps: list[str] | None = None
    tools: dict[str, Any] = Field(default_factory=dict)
    model: dict[str, str] | None = None
    budget: dict[str, int] = Field(default_factory=dict)


class ExpertResponse(BaseModel):
    definition_id: str
    revision: str
    valid: bool
    validation_error: str = ""
    enabled: bool = False
    definition: dict[str, Any] | None = None
    toml: str


async def _repository(request: Request) -> AgentOwnedDefinitionRepository:
    """Resolve the selected Agent, falling back to its active profile."""
    from ..agent_context import get_agent_and_config_for_request

    workspace, _ = await get_agent_and_config_for_request(request)
    builtin_names = {
        definition.name
        for definition in builtin_definition_provider().list_definitions()
    }
    return AgentOwnedDefinitionRepository(
        Path(workspace.workspace_dir).expanduser() / "agents",
        owner_scope=f"{workspace.tenant_id}/{workspace.agent_id}",
        builtin_names=builtin_names,
    )


def _response(package: AgentOwnedDefinitionPackage) -> ExpertResponse:
    definition = package.definition
    return ExpertResponse(
        definition_id=package.definition_id,
        revision=package.revision,
        valid=package.valid,
        validation_error=package.validation_error,
        enabled=definition.enabled if definition is not None else False,
        definition=(
            definition.model_dump(mode="json") if definition else None
        ),
        toml=package.toml,
    )


def _conflict(exc: ValueError) -> HTTPException:
    status = 409 if isinstance(exc, AgentOwnedDefinitionConflict) else 422
    return HTTPException(status_code=status, detail=str(exc))


@router.get("", response_model=list[ExpertResponse])
async def list_experts(request: Request) -> list[ExpertResponse]:
    repository = await _repository(request)
    return [
        _response(package)
        for package in await run_runtime_state_work(repository.list)
    ]


@router.get("/{definition_id}", response_model=ExpertResponse)
async def get_expert(definition_id: str, request: Request) -> ExpertResponse:
    try:
        repository = await _repository(request)
        package = await run_runtime_state_work(repository.get, definition_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if package is None:
        raise HTTPException(status_code=404, detail="expert not found")
    return _response(package)


@router.post("/preview", response_model=ExpertResponse)
async def preview_expert(
    payload: ExpertPayload,
    request: Request,
) -> ExpertResponse:
    try:
        repository = await _repository(request)
        return _response(
            await run_runtime_state_work(
                repository.preview,
                payload.model_dump(),
            ),
        )
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.post("", response_model=ExpertResponse, status_code=201)
async def create_expert(
    payload: ExpertPayload,
    request: Request,
) -> ExpertResponse:
    try:
        repository = await _repository(request)
        return _response(
            await run_runtime_state_work(
                repository.create,
                payload.model_dump(),
            ),
        )
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.put("/{definition_id}", response_model=ExpertResponse)
async def update_expert(
    definition_id: str,
    payload: ExpertPayload,
    request: Request,
    if_match: Annotated[str, Header(alias="If-Match")],
) -> ExpertResponse:
    try:
        repository = await _repository(request)
        package = await run_runtime_state_work(
            repository.update,
            definition_id,
            payload.model_dump(),
            expected_revision=if_match,
        )
    except ValueError as exc:
        raise _conflict(exc) from exc
    return _response(package)


@router.post("/{definition_id}/enable", response_model=ExpertResponse)
async def enable_expert(
    definition_id: str,
    request: Request,
    if_match: Annotated[str, Header(alias="If-Match")],
) -> ExpertResponse:
    try:
        repository = await _repository(request)
        package = await run_runtime_state_work(
            repository.enable,
            definition_id,
            expected_revision=if_match,
        )
    except ValueError as exc:
        raise _conflict(exc) from exc
    return _response(package)


@router.post("/{definition_id}/disable", response_model=ExpertResponse)
async def disable_expert(
    definition_id: str,
    request: Request,
    if_match: Annotated[str, Header(alias="If-Match")],
) -> ExpertResponse:
    try:
        repository = await _repository(request)
        package = await run_runtime_state_work(
            repository.disable,
            definition_id,
            expected_revision=if_match,
        )
    except ValueError as exc:
        raise _conflict(exc) from exc
    return _response(package)


@router.delete("/{definition_id}", status_code=204)
async def delete_expert(
    definition_id: str,
    request: Request,
    if_match: Annotated[str, Header(alias="If-Match")],
) -> None:
    try:
        repository = await _repository(request)
        await run_runtime_state_work(
            repository.delete,
            definition_id,
            expected_revision=if_match,
        )
    except ValueError as exc:
        raise _conflict(exc) from exc

# -*- coding: utf-8 -*-
"""HTTP and SSE API for the W+ SOP workspace."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, AsyncGenerator
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.responses import FileResponse, Response, StreamingResponse

from ...agents.skills_manager import resolve_effective_skill_dir
from ..agent_context import get_agent_for_request
from .models import OwnershipTuple
from .runtime import get_wplus_safe_stream_trace_registry
from .service import (
    WPlusCommandError,
    WPlusOwningChatFinalizingError,
    WPlusOwnershipError,
    WPlusRuntimeStartError,
    WPlusSopService,
    serialize_session,
    store_path_for_workspace,
)
from .store import (
    ActiveSessionExistsError,
    EntryProposalConflictError,
    StaleStateVersionError,
    WPlusSopStore,
)

router = APIRouter(prefix="/wplus-sop", tags=["wplus-sop"])

# Protected inline previews are capped at 5 MiB; downloads remain streamable.
DEFAULT_MAX_ARTIFACT_PREVIEW_BYTES = 5 * 1024 * 1024
MAX_ARTIFACT_PREVIEW_BYTES = DEFAULT_MAX_ARTIFACT_PREVIEW_BYTES
_ARTIFACT_READ_CHUNK_BYTES = 64 * 1024
_ARTIFACT_PREVIEW_TOO_LARGE_DETAIL = (
    "W+ SOP artifact preview exceeds 5 MiB limit"
)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntryCommandRequest(StrictRequest):
    command_request_id: str = Field(min_length=1)


class WPlusCommandRequest(StrictRequest):
    command: str = Field(min_length=1)
    command_request_id: str = Field(min_length=1)
    expected_state_version: int = Field(ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)


def _request_identity(request: Request, workspace: Any) -> dict[str, str]:
    identity = {
        "tenant_id": str(
            getattr(request.state, "tenant_id", None) or "",
        ).strip(),
        "source_id": str(
            getattr(request.state, "source_id", None)
            or request.headers.get("X-Source-Id")
            or "",
        ).strip(),
        "user_id": str(
            getattr(request.state, "user_id", None)
            or request.headers.get("X-User-Id")
            or "",
        ).strip(),
        "agent_id": str(getattr(workspace, "agent_id", "") or "").strip(),
    }
    if not all(identity.values()):
        raise HTTPException(status_code=404, detail="W+ SOP Session not found")
    return identity


def _identity_matches(
    ownership: OwnershipTuple,
    identity: dict[str, str],
) -> bool:
    return all(
        getattr(ownership, key) == value
        for key, value in identity.items()
    )


async def _service_for_chat(
    request: Request,
    chat_id: str,
) -> WPlusSopService:
    workspace = await get_agent_for_request(request)
    identity = _request_identity(request, workspace)
    chat = await workspace.chat_manager.get_chat(chat_id)
    if chat is None or chat.user_id != identity["user_id"]:
        raise HTTPException(status_code=404, detail="W+ SOP Session not found")
    ownership = OwnershipTuple(
        **identity,
        chat_id=chat.id,
        logical_chat_session_id=chat.session_id,
    )
    return WPlusSopService(workspace=workspace, ownership=ownership)


async def _service_for_session(
    request: Request,
    sop_session_id: str,
) -> WPlusSopService:
    workspace = await get_agent_for_request(request)
    identity = _request_identity(request, workspace)
    store = WPlusSopStore(store_path_for_workspace(workspace.workspace_dir))
    record = store.get_session(sop_session_id)
    if record is None or not _identity_matches(
        record.projection.ownership,
        identity,
    ):
        raise HTTPException(status_code=404, detail="W+ SOP Session not found")
    return WPlusSopService(
        workspace=workspace,
        ownership=record.projection.ownership,
        store=store,
    )


async def _service_for_proposal(
    request: Request,
    proposal_id: str,
) -> WPlusSopService:
    workspace = await get_agent_for_request(request)
    identity = _request_identity(request, workspace)
    store = WPlusSopStore(store_path_for_workspace(workspace.workspace_dir))
    proposal = store.get_entry_proposal(proposal_id)
    if proposal is None or not _identity_matches(proposal.ownership, identity):
        raise HTTPException(status_code=404, detail="W+ SOP proposal not found")
    return WPlusSopService(
        workspace=workspace,
        ownership=proposal.ownership,
        store=store,
    )


def _skill_snapshot_id(workspace_dir: Path | str) -> str:
    skill_dir = resolve_effective_skill_dir(
        Path(workspace_dir),
        "wplus-sop-miner",
    )
    skill_file = skill_dir / "SKILL.md" if skill_dir is not None else None
    if skill_file is None or not skill_file.is_file():
        raise HTTPException(
            status_code=503,
            detail="W+ SOP Miner contract is unavailable",
        )
    digest = hashlib.sha256(skill_file.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, WPlusOwnershipError):
        raise HTTPException(
            status_code=404,
            detail="W+ SOP Session not found",
        ) from exc
    if isinstance(exc, StaleStateVersionError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, WPlusOwningChatFinalizingError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "message": str(exc),
                "retry_after_ms": exc.retry_after_ms,
            },
        ) from exc
    if isinstance(
        exc,
        (
            WPlusCommandError,
            EntryProposalConflictError,
            ValidationError,
        ),
    ):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if isinstance(exc, ActiveSessionExistsError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, WPlusRuntimeStartError):
        raise HTTPException(
            status_code=503,
            detail="W+ SOP run could not start; Session is recoverable",
        ) from exc
    raise exc


async def _session_snapshot(
    service: WPlusSopService,
    record: Any,
    *,
    sop_session_id: str | None = None,
    runtime_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = serialize_session(record)
    if runtime_status is not None:
        snapshot["runtime_status"] = runtime_status
        return snapshot
    get_runtime_status = getattr(service, "get_runtime_status", None)
    if not callable(get_runtime_status):
        return snapshot
    resolved_session_id = sop_session_id or str(
        record.projection.sop_session_id,
    )
    snapshot["runtime_status"] = await get_runtime_status(
        resolved_session_id,
    )
    return snapshot


@router.get("/sessions/{sop_session_id}")
async def get_wplus_sop_session(
    sop_session_id: str,
    request: Request,
) -> dict[str, Any]:
    service = await _service_for_session(request, sop_session_id)
    await service.recover_orphaned_generation_run(sop_session_id)
    await service.flush_chat_projection_outbox()
    return await _session_snapshot(
        service,
        service.get_session(sop_session_id),
        sop_session_id=sop_session_id,
    )


def _artifact_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="W+ SOP artifact not found")


def _verify_artifact_file(
    static_root: Path,
    artifact: Any,
    *,
    read_content: bool,
) -> tuple[Path, bytes | None]:
    """Resolve and verify one artifact; intended to run in a worker thread."""
    static_root = static_root.expanduser().resolve()
    try:
        local_file = (static_root / artifact.static_file_name).resolve()
        local_file.relative_to(static_root)
        file_size = local_file.stat().st_size
    except (OSError, ValueError) as exc:
        raise _artifact_not_found() from exc
    if read_content and file_size > MAX_ARTIFACT_PREVIEW_BYTES:
        raise HTTPException(
            status_code=413,
            detail=_ARTIFACT_PREVIEW_TOO_LARGE_DETAIL,
        )

    digest = hashlib.sha256()
    content = bytearray() if read_content else None
    try:
        with local_file.open("rb") as artifact_file:
            while chunk := artifact_file.read(_ARTIFACT_READ_CHUNK_BYTES):
                digest.update(chunk)
                if content is not None:
                    content.extend(chunk)
                    if len(content) > MAX_ARTIFACT_PREVIEW_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=_ARTIFACT_PREVIEW_TOO_LARGE_DETAIL,
                        )
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise _artifact_not_found() from exc
    if digest.hexdigest() != artifact.sha256:
        raise _artifact_not_found()
    return local_file, bytes(content) if content is not None else None


async def _artifact_response(
    service: WPlusSopService,
    artifact: Any,
    *,
    download: bool,
) -> Response:
    static_root = (
        Path(service.workspace.workspace_dir).expanduser().resolve() / "static"
    ).resolve()
    local_file, raw = await asyncio.to_thread(
        _verify_artifact_file,
        static_root,
        artifact,
        read_content=not download,
    )
    media_type = {
        ".json": "application/json",
        ".md": "text/markdown",
        ".html": "text/html",
    }.get(Path(artifact.name).suffix)
    if media_type is None:
        raise _artifact_not_found()
    headers = {
        "Cache-Control": "private, no-store",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "X-Content-Type-Options": "nosniff",
    }
    if download:
        filename = Path(str(artifact.name)).name
        headers["Content-Disposition"] = (
            "attachment; filename*=UTF-8''" + quote(filename, safe="")
        )
        return FileResponse(
            path=local_file,
            media_type=media_type,
            headers=headers,
        )
    if raw is None:
        raise _artifact_not_found()
    return Response(
        content=raw,
        media_type="text/plain",
        headers=headers,
    )


@router.get("/sessions/{sop_session_id}/artifacts/{artifact_id}")
async def download_wplus_sop_artifact(
    sop_session_id: str,
    artifact_id: str,
    request: Request,
    download: bool = False,
) -> Response:
    service = await _service_for_session(request, sop_session_id)
    result = service.get_session(sop_session_id).projection.final_result
    if result is None:
        raise HTTPException(status_code=404, detail="W+ SOP artifact not found")

    artifact = next(
        (
            candidate
            for candidate in result.artifacts
            if candidate.artifact_id == artifact_id
        ),
        None,
    )
    if artifact is None:
        raise _artifact_not_found()
    return await _artifact_response(service, artifact, download=download)


@router.get(
    "/sessions/{sop_session_id}/stage-report-artifacts/{artifact_id}",
)
async def download_wplus_sop_stage_report_artifact(
    sop_session_id: str,
    artifact_id: str,
    stage_id: str,
    revision: int,
    report_no: int,
    request: Request,
    download: bool = False,
) -> Response:
    service = await _service_for_session(request, sop_session_id)
    reports = service.get_session(sop_session_id).projection.stage_reports
    report = next(
        (
            candidate
            for candidate in reports
            if candidate.stage_id == stage_id
            and candidate.revision == revision
            and candidate.report_no == report_no
        ),
        None,
    )
    if report is None:
        raise _artifact_not_found()
    artifact = next(
        (
            candidate
            for candidate in report.artifacts
            if candidate.artifact_id == artifact_id
        ),
        None,
    )
    if artifact is None:
        raise _artifact_not_found()
    return await _artifact_response(service, artifact, download=download)


@router.get(
    "/sessions/{sop_session_id}/cumulative-artifacts/{artifact_id}",
)
async def download_wplus_sop_cumulative_artifact(
    sop_session_id: str,
    artifact_id: str,
    preview_version: int,
    request: Request,
    download: bool = False,
) -> Response:
    service = await _service_for_session(request, sop_session_id)
    preview = service.get_session(
        sop_session_id,
    ).projection.cumulative_preview
    if preview is None or preview.preview_version != preview_version:
        raise _artifact_not_found()
    artifact = next(
        (
            candidate
            for candidate in preview.artifacts
            if candidate.artifact_id == artifact_id
        ),
        None,
    )
    if artifact is None:
        raise _artifact_not_found()
    return await _artifact_response(service, artifact, download=download)


@router.get("/chats/{chat_id}/active-session")
async def get_active_wplus_sop_session(
    chat_id: str,
    request: Request,
) -> dict[str, Any] | None:
    service = await _service_for_chat(request, chat_id)
    record = service.get_active_session()
    if record is None:
        return None
    await service.flush_chat_projection_outbox()
    return await _session_snapshot(service, record)


@router.post("/entry-proposals/{proposal_id}/confirm")
async def confirm_wplus_sop_entry(
    proposal_id: str,
    body: EntryCommandRequest,
    request: Request,
) -> dict[str, Any]:
    service = await _service_for_proposal(request, proposal_id)
    try:
        mutation = await service.confirm_entry(
            proposal_id=proposal_id,
            command_request_id=body.command_request_id,
            skill_snapshot_id=_skill_snapshot_id(
                service.workspace.workspace_dir,
            ),
        )
    except Exception as exc:  # translated into the public error contract
        _raise_http(exc)
        raise
    await service.flush_chat_projection_outbox()
    return {
        "command_request_id": body.command_request_id,
        "accepted": True,
        "session": await _session_snapshot(service, mutation.record),
        "run_id": mutation.receipt.run_id if mutation.receipt else None,
        "attempt_id": (
            mutation.receipt.attempt_id if mutation.receipt else None
        ),
    }


@router.post("/entry-proposals/{proposal_id}/reject")
async def reject_wplus_sop_entry(
    proposal_id: str,
    body: EntryCommandRequest,
    request: Request,
) -> dict[str, Any]:
    service = await _service_for_proposal(request, proposal_id)
    try:
        proposal = service.reject_entry(
            proposal_id=proposal_id,
            command_request_id=body.command_request_id,
        )
    except Exception as exc:
        _raise_http(exc)
        raise
    await service.project_entry_proposal(proposal)
    await service.flush_chat_projection_outbox()
    return {
        "proposal_id": proposal.proposal_id,
        "status": proposal.status.value,
        "suppression_token": proposal.suppression_token,
        "original_request": proposal.original_request,
    }


@router.post("/sessions/{sop_session_id}/commands")
async def post_wplus_sop_command(
    sop_session_id: str,
    body: WPlusCommandRequest,
    request: Request,
) -> dict[str, Any]:
    service = await _service_for_session(request, sop_session_id)
    try:
        mutation = await service.execute_command(
            sop_session_id=sop_session_id,
            command=body.command,
            command_request_id=body.command_request_id,
            expected_state_version=body.expected_state_version,
            payload=body.payload,
        )
    except Exception as exc:
        _raise_http(exc)
        raise
    await service.flush_chat_projection_outbox()
    return {
        "command_request_id": body.command_request_id,
        "accepted": True,
        "session": await _session_snapshot(service, mutation.record),
        "run_id": mutation.receipt.run_id if mutation.receipt else None,
        "attempt_id": (
            mutation.receipt.attempt_id if mutation.receipt else None
        ),
    }


@router.get("/sessions/{sop_session_id}/events")
async def stream_wplus_sop_events(
    sop_session_id: str,
    request: Request,
    after_state_version: int = 0,
) -> StreamingResponse:
    service = await _service_for_session(request, sop_session_id)

    async def generate() -> AsyncGenerator[str, None]:
        cursor = after_state_version
        idle_ticks = 0
        last_safe_trace: tuple[str, int] | None = None
        last_runtime_status: dict[str, Any] | None = None
        while not await request.is_disconnected():
            await service.recover_orphaned_generation_run(sop_session_id)
            record = service.get_session(sop_session_id)
            emitted = False
            current_runtime_status: dict[str, Any] | None = None
            get_runtime_status = getattr(service, "get_runtime_status", None)
            if callable(get_runtime_status):
                runtime_status = await get_runtime_status(sop_session_id)
                current_runtime_status = runtime_status
                if runtime_status != last_runtime_status:
                    data = {
                        "event_id": f"runtime:{sop_session_id}:{uuid4().hex}",
                        "session_id": sop_session_id,
                        "state_version": record.projection.state_version,
                        "kind": "runtime_status",
                        "run_id": record.projection.current_run_id,
                        "runtime_status": runtime_status,
                    }
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                    last_runtime_status = runtime_status
                    emitted = True
            for event in record.events:
                if event.state_version <= cursor:
                    continue
                snapshot = await _session_snapshot(
                    service,
                    record,
                    sop_session_id=sop_session_id,
                    runtime_status=current_runtime_status,
                )
                data = {
                    "event_id": event.event_id,
                    "session_id": sop_session_id,
                    "state_version": event.state_version,
                    "kind": event.kind.value,
                    "run_id": record.projection.current_run_id,
                    "snapshot": snapshot,
                }
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                cursor = event.state_version
                emitted = True
            current_run_id = record.projection.current_run_id
            if current_run_id:
                safe_trace_snapshot = get_wplus_safe_stream_trace_registry(
                    service.workspace,
                ).snapshot(sop_session_id, current_run_id)
                if (
                    safe_trace_snapshot is not None
                    and safe_trace_snapshot.sequence > 0
                    and last_safe_trace
                    != (current_run_id, safe_trace_snapshot.sequence)
                ):
                    data = {
                        "event_id": (
                            "trace:"
                            f"{sop_session_id}:{current_run_id}:"
                            f"{safe_trace_snapshot.sequence}"
                        ),
                        "session_id": sop_session_id,
                        "state_version": record.projection.state_version,
                        "kind": "safe_stream_trace",
                        "run_id": current_run_id,
                        "safe_stream_trace": {
                            "sequence": safe_trace_snapshot.sequence,
                            "summary_text": safe_trace_snapshot.summary_text,
                            "truncated": safe_trace_snapshot.truncated,
                            "entries": [
                                entry.to_dict()
                                for entry in safe_trace_snapshot.entries
                            ],
                        },
                    }
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                    last_safe_trace = (
                        current_run_id,
                        safe_trace_snapshot.sequence,
                    )
                    emitted = True
            if record.projection.is_terminal:
                break
            if emitted:
                idle_ticks = 0
            else:
                idle_ticks += 1
                if idle_ticks >= 15:
                    yield ": keep-alive\n\n"
                    idle_ticks = 0
            await asyncio.sleep(1)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

# -*- coding: utf-8 -*-
"""Authorized run-list query and tamper-evident pagination cursors."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from ..config.context import is_valid_identity_value
from .config import EmitterConfig, capture_enabled
from .emitter import default_emitter
from .identity import service_unit_id
from .store import ObservabilityStore


def _request_tenant_id(request: Request) -> str:
    """读取中间件解析出的有效租户；缺失时拒绝访问，避免跨租户读取。"""
    state = getattr(request, "state", None)
    value = getattr(state, "effective_tenant_id", None) or getattr(
        state,
        "tenant_id",
        None,
    )
    if not value:
        raise HTTPException(
            status_code=403,
            detail="tenant identity required",
        )
    return str(value)


router = APIRouter(prefix="/agent-observability", tags=["agent-observability"])
_MAX_PAGE_SIZE = 200
_MAX_EVENT_PAGE_SIZE = 500
_MAX_EVENT_RESPONSE_BYTES = 1_000_000
_MAX_RUN_CANDIDATES = 50_000
_MAX_RUN_RESPONSE_BYTES = 1_000_000
_MAX_SPAN_SEARCH_CANDIDATES = 50_000
_MAX_SPAN_SEARCH_RESPONSE_BYTES = 1_000_000
_MAX_EVIDENCE_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_WINDOW = timedelta(days=31)
_RUN_TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "timed_out", "rejected"},
)


class RunListResponse(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None
    generated_at: datetime
    window_start: datetime
    window_end: datetime


class CapabilitiesResponse(BaseModel):
    capture_enabled: bool
    storage_configured: bool
    storage_ready: bool
    projector_running: bool
    queue_events: int
    queue_bytes: int
    max_event_bytes: int


class HealthResponse(BaseModel):
    capture_enabled: bool
    storage_ready: bool
    projector_running: bool
    dropped_events: int | None
    generated_at: datetime


class RunDetailResponse(BaseModel):
    run: dict[str, Any]
    attempts: list[dict[str, Any]]
    spans: list[dict[str, Any]]
    generated_at: datetime
    spans_has_more: bool
    spans_next_cursor: str | None = None


class SpanSearchResponse(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None
    generated_at: datetime


class EventListResponse(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None
    generated_at: datetime


class EvidenceResponse(BaseModel):
    items: list[dict[str, Any]]
    generated_at: datetime


class AssetResponse(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None
    quality: str
    generated_at: datetime


class SpanAncestorResponse(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None
    quality: str
    generated_at: datetime


class PerformanceResponse(BaseModel):
    population_source: str
    window_start: datetime
    window_end: datetime
    generated_at: datetime
    sample_count: int
    sample_start_at: datetime | None
    sample_end_at: datetime | None
    sample_quality: dict[str, Any]
    run_count: int
    run_elapsed_us: dict[str, Any]
    active_duration_us: dict[str, Any]
    agent_loops: dict[str, Any]
    duration_us: dict[str, Any]
    tool_failures: dict[str, Any]
    run_status_filter: str | None = None
    llm: dict[str, Any]
    by_span_type: list[dict[str, Any]]


class BusinessPopulationResponse(BaseModel):
    population_source: dict[str, str]
    window_start: datetime
    window_end: datetime
    generated_at: datetime
    business_as_of: datetime
    observability_as_of: datetime | None
    subagents_as_of: datetime | None
    business: dict[str, Any]
    runs: dict[str, Any]
    subagents: dict[str, Any]
    items: list[dict[str, Any]]
    items_has_more: bool
    unresolved_runs: list[dict[str, Any]]
    unresolved_runs_has_more: bool


def _require_observability_reader(request: Request) -> str:
    role = request.headers.get("X-User-Role", "").strip().lower()
    if role not in {"manager", "admin"}:
        raise HTTPException(status_code=403, detail="Manager role required")
    if role == "manager" and not getattr(request.state, "source_id", None):
        raise HTTPException(status_code=403, detail="Source scope required")
    return role


async def _storage_ready(request: Request) -> bool:
    store = getattr(request.app.state, "agent_observability_store", None)
    if not isinstance(store, ObservabilityStore):
        return False
    try:
        readiness = await store.readiness()
    except Exception:
        return False
    return bool(readiness.get("ready"))


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def capabilities(request: Request) -> CapabilitiesResponse:
    _require_observability_reader(request)
    config = EmitterConfig.from_environment()
    return CapabilitiesResponse(
        capture_enabled=capture_enabled(),
        storage_configured=getattr(
            request.app.state,
            "agent_observability_connection",
            None,
        )
        is not None,
        storage_ready=await _storage_ready(request),
        projector_running=bool(
            getattr(
                getattr(
                    request.app.state,
                    "agent_observability_projector",
                    None,
                ),
                "running",
                False,
            ),
        ),
        queue_events=config.max_queue_events,
        queue_bytes=config.max_queue_bytes,
        max_event_bytes=config.max_event_bytes,
    )


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    if _require_observability_reader(request) != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    dropped_events = None
    if capture_enabled():
        try:
            dropped_events = default_emitter().dropped_count
        except Exception:
            dropped_events = None
    return HealthResponse(
        capture_enabled=capture_enabled(),
        storage_ready=await _storage_ready(request),
        projector_running=bool(
            getattr(
                getattr(
                    request.app.state,
                    "agent_observability_projector",
                    None,
                ),
                "running",
                False,
            ),
        ),
        dropped_events=dropped_events,
        generated_at=datetime.now(timezone.utc),
    )


def _authorized_sources(
    request: Request,
    requested: list[str] | None,
) -> list[str]:
    role = request.headers.get("X-User-Role", "").strip().lower()
    current = getattr(request.state, "source_id", None)
    if role not in {"manager", "admin"}:
        raise HTTPException(status_code=403, detail="Manager role required")
    if not isinstance(current, str) or not current:
        raise HTTPException(status_code=403, detail="Source scope required")
    if role == "manager":
        if requested and any(source != current for source in requested):
            raise HTTPException(status_code=403, detail="Source scope denied")
        return [current]
    sources = list(dict.fromkeys(requested or [current]))
    if not sources or any(
        not is_valid_identity_value(source) or len(source.encode()) > 256
        for source in sources
    ):
        raise HTTPException(status_code=400, detail="Invalid Source filter")
    if len(sources) > 32:
        raise HTTPException(status_code=400, detail="Too many Source filters")
    return sources


def _population_identity(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


def _population_run_key(row: dict[str, Any]) -> tuple[str, str, str] | None:
    chat_id = _population_identity(row.get("chat_key"))
    session_id = _population_identity(row.get("session_key"))
    question_id = _population_identity(row.get("root_question_key"))
    if not chat_id or not session_id or not question_id:
        return None
    return chat_id, session_id, question_id


def _population_turn_key(row: dict[str, Any]) -> tuple[str, str, str] | None:
    chat_id = _population_identity(row.get("chat_id"))
    session_id = _population_identity(row.get("session_id"))
    question_id = _population_identity(row.get("question_id"))
    if not chat_id or not session_id or not question_id:
        return None
    return chat_id, session_id, question_id


def _population_time(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return (
        value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    )


def _population_run_index(runs: list[dict[str, Any]]) -> dict:
    indexed: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for run in runs:
        key = _population_run_key(run)
        if key is not None:
            indexed.setdefault(key, []).append(run)
    return indexed


def _classify_business_turn(
    turn: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    observation_available: bool,
) -> tuple[dict[str, Any], str]:
    turn_status = (
        _population_identity(turn.get("history_status"))
        or _population_identity(turn.get("state"))
        or "unknown"
    )
    item: dict[str, Any] = {
        "chat_id": _population_identity(turn.get("chat_id")),
        "session_id": _population_identity(turn.get("session_id")),
        "question_id": _population_identity(turn.get("question_id")),
        "turn_status": turn_status,
        "observation_status": "unknown",
    }
    key = _population_turn_key(turn)
    if not observation_available:
        return item, "unknown"
    if key is None or len(candidates) > 1:
        item["observation_status"] = "unresolved"
        return item, "unresolved"
    if not candidates:
        item["observation_status"] = "not_collected"
        return item, "not_collected"

    run = candidates[0]
    if isinstance(run.get("run_id"), str):
        item["obs_run_id"] = run["run_id"]
    status = run.get("status")
    item["run_status"] = status
    capture_enabled = run.get("capture_enabled")
    if capture_enabled not in (True, 1):
        if capture_enabled in (False, 0):
            item["observation_status"] = "capture_disabled"
            return item, "capture_disabled"
        return item, "unknown"
    if run.get("ended_at") is None:
        item["observation_status"] = "pending"
        return item, "pending"
    if (
        run.get("terminal_fact_source") != "business_settlement"
        or status not in _RUN_TERMINAL_STATUSES
    ):
        item["observation_status"] = "unresolved"
        return item, "unresolved"
    item["observation_status"] = "settled"
    return item, f"settled:{status}"


def _project_business_turns(turns, runs_by_key, *, observation_available):
    items = []
    statuses: dict[str, int] = {}
    counts = {
        key: 0
        for key in (
            "linked",
            "not_collected",
            "pending",
            "unresolved",
            "unknown",
            "capture_disabled",
            "observed",
            "settled",
            "success",
        )
    }
    terminal_statuses: dict[str, int] = {}
    for turn in turns:
        key = _population_turn_key(turn)
        candidates = runs_by_key.get(key, []) if key else []
        item, classification = _classify_business_turn(
            turn,
            candidates,
            observation_available=observation_available,
        )
        items.append(item)
        turn_status = item["turn_status"]
        statuses[turn_status] = statuses.get(turn_status, 0) + 1
        if classification in {
            "pending",
            "capture_disabled",
        } or classification.startswith("settled:"):
            counts["linked"] += 1
        if classification == "not_collected":
            counts["not_collected"] += 1
        elif classification in {
            "pending",
            "unresolved",
            "unknown",
            "capture_disabled",
        }:
            counts[classification] += 1
        if classification == "pending" or classification.startswith(
            "settled:",
        ):
            counts["observed"] += 1
        if classification.startswith("settled:"):
            status = classification.split(":", 1)[1]
            counts["settled"] += 1
            counts["success"] += status == "succeeded"
            terminal_statuses[status] = terminal_statuses.get(status, 0) + 1
    return items, statuses, terminal_statuses, counts


def _unresolved_business_runs(turns, runs, runs_by_key):
    business_keys = {
        key
        for turn in turns
        if (key := _population_turn_key(turn)) is not None
    }
    matched = {
        run.get("run_id")
        for key, candidates in runs_by_key.items()
        if key in business_keys and len(candidates) == 1
        for run in candidates
    }
    return [run for run in runs if run.get("run_id") not in matched]


def _business_population_projection(
    turns,
    runs,
    *,
    observation_available=True,
    item_limit=200,
):
    runs_by_key = _population_run_index(runs)
    items, statuses, terminal_statuses, counts = _project_business_turns(
        turns,
        runs_by_key,
        observation_available=observation_available,
    )
    unresolved_runs = _unresolved_business_runs(turns, runs, runs_by_key)
    return {
        "business": {
            "turn_count": len(turns),
            "by_turn_status": statuses,
            "linked_turn_count": counts["linked"],
            "not_collected_count": counts["not_collected"],
            "pending_count": counts["pending"],
            "unresolved_count": counts["unresolved"],
            "unknown_count": counts["unknown"] + counts["capture_disabled"],
            "capture_disabled_count": counts["capture_disabled"],
            "run_identity_coverage": (
                counts["linked"] / len(turns) if turns else None
            ),
            "observation_coverage": (
                counts["observed"] / len(turns) if turns else None
            ),
        },
        "runs": _population_run_summary(
            runs,
            terminal_statuses,
            counts,
            unresolved_runs,
            observation_available,
        ),
        "items": items[:item_limit],
        "items_has_more": len(items) > item_limit,
        "unresolved_runs": [
            {
                "obs_run_id": run.get("run_id"),
                "run_status": run.get("status"),
                "chat_id": _population_identity(run.get("chat_key")) or None,
                "session_id": _population_identity(run.get("session_key"))
                or None,
                "question_id": _population_identity(
                    run.get("root_question_key"),
                )
                or None,
                "reason": "missing_or_unmatched_business_identity",
            }
            for run in unresolved_runs[:item_limit]
        ],
        "unresolved_runs_has_more": len(unresolved_runs) > item_limit,
    }


def _population_run_summary(
    runs,
    terminal_statuses,
    counts,
    unresolved_runs,
    observation_available,
):
    if not observation_available:
        return {
            "population_source": "business_settled_runs_linked_by_exact_identity",
            "status": "unavailable",
            "settled_count": None,
            "success_count": None,
            "non_success_count": None,
            "by_terminal_status": None,
            "capture_enabled_count": None,
            "capture_disabled_count": None,
            "success_rate": None,
            "unresolved_count": None,
        }
    settled = counts["settled"]
    success = counts["success"]
    return {
        "population_source": "business_settled_runs_linked_by_exact_identity",
        "status": "available",
        "settled_count": settled,
        "success_count": success,
        "non_success_count": settled - success,
        "by_terminal_status": terminal_statuses,
        "capture_enabled_count": sum(
            run.get("capture_enabled") in (True, 1) for run in runs
        ),
        "capture_disabled_count": sum(
            run.get("capture_enabled") in (False, 0) for run in runs
        ),
        "success_rate": success / settled if settled else None,
        "unresolved_count": len(unresolved_runs),
    }


def _population_window(
    start: datetime | None,
    end: datetime | None,
) -> tuple[datetime, datetime]:
    window_end = end or datetime.now(timezone.utc)
    window_start = start or window_end - timedelta(hours=24)
    if (
        window_start.tzinfo is None
        or window_end.tzinfo is None
        or window_end <= window_start
        or window_end - window_start > _MAX_WINDOW
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid population window",
        )
    return window_start, window_end


async def _load_business_population(
    request: Request,
    sources: list[str],
    window_start: datetime,
    window_end: datetime,
):
    try:
        from ..app.runner.api import get_workspace

        workspace = await get_workspace(request)
        storage = getattr(workspace, "conversation_storage", None)
        history = getattr(storage, "history", None)
        scope = getattr(history, "scope", None)
        if history is None or scope is None:
            raise HTTPException(
                status_code=503,
                detail="Business population storage unavailable",
            )
        if scope.source_id != sources[0]:
            raise HTTPException(
                status_code=403,
                detail="Workspace Source denied",
            )
        turns = await history.business_turn_population(
            start_at=window_start,
            end_at=window_end,
            limit=_MAX_RUN_CANDIDATES,
        )
        visible_chat_sessions = await history.business_visible_chat_sessions(
            limit=_MAX_RUN_CANDIDATES,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Business population query unavailable",
        ) from exc
    if len(turns) > _MAX_RUN_CANDIDATES:
        raise HTTPException(status_code=422, detail="business_range_too_large")
    if len(visible_chat_sessions) > _MAX_RUN_CANDIDATES:
        raise HTTPException(
            status_code=422,
            detail="workspace_range_too_large",
        )
    return workspace, scope, turns, visible_chat_sessions


async def _load_observed_population(
    store: Any,
    scope: Any,
    window_start: datetime,
    window_end: datetime,
) -> tuple[list[dict[str, Any]], bool, datetime | None]:
    if not isinstance(store, ObservabilityStore):
        return [], False, None
    try:
        run_rows = await store.business_run_population(
            service_unit_id=scope.service_unit_id,
            tenant_id=scope.tenant_id,
            source_id=scope.source_id,
            agent_id=scope.agent_id,
            start_at=window_start,
            end_at=window_end,
            limit=_MAX_RUN_CANDIDATES,
        )
        if len(run_rows) > _MAX_RUN_CANDIDATES:
            raise HTTPException(
                status_code=422,
                detail="observed_run_range_too_large",
            )
    except HTTPException:
        raise
    except Exception:
        return [], False, None
    return run_rows, True, datetime.now(timezone.utc)


def _chats_by_session(rows: list[dict[str, Any]]) -> dict[str, set[str]]:
    chats: dict[str, set[str]] = {}
    for row in rows:
        session_id = _population_identity(row.get("session_id"))
        chat_id = _population_identity(row.get("chat_id"))
        if session_id and chat_id:
            chats.setdefault(session_id, set()).add(chat_id)
    return chats


async def _load_subagent_population(
    workspace: Any,
    visible_chat_sessions: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
) -> tuple[dict[str, Any], datetime | None]:
    try:
        from ..app.subagents.monitor import create_monitor_service

        subagents = await create_monitor_service(workspace).population(
            chats_by_session=_chats_by_session(visible_chat_sessions),
            start_at=window_start,
            end_at=window_end,
            limit=_MAX_RUN_CANDIDATES,
        )
    except Exception:
        return {
            "count": None,
            "by_status": {},
            "unresolved_count": None,
            "has_more": None,
            "status": "unavailable",
        }, None
    subagents["status"] = "partial" if subagents["has_more"] else "available"
    return subagents, datetime.now(timezone.utc)


@router.get("/business-population", response_model=BusinessPopulationResponse)
async def business_population(
    request: Request,
    start: datetime | None = Query(None),
    end: datetime | None = Query(None),
) -> BusinessPopulationResponse:
    """Compare current business Turn, SubAgent, and linked Run populations."""
    _require_observability_reader(request)
    sources = _authorized_sources(request, None)
    window_start, window_end = _population_window(start, end)
    workspace, scope, turns, visible_chat_sessions = (
        await _load_business_population(
            request,
            sources,
            window_start,
            window_end,
        )
    )
    business_as_of = datetime.now(timezone.utc)
    store = getattr(request.app.state, "agent_observability_store", None)
    run_rows, observation_available, observability_as_of = (
        await _load_observed_population(store, scope, window_start, window_end)
    )
    projection = _business_population_projection(
        turns,
        run_rows,
        observation_available=observation_available,
    )
    subagents, subagents_as_of = await _load_subagent_population(
        workspace,
        visible_chat_sessions,
        window_start,
        window_end,
    )
    return BusinessPopulationResponse(
        population_source={
            "business": "current_workspace_admitted_turns",
            "subagents": "current_workspace_retained_background_subagent_records",
            "runs": "independent_observability_runs_exact_business_identity",
        },
        window_start=window_start,
        window_end=window_end,
        generated_at=datetime.now(timezone.utc),
        business_as_of=business_as_of,
        observability_as_of=observability_as_of,
        subagents_as_of=subagents_as_of,
        business=projection["business"],
        runs=projection["runs"],
        subagents=subagents,
        items=projection["items"],
        items_has_more=projection["items_has_more"],
        unresolved_runs=projection["unresolved_runs"],
        unresolved_runs_has_more=projection["unresolved_runs_has_more"],
    )


def _cursor_secret() -> bytes:
    secret = os.environ.get("SWE_AGENTOBS_CURSOR_SECRET", "")
    if len(secret.encode()) < 32:
        raise HTTPException(
            status_code=503,
            detail="Observability cursor key unavailable",
        )
    return secret.encode()


def _encode_evidence_cursor(
    *,
    evidence_ref: str,
    offset: int,
    content_digest: str,
    authority: dict[str, Any],
) -> str:
    body = json.dumps(
        {
            "v": 1,
            "evidence_ref": evidence_ref,
            "offset": offset,
            "content_digest": content_digest,
            "authority": authority,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(_cursor_secret(), body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(body + signature).decode().rstrip("=")


def _decode_evidence_cursor(
    cursor: str,
    authority: dict[str, Any],
) -> tuple[str, int, str]:
    try:
        packed = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        body, signature = packed[:-32], packed[-32:]
        expected = hmac.new(_cursor_secret(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("bad signature")
        payload = json.loads(body)
        if payload.get("v") != 1 or payload.get("authority") != authority:
            raise ValueError("authority changed")
        evidence_ref = payload.get("evidence_ref")
        offset = payload.get("offset")
        digest = payload.get("content_digest")
        if not isinstance(evidence_ref, str) or not evidence_ref.startswith(
            "event:",
        ):
            raise ValueError("invalid evidence reference")
        if (
            type(offset) is not int
            or not 0 < offset < _MAX_EVIDENCE_SOURCE_BYTES
        ):
            raise ValueError("invalid offset")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("invalid digest")
        if any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("invalid cursor fields")
        return evidence_ref, offset, digest
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid evidence cursor",
        ) from exc


def _is_mysql_query_timeout(error: Exception) -> bool:
    current: BaseException | None = error
    while current is not None:
        if getattr(current, "errno", None) == 3024:
            return True
        current = current.__cause__ or current.__context__
    return False


def _encode_cursor(
    last_value: datetime | int,
    run_id: str,
    scope: dict[str, Any],
) -> str:
    cursor_value: dict[str, Any] = (
        {"duration_us": last_value}
        if isinstance(last_value, int)
        else {"started_at": last_value.isoformat()}
    )
    body = json.dumps(
        {
            "v": 1,
            **cursor_value,
            "run_id": run_id,
            "scope": scope,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(_cursor_secret(), body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(body + signature).decode().rstrip("=")


def _decode_cursor(
    cursor: str,
    expected_authority: dict[str, Any],
) -> tuple[datetime | int, str, dict[str, Any]]:
    try:
        packed = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        body, signature = packed[:-32], packed[-32:]
        if not hmac.compare_digest(
            signature,
            hmac.new(_cursor_secret(), body, hashlib.sha256).digest(),
        ):
            raise ValueError("bad signature")
        payload = json.loads(body)
        scope = payload.get("scope")
        if payload.get("v") != 1 or not isinstance(scope, dict):
            raise ValueError("scope changed")
        if any(
            scope.get(key) != value
            for key, value in expected_authority.items()
        ):
            raise ValueError("authorization changed")
        if "duration_us" in payload:
            sort_value: datetime | int = int(payload["duration_us"])
        else:
            sort_value = datetime.fromisoformat(payload["started_at"])
            if sort_value.tzinfo is not None:
                sort_value = sort_value.astimezone(timezone.utc).replace(
                    tzinfo=None,
                )
        run_id = str(payload["run_id"])
        if len(run_id) != 36:
            raise ValueError("invalid run ID")
        return sort_value, run_id, scope
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid run cursor",
        ) from exc


def _run_filters(
    *,
    run_id: str | None,
    attempt_id: str | None,
    session_id: str | None,
    agent_id: str | None,
    status: str | None,
    min_duration_ms: int | None,
    sort_by: str,
    cursor_scope: dict[str, Any],
    has_cursor: bool,
) -> dict[str, Any]:
    if (
        sum(value is not None for value in (run_id, attempt_id, session_id))
        > 1
    ):
        raise HTTPException(
            status_code=400,
            detail="Choose one exact Run, Attempt, or Session identity",
        )
    for identity in (run_id, attempt_id):
        if identity is not None:
            try:
                UUID(identity)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid execution identity",
                ) from exc
    if session_id is not None and not session_id:
        raise HTTPException(status_code=400, detail="Invalid session identity")
    filters: dict[str, Any] = {
        "run_id": run_id,
        "attempt_id": attempt_id,
        "session_id": session_id,
        "agent_id": agent_id,
        "status": status,
        "min_duration_ms": min_duration_ms,
        "sort_by": sort_by,
    }
    if has_cursor:
        for key, value in filters.items():
            if value is not None and value != cursor_scope.get(key):
                raise HTTPException(
                    status_code=400,
                    detail="Run cursor filter changed",
                )
            if value is None:
                filters[key] = cursor_scope.get(key)
    return filters


def _check_cursor_window(
    start: datetime | None,
    end: datetime | None,
    cursor_scope: dict[str, Any],
) -> None:
    if start and start.isoformat() != cursor_scope.get("start"):
        raise HTTPException(
            status_code=400,
            detail="Run cursor filter changed",
        )
    if end and end.isoformat() != cursor_scope.get("end"):
        raise HTTPException(
            status_code=400,
            detail="Run cursor filter changed",
        )


@router.get("/runs", response_model=RunListResponse)
async def list_runs(
    request: Request,
    source_id: list[str] | None = Query(None),
    limit: int = Query(50, ge=1, le=_MAX_PAGE_SIZE),
    cursor: str | None = Query(None, max_length=2048),
    start: datetime | None = Query(None),
    end: datetime | None = Query(None),
    run_id: str | None = Query(None, max_length=36),
    attempt_id: str | None = Query(None, max_length=36),
    session_id: str | None = Query(None, max_length=1024),
    agent_id: str | None = Query(None, max_length=256),
    status: str | None = Query(
        None,
        pattern="^(accepted|running|waiting_approval|succeeded|failed|cancelled|timed_out|rejected)$",
    ),
    min_duration_ms: int | None = Query(None, ge=0, le=31_536_000_000),
    sort_by: str = Query("started_at", pattern="^(started_at|duration_us)$"),
) -> RunListResponse:
    sources = _authorized_sources(request, source_id)
    _cursor_secret()
    authority = {
        "role": request.headers.get("X-User-Role", "").strip().lower(),
        "sources": sources,
    }
    decoded = _decode_cursor(cursor, authority) if cursor else None
    cursor_scope = decoded[2] if decoded else {}
    filters = _run_filters(
        run_id=run_id,
        attempt_id=attempt_id,
        session_id=session_id,
        agent_id=agent_id,
        status=status,
        min_duration_ms=min_duration_ms,
        sort_by=sort_by,
        cursor_scope=cursor_scope,
        has_cursor=decoded is not None,
    )
    start_at, end_at = _run_list_window(
        start,
        end,
        cursor_scope,
        has_cursor=decoded is not None,
    )
    scope = {
        **authority,
        "start": start_at.isoformat(),
        "end": end_at.isoformat(),
        **filters,
    }
    before = decoded[:2] if decoded else None
    store = _request_observability_store(request)
    query_filters = _run_list_store_filters(
        filters,
        start,
        end,
        start_at,
        end_at,
    )
    rows = await _load_run_list_page(
        store,
        request,
        sources,
        query_filters,
        limit,
        before,
    )
    return _run_list_response(rows, limit, sort_by, scope, start_at, end_at)


def _run_list_response(
    rows,
    limit,
    sort_by,
    scope,
    start_at,
    end_at,
) -> RunListResponse:
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = _encode_cursor(
            (
                last["duration_us"]
                if sort_by == "duration_us"
                else last["started_at"]
            ),
            str(last["run_id"]),
            scope,
        )
    response = RunListResponse(
        items=_serialize_rows(rows),
        next_cursor=next_cursor,
        generated_at=datetime.now(timezone.utc),
        window_start=start_at,
        window_end=end_at,
    )
    response_size = len(
        json.dumps(
            jsonable_encoder(response),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    if response_size > _MAX_RUN_RESPONSE_BYTES:
        raise HTTPException(status_code=413, detail="run_response_too_large")
    return response


def _run_list_window(start, end, cursor_scope, *, has_cursor):
    start_at = start or (
        datetime.fromisoformat(cursor_scope["start"])
        if cursor_scope
        else datetime.now(timezone.utc) - timedelta(hours=24)
    )
    end_at = end or (
        datetime.fromisoformat(cursor_scope["end"])
        if cursor_scope
        else datetime.now(timezone.utc)
    )
    if has_cursor:
        _check_cursor_window(start, end, cursor_scope)
    if start_at.tzinfo is None or end_at.tzinfo is None:
        raise HTTPException(
            status_code=400,
            detail="Time filters must include a timezone",
        )
    if end_at <= start_at or end_at - start_at > _MAX_WINDOW:
        raise HTTPException(
            status_code=400,
            detail="Invalid run-list time window",
        )
    return start_at, end_at


def _request_observability_store(request: Request) -> ObservabilityStore:
    store = getattr(request.app.state, "agent_observability_store", None)
    if not isinstance(store, ObservabilityStore):
        raise HTTPException(
            status_code=503,
            detail="Agent observability storage unavailable",
        )
    return store


def _run_list_store_filters(filters, start, end, start_at, end_at):
    exact_identity = any(
        filters[key] is not None
        for key in ("run_id", "attempt_id", "session_id")
    )
    min_duration_ms = filters["min_duration_ms"]
    return {
        "start_at": start if exact_identity else start_at,
        "end_at": end if exact_identity else end_at,
        "run_id": filters["run_id"],
        "attempt_id": filters["attempt_id"],
        "session_id": filters["session_id"],
        "agent_id": filters["agent_id"],
        "status": filters["status"],
        "min_duration_us": (
            min_duration_ms * 1000 if min_duration_ms is not None else None
        ),
        "sort_by": filters["sort_by"],
    }


async def _load_run_list_page(
    store,
    request,
    sources,
    query_filters,
    limit,
    before,
):
    try:
        candidate_count = await store.count_run_candidates(
            service_unit_id=service_unit_id(),
            source_ids=sources,
            tenant_id=_request_tenant_id(request),
            **query_filters,
            max_candidates=_MAX_RUN_CANDIDATES,
        )
        if candidate_count > _MAX_RUN_CANDIDATES:
            raise HTTPException(
                status_code=422,
                detail="range_too_large",
            )
        return await store.list_runs(
            service_unit_id=service_unit_id(),
            source_ids=sources,
            tenant_id=_request_tenant_id(request),
            limit=limit + 1,
            before=before,
            **query_filters,
        )
    except HTTPException:
        raise
    except Exception as exc:
        if _is_mysql_query_timeout(exc):
            raise HTTPException(
                status_code=503,
                detail="Agent observability query exceeded its 5-second budget",
            ) from exc
        raise HTTPException(
            status_code=503,
            detail="Agent observability query unavailable",
        ) from exc


def _serialize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        item = {}
        for key, value in row.items():
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            elif isinstance(value, datetime) and value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            item[key] = value
        result.append(item)
    return result


def _parse_event_rows(
    rows: list[dict[str, Any]],
    limit: int,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    parsed_rows = []
    for row in rows[:limit]:
        try:
            metadata = json.loads(row.pop("metadata_text"))
        except (json.JSONDecodeError, TypeError) as exc:
            raise HTTPException(
                status_code=503,
                detail="Agent observability event unavailable",
            ) from exc
        if not isinstance(metadata, dict):
            raise HTTPException(
                status_code=503,
                detail="Agent observability event unavailable",
            )
        parsed_rows.append((row, metadata))
    return parsed_rows


async def _available_link_targets(
    store: ObservabilityStore,
    run_id: str,
    parsed_rows: list[tuple[dict[str, Any], dict[str, Any]]],
) -> tuple[set[str], set[str]]:
    span_ids = []
    attempt_ids = []
    for _row, metadata in parsed_rows:
        links = metadata.get("links")
        if not isinstance(links, list):
            continue
        for link in links:
            if not isinstance(link, dict):
                continue
            if isinstance(link.get("obs_span_id"), str):
                span_ids.append(link["obs_span_id"])
            if isinstance(link.get("obs_attempt_id"), str):
                attempt_ids.append(link["obs_attempt_id"])
    try:
        return await store.available_link_targets(
            run_id=run_id,
            span_ids=span_ids,
            attempt_ids=attempt_ids,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Agent observability query unavailable",
        ) from exc


def _redact_unavailable_links(
    parsed_rows: list[tuple[dict[str, Any], dict[str, Any]]],
    available_spans: set[str],
    available_attempts: set[str],
) -> None:
    for _row, metadata in parsed_rows:
        links = metadata.get("links")
        if not isinstance(links, list):
            continue
        safe_links = []
        for link in links:
            if not isinstance(link, dict):
                continue
            span_id = link.get("obs_span_id")
            attempt_id = link.get("obs_attempt_id")
            available = (
                span_id in available_spans
                if isinstance(span_id, str)
                else attempt_id in available_attempts
            )
            safe_links.append(
                (
                    link
                    if available
                    else {
                        "relation_type": link["relation_type"],
                        "target_unavailable": True,
                    }
                ),
            )
        metadata["links"] = safe_links


@router.get("/runs/{run_id}", response_model=RunDetailResponse)
async def run_detail(
    run_id: str,
    request: Request,
    attempt_id: str | None = Query(None),
    span_limit: int = Query(200, ge=1, le=500),
    span_cursor: str | None = Query(None, max_length=2048),
) -> RunDetailResponse:
    role = _require_observability_reader(request)
    try:
        UUID(run_id)
        if attempt_id is not None:
            UUID(attempt_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid run identity",
        ) from exc
    sources = _authorized_sources(request, None)
    authority = {
        "role": role,
        "sources": sources,
        "run_id": run_id,
        "attempt_id": attempt_id,
    }
    decoded = _decode_cursor(span_cursor, authority) if span_cursor else None
    store = getattr(request.app.state, "agent_observability_store", None)
    if not isinstance(store, ObservabilityStore):
        raise HTTPException(
            status_code=503,
            detail="Agent observability storage unavailable",
        )
    try:
        row = await store.get_run(
            service_unit_id=service_unit_id(),
            run_id=run_id,
            source_ids=sources,
            tenant_id=_request_tenant_id(request),
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Run not found")
        attempts = await store.list_attempts(run_id=run_id)
        spans = await store.list_spans(
            run_id=run_id,
            attempt_id=attempt_id,
            limit=span_limit + 1,
            after=decoded[:2] if decoded else None,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Agent observability query unavailable",
        ) from exc
    span_rows = _serialize_rows(spans[:span_limit])
    has_more = len(spans) > span_limit
    next_cursor = None
    if has_more and span_rows:
        last = span_rows[-1]
        next_cursor = _encode_cursor(
            last["started_at"],
            str(last["span_id"]),
            authority,
        )
    return RunDetailResponse(
        run=_serialize_rows([row])[0],
        attempts=_serialize_rows(attempts),
        spans=span_rows,
        generated_at=datetime.now(timezone.utc),
        spans_has_more=has_more,
        spans_next_cursor=next_cursor,
    )


@router.get(
    "/runs/{run_id}/spans/search",
    response_model=SpanSearchResponse,
)
async def search_run_spans(
    run_id: str,
    request: Request,
    attempt_id: str | None = Query(None),
    query_text: str | None = Query(None, alias="q", max_length=128),
    errors_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=200),
    cursor: str | None = Query(None, max_length=2048),
) -> SpanSearchResponse:
    role = _require_observability_reader(request)
    try:
        UUID(run_id)
        if attempt_id is not None:
            UUID(attempt_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid run identity",
        ) from exc
    normalized_query = (query_text or "").strip()
    if not normalized_query and not errors_only:
        raise HTTPException(
            status_code=400,
            detail="A Span query or errors_only filter is required",
        )
    sources = _authorized_sources(request, None)
    authority = {
        "role": role,
        "sources": sources,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "q": normalized_query,
        "errors_only": errors_only,
    }
    decoded = _decode_cursor(cursor, authority) if cursor else None
    store = getattr(request.app.state, "agent_observability_store", None)
    if not isinstance(store, ObservabilityStore):
        raise HTTPException(
            status_code=503,
            detail="Agent observability storage unavailable",
        )
    try:
        row = await store.get_run(
            service_unit_id=service_unit_id(),
            run_id=run_id,
            source_ids=sources,
            tenant_id=_request_tenant_id(request),
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Run not found")
        candidate_count = await store.count_span_search_candidates(
            run_id=run_id,
            attempt_id=attempt_id,
            query_text=normalized_query,
            errors_only=errors_only,
            max_candidates=_MAX_SPAN_SEARCH_CANDIDATES,
        )
        if candidate_count > _MAX_SPAN_SEARCH_CANDIDATES:
            raise HTTPException(status_code=422, detail="range_too_large")
        spans = await store.search_spans(
            run_id=run_id,
            attempt_id=attempt_id,
            query_text=normalized_query,
            errors_only=errors_only,
            limit=limit + 1,
            after=decoded[:2] if decoded else None,
        )
    except HTTPException:
        raise
    except Exception as exc:
        if _is_mysql_query_timeout(exc):
            raise HTTPException(
                status_code=503,
                detail="Span search exceeded its 5-second budget",
            ) from exc
        raise HTTPException(
            status_code=503,
            detail="Agent observability query unavailable",
        ) from exc
    has_more = len(spans) > limit
    items = _serialize_rows(spans[:limit])
    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = _encode_cursor(
            last["started_at"],
            str(last["span_id"]),
            authority,
        )
    response = SpanSearchResponse(
        items=items,
        next_cursor=next_cursor,
        generated_at=datetime.now(timezone.utc),
    )
    response_size = len(
        json.dumps(
            jsonable_encoder(response),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
    )
    if response_size > _MAX_SPAN_SEARCH_RESPONSE_BYTES:
        raise HTTPException(status_code=413, detail="span_search_too_large")
    return response


@router.get("/runs/{run_id}/events", response_model=EventListResponse)
async def run_events(
    run_id: str,
    request: Request,
    span_id: str | None = Query(None),
    view: str = Query("events", pattern="^(events|links)$"),
    limit: int = Query(100, ge=1, le=_MAX_EVENT_PAGE_SIZE),
    cursor: str | None = Query(None, max_length=2048),
) -> EventListResponse:
    role = _require_observability_reader(request)
    try:
        UUID(run_id)
        if span_id is not None:
            UUID(span_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid event identity",
        ) from exc
    sources = _authorized_sources(request, None)
    authority = {
        "role": role,
        "sources": sources,
        "run_id": run_id,
        "span_id": span_id,
        "view": view,
    }
    decoded = _decode_cursor(cursor, authority) if cursor else None
    store = getattr(request.app.state, "agent_observability_store", None)
    if not isinstance(store, ObservabilityStore):
        raise HTTPException(
            status_code=503,
            detail="Agent observability storage unavailable",
        )
    try:
        run = await store.get_run(
            service_unit_id=service_unit_id(),
            run_id=run_id,
            source_ids=sources,
            tenant_id=_request_tenant_id(request),
        )
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        rows = await store.list_events(
            run_id=run_id,
            source_ids=sources,
            span_id=span_id,
            links_only=view == "links",
            before=decoded[:2] if decoded else None,
            limit=limit + 1,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Agent observability query unavailable",
        ) from exc

    serialized = _serialize_rows(rows)
    items: list[dict[str, Any]] = []
    parsed_rows = _parse_event_rows(serialized, limit)
    available_spans, available_attempts = await _available_link_targets(
        store,
        run_id,
        parsed_rows,
    )
    _redact_unavailable_links(
        parsed_rows,
        available_spans,
        available_attempts,
    )

    for row, metadata in parsed_rows:
        candidate = {**row, "metadata": metadata}
        projected = json.dumps(
            [*items, candidate],
            ensure_ascii=False,
            separators=(",", ":"),
            default=lambda value: (
                value.isoformat()
                if isinstance(value, datetime)
                else str(value)
            ),
        ).encode("utf-8")
        if len(projected) > _MAX_EVENT_RESPONSE_BYTES:
            break
        items.append(candidate)

    has_more = len(serialized) > len(items)
    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = _encode_cursor(
            last["occurred_at"],
            str(last["event_id"]),
            authority,
        )
    return EventListResponse(
        items=items,
        next_cursor=next_cursor,
        generated_at=datetime.now(timezone.utc),
    )


def _evidence_status(value: Any) -> str:
    return (
        value
        if isinstance(value, str)
        and value
        in {
            "available",
            "pending",
            "not_saved",
            "compacted",
            "unavailable",
            "unresolved",
            "truncated",
        }
        else "unresolved"
    )


def _nearest_rank(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(
        0,
        min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1),
    )
    return ordered[index]


def _performance_sample_bounds(
    rows: list[dict[str, Any]],
) -> tuple[datetime | None, datetime | None]:
    starts = [
        value
        for row in rows
        if (value := _population_time(row.get("started_at"))) is not None
    ]
    ends = [
        value
        for row in rows
        if (value := _population_time(row.get("ended_at"))) is not None
    ]
    return (min(starts) if starts else None, max(ends) if ends else None)


_QUALITY_ALIASES = {
    "complete": "complete",
    "observed": "complete",
    "partial": "partial",
}


def _performance_sample_quality(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"complete": 0, "partial": 0, "unknown": 0}
    for row in rows:
        quality = row.get("quality")
        key = (
            _QUALITY_ALIASES.get(quality, "unknown")
            if isinstance(quality, str)
            else "unknown"
        )
        counts[key] += 1
    if not rows:
        status = "empty"
    elif counts["partial"]:
        status = "partial"
    elif counts["unknown"]:
        status = "unknown"
    else:
        status = "complete"
    return {
        "status": status,
        "query_complete": True,
        "span_quality_counts": counts,
    }


def _authorize_evidence_chat(
    request: Request,
    workspace: Any,
    chat: Any,
) -> None:
    metadata = chat.meta if isinstance(chat.meta, dict) else {}
    requested_source = getattr(request.state, "source_id", None)
    chat_source = metadata.get("source_id")
    if (
        isinstance(requested_source, str)
        and isinstance(chat_source, str)
        and chat_source
        and chat_source != requested_source
    ):
        raise KeyError("chat source is outside the request scope")
    workspace_agent = getattr(workspace, "agent_id", None)
    chat_agent = metadata.get("agent_id")
    if (
        isinstance(workspace_agent, str)
        and isinstance(chat_agent, str)
        and chat_agent
        and chat_agent != workspace_agent
    ):
        raise KeyError("chat agent is outside the request scope")


async def _read_admitted_question(
    workspace,
    request,
    run,
    chat_id,
    message_id,
):
    """Read a question only when its durable admission identifies this Run."""
    from ..app.runner.api import (
        _redact_hidden_context_messages,
        _turn_state_to_message,
    )

    def text(value):
        return value.decode("utf-8") if isinstance(value, bytes) else value

    from ..app.conversation_storage.runtime import resolved_scope
    from .identity import same_tenant_scope

    workspace_scope = resolved_scope(
        workspace,
        service_unit_id=text(run.get("service_unit_key")),
    )
    if any(
        getattr(workspace_scope, field) != text(run.get(column))
        for field, column in (
            ("service_unit_id", "service_unit_key"),
            ("source_id", "source_key"),
            ("agent_id", "agent_key"),
        )
    ) or not same_tenant_scope(
        workspace_scope.tenant_id,
        text(run.get("tenant_key")),
        workspace_scope.source_id,
    ):
        return {"status": "unresolved"}
    if message_id != text(run.get("root_question_key")):
        return {"status": "unresolved"}
    chat = await workspace.chat_manager.get_chat(chat_id)
    if chat is None or chat.id != chat_id:
        return {"status": "unavailable"}
    _authorize_evidence_chat(request, workspace, chat)
    if chat.session_id != text(run.get("session_key")):
        return {"status": "unresolved"}
    session = workspace.runner.session
    state = await session.get_persisted_session_state_dict(
        chat.session_id,
        chat.user_id,
    )
    turn = await session.get_turn_state(
        chat.session_id,
        chat.user_id,
        state,
        message_id,
    )
    if not isinstance(turn, dict) or turn.get("chat_id") != chat_id:
        return {"status": "unresolved"}
    carrier = turn.get("agent_observability_context")
    if not isinstance(carrier, dict) or carrier.get("obs_run_id") != run.get(
        "run_id",
    ):
        return {"status": "unresolved"}
    business = carrier.get("business_ref")
    raw_message = turn.get("message")
    if (
        not isinstance(business, dict)
        or business.get("chat_id") != chat_id
        or business.get("root_question_msgid") != message_id
        or not isinstance(raw_message, dict)
        or raw_message.get("id") != message_id
    ):
        return {"status": "unresolved"}
    scope = carrier.get("scope")
    if not isinstance(scope, dict) or any(
        scope.get(field) != text(run.get(column))
        for field, column in (
            ("service_unit_id", "service_unit_key"),
            ("source_id", "source_key"),
            ("tenant_id", "tenant_key"),
            ("agent_id", "agent_key"),
        )
    ):
        return {"status": "unresolved"}
    message = _turn_state_to_message(message_id, turn, chat_id=chat_id)
    if message is None:
        return {"status": "unavailable"}
    from ..app.runner.hidden_context_injection import (
        HIDDEN_CONTEXT_METADATA_KEY,
    )

    metadata = message.metadata
    nested = metadata.get("metadata") if isinstance(metadata, dict) else None
    for value in (metadata, nested):
        if isinstance(value, dict) and HIDDEN_CONTEXT_METADATA_KEY in value:
            marker = value[HIDDEN_CONTEXT_METADATA_KEY]
            if not isinstance(marker, dict) or not all(
                isinstance(marker.get(key), str)
                for key in ("visible_text", "suffix")
            ):
                return {"status": "unavailable"}
    messages = _redact_hidden_context_messages([message])
    # Recheck the binding after reading: deletion or a metadata change must
    # not leave an authorized snapshot usable under a different Chat.
    current = await workspace.chat_manager.get_chat(chat_id)
    if current is None or current != chat:
        return {"status": "unavailable"}
    _authorize_evidence_chat(request, workspace, current)
    latest = await session.get_persisted_session_state_dict(
        chat.session_id,
        chat.user_id,
    )
    if (
        await session.get_turn_state(
            chat.session_id,
            chat.user_id,
            latest,
            message_id,
        )
        != turn
    ):
        return {"status": "unavailable"}
    payload = messages[0].model_dump(mode="json")
    if (
        len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        > _MAX_EVIDENCE_SOURCE_BYTES
    ):
        return {"status": "truncated"}
    return {"status": "available", "message": payload}


def _evidence_message_payload(raw, session_id):
    """Build the exact public projection used for evidence and migration."""
    from agentscope.message import Msg
    from ..app.runner.api import _history_msg_to_messages

    raw = dict(raw)
    raw["metadata"] = {
        key: value
        for key, value in raw["metadata"].items()
        if key
        not in {
            "agent_observability_evidence_ref",
            "agent_observability_outbox",
        }
    }
    # Tool-output projection serializes nested objects into JSON strings.
    # Normalize their key order before conversion so archive serialization
    # cannot change the public output or its migration digest.
    raw = json.loads(
        json.dumps(raw, ensure_ascii=False, sort_keys=True, allow_nan=False),
    )
    return [
        message.model_dump(mode="json")
        for message in _history_msg_to_messages(
            [Msg.from_dict(raw)],
            session_id=session_id,
        )
    ]


# Keep the session/archive revalidation sequence together.
async def _read_pending_online(  # pylint: disable=too-many-statements
    workspace,
    request,
    run,
    reference,
    span_id,
):
    """Prove a pending locator against a stable, committed session root."""
    from ..app.conversation_storage.runtime import resolved_scope
    from ..app.runner.api import _archive_store
    from .identity import same_observability_scope, same_tenant_scope

    def text(value):
        return value.decode("utf-8") if isinstance(value, bytes) else value

    scope = resolved_scope(
        workspace,
        service_unit_id=text(run.get("service_unit_key")),
    )
    if any(
        getattr(scope, field) != text(run.get(column))
        for field, column in (
            ("service_unit_id", "service_unit_key"),
            ("source_id", "source_key"),
            ("agent_id", "agent_key"),
        )
    ) or not same_tenant_scope(
        scope.tenant_id,
        text(run.get("tenant_key")),
        scope.source_id,
    ):
        return {"status": "unresolved"}
    chat_id = reference["chat_id"]
    if chat_id != text(run.get("chat_key")):
        return {"status": "unresolved"}
    chat = await workspace.chat_manager.get_chat(chat_id)
    if chat is None or chat.id != chat_id:
        return {"status": "pending"}
    _authorize_evidence_chat(request, workspace, chat)
    if chat.session_id != text(run.get("session_key")):
        return {"status": "unresolved"}
    session = workspace.runner.session
    state = await session.get_persisted_session_state_dict(
        chat.session_id,
        chat.user_id,
    )
    archive = _archive_store(workspace)
    if archive is None:
        return {"status": "unavailable"}
    archived = await archive.read_evidence_occurrence(
        chat_id,
        reference["reference_id"],
        reference["message_id"],
        max_bytes=_MAX_EVIDENCE_SOURCE_BYTES,
    )
    if not archived.get("snapshot"):
        return {
            "status": (
                "unresolved"
                if archived.get("status") == "unresolved"
                else "unavailable"
            ),
        }
    memory = state.get("agent", {}).get("memory", {})
    entries = memory.get("content", []) if isinstance(memory, dict) else []
    if not isinstance(entries, list):
        return {"status": "unresolved"}
    matches = []
    for entry in entries:
        raw = entry[0] if isinstance(entry, (list, tuple)) and entry else None
        if not isinstance(raw, dict) or not isinstance(
            raw.get("metadata"),
            dict,
        ):
            continue
        if (
            raw["metadata"].get("agent_observability_evidence_ref")
            == reference["reference_id"]
        ):
            matches.append(raw)
    archived_again = await archive.read_evidence_occurrence(
        chat_id,
        reference["reference_id"],
        reference["message_id"],
        max_bytes=_MAX_EVIDENCE_SOURCE_BYTES,
    )

    def archive_identity(value):
        message = value.get("message")
        return (
            value.get("status"),
            value.get("snapshot"),
            message.to_dict() if message is not None else None,
        )

    if archive_identity(archived_again) != archive_identity(archived):
        return {"status": "pending"}
    latest = await session.get_persisted_session_state_dict(
        chat.session_id,
        chat.user_id,
    )
    if latest != state:
        return {"status": "pending"}
    current = await workspace.chat_manager.get_chat(chat_id)
    if current is None or current != chat:
        return {"status": "pending"}
    _authorize_evidence_chat(request, workspace, current)
    archive_status = archived.get("status")
    if len(matches) > 1 or (matches and archive_status != "unavailable"):
        return {"status": "unresolved"}
    if not matches and archive_status == "unavailable":
        return {"status": "pending"}
    if not matches and archive_status == "truncated":
        return {"status": "truncated"}
    if matches:
        raw = matches[0]
    elif archive_status == "available":
        raw = archived["message"].to_dict()
    else:
        return {"status": "unresolved"}
    if raw.get("id") != reference["message_id"]:
        return {"status": "unresolved"}
    from .outbox import verify_event

    event = verify_event(raw["metadata"].get("agent_observability_outbox"))
    if (
        any(
            event.get(key) != expected
            for key, expected in (
                ("obs_run_id", text(run.get("run_id"))),
                ("obs_span_id", span_id),
                ("chat_id", chat_id),
                ("message_id", reference["message_id"]),
                ("reference_id", reference["reference_id"]),
                ("content_type", reference["content_type"]),
                ("event_type", "evidence.linked"),
                ("saved_state", "available"),
            )
        )
        or not same_observability_scope(
            event.get("scope"),
            {
                "service_unit_id": scope.service_unit_id,
                "source_id": scope.source_id,
                "tenant_id": scope.tenant_id,
                "agent_id": scope.agent_id,
            },
        )
        or (
            reference.get("saved_state") != "pending"
            and event.get("event_id")
            != str(reference["evidence_ref"]).removeprefix("event:")
        )
    ):
        return {"status": "unresolved"}
    expected_role = {"tool_output": "system", "assistant_answer": "assistant"}
    if raw.get("role") != expected_role.get(reference.get("content_type")):
        return {"status": "unresolved"}
    blocks = raw.get("content")
    if not isinstance(blocks, list):
        return {"status": "unresolved"}
    block_types = {
        block.get("type") for block in blocks if isinstance(block, dict)
    }
    if (
        reference["content_type"] == "tool_output"
        and "tool_result" not in block_types
    ):
        return {"status": "unresolved"}
    if reference["content_type"] == "assistant_answer" and (
        "text" not in block_types or "tool_use" in block_types
    ):
        return {"status": "unresolved"}
    payload = _evidence_message_payload(raw, chat.session_id)
    if (
        len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        > _MAX_EVIDENCE_SOURCE_BYTES
    ):
        return {"status": "truncated"}
    return {
        "status": "compacted" if not matches else "available",
        "message": payload,
    }


async def _nas_message_occurrence(workspace, request, run, reference):
    from ..app.conversation_storage.runtime import resolved_scope
    from .identity import same_tenant_scope

    unavailable = {"status": "unavailable", "message": None}
    chat_id = reference["chat_id"]
    if _population_identity(run.get("chat_key")) != chat_id:
        return unavailable
    scope = resolved_scope(
        workspace,
        service_unit_id=_population_identity(run.get("service_unit_key")),
    )
    if (
        scope.agent_id != _population_identity(run.get("agent_key"))
        or scope.source_id != _population_identity(run.get("source_key"))
        or not same_tenant_scope(
            scope.tenant_id,
            _population_identity(run.get("tenant_key")),
            scope.source_id,
        )
    ):
        return unavailable
    chat = await workspace.chat_manager.get_chat(chat_id)
    if (
        chat is None
        or chat.id != chat_id
        or chat.session_id != _population_identity(run.get("session_key"))
    ):
        return unavailable
    _authorize_evidence_chat(request, workspace, chat)

    if reference.get("occurrence_id") is not None:
        return unavailable

    messages = await _find_nas_evidence_messages(
        workspace,
        chat,
        reference,
    )
    if not messages or await workspace.chat_manager.get_chat(chat_id) != chat:
        return unavailable
    payload = _nas_evidence_payload(messages[0], chat.session_id, reference)
    return (
        {"status": "available", "message": payload} if payload else unavailable
    )


async def _find_nas_evidence_messages(workspace, chat, reference):
    from agentscope.message import Msg

    from ..app.runner import api
    from ..app.runner.query_attempt import (
        _get_memory_entry_message,
        _get_persisted_memory_entries,
    )
    from .nas_evidence import nas_reference

    chat_id = reference["chat_id"]

    def matches(message):
        return (
            isinstance(message, dict)
            and message.get("id") == reference["message_id"]
            and nas_reference(message) == reference["reference_id"]
        )

    state = await api._read_history_state(
        workspace.runner.session,
        chat.session_id,
        chat.user_id,
    )
    messages = []
    for entry in _get_persisted_memory_entries(state) or ():
        message = _get_memory_entry_message(entry)
        if matches(message):
            messages.append(Msg.from_dict(message))
            break
    archive = api._archive_store(workspace)
    if archive is not None and not messages:
        messages = await archive.find_messages(
            chat_id,
            lambda message: matches(message.to_dict()),
            limit=1,
        )
    return messages


def _nas_evidence_payload(message, session_id, reference):
    from agentscope.message import Msg

    from ..app.runner import api

    kind = reference.get("content_type")
    content = message.content
    if kind in {"assistant_answer", "tool_output"}:
        if kind == "assistant_answer" and message.role != "assistant":
            return None
        block_type = "text" if kind == "assistant_answer" else "tool_result"
        if isinstance(content, list):
            content = [
                block
                for block in content
                if isinstance(block, dict) and block.get("type") == block_type
            ]
        elif kind == "tool_output":
            return None
        if not content:
            return None
        message = Msg.from_dict(
            {**message.to_dict(), "content": content, "metadata": {}},
        )
    elif kind != "user_question" or message.role != "user":
        return None

    fragments = api._redact_hidden_context_messages(
        api._history_msg_to_messages([message], session_id=session_id),
    )
    expected_type = (
        "plugin_call_output" if kind == "tool_output" else "message"
    )
    return next(
        (
            fragment.model_dump(mode="json")
            for fragment in fragments
            if fragment.type == expected_type
        ),
        None,
    )


async def _load_evidence_item(
    reference: dict[str, Any],
    *,
    request: Request,
    run: dict[str, Any],
    history: Any,
    workspace: Any,
    max_bytes: int,
    authority: dict[str, Any],
    chunk_offset: int = 0,
    expected_digest: str | None = None,
) -> tuple[dict[str, Any], Any, Any]:
    item = {
        key: reference[key]
        for key in (
            "evidence_ref",
            "content_type",
            "chat_id",
            "message_id",
            "occurrence_id",
            "reference_id",
        )
        if key in reference
    }
    status = _evidence_status(reference.get("saved_state"))
    item["status"] = status
    reference_id = reference.get("reference_id")
    is_nas_reference = isinstance(
        reference_id,
        str,
    ) and reference_id.startswith(
        "nas:",
    )
    pending_reference = (
        not is_nas_reference
        and reference.get("occurrence_id") is None
        and status in {"pending", "available", "compacted"}
        and reference.get("content_type")
        in {"tool_output", "assistant_answer"}
        and isinstance(reference_id, str)
    )
    if status not in {"available", "compacted"} and not pending_reference:
        return item, history, workspace
    if not _evidence_reference_matches_run(reference, run):
        item["status"] = "unresolved"
        return item, history, workspace
    chat_id = reference.get("chat_id")
    message_id = reference.get("message_id")
    occurrence_no = reference.get("occurrence_id")
    if history is None:
        try:
            from ..app.runner.api import get_workspace

            workspace = await get_workspace(request)
            history = getattr(
                getattr(workspace, "conversation_storage", None),
                "history",
                None,
            )
        except Exception:
            history = None
    try:
        if workspace is not None and is_nas_reference:
            result = await _nas_message_occurrence(
                workspace,
                request,
                run,
                reference,
            )
        elif pending_reference and workspace is not None:
            result = await _read_pending_online(
                workspace,
                request,
                run,
                reference,
                authority["span_id"],
            )
        elif history is not None and hasattr(history, "message_occurrence"):
            result = await history.message_occurrence(
                chat_id,
                message_id,
                occurrence_no=occurrence_no,
                max_bytes=_MAX_EVIDENCE_SOURCE_BYTES,
                authorize=lambda chat: _authorize_evidence_chat(
                    request,
                    workspace,
                    chat,
                ),
            )
        elif (
            workspace is not None
            and reference.get("content_type") == "user_question"
            and occurrence_no is None
        ):
            result = await _read_admitted_question(
                workspace,
                request,
                run,
                chat_id,
                message_id,
            )
        else:
            result = {
                "status": "pending" if pending_reference else "unavailable",
            }
    except Exception:
        result = {"status": "unavailable", "message": None}
    result_status = _evidence_status(result.get("status"))
    item["status"] = (
        "compacted"
        if status == "compacted" and result_status == "available"
        else result_status
    )
    if result_status == "truncated":
        item["truncated"] = True
    payload = result.get("message")
    if result_status in {"available", "compacted"}:
        if not isinstance(payload, (dict, list)) or (
            isinstance(payload, dict) and payload.get("type") == "reasoning"
        ):
            item["status"] = "unavailable"
            return item, history, workspace
        _set_evidence_content(
            item,
            payload,
            max_bytes=max_bytes,
            authority=authority,
            chunk_offset=chunk_offset,
            expected_digest=expected_digest,
        )
    return item, history, workspace


def _evidence_reference_matches_run(reference, run) -> bool:
    chat_id = reference.get("chat_id")
    message_id = reference.get("message_id")
    occurrence_no = reference.get("occurrence_id")
    if (
        not isinstance(chat_id, str)
        or not isinstance(message_id, str)
        or (
            occurrence_no is not None
            and (type(occurrence_no) is not int or occurrence_no < 0)
        )
    ):
        return False
    run_chat = run.get("chat_key")
    if isinstance(run_chat, bytes):
        run_chat = run_chat.decode("utf-8", errors="replace")
    return not (isinstance(run_chat, str) and run_chat and chat_id != run_chat)


def _set_evidence_content(
    item: dict[str, Any],
    payload: Any,
    *,
    max_bytes: int,
    authority: dict[str, Any],
    chunk_offset: int,
    expected_digest: str | None,
) -> None:
    compacted = item.get("status") == "compacted"
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_EVIDENCE_SOURCE_BYTES:
        item["truncated"] = True
        if not compacted:
            item["status"] = "truncated"
        return
    content_digest = hashlib.sha256(encoded).hexdigest()
    if expected_digest is not None and content_digest != expected_digest:
        raise HTTPException(
            status_code=409,
            detail="Evidence content changed during continuation",
        )
    if chunk_offset < 0 or chunk_offset >= len(encoded):
        raise HTTPException(status_code=400, detail="Invalid evidence offset")
    if chunk_offset == 0 and len(encoded) <= max_bytes:
        item["message"] = payload
        return
    chunk_end = min(chunk_offset + max_bytes, len(encoded))
    has_more = chunk_end < len(encoded)
    item.update(
        status=(
            "compacted"
            if compacted
            else "truncated" if has_more else "available"
        ),
        truncated=has_more,
        message_chunk=base64.urlsafe_b64encode(
            encoded[chunk_offset:chunk_end],
        )
        .decode()
        .rstrip("="),
        chunk_encoding="base64url-json",
        chunk_offset=chunk_offset,
        content_bytes=len(encoded),
        has_more=has_more,
        next_cursor=(
            _encode_evidence_cursor(
                evidence_ref=str(item["evidence_ref"]),
                offset=chunk_end,
                content_digest=content_digest,
                authority=authority,
            )
            if has_more
            else None
        ),
    )


@router.get(
    "/runs/{run_id}/spans/{span_id}/evidence",
    response_model=EvidenceResponse,
)
async def span_evidence(
    run_id: str,
    span_id: str,
    request: Request,
    max_bytes: int = Query(32 * 1024, ge=32 * 1024, le=256 * 1024),
    cursor: str | None = Query(None, max_length=2048),
) -> EvidenceResponse:
    role = _require_observability_reader(request)
    if not isinstance(max_bytes, int):
        max_bytes = 32 * 1024
    if not isinstance(cursor, str):
        cursor = None
    try:
        UUID(run_id)
        UUID(span_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid Span identity",
        ) from exc
    sources = _authorized_sources(request, None)
    authority = {
        "kind": "evidence_content",
        "role": role,
        "sources": sources,
        "run_id": run_id,
        "span_id": span_id,
        "max_bytes": max_bytes,
    }
    continuation = (
        _decode_evidence_cursor(cursor, authority) if cursor else None
    )
    store = getattr(request.app.state, "agent_observability_store", None)
    if not isinstance(store, ObservabilityStore):
        raise HTTPException(
            status_code=503,
            detail="Agent observability storage unavailable",
        )
    try:
        run = await store.get_run(
            service_unit_id=service_unit_id(),
            run_id=run_id,
            source_ids=sources,
            tenant_id=_request_tenant_id(request),
        )
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        refs = await store.list_span_evidence_refs(
            run_id=run_id,
            span_id=span_id,
            source_ids=sources,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Agent observability evidence unavailable",
        ) from exc

    chunk_limit = (
        max_bytes
        if continuation is not None or not refs
        else min(max_bytes, (256 * 1024) // len(refs))
    )
    if continuation is not None:
        evidence_ref = continuation[0]
        reference = next(
            (
                item
                for item in refs
                if item.get("evidence_ref") == evidence_ref
            ),
            None,
        )
        if reference is None:
            raise HTTPException(status_code=404, detail="Evidence not found")
        refs = [reference]

    items: list[dict[str, Any]] = []
    history = None
    workspace = None
    for reference in refs:
        item, history, workspace = await _load_evidence_item(
            reference,
            request=request,
            run=run,
            history=history,
            workspace=workspace,
            max_bytes=chunk_limit,
            authority=authority,
            chunk_offset=continuation[1] if continuation else 0,
            expected_digest=continuation[2] if continuation else None,
        )
        items.append(item)
    response = EvidenceResponse(
        items=items,
        generated_at=datetime.now(timezone.utc),
    )
    if (
        len(
            json.dumps(
                jsonable_encoder(response),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
        > _MAX_EVENT_RESPONSE_BYTES
    ):
        raise HTTPException(
            status_code=413,
            detail="evidence_response_too_large",
        )
    return response


@router.get("/runs/{run_id}/assets", response_model=AssetResponse)
async def run_assets(
    run_id: str,
    request: Request,
    span_id: str | None = Query(None),
    limit: int = Query(32, ge=1, le=32),
    cursor: str | None = Query(None),
) -> AssetResponse:
    role = _require_observability_reader(request)
    try:
        UUID(run_id)
        if span_id is not None:
            UUID(span_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid asset identity",
        ) from exc
    sources = _authorized_sources(request, None)
    authority = {
        "kind": "assets",
        "role": role,
        "sources": sources,
        "run_id": run_id,
        "span_id": span_id,
    }
    decoded_cursor = _decode_cursor(cursor, authority) if cursor else None
    before = None
    if decoded_cursor is not None:
        cursor_time, event_id, _ = decoded_cursor
        if not isinstance(cursor_time, datetime):
            raise HTTPException(status_code=400, detail="Invalid asset cursor")
        before = cursor_time, event_id
    store = getattr(request.app.state, "agent_observability_store", None)
    if not isinstance(store, ObservabilityStore):
        raise HTTPException(
            status_code=503,
            detail="Agent observability storage unavailable",
        )
    try:
        run = await store.get_run(
            service_unit_id=service_unit_id(),
            run_id=run_id,
            source_ids=sources,
            tenant_id=_request_tenant_id(request),
        )
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        assets = await store.list_run_assets(
            run_id=run_id,
            source_ids=sources,
            span_id=span_id,
            limit=limit,
            before=before,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Agent observability assets unavailable",
        ) from exc
    next_cursor = None
    if assets["next_key"] is not None:
        next_cursor = _encode_cursor(
            assets["next_key"][0],
            assets["next_key"][1],
            authority,
        )
    return AssetResponse(
        items=assets["items"],
        next_cursor=next_cursor,
        quality=assets["quality"],
        generated_at=datetime.now(timezone.utc),
    )


@router.get(
    "/runs/{run_id}/spans/{span_id}/ancestors",
    response_model=SpanAncestorResponse,
)
async def span_ancestors(
    run_id: str,
    span_id: str,
    request: Request,
    limit: int = Query(64, ge=1, le=64),
    cursor: str | None = Query(None),
) -> SpanAncestorResponse:
    _require_observability_reader(request)
    try:
        UUID(run_id)
        UUID(span_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid ancestor identity",
        ) from exc
    sources = _authorized_sources(request, None)
    store = getattr(request.app.state, "agent_observability_store", None)
    if not isinstance(store, ObservabilityStore):
        raise HTTPException(
            status_code=503,
            detail="Agent observability storage unavailable",
        )
    try:
        run = await store.get_run(
            service_unit_id=service_unit_id(),
            run_id=run_id,
            source_ids=sources,
            tenant_id=_request_tenant_id(request),
        )
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        result = await store.list_span_ancestors(
            run_id=run_id,
            span_id=span_id,
            limit=limit,
            cursor=cursor,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Agent observability ancestors unavailable",
        ) from exc
    if not result["items"]:
        raise HTTPException(status_code=404, detail="Span not found")
    return SpanAncestorResponse(
        items=result["items"],
        next_cursor=result["next_cursor"],
        quality=result["quality"],
        generated_at=datetime.now(timezone.utc),
    )


def _performance_window(
    start: datetime | None,
    end: datetime | None,
) -> tuple[datetime, datetime]:
    window_end = end or datetime.now(timezone.utc)
    window_start = start or window_end - timedelta(hours=24)
    if (
        window_start.tzinfo is None
        or window_end.tzinfo is None
        or window_end <= window_start
        or window_end - window_start > _MAX_WINDOW
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid performance window",
        )
    return window_start, window_end


async def _load_performance_samples(
    store: ObservabilityStore,
    *,
    sources: list[str],
    window_start: datetime,
    window_end: datetime,
    status: str | None,
    run_status: str | None,
    span_type: str | None,
    operation: str | None,
    tenant_id: str | None,
) -> list[dict[str, Any]]:
    try:
        candidate_count = await store.count_performance_candidates(
            service_unit_id=service_unit_id(),
            source_ids=sources,
            start_at=window_start,
            end_at=window_end,
            status=status,
            run_status=run_status,
            span_type=span_type,
            operation=operation,
            max_candidates=_MAX_RUN_CANDIDATES,
            tenant_id=tenant_id,
        )
        if candidate_count > _MAX_RUN_CANDIDATES:
            raise HTTPException(status_code=422, detail="range_too_large")
        rows = await store.performance_samples(
            service_unit_id=service_unit_id(),
            source_ids=sources,
            start_at=window_start,
            end_at=window_end,
            status=status,
            run_status=run_status,
            span_type=span_type,
            operation=operation,
            max_samples=_MAX_RUN_CANDIDATES,
            tenant_id=tenant_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        if _is_mysql_query_timeout(exc):
            raise HTTPException(
                status_code=503,
                detail="Performance query exceeded its 5-second budget",
            ) from exc
        raise HTTPException(
            status_code=503,
            detail="Performance query unavailable",
        ) from exc
    if len(rows) > _MAX_RUN_CANDIDATES:
        raise HTTPException(status_code=422, detail="range_too_large")
    return rows


@router.get("/performance", response_model=PerformanceResponse)
async def performance_overview(
    request: Request,
    source_id: list[str] | None = Query(None),
    start: datetime | None = Query(None),
    end: datetime | None = Query(None),
    status: str | None = Query(None),
    run_status: str | None = Query(None),
    span_type: str | None = Query(None),
    operation: str | None = Query(None),
) -> PerformanceResponse:
    _require_observability_reader(request)
    source_id = source_id if isinstance(source_id, list) else None
    status = status if isinstance(status, str) else None
    run_status = run_status if isinstance(run_status, str) else None
    span_type = span_type if isinstance(span_type, str) else None
    operation = operation if isinstance(operation, str) else None
    sources = _authorized_sources(request, source_id)
    window_start, window_end = _performance_window(start, end)
    store = _request_observability_store(request)
    rows = await _load_performance_samples(
        store,
        sources=sources,
        window_start=window_start,
        window_end=window_end,
        status=status,
        run_status=run_status,
        span_type=span_type,
        operation=operation,
        tenant_id=_request_tenant_id(request),
    )
    return _performance_response(rows, window_start, window_end, run_status)


def _performance_response(
    rows,
    window_start,
    window_end,
    run_status,
) -> PerformanceResponse:
    run_metrics = _performance_run_metrics(rows)
    durations = _known_performance_values(rows, "duration_us")
    grouped: dict[str, int] = {}
    for row in rows:
        key = str(row.get("span_type") or "unknown")
        grouped[key] = grouped.get(key, 0) + 1
    sample_start_at, sample_end_at = _performance_sample_bounds(rows)
    return PerformanceResponse(
        population_source="observed_ended_spans",
        window_start=window_start,
        window_end=window_end,
        generated_at=datetime.now(timezone.utc),
        sample_count=len(rows),
        sample_start_at=sample_start_at,
        sample_end_at=sample_end_at,
        sample_quality=_performance_sample_quality(rows),
        **run_metrics,
        duration_us={
            "known_count": len(durations),
            "p50": _nearest_rank(durations, 0.50),
            "p95": _nearest_rank(durations, 0.95),
            "p99": _nearest_rank(durations, 0.99),
        },
        tool_failures=_performance_tool_failures(rows),
        run_status_filter=run_status,
        llm=_performance_llm_metrics(rows),
        by_span_type=[
            {"span_type": key, "count": count}
            for key, count in sorted(grouped.items())
        ],
    )


def _known_performance_values(rows, key):
    return [row[key] for row in rows if row.get(key) is not None]


def _performance_run_metrics(rows):
    runs_by_id = {
        row["run_id"]: row
        for row in rows
        if isinstance(row.get("run_id"), str)
    }
    run_elapsed = _known_performance_values(
        runs_by_id.values(),
        "run_duration_us",
    )
    active = _known_performance_values(
        runs_by_id.values(),
        "active_duration_us",
    )
    loops = _known_performance_values(runs_by_id.values(), "agent_loops")
    return {
        "run_count": len(runs_by_id),
        "run_elapsed_us": {
            "known_count": len(run_elapsed),
            "p50": _nearest_rank(run_elapsed, 0.50),
            "p95": _nearest_rank(run_elapsed, 0.95),
            "p99": _nearest_rank(run_elapsed, 0.99),
        },
        "active_duration_us": {
            "known_count": len(active),
            "p50": _nearest_rank(active, 0.50),
            "p95": _nearest_rank(active, 0.95),
            "p99": _nearest_rank(active, 0.99),
        },
        "agent_loops": {
            "known_count": len(loops),
            "total": sum(loops) if loops else None,
            "p50": _nearest_rank(loops, 0.50),
            "p95": _nearest_rank(loops, 0.95),
        },
    }


def _performance_tool_failures(rows):
    tools = [row for row in rows if row.get("span_type") == "Tool"]
    uncancelled_tools = [
        row for row in tools if row.get("status") != "cancelled"
    ]
    tool_failures = [
        row
        for row in uncancelled_tools
        if (
            row.get("has_error") == 1
            or row.get("status") in {"failed", "rejected", "timed_out"}
        )
    ]
    return {
        "count": len(tool_failures),
        "sample_count": len(uncancelled_tools),
        "cancelled_count": len(tools) - len(uncancelled_tools),
        "rate": (
            len(tool_failures) / len(uncancelled_tools)
            if uncancelled_tools
            else None
        ),
    }


def _performance_llm_metrics(rows):
    llm = [
        row
        for row in rows
        if row.get("span_type") == "LLM" and row.get("operation") == "chat"
    ]
    request_attempts = [
        row
        for row in rows
        if row.get("span_type") == "LLM"
        and row.get("operation") == "request_attempt"
    ]
    input_known = output_known = usage_known = 0
    for row in request_attempts:
        has_input = row.get("input_tokens") is not None
        has_output = row.get("output_tokens") is not None
        input_known += has_input
        output_known += has_output
        usage_known += has_input and has_output
    first_chunk = [
        row["time_to_first_chunk_us"]
        for row in request_attempts
        if row.get("time_to_first_chunk_us") is not None
    ]
    visible = [
        row["time_to_first_visible_text_us"]
        for row in request_attempts
        if row.get("time_to_first_visible_text_us") is not None
    ]
    first_chunk_percentiles = {
        "known_count": len(first_chunk),
        "p50": _nearest_rank(first_chunk, 0.50),
        "p95": _nearest_rank(first_chunk, 0.95),
        "p99": _nearest_rank(first_chunk, 0.99),
    }
    return {
        "call_count": len(llm),
        "request_attempt_count": len(request_attempts),
        "usage_known_count": usage_known,
        "input_tokens_known_count": input_known,
        "output_tokens_known_count": output_known,
        "usage_coverage": (
            usage_known / len(request_attempts) if request_attempts else None
        ),
        "first_chunk_us": first_chunk_percentiles,
        # Legacy field name, now using the request-attempt sample set.
        "ttft_us": first_chunk_percentiles,
        "first_visible_text_us": {
            "known_count": len(visible),
            "p50": _nearest_rank(visible, 0.50),
            "p95": _nearest_rank(visible, 0.95),
            "p99": _nearest_rank(visible, 0.99),
        },
    }

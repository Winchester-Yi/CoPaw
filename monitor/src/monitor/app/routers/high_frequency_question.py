# -*- coding: utf-8 -*-
"""High-frequency question analysis API router."""

from __future__ import annotations

import logging
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Header, HTTPException

from ..models.high_frequency_question import (
    HighFrequencyQuestionCriteriaRequest,
    HighFrequencyQuestionMessageListResponse,
    HighFrequencyQuestionMessageQueryRequest,
    HighFrequencyQuestionPrewarmRequest,
    HighFrequencyQuestionResultQueryResponse,
    HighFrequencyQuestionResultSaveRequest,
    HighFrequencyQuestionResultSaveResponse,
    HighFrequencyQuestionScheduledTaskRequest,
    HighFrequencyQuestionTaskSubmitRequest,
    HighFrequencyQuestionTaskSubmitResponse,
)
from ..services.tracing.high_frequency_question import (
    HighFrequencyQuestionService,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/monitor/high-frequency-question",
    tags=["high-frequency-question"],
)


def _resolve_source_id(
    header_source_id: str | None,
    fallback_source_id: str | None = None,
) -> str:
    source_id = (header_source_id or "").strip()
    if source_id:
        return source_id
    source_id = (fallback_source_id or "").strip()
    if source_id:
        return source_id
    return "default"


@router.post(
    "/messages",
    response_model=HighFrequencyQuestionMessageListResponse,
    summary="查询高频问题分析源消息",
)
async def query_high_frequency_question_messages(
    body: HighFrequencyQuestionMessageQueryRequest,
    x_source_id: str | None = Header(default=None, alias="X-Source-Id"),
) -> HighFrequencyQuestionMessageListResponse:
    """Query clean source messages for high-frequency question analysis."""
    try:
        resolved_body = body.model_copy(
            update={
                "source_id": _resolve_source_id(x_source_id, body.source_id),
            },
        )
        service = HighFrequencyQuestionService.get_instance()
        return await service.query_messages(resolved_body)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Database not available",
        ) from exc


@router.post(
    "/results",
    response_model=HighFrequencyQuestionResultSaveResponse,
    summary="批量保存高频问题分析结果",
)
async def save_high_frequency_question_results(
    body: HighFrequencyQuestionResultSaveRequest,
    x_source_id: str | None = Header(default=None, alias="X-Source-Id"),
) -> HighFrequencyQuestionResultSaveResponse:
    """Save AI-generated high-frequency question analysis results."""
    try:
        resolved_body = body.model_copy(
            update={
                "source_id": _resolve_source_id(x_source_id, body.source_id),
            },
        )
        service = HighFrequencyQuestionService.get_instance()
        return await service.save_results(resolved_body)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Database not available",
        ) from exc


@router.post(
    "/tasks",
    response_model=HighFrequencyQuestionTaskSubmitResponse,
    summary="提交高频问题分析任务",
)
async def submit_high_frequency_question_task(
    body: HighFrequencyQuestionTaskSubmitRequest,
    x_source_id: str | None = Header(default=None, alias="X-Source-Id"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_user_name: str | None = Header(default=None, alias="X-User-Name"),
) -> HighFrequencyQuestionTaskSubmitResponse:
    """Submit analysis task or reuse a recent successful result."""
    try:
        resolved_body = body.model_copy(
            update={
                "source_id": _resolve_source_id(x_source_id, body.source_id),
            },
        )
        service = HighFrequencyQuestionService.get_instance()
        return await service.submit_task(
            resolved_body,
            actor_user_id=x_user_id or "",
            actor_user_name=unquote(x_user_name or ""),
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Database not available",
        ) from exc


@router.post(
    "/prewarm",
    response_model=HighFrequencyQuestionTaskSubmitResponse,
    summary="提交高频问题分析预跑任务",
)
async def prewarm_high_frequency_question_task(
    body: HighFrequencyQuestionPrewarmRequest,
    x_source_id: str | None = Header(default=None, alias="X-Source-Id"),
) -> HighFrequencyQuestionTaskSubmitResponse:
    """Submit scheduler-driven prewarm through the normal task flow."""
    try:
        resolved_body = body.model_copy(
            update={
                "source_id": _resolve_source_id(x_source_id, body.source_id),
            },
        )
        service = HighFrequencyQuestionService.get_instance()
        return await service.submit_prewarm(resolved_body)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Database not available",
        ) from exc


@router.post(
    "/scheduled-tasks",
    response_model=HighFrequencyQuestionTaskSubmitResponse,
    summary="提交调度平台高频问题分析任务",
)
async def submit_scheduled_high_frequency_question_task(
    body: HighFrequencyQuestionScheduledTaskRequest,
) -> HighFrequencyQuestionTaskSubmitResponse:
    """Submit a body-only scheduler task without requiring custom headers."""
    try:
        service = HighFrequencyQuestionService.get_instance()
        return await service.submit_scheduled_task(body)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Database not available",
        ) from exc


@router.get(
    "/results",
    response_model=HighFrequencyQuestionResultQueryResponse,
    summary="查询高频问题分析结果",
)
async def query_high_frequency_question_results(
    request: HighFrequencyQuestionCriteriaRequest = Depends(),
    x_source_id: str | None = Header(default=None, alias="X-Source-Id"),
) -> HighFrequencyQuestionResultQueryResponse:
    """Query recent or stale successful analysis results."""
    try:
        resolved_request = request.model_copy(
            update={
                "source_id": _resolve_source_id(x_source_id, request.source_id),
            },
        )
        service = HighFrequencyQuestionService.get_instance()
        return await service.query_results(resolved_request)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Database not available",
        ) from exc

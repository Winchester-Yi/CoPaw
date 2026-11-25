# -*- coding: utf-8 -*-
"""Direct internal-platform admission and delivery queries for cron reports."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..database.connection import get_db_connection
from ..services.cron.batch_status_report.store import ReportStore
from ..services.cron.managed.identity import identity_value
from ..services.cron.managed.storage import transaction

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/scheduler/cron/batch-status-reports",
    tags=["batch-status-report"],
)
EmployeeId = Annotated[str, Field(strict=True, pattern=r"^[0-9]{8}$")]


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(strict=True, min_length=1, max_length=128)
    recipient_ids: list[EmployeeId] = Field(min_length=1, max_length=100)

    @field_validator("source_id")
    @classmethod
    def source(cls, value):
        return identity_value(value)

    @field_validator("recipient_ids")
    @classmethod
    def recipient_set(cls, value):
        return sorted(set(value))


def storage_error(exc):
    logger.warning("批调度简报存储不可用 error_type=%s", type(exc).__name__)
    return HTTPException(503, "cron_report_storage_unavailable")


@router.post("", status_code=202)
async def submit(body: ReportRequest, request: Request):
    if not getattr(request.app.state, "batch_report_ready", False):
        raise HTTPException(503, "cron_report_not_ready")
    try:
        return await ReportStore(get_db_connection()).submit(body)
    except Exception as exc:
        raise storage_error(exc) from exc


@router.get("/{report_id}")
async def status(
    report_id: UUID,
    source_id: str = Query(min_length=1, max_length=128),
):
    try:
        source = identity_value(source_id)
    except ValueError as exc:
        raise HTTPException(422, "invalid_cron_identity") from exc
    try:
        async with transaction(get_db_connection()) as tx:
            store = ReportStore(tx)
            report = await store.report_for_source(source, str(report_id))
            if not report:
                raise HTTPException(404, "cron_report_not_found")
            return await store.results(report)
    except HTTPException:
        raise
    except Exception as exc:
        raise storage_error(exc) from exc

# -*- coding: utf-8 -*-
"""Internal snapshot for the scheduled Zhaohu batch report."""

import logging
from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..database import get_db_connection
from ..services.cron.batch_status_report import report_snapshot

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/monitor/internal/cron",
    tags=["batch-status-report"],
)


class SnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^\S(?:[^\x00-\x1f\x7f]*\S)?$",
    )
    business_date: date


@router.post("/batch-status-report-snapshot")
async def snapshot(body: SnapshotRequest):
    try:
        return await report_snapshot(
            get_db_connection(),
            body.source_id,
            body.business_date,
        )
    except Exception as exc:
        logger.warning("批调度简报取数失败 error_type=%s", type(exc).__name__)
        raise HTTPException(503, "cron_report_snapshot_unavailable") from exc

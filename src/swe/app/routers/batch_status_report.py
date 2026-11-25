# -*- coding: utf-8 -*-
"""Internal single-attempt Zhaohu delivery for the cron report worker."""

import asyncio
import logging
import os
from datetime import date
from secrets import compare_digest
from typing import NoReturn
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...config.config import load_agent_config
from ...config.context import (
    is_valid_identity_value,
    resolve_runtime_tenant_id,
)
from ...config.utils import get_tenant_storage_config_path
from ..channels.zhaohu.channel import ZhaohuChannel
from ..crons.monitor_sync_client import get_monitor_sync_client

logger = logging.getLogger(__name__)


def check_internal_token(x_internal_token: str = Header(default="")):
    token = os.environ.get("SWE_INTERNAL_TOKEN")
    if token and not compare_digest(
        x_internal_token.encode(),
        f"Bearer {token}".encode(),
    ):
        raise HTTPException(401, "cron_report_internal_unauthorized")


router = APIRouter(
    prefix="/internal/cron/batch-status-reports",
    tags=["internal"],
    dependencies=[Depends(check_internal_token)],
)


class ReportSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=128)

    @field_validator("source_id")
    @classmethod
    def validate_source(cls, value):
        if not is_valid_identity_value(value):
            raise ValueError("invalid_cron_identity")
        return value


class SnapshotRequest(ReportSource):
    business_date: date


class DeliveryRequest(ReportSource):
    report_id: UUID
    delivery_id: UUID
    part_no: int = Field(strict=True, ge=1)
    recipient_id: str = Field(strict=True, pattern=r"^[0-9]{8}$")
    text: str = Field(min_length=1, max_length=12000)


def report_channel_inbound(_request) -> NoReturn:
    raise RuntimeError("cron_report_channel_is_outbound_only")


def report_channel(source):
    tenant_id = resolve_runtime_tenant_id("default", source)
    path = get_tenant_storage_config_path(tenant_id)
    if not path.is_file():
        raise FileNotFoundError("cron_report_source_template_unavailable")
    config = load_agent_config("default", config_path=path).channels.zhaohu
    return ZhaohuChannel.from_config(report_channel_inbound, config)


@router.post("/snapshot")
async def snapshot(body: SnapshotRequest):
    try:
        return await get_monitor_sync_client().fetch_batch_status_report(
            body.source_id,
            body.business_date.isoformat(),
        )
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("批调度简报取数失败 error_type=%s", type(exc).__name__)
        raise HTTPException(503, "cron_report_snapshot_unavailable") from exc


async def send_part_once(body):
    try:
        channel = await asyncio.wait_for(
            asyncio.to_thread(report_channel, body.source_id),
            5,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        AttributeError,
        asyncio.TimeoutError,
    ):
        return {
            "status": "failed",
            "error_code": "cron_report_sender_unavailable",
        }
    return await asyncio.wait_for(
        channel.send_batch_status_report(
            body.recipient_id,
            body.text,
            str(body.delivery_id),
        ),
        18,
    )


@router.post("/send-part")
async def send_part(body: DeliveryRequest):
    try:
        return await send_part_once(body)
    except Exception as exc:
        logger.warning(
            "批调度简报投递结果未知 report_id=%s delivery_id=%s error_type=%s",
            body.report_id,
            body.delivery_id,
            type(exc).__name__,
        )
        return {
            "status": "outcome_unknown",
            "error_code": "cron_report_delivery_unknown",
        }

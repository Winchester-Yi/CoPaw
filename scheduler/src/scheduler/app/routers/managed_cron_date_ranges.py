# -*- coding: utf-8 -*-
"""外部平台主动调用的单次日期维护，不创建内部周期任务。"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .managed_cron_controls import control_service
from ..services.cron.managed.date_ranges import DateMaintenanceService
from ..services.cron.managed.diagnostics import format_management_error
from ..services.cron.managed.identity import identity_value

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/scheduler/cron/date-ranges",
    tags=["cron-date-ranges"],
)


class ApplyDatesRequest(BaseModel):
    """平台不能传用户 Header；只允许选取一个来源，不接收历史日期。"""

    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=128)

    @field_validator("source_id")
    @classmethod
    def validate_source(cls, value):
        return identity_value(value)


def date_service(controls=Depends(control_service)):
    return DateMaintenanceService(controls.db, controls.platform)


async def _apply_background(source_id, service):
    """只处理本次请求，后台失败写日志，不保存调用或自动恢复进程中断。"""
    try:
        result = await service.apply(source_id)
    except Exception as error:
        logger.error(
            "日期维护后台执行失败 source_id=%s\n%s",
            source_id,
            format_management_error(error),
        )
        return
    summary = {
        name: result.get(name)
        for name in (
            "evaluated_date",
            "preopen_date",
            "scanned_count",
            "date_paused_count",
            "date_released_count",
            "platform_updated_count",
            "failed_count",
            "failures_truncated",
        )
    }
    logger.log(
        logging.WARNING if result.get("failed_count") else logging.INFO,
        "日期维护后台执行完成 source_id=%s result=%s",
        source_id,
        summary,
    )


@router.post(
    "/apply",
    status_code=202,
    summary="异步受理日期维护，保留当日入口并预开次日入口",
)
async def apply_dates(
    body: ApplyDatesRequest,
    background_tasks: BackgroundTasks,
    service=Depends(date_service),
):
    """202 仅表示受理；结果见后台日志，无用户 Header、进度接口或持久调用。"""
    background_tasks.add_task(_apply_background, body.source_id, service)
    logger.info("日期维护异步已受理 source_id=%s", body.source_id)
    return {"accepted": True, "source_id": body.source_id}

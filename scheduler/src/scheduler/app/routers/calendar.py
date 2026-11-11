# -*- coding: utf-8 -*-
"""为 SWE 和 Scheduler 提供可刷新日期缓存的内部日历接口。"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from ..services.cron.calendar_schedule import (
    allows,
    next_run_times,
    _MemoCalendar,
    validate_cron,
)
from ..services.cron.workday_calendar import get_calendar_service

router = APIRouter(prefix="/scheduler/cron/calendar", tags=["cron-calendar"])


class CalendarRequest(BaseModel):
    """不接收用户任务写入，只声明规则、带时区时间和有限预览数量。"""

    cron: str = Field(default="", max_length=255)
    timezone: str = "Asia/Shanghai"
    at: datetime | None = None
    run_on_non_workday: bool = False
    count: int = Field(default=0, ge=0, le=3)

    @field_validator("cron")
    @classmethod
    def validate_expression(cls, value):
        """日期查询可以没有表达式，但提供表达式时必须合法。"""
        return validate_cron(value) if value else ""

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value):
        """即使只查日期也拒绝未知时区，避免预览和准入口径分裂。"""
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("invalid calendar timezone") from exc
        return value

    @field_validator("at")
    @classmethod
    def require_timestamp_timezone(cls, value):
        """显式时间必须带偏移，不能受服务实例时区配置影响。"""
        if value is not None and value.tzinfo is None:
            raise ValueError("at must include a timezone offset")
        return value


@router.post("/resolve")
async def resolve_calendar(body: CalendarRequest):
    """查询日期与未来时间，只允许写日历缓存，不修改任务业务数据。"""
    at = body.at or datetime.now(timezone.utc)
    calendar = _MemoCalendar(get_calendar_service())
    try:
        result = await allows(
            body.cron,
            at,
            calendar,
            run_on_non_workday=body.run_on_non_workday,
        )
        if body.count:
            if not body.cron:
                raise ValueError("cron is required for preview")
            result["next_run_times"] = await next_run_times(
                body.cron,
                body.timezone,
                calendar,
                run_on_non_workday=body.run_on_non_workday,
                count=body.count,
                now=at,
            )
            result["preview_source"] = (
                "fallback"
                if any(
                    value["source"] == "fallback"
                    for value in calendar.days.values()
                )
                else "api"
            )
        return result
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc

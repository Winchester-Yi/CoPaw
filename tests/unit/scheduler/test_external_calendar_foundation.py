# -*- coding: utf-8 -*-
"""独立查询提交所需日历底座，不依赖系统任务或用户执行链路迁移。"""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from scheduler.app.services.cron.calendar_schedule import next_run_times
from scheduler.app.services.cron.workday_calendar import CalendarService


class Store:
    def __init__(self):
        self.rows = {}

    async def get(self, day, now):
        row = self.rows.get(day)
        return row if row and row["expires_at"] > now else None

    async def claim(self, day, token, now):
        return True

    async def save(self, day, result, token, now):
        self.rows[day] = result


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 1])
async def test_authoritative_date_is_cached_for_one_day(value):
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    service = CalendarService(
        store=Store(),
        fetcher=AsyncMock(return_value=value),
        clock=lambda: now,
    )
    first = await service.get_day(date(2026, 10, 1))
    assert first["source"] == "api"
    assert first["is_workday"] is bool(value)
    assert first["expires_at"] == now + timedelta(days=1)
    assert await service.get_day(date(2026, 10, 1)) == first


@pytest.mark.asyncio
async def test_unknown_date_has_explicit_short_lived_weekday_fallback():
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    service = CalendarService(
        store=Store(),
        fetcher=AsyncMock(side_effect=TimeoutError()),
        clock=lambda: now,
    )
    result = await service.get_day(date(2026, 10, 10))
    assert result["is_workday"] is False
    assert result["source"] == "fallback"
    assert result["expires_at"] == now + timedelta(minutes=5)


@pytest.mark.asyncio
async def test_known_holiday_is_not_lost_when_cache_write_fails():
    store = Store()
    store.save = AsyncMock(side_effect=ConnectionError())
    result = await CalendarService(
        store=store,
        fetcher=AsyncMock(return_value=0),
    ).get_day(date(2026, 10, 1))
    assert result["source"] == "api" and result["is_workday"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expression,expected",
    [
        ("0 10 LW * *", "2026-09-29T10:00:00+08:00"),
        ("0 10 15W * *", "2026-09-14T10:00:00+08:00"),
    ],
)
async def test_special_preview_uses_holidays_and_backward_workday(
    expression,
    expected,
):
    class Calendar:
        async def get_day(self, day):
            return {
                "is_workday": day.weekday() < 5 and day.day not in {15, 30},
                "source": "api",
            }

    result = await next_run_times(
        expression,
        "Asia/Shanghai",
        Calendar(),
        count=1,
        now=datetime.fromisoformat("2026-09-01T00:00:00+08:00"),
    )
    assert [at.isoformat() for at in result] == [expected]

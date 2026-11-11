# -*- coding: utf-8 -*-
"""合法无候选不破坏列表，预览预算不改变实际触发的日历规则。"""

import asyncio
from datetime import datetime

import pytest

from scheduler.app.services.cron import calendar_schedule


class FallbackCalendar:
    def __init__(self, slow=False):
        self.calls = 0
        self.slow = slow

    async def get_day(self, day):
        self.calls += 1
        if self.slow:
            await asyncio.sleep(0.02)
        return {"is_workday": day.weekday() < 5, "source": "fallback"}


@pytest.mark.asyncio
async def test_valid_sunday_rule_with_no_eligible_dates_returns_empty_preview():
    result = await calendar_schedule.next_run_times(
        "0 9 * * sun",
        "Asia/Shanghai",
        FallbackCalendar(),
        now=datetime.fromisoformat("2026-10-20T00:00:00+08:00"),
    )
    assert result == []


@pytest.mark.asyncio
async def test_preview_stops_repeated_slow_upstream_lookups(monkeypatch):
    monkeypatch.setattr(calendar_schedule, "PREVIEW_BUDGET_SECONDS", 0.005)
    service = FallbackCalendar(slow=True)
    result = await asyncio.wait_for(
        calendar_schedule.next_run_times(
            "0 9 * * sun",
            "Asia/Shanghai",
            service,
            now=datetime.fromisoformat("2026-10-20T00:00:00+08:00"),
        ),
        timeout=1,
    )
    assert result == []
    assert service.calls <= 1


@pytest.mark.asyncio
async def test_same_page_reuses_even_an_empty_preview(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(calendar_schedule, "PREVIEW_BUDGET_SECONDS", 0)
    service = FallbackCalendar()
    memo = calendar_schedule._MemoCalendar(service)
    now = datetime.fromisoformat("2026-10-20T00:00:00+08:00")
    assert (
        await calendar_schedule.next_run_times(
            "0 9 * * sun",
            "Asia/Shanghai",
            memo,
            now=now,
        )
        == []
    )
    traversal = AsyncMock(wraps=calendar_schedule._regular_runs)
    monkeypatch.setattr(calendar_schedule, "_regular_runs", traversal)
    for _ in range(200):
        assert (
            await calendar_schedule.next_run_times(
                "0 9 * * sun",
                "Asia/Shanghai",
                memo,
                now=now,
            )
            == []
        )
    traversal.assert_not_called()
    assert service.calls == 0


@pytest.mark.asyncio
async def test_preview_budget_does_not_apply_to_actual_admission(monkeypatch):
    monkeypatch.setattr(calendar_schedule, "PREVIEW_BUDGET_SECONDS", 0)

    class AdjustedSunday:
        async def get_day(self, day):
            return {"is_workday": True, "source": "api"}

    result = await calendar_schedule.allows(
        "0 9 * * sun",
        datetime.fromisoformat("2026-10-25T09:00:00+08:00"),
        AdjustedSunday(),
        run_on_non_workday=False,
    )
    assert result["allowed"] is True
    assert result["source"] == "api"


@pytest.mark.asyncio
async def test_budget_retains_completed_authoritative_day_query(monkeypatch):
    monkeypatch.setattr(calendar_schedule, "PREVIEW_BUDGET_SECONDS", 0.005)

    class SlowHoliday:
        async def get_day(self, day):
            await asyncio.sleep(0.02)
            return {"is_workday": False, "source": "api"}

    memo = calendar_schedule._MemoCalendar(SlowHoliday())
    result = await calendar_schedule.next_run_times(
        "0 9 * * *",
        "Asia/Shanghai",
        memo,
        count=1,
        now=datetime.fromisoformat("2026-10-20T00:00:00+08:00"),
    )
    # 已观测到的20日是休息日，不能因展示预算到期又用星期近似覆盖它。
    assert result[0].isoformat() == "2026-10-21T09:00:00+08:00"
    assert next(iter(memo.days.values()))["source"] == "api"


@pytest.mark.asyncio
async def test_valid_calendar_rule_without_workdays_returns_empty(monkeypatch):
    monkeypatch.setattr(calendar_schedule, "PREVIEW_BUDGET_SECONDS", 60)

    class NoWorkdays:
        async def get_day(self, day):
            return {"is_workday": False, "source": "api"}

    assert (
        await calendar_schedule.next_run_times(
            "0 9 LW * *",
            "Asia/Shanghai",
            NoWorkdays(),
            now=datetime.fromisoformat("2026-10-20T00:00:00+08:00"),
        )
        == []
    )


@pytest.mark.asyncio
async def test_empty_preview_does_not_break_other_jobs_in_same_page():
    from scheduler.app.services.cron.managed.contracts import QueryOptions
    from scheduler.app.services.cron.managed.identity import CallerIdentity
    from scheduler.app.services.cron.managed.repository import (
        ManagedCronRepository,
    )
    from tests.unit.scheduler.test_managed_cron_queries import QueryDatabase

    db = QueryDatabase()
    try:
        db.add("sunday")
        db.add("weekday")
        db.conn.execute(
            "UPDATE swe_cron_jobs_v2 SET cron_expr='0 9 * * sun' WHERE job_id='sunday'",
        )
        db.conn.commit()
        repository = ManagedCronRepository(
            db,
            calendar=FallbackCalendar(),
            clock=lambda: datetime.fromisoformat("2026-10-20T00:00:00+08:00"),
        )
        response = await repository.page(
            CallerIdentity("alice", "bank", "default", "alice"),
            QueryOptions(),
        )
        jobs = {item["id"]: item for item in response["items"]}
        assert jobs["sunday"]["state"]["next_run_times"] == []
        assert len(jobs["weekday"]["state"]["next_run_times"]) == 3
        assert db.writes == 0
    finally:
        db.conn.close()

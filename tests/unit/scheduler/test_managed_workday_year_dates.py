# -*- coding: utf-8 -*-
"""日历迁入必须保留当前年份输入及任务独立的假期执行设置。"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scheduler.app.services.cron.managed.definition import _schedule


def test_new_schedule_defaults_to_skip_non_workdays():
    value = _schedule({"cron": "0 9 * * * 2027"})
    assert value.get("run_on_non_workday", "missing") is False
    assert value["cron"] == "0 9 * * * 2027"


def test_new_schedule_preserves_explicit_holiday_execution():
    value = _schedule({"cron": "0 9 * * *", "run_on_non_workday": True})
    assert value.get("run_on_non_workday") is True


@pytest.mark.parametrize("value", ["false", 0, None])
def test_calendar_flag_rejects_non_boolean_values(value):
    with pytest.raises(ValueError, match="invalid_cron_schedule"):
        _schedule({"cron": "0 9 * * *", "run_on_non_workday": value})


@pytest.mark.parametrize("year", ["2027", "2026,2027", "2026-2029"])
@pytest.mark.parametrize("day", ["LW", "15W"])
def test_calendar_day_and_business_year_can_be_saved_together(day, year):
    expression = f"0 9 {day} * * {year}"
    assert _schedule({"cron": expression})["cron"] == expression


def test_swe_preparation_accepts_calendar_and_year_without_changing_legacy():
    from swe.app.crons.managed_models import ManagedScheduleSpec
    from swe.app.crons.models import ScheduleSpec

    assert (
        ManagedScheduleSpec(cron="0 9 LW * * 2027").cron == "0 9 LW * * 2027"
    )
    with pytest.raises(ValueError):
        ScheduleSpec(cron="0 9 * * * 2027")


def test_system_tasks_do_not_silently_drop_user_year_field():
    from scheduler.app.services.cron.system.configuration import (
        normalize_config,
    )
    from scheduler.app.services.cron.system.platform import (
        system_platform_cron,
    )

    with pytest.raises(ValueError, match="invalid_system_cron"):
        normalize_config({"enabled": True, "cron": "0 9 * * * 2027"})
    with pytest.raises(ValueError, match="invalid_system_cron"):
        system_platform_cron("0 9 * * * 2027")


@pytest.mark.parametrize("day", ["LW", "15W"])
def test_system_calendar_fields_keep_original_platform_semantics(day):
    from scheduler.app.services.cron.system.configuration import (
        normalize_config,
    )
    from scheduler.app.services.cron.system.platform import (
        system_platform_cron,
    )

    config = normalize_config({"enabled": True, "cron": f"0 9 {day} * *"})
    assert system_platform_cron(config["cron"]) == f"0 0 9 {day} * ?"


class BusinessCalendar:
    async def get_day(self, day):
        overrides = {"2026-11-06": False, "2026-11-07": True}
        return {
            "date": day.isoformat(),
            "is_workday": overrides.get(day.isoformat(), day.weekday() < 5),
            "source": "api",
        }


@pytest.mark.asyncio
async def test_last_workday_preview_keeps_future_year():
    from scheduler.app.services.cron.calendar_schedule import next_run_times

    values = await next_run_times(
        "0 9 LW * * 2027",
        "Asia/Shanghai",
        BusinessCalendar(),
        now=datetime(2026, 11, 4, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert [value.isoformat() for value in values] == [
        "2027-01-29T09:00:00+08:00",
        "2027-02-26T09:00:00+08:00",
        "2027-03-31T09:00:00+08:00",
    ]


@pytest.mark.asyncio
async def test_expired_calendar_year_has_no_preview():
    from scheduler.app.services.cron.calendar_schedule import next_run_times

    assert (
        await next_run_times(
            "0 9 LW * * 2025",
            "Asia/Shanghai",
            BusinessCalendar(),
            now=datetime(2026, 11, 4, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        == []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("advance,expected_hour", [(0, "09"), (240, "05")])
async def test_managed_preview_intersects_holiday_year_and_actual_date_range(
    advance,
    expected_hour,
):
    from scheduler.app.services.cron.managed.calendar_views import _preview

    expression = "0 9 * * * 2026"
    row = {
        "cron_expr": expression,
        "timezone": "Asia/Shanghai",
        "run_start_date": "2026-11-06",
        "run_end_date": "2026-11-07",
        "spec_json": json.dumps(
            {
                "schedule": {"run_on_non_workday": False},
                "meta": {
                    "broadcast_original_cron": expression,
                    "broadcast_offset_minutes": advance,
                },
            },
        ),
    }
    times = await _preview(
        row,
        BusinessCalendar(),
        datetime(2026, 11, 4, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert times == [f"2026-11-07T{expected_hour}:00:00+08:00"]


@pytest.mark.asyncio
async def test_external_query_does_not_reuse_preview_across_different_date_ranges():
    from scheduler.app.services.cron.managed.external_query import (
        ExternalManagedJobs,
    )
    from tests.unit.scheduler.test_managed_cron_queries import QueryDatabase

    db = QueryDatabase()
    try:
        for name, end in [("short", "2026-11-07"), ("long", "2026-11-09")]:
            db.add(name)
            row = db.conn.execute(
                "SELECT spec_json FROM swe_cron_jobs_v2 WHERE job_id=?",
                (name,),
            ).fetchone()
            spec = json.loads(row[0])
            spec["run_date_range"] = {
                "start_date": "2026-11-06",
                "end_date": end,
            }
            db.conn.execute(
                "UPDATE swe_cron_jobs_v2 SET spec_json=?,run_start_date=?,run_end_date=? WHERE job_id=?",
                (json.dumps(spec), "2026-11-06", end, name),
            )
        items = await ExternalManagedJobs(
            db,
            calendar=BusinessCalendar(),
            clock=lambda: datetime(
                2026,
                11,
                4,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ),
        ).list("alice", "bank", "default")
        by_id = {item["id"]: item["state"]["next_run_times"] for item in items}
        assert [value.isoformat() for value in by_id["short"]] == [
            "2026-11-07T09:00:00+08:00",
        ]
        assert [value.isoformat() for value in by_id["long"]] == [
            "2026-11-07T09:00:00+08:00",
            "2026-11-09T09:00:00+08:00",
        ]
    finally:
        db.conn.close()

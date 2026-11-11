# -*- coding: utf-8 -*-
"""nW包含锚点、仅向当月前方查找，执行和预览采用同一规则。"""

import json
from datetime import datetime

import pytest

from scheduler.app.services.cron.calendar_schedule import (
    allows,
    next_run_times,
)
from scheduler.app.services.cron.managed.calendar_views import _preview
from scheduler.app.services.cron.managed.scheduling import (
    ManagedSchedulingService,
)
from tests.unit.scheduler.test_managed_cron_callbacks import (
    callback_setup,
    body,
)
from tests.unit.scheduler.test_managed_cron_calendar_callbacks import (
    Calendar,
    create,
)


class Workdays:
    async def get_day(self, day):
        return {
            "date": day.isoformat(),
            "is_workday": day.weekday() < 5,
            "source": "api",
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expression,now,expected",
    [
        (
            "0 9 15W 11 * 2026",
            "2026-11-01T00:00:00+08:00",
            ["2026-11-13T09:00:00+08:00"],
        ),
        (
            "0 9 15W * * 2026",
            "2026-11-14T00:00:00+08:00",
            ["2026-12-15T09:00:00+08:00"],
        ),
        (
            "0 9 16W 11 * 2026",
            "2026-11-01T00:00:00+08:00",
            ["2026-11-16T09:00:00+08:00"],
        ),
        ("0 9 1W 11 * 2026", "2026-11-01T00:00:00+08:00", []),
        (
            "0 9 1W * * 2026",
            "2026-11-01T00:00:00+08:00",
            ["2026-12-01T09:00:00+08:00"],
        ),
        (
            "0 9 31W 2 * 2027",
            "2027-02-01T00:00:00+08:00",
            ["2027-02-26T09:00:00+08:00"],
        ),
        (
            "0 9 31W 2 * 2028",
            "2028-02-01T00:00:00+08:00",
            ["2028-02-29T09:00:00+08:00"],
        ),
        (
            "0 9 LW 11 * 2026",
            "2026-11-01T00:00:00+08:00",
            ["2026-11-30T09:00:00+08:00"],
        ),
    ],
)
async def test_preview_searches_back_including_anchor_without_crossing_month(
    expression,
    now,
    expected,
):
    values = await next_run_times(
        expression,
        "Asia/Shanghai",
        Workdays(),
        count=1,
        now=datetime.fromisoformat(now),
    )
    assert [value.isoformat() for value in values] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_non_workday", [False, True])
async def test_calendar_rule_cannot_move_forward_even_when_holidays_are_enabled(
    allow_non_workday,
):
    for day, expected in [(13, True), (15, False), (16, False)]:
        result = await allows(
            "0 9 15W 11 * 2026",
            datetime.fromisoformat(f"2026-11-{day}T09:00:00+08:00"),
            Workdays(),
            run_on_non_workday=allow_non_workday,
        )
        assert result["allowed"] is expected


@pytest.mark.asyncio
async def test_actual_workday_calendar_including_makeup_days_controls_backward_search():
    class MakeupCalendar:
        async def get_day(self, day):
            return {
                "date": day.isoformat(),
                "is_workday": day.day in {14, 16},
                "source": "api",
            }

    values = await next_run_times(
        "0 9 15W 11 * 2026",
        "Asia/Shanghai",
        MakeupCalendar(),
        count=1,
        now=datetime.fromisoformat("2026-11-01T00:00:00+08:00"),
    )
    assert [value.isoformat() for value in values] == [
        "2026-11-14T09:00:00+08:00",
    ]


@pytest.mark.asyncio
async def test_date_range_cannot_replace_an_earlier_selected_day_with_a_later_day():
    expression = "0 9 15W 11 * 2026"
    row = {
        "cron_expr": expression,
        "timezone": "Asia/Shanghai",
        "run_start_date": "2026-11-15",
        "run_end_date": "2026-11-30",
        "spec_json": json.dumps(
            {
                "schedule": {"cron": expression, "run_on_non_workday": False},
                "meta": {},
            },
        ),
    }
    values = await _preview(
        row,
        Workdays(),
        datetime.fromisoformat("2026-11-01T00:00:00+08:00"),
    )
    assert values == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,kind",
    [("ordinary", "ordinary"), ("batch_enabled", "batch")],
)
@pytest.mark.parametrize("day,accepted", [(13, True), (16, False)])
async def test_real_callback_accepts_previous_workday_and_rejects_later_workday(
    callback_setup,
    mode,
    kind,
    day,
    accepted,
):
    db, caller, _ = callback_setup
    created = await create(
        callback_setup,
        mode,
        expression="0 9 15W 11 * 2026",
    )
    command = body(db, created, kind=kind)
    command["scheduled_fire_at"] = f"2026-11-{day}T09:00:00+08:00"
    service = ManagedSchedulingService(
        db,
        calendar=Calendar(db),
        clock=lambda: datetime.fromisoformat(command["scheduled_fire_at"]),
    )
    response = await service.accept(caller, command, kind)
    assert response["accepted"] is accepted
    assert db.conn.execute(
        "SELECT COUNT(*) FROM swe_cron_execution_runs",
    ).fetchone()[0] == int(accepted)
    if not accepted:
        assert response["reason"] == "calendar_rule"

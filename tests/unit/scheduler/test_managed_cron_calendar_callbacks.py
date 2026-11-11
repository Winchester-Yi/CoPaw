# -*- coding: utf-8 -*-
"""自动回调按逻辑业务日准入，批内成员继承冻结结论。"""

from datetime import date, datetime
import json

import pytest

from scheduler.app.services.cron.managed.scheduling import (
    ManagedSchedulingService,
)
from tests.unit.scheduler.test_managed_cron_callbacks import (
    callback_setup,
    body,
)


class Calendar:
    """日历访问必须在任务事务之外，九月三十日模拟为假期。"""

    def __init__(self, db):
        self.db = db
        self.days = []

    async def get_day(self, day):
        assert not self.db.conn.in_transaction
        self.days.append(day)
        return {
            "is_workday": day.weekday() < 5 and day != date(2026, 9, 30),
            "source": "api",
            "date": day.isoformat(),
        }


async def create(callback_setup, mode, *, expression="0 9 * * *", allow=None):
    """创建真实管理记录和平台引用，不直接伪造回调授权。"""
    _, caller, management = callback_setup
    schedule = {"cron": expression, "timezone": "Asia/Shanghai"}
    if allow is not None:
        schedule["run_on_non_workday"] = allow
    return await management.create(
        caller,
        {
            "request_id": "calendar-source",
            "dispatch_state": mode,
            "spec": {
                "name": "日历任务",
                "task_type": "text",
                "text": "body",
                "schedule": schedule,
            },
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,kind",
    [("ordinary", "ordinary"), ("batch_enabled", "batch")],
)
async def test_missing_switch_skips_non_workday_without_enqueuing(
    callback_setup,
    mode,
    kind,
):
    db, caller, _ = callback_setup
    created = await create(callback_setup, mode)
    command = body(db, created, kind=kind)
    command["scheduled_fire_at"] = "2026-10-17T09:00:00+08:00"
    calendar = Calendar(db)
    service = ManagedSchedulingService(
        db,
        calendar=calendar,
        clock=lambda: datetime.fromisoformat(command["scheduled_fire_at"]),
    )

    result = await service.accept(caller, command, kind)

    assert result["status"] == "skipped" and result["reason"] == "non_workday"
    assert not result.get("batch_id")
    assert (
        db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 0
    )
    receipt = db.conn.execute(
        "SELECT status FROM swe_cron_managed_trigger_receipts",
    ).fetchone()[0]
    assert receipt == "skipped"
    assert calendar.days == [date(2026, 10, 17)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,mode",
    [("ordinary", "ordinary"), ("batch", "batch_enabled")],
)
async def test_explicit_override_keeps_independent_callbacks_and_freezes_decision(
    callback_setup,
    kind,
    mode,
):
    db, caller, _ = callback_setup
    created = await create(callback_setup, mode, allow=True)
    command = body(db, created, kind=kind)
    command["scheduled_fire_at"] = "2026-10-17T09:00:00+08:00"
    calendar = Calendar(db)
    service = ManagedSchedulingService(
        db,
        calendar=calendar,
        clock=lambda: datetime.fromisoformat(command["scheduled_fire_at"]),
    )

    first = await service.accept(caller, command, kind)
    second = await service.accept(caller, command, kind)

    assert first["accepted"] and second["accepted"] and not second["reused"]
    runs = db.conn.execute(
        "SELECT spec_json, dispatch_meta_json FROM swe_cron_execution_runs",
    ).fetchall()
    assert len(runs) == 2
    for spec, metadata in runs:
        assert json.loads(spec)["schedule"]["run_on_non_workday"] is True
        admission = json.loads(metadata)["calendar_admission"]
        assert admission["business_date"] == "2026-10-17"
        assert admission["source"] == "api" and admission["allowed"]
        assert admission["definition_version"] == 1


@pytest.mark.asyncio
async def test_calendar_month_and_last_workday_gate_use_business_expression(
    callback_setup,
):
    db, caller, _ = callback_setup
    created = await create(callback_setup, "ordinary", expression="0 9 LW 9 *")
    command = body(db, created)
    command["scheduled_fire_at"] = "2026-09-29T09:00:00+08:00"
    calendar = Calendar(db)
    service = ManagedSchedulingService(
        db,
        calendar=calendar,
        clock=lambda: datetime.fromisoformat(command["scheduled_fire_at"]),
    )
    result = await service.accept(caller, command, "ordinary")
    assert result["accepted"]
    assert calendar.days.count(date(2026, 9, 29)) == 1
    command["scheduled_fire_at"] = "2026-10-30T09:00:00+08:00"
    service.clock = lambda: datetime.fromisoformat(
        command["scheduled_fire_at"],
    )
    before = list(calendar.days)
    skipped = await service.accept(caller, command, "ordinary")
    assert skipped["reason"] == "calendar_rule"
    assert calendar.days == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "supplied",
    [None, "2026-10-17T09:00:00", "not-a-time"],
)
async def test_untrusted_trigger_time_uses_received_beijing_date(
    callback_setup,
    supplied,
):
    """固定Body或无时区时间不能被当成平台原周期。"""
    db, caller, _ = callback_setup
    created = await create(callback_setup, "ordinary")
    command = body(db, created)
    if supplied is None:
        command.pop("scheduled_fire_at")
    else:
        command["scheduled_fire_at"] = supplied
    now = datetime.fromisoformat("2026-10-20T09:05:00+08:00")
    result = await ManagedSchedulingService(
        db,
        clock=lambda: now,
        calendar=Calendar(db),
    ).accept(caller, command, "ordinary")
    assert result["accepted"]
    assert result["trigger_time_basis"] == "callback_received_at"
    record = json.loads(
        db.conn.execute(
            "SELECT dispatch_meta_json FROM swe_cron_execution_runs",
        ).fetchone()[0],
    )
    assert record["calendar_admission"]["business_date"] == "2026-10-20"
    assert record["calendar_admission"]["time_basis"] == "callback_received_at"

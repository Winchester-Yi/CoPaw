# -*- coding: utf-8 -*-
"""新版任务日历字段、异步预览与平台候选必须保持同一规则。"""

from datetime import date, datetime
import json

import pytest

from scheduler.app.services.cron.managed.definition import normalize_spec
from scheduler.app.services.cron.managed.identity import CallerIdentity
from scheduler.app.services.cron.managed.repository import (
    ManagedCronRepository,
)
from scheduler.app.services.cron.managed.platform import platform_cron
from tests.unit.scheduler.test_managed_cron_management import create_payload
from tests.unit.scheduler.test_managed_cron_management import setup
from tests.unit.scheduler.test_managed_cron_queries import QueryDatabase

CALLER = CallerIdentity("alice", "bank", "default", "alice")


class Calendar:
    """标记指定假期，并核对观察发生在任务事务之外。"""

    def __init__(self, db=None):
        self.db = db
        self.days = []

    async def get_day(self, day):
        if self.db is not None:
            assert not self.db.conn.in_transaction
        self.days.append(day)
        return {
            "date": day.isoformat(),
            "is_workday": day.weekday() < 5 and day != date(2026, 9, 30),
            "source": "api",
        }


@pytest.mark.parametrize("value", [False, True])
def test_definition_preserves_explicit_calendar_switch(value):
    raw = create_payload()["spec"]
    raw["schedule"]["run_on_non_workday"] = value
    spec = normalize_spec(raw, CALLER, "calendar-job")
    assert spec["schedule"]["run_on_non_workday"] is value


def test_definition_defaults_missing_calendar_switch_to_false():
    spec = normalize_spec(create_payload()["spec"], CALLER, "calendar-job")
    assert spec["schedule"]["run_on_non_workday"] is False


@pytest.mark.parametrize("value", ["false", 0, None])
def test_definition_rejects_ambiguous_calendar_switch(value):
    raw = create_payload()["spec"]
    raw["schedule"]["run_on_non_workday"] = value
    with pytest.raises(ValueError, match="invalid_cron_schedule"):
        normalize_spec(raw, CALLER, "calendar-job")


@pytest.mark.parametrize("expression", ["0 9 LW * *", "0 9 15W 6 *"])
def test_definition_keeps_calendar_business_expression(expression):
    raw = create_payload()["spec"]
    raw["schedule"]["cron"] = expression
    assert (
        normalize_spec(raw, CALLER, "calendar-job")["schedule"]["cron"]
        == expression
    )


def test_platform_expands_calendar_business_expression_to_daily_candidates():
    assert platform_cron("0 9 LW 6 *") == "0 0 9 * * ?"


@pytest.mark.asyncio
async def test_query_preview_is_calendar_aware_without_task_writes():
    db = QueryDatabase()
    try:
        db.add("calendar-job")
        raw = json.loads(
            db.conn.execute(
                "SELECT spec_json FROM swe_cron_jobs_v2",
            ).fetchone()[0],
        )
        raw["schedule"] = {
            "type": "cron",
            "cron": "0 9 LW * *",
            "timezone": "Asia/Shanghai",
            "run_on_non_workday": False,
        }
        db.conn.execute(
            "UPDATE swe_cron_jobs_v2 SET cron_expr='0 9 LW * *', spec_json=?",
            (json.dumps(raw),),
        )
        db.conn.commit()
        calendar = Calendar(db)
        repo = ManagedCronRepository(
            db,
            calendar=calendar,
            clock=lambda: datetime.fromisoformat("2026-09-01T00:00:00+08:00"),
        )
        detail = await repo.detail(CALLER, "calendar-job")
        assert detail["spec"]["schedule"]["run_on_non_workday"] is False
        assert (
            detail["state"]["next_run_times"][0] == "2026-09-29T09:00:00+08:00"
        )
        assert db.writes == 0
        assert len(calendar.days) == len(set(calendar.days))
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_existing_managed_task_without_switch_skips_weekends_in_preview():
    db = QueryDatabase()
    try:
        db.add("existing")
        repo = ManagedCronRepository(
            db,
            calendar=Calendar(),
            clock=lambda: datetime.fromisoformat("2026-10-16T10:00:00+08:00"),
        )
        result = await repo.detail(CALLER, "existing")
        assert result["spec"]["schedule"]["run_on_non_workday"] is False
        assert (
            result["state"]["next_run_times"][0] == "2026-10-19T09:00:00+08:00"
        )
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_child_preview_uses_logical_workday_before_subtracting_advance():
    """周日提前执行的子任务属于周一业务日，不按物理日期误跳过。"""
    db = QueryDatabase()
    try:
        db.add("child")
        raw = json.loads(
            db.conn.execute(
                "SELECT spec_json FROM swe_cron_jobs_v2",
            ).fetchone()[0],
        )
        raw["schedule"]["cron"] = "0 21 * * sun"
        raw["meta"].update(
            broadcast_original_cron="0 1 * * mon",
            broadcast_original_timezone="Asia/Shanghai",
            broadcast_offset_minutes=240,
        )
        db.conn.execute(
            "UPDATE swe_cron_jobs_v2 SET cron_expr='0 21 * * sun', spec_json=?",
            (json.dumps(raw),),
        )
        db.conn.commit()
        repo = ManagedCronRepository(
            db,
            calendar=Calendar(),
            clock=lambda: datetime.fromisoformat("2026-05-31T20:00:00+08:00"),
        )
        detail = await repo.detail(CALLER, "child")
        assert detail["state"]["next_run_at"] == "2026-05-31T21:00:00+08:00"
        assert db.writes == 0
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_one_list_page_reuses_date_observations_across_jobs():
    """列表不能按每行重复访问日期上游。"""
    from scheduler.app.services.cron.managed.contracts import QueryOptions

    db = QueryDatabase()
    try:
        db.add("one")
        db.add("two")
        calendar = Calendar()
        repo = ManagedCronRepository(
            db,
            calendar=calendar,
            clock=lambda: datetime.fromisoformat("2026-10-20T10:00:00+08:00"),
        )
        page = await repo.page(CALLER, QueryOptions())
        assert len(page["items"]) == 2
        assert len(calendar.days) == 3
        assert len(set(calendar.days)) == 3
        assert db.writes == 0
    finally:
        db.conn.close()


@pytest.mark.asyncio
async def test_compat_projection_keeps_calendar_fields_when_metadata_is_truncated(
    setup,
):
    """旧表兼容投影仍保留真实日历定义，不因元数据限长丢失开关。"""
    db, _, _, caller, management = setup
    created = await management.create(
        caller,
        create_payload(
            schedule={
                "cron": "0 9 LW * *",
                "timezone": "Asia/Shanghai",
                "run_on_non_workday": True,
            },
            meta={"user_note": "x" * 10000},
        ),
    )
    assert created["status"] == "completed"
    meta = json.loads(
        db.conn.execute("SELECT meta FROM swe_cron_jobs").fetchone()[0],
    )
    assert meta["compat_meta_truncated"]
    assert meta["calendar_schedule"] == {
        "type": "cron",
        "cron": "0 9 LW * *",
        "timezone": "Asia/Shanghai",
        "run_on_non_workday": True,
    }

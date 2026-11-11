# -*- coding: utf-8 -*-
"""分发和提前调度不能丢失日历开关，也不能平移业务工作日规则。"""

from datetime import datetime, timezone
import json

import pytest

from scheduler.app.services.cron.managed.distribution import BroadcastStore
from scheduler.app.services.cron.managed.distribution_worker import (
    BroadcastProcessor,
)
from scheduler.app.services.cron.managed.platform import ManagedPlatformClient
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
from tests.unit.scheduler.test_managed_cron_distribution import payload


@pytest.mark.asyncio
async def test_distribution_and_zero_target_retime_keep_non_workday_switch(
    callback_setup,
):
    db, caller, management = callback_setup
    source = await create(callback_setup, "batch_enabled", allow=True)
    store = BroadcastStore(db)
    first = await store.submit(caller, source["job_id"], payload())
    processor = BroadcastProcessor(
        db,
        management.resources,
        management.platform,
    )
    await processor.process(first["task_id"])
    second = await store.submit(
        caller,
        source["job_id"],
        {**payload("retime"), "targets": [], "offset_window_hours": 4},
    )
    await processor.process(second["task_id"])
    rows = db.conn.execute(
        "SELECT spec_json FROM swe_cron_jobs_v2 WHERE parent_job_id IS NOT NULL",
    ).fetchall()
    assert len(rows) == 2
    assert all(
        json.loads(row[0])["schedule"]["run_on_non_workday"] is True
        for row in rows
    )


@pytest.mark.asyncio
async def test_batch_workday_rule_crosses_month_only_in_platform_candidates(
    callback_setup,
):
    db, caller, management = callback_setup
    source = await create(
        callback_setup,
        "batch_enabled",
        expression="0 1 1W 6 *",
    )
    store = BroadcastStore(db)
    task = await store.submit(
        caller,
        source["job_id"],
        {**payload(), "offset_window_hours": 4},
    )
    await BroadcastProcessor(
        db,
        management.resources,
        management.platform,
    ).process(task["task_id"])
    record = await db.fetch_one(
        "SELECT payload_json FROM swe_cron_managed_broadcast_tasks WHERE task_id=%s",
        (task["task_id"],),
    )
    timer = json.loads(record["payload_json"])["timer_sync"]
    assert timer["advance_minutes"] == 240
    assert timer["spec"]["schedule"]["cron"] == "0 1 1W 6 *"
    platform = ManagedPlatformClient(
        {
            "base_url": "http://platform.test",
            "callback_base": "http://scheduler.test",
        },
    )
    operation = {
        **dict(caller.__dict__),
        "operation_id": task["task_id"],
        "job_id": source["job_id"],
    }
    assert (
        platform._payload(operation, timer["spec"], "batch_enabled")["jobCron"]
        == "0 0 21 * * ?"
    )
    command = body(db, source, kind="batch")
    command["operation_id"] = task["task_id"]
    command["scheduled_fire_at"] = "2026-06-01T01:00:00+08:00"
    calendar = Calendar(db)
    now = datetime.fromisoformat("2026-05-31T21:00:00+08:00")
    result = await ManagedSchedulingService(
        db,
        clock=lambda: now,
        calendar=calendar,
    ).accept(caller, command, "batch")
    assert result["accepted"] and result["enqueued_intents"] == 3
    runs = db.conn.execute(
        "SELECT not_before_at, dispatch_meta_json FROM swe_cron_execution_runs",
    ).fetchall()
    for due, metadata in runs:
        assert datetime.fromisoformat(due) == now.astimezone(
            timezone.utc,
        ).replace(tzinfo=None)
        assert (
            json.loads(metadata)["calendar_admission"]["business_date"]
            == "2026-06-01"
        )
    assert all(day.month == 6 for day in calendar.days)

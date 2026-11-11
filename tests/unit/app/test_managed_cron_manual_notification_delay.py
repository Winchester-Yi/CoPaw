# -*- coding: utf-8 -*-
"""新版手动执行不叠加通知延迟，自动任务原计算规则不变。"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from swe.app.crons import managed_history
from tests.unit.scheduler.test_managed_cron_executions import (
    execution_setup,
)

NOW = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def notification_config(monkeypatch):
    monkeypatch.setattr(
        managed_history,
        "get_current_source_system_config",
        lambda: None,
    )
    monkeypatch.setattr(
        managed_history,
        "resolve_cron_notification_config",
        lambda _: SimpleNamespace(skip_weekend_zhaohu_enabled=False),
    )


def fields(kind, task_type, dispatch, meta):
    run = {
        "kind": kind,
        "started_at": NOW - timedelta(hours=3),
        "dispatch_meta_json": json.dumps(dispatch),
    }
    spec = {
        "task_type": task_type,
        "schedule": {"timezone": "Asia/Shanghai"},
        "meta": meta,
    }
    return managed_history.notification_fields(
        run,
        spec,
        SimpleNamespace(status="success"),
        NOW,
    )


@pytest.mark.parametrize("task_type", ["agent", "workflow"])
@pytest.mark.parametrize("policy", ["ordinary", "parent", "offset"])
def test_manual_notification_is_due_at_completion(task_type, policy):
    meta = {"notification_delay_minutes": 120}
    dispatch = {}
    if policy == "parent":
        dispatch["parent_scheduled_fire_at"] = "2026-10-21T09:00:00+08:00"
    elif policy == "offset":
        meta.update(
            broadcast_notification_policy="original_schedule",
            broadcast_offset_minutes=180,
        )
    result = fields("manual", task_type, dispatch, meta)
    assert result["notification_due_at"] == NOW.astimezone(
        ZoneInfo("Asia/Shanghai"),
    ).replace(tzinfo=None)
    # 本次只改到期时间，不把“不延迟”改成“不通知”。
    assert result["notification_status"] == "pending"


@pytest.mark.parametrize(
    "dispatch,extra,expected",
    [
        ({}, {}, "2026-10-20T18:30:00"),
        (
            {"parent_scheduled_fire_at": "2026-10-20T11:00:00+00:00"},
            {},
            "2026-10-20T19:30:00",
        ),
        (
            {},
            {
                "broadcast_notification_policy": "original_schedule",
                "broadcast_offset_minutes": 60,
            },
            "2026-10-20T16:30:00",
        ),
    ],
)
def test_automatic_notification_keeps_existing_time_basis(
    dispatch,
    extra,
    expected,
):
    result = fields(
        "ordinary",
        "agent",
        dispatch,
        {"notification_delay_minutes": 30, **extra},
    )
    assert result["notification_due_at"] == datetime.fromisoformat(expected)
    assert result["notification_status"] == "pending"


@pytest.mark.asyncio
async def test_manual_history_persists_completion_as_notification_due(
    request,
):
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )
    from swe.app.crons.managed_results import ExecutionOutcome, ExecutionWriter
    from tests.unit.scheduler.test_managed_cron_management import (
        create_payload,
    )

    db, caller, management = request.getfixturevalue(execution_setup.__name__)
    created = await management.create(
        caller,
        create_payload(meta={"notification_delay_minutes": 120}),
    )
    run, _ = await ExecutionStore(db).enqueue(
        caller,
        created["job_id"],
        ExecutionRequest("manual", "manual-notification-delay", None),
    )
    key = caller.job_key(created["job_id"])
    writer = ExecutionWriter(db)
    await writer.accept(run["execution_id"], key)
    assert await writer.start(run["execution_id"], key, "owner")
    assert await writer.complete(
        run["execution_id"],
        key,
        "owner",
        ExecutionOutcome("success"),
    )
    row = await db.fetch_one("SELECT * FROM swe_cron_executions")
    assert row["is_manual"] == 1
    assert row["notification_due_at"] == row["end_time"]
    assert row["notification_status"] == "pending"

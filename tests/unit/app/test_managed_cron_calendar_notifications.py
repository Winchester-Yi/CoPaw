# -*- coding: utf-8 -*-
"""新版完成提醒按北京时间原通知日期查日历，不在结果事务中访问网络。"""

from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest

from tests.unit.scheduler.test_managed_cron_executions import execution_setup
from tests.unit.scheduler.test_managed_cron_management import create_payload


async def running(
    execution_setup,
    *,
    kind="ordinary",
    due="2026-09-30T23:50:00+08:00",
):
    """通过真实管理、入队、受理和开始流程取得可提交结果的执行。"""
    from scheduler.app.services.cron.managed.executions import (
        ExecutionRequest,
        ExecutionStore,
    )
    from swe.app.crons.managed_results import ExecutionWriter

    db, caller, management = execution_setup
    created = await management.create(
        caller,
        create_payload(
            schedule={
                "cron": "0 9 * * *",
                "timezone": "America/New_York",
                "run_on_non_workday": True,
            },
            meta={"notification_delay_minutes": 20},
        ),
    )
    run, _ = await ExecutionStore(db).enqueue(
        caller,
        created["job_id"],
        ExecutionRequest(
            kind,
            "notify",
            datetime.fromisoformat(due),
            dispatch_meta={"parent_scheduled_fire_at": due},
        ),
    )
    key = caller.job_key(created["job_id"])
    writer = ExecutionWriter(db)
    await writer.accept(run["execution_id"], key)
    await writer.start(run["execution_id"], key, "owner")
    return writer, run, key


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "working,expected",
    [(False, "not_required"), (True, "pending")],
)
async def test_notification_calendar_runs_outside_result_transaction_and_uses_due_date(
    execution_setup,
    monkeypatch,
    working,
    expected,
):
    from swe.app.crons import calendar_client, managed_history
    from swe.app.crons.managed_results import ExecutionOutcome

    db = execution_setup[0]
    monkeypatch.setattr(
        managed_history,
        "resolve_cron_notification_config",
        lambda _: SimpleNamespace(skip_weekend_zhaohu_enabled=True),
    )
    requests = []

    async def resolve(**kwargs):
        assert not db.conn.in_transaction
        requests.append(kwargs)
        assert (
            kwargs["at"].astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
            == "2026-10-01T00:10:00+08:00"
        )
        return {
            "is_workday": working,
            "allowed": working,
            "source": "api",
            "business_date": "2026-10-01",
        }

    monkeypatch.setattr(calendar_client, "resolve_calendar", resolve)
    writer, run, key = await running(execution_setup)
    assert await writer.complete(
        run["execution_id"],
        key,
        "owner",
        ExecutionOutcome("success", preview="result"),
    )
    assert len(requests) == 1
    row = await db.fetch_one("SELECT * FROM swe_cron_executions")
    assert row["notification_status"] == expected
    assert row["status"] == "success" and row["is_read"] == 0
    state = await db.fetch_one("SELECT * FROM swe_cron_job_states")
    assert state["unread_execution_count"] == 1
    assert state["latest_scheduled_preview"] == "result"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,skip", [("manual", True), ("ordinary", False)])
async def test_manual_or_source_override_does_not_query_notification_calendar(
    execution_setup,
    monkeypatch,
    kind,
    skip,
):
    from swe.app.crons import calendar_client, managed_history
    from swe.app.crons.managed_results import ExecutionOutcome

    monkeypatch.setattr(
        managed_history,
        "resolve_cron_notification_config",
        lambda _: SimpleNamespace(skip_weekend_zhaohu_enabled=skip),
    )
    query = AsyncMock(
        side_effect=AssertionError("calendar must not be queried"),
    )
    monkeypatch.setattr(calendar_client, "resolve_calendar", query)
    writer, run, key = await running(execution_setup, kind=kind)
    assert await writer.complete(
        run["execution_id"],
        key,
        "owner",
        ExecutionOutcome("success"),
    )
    query.assert_not_awaited()
    assert (
        await execution_setup[0].fetch_one(
            "SELECT notification_status FROM swe_cron_executions",
        )
    )["notification_status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("unavailable"), httpx.InvalidURL("invalid address")],
)
async def test_calendar_client_uses_beijing_fallback_and_cooldown(
    monkeypatch,
    error,
):
    from swe.app.crons import calendar_client

    request = AsyncMock(side_effect=error)
    monkeypatch.setattr(calendar_client, "_request_calendar", request)
    monkeypatch.setattr(calendar_client, "_retry_after", 0)
    at = datetime.fromisoformat("2026-10-09T16:30:00+00:00")
    first = await calendar_client.resolve_calendar(at=at)
    second = await calendar_client.resolve_calendar(at=at)
    assert first["business_date"] == "2026-10-10"
    assert first["source"] == "fallback" and not first["is_workday"]
    assert second == first
    request.assert_awaited_once()

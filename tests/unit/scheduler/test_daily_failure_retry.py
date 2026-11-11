# -*- coding: utf-8 -*-
"""覆盖当日补跑选择、去重和无限续跑，执行真实管理及历史链路。"""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from tests.fakes.daily_failure_retry import (
    DailyRetryDatabase,
    DailyRetryScenario,
)
from scheduler.app.services.cron.managed.daily_retry_service import (
    DailyFailureRetry,
)
from scheduler.app.services.cron.managed.daily_retry_enqueue import (
    retry_not_before_at,
)


@pytest.fixture
def scenario(monkeypatch):
    from scheduler.app.services.cron.managed import executions
    from swe.app.crons import managed_results

    now = [datetime(2026, 10, 27, 1)]
    monkeypatch.setattr(executions, "utc_now", lambda: now[0])
    monkeypatch.setattr(managed_results, "utc_now", lambda: now[0])
    db = DailyRetryDatabase()
    value = DailyRetryScenario(db, lambda: now[0])
    value.now = now
    yield value
    db.conn.close()


async def scan(scenario, service=None):
    service = service or DailyFailureRetry(scenario.db, clock=scenario.clock)
    call = await service.calls.submit("bank")
    await service.process(call["call_id"])
    return await service.detail("bank", call["call_id"])


@pytest.mark.asyncio
async def test_new_execution_latest_definition_and_no_fake_history(scenario):
    failed = await scenario.run()
    before = scenario.db.conn.execute(
        "SELECT COUNT(*) FROM swe_cron_executions",
    ).fetchone()[0]
    spec = json.loads(failed["spec_json"])
    spec["text"] = "updated content"
    await scenario.db.execute(
        "UPDATE swe_cron_jobs_v2 SET spec_json=%s,definition_version=2 WHERE job_id=%s",
        (json.dumps(spec), failed["job_id"]),
    )
    result = await scan(scenario)
    assert result["call"]["queued_count"] == 1
    new_id = result["items"][0]["execution"]["execution_id"]
    row = await scenario.db.fetch_one(
        "SELECT * FROM swe_cron_execution_runs WHERE execution_id=%s",
        (new_id,),
    )
    assert row["status"] == "queued"
    expected_due = retry_not_before_at(result["call"], failed)
    actual_due = datetime.fromisoformat(row["not_before_at"]).replace(
        tzinfo=timezone.utc,
    )
    assert actual_due == expected_due
    accepted_at = datetime.fromisoformat(result["call"]["cutoff_at"])
    assert accepted_at <= expected_due < accepted_at + timedelta(hours=1)
    assert json.loads(row["spec_json"])["text"] == "updated content"
    marker = json.loads(row["dispatch_meta_json"])["daily_failure_retry"]
    assert marker["previous_execution_id"] == failed["execution_id"]
    assert marker["retry_count"] == 1
    assert (
        scenario.db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_executions",
        ).fetchone()[0]
        == before
    )
    assert (
        await scenario.db.fetch_one(
            "SELECT last_status FROM swe_cron_job_states",
        )
    )["last_status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("new_status", ["success", "queued"])
async def test_newer_scheduled_success_or_work_blocks_old_failure(
    scenario,
    new_status,
):
    failed = await scenario.run()
    await scenario.run(job_id=failed["job_id"], status=new_status)
    result = await scan(scenario)
    assert result["call"]["queued_count"] == 0
    assert result["items"][0]["reason"] == "cron_daily_retry_not_latest"


@pytest.mark.asyncio
async def test_manual_does_not_mask_failure_and_other_source_is_not_scanned(
    scenario,
):
    failed = await scenario.run()
    await scenario.run(
        job_id=failed["job_id"],
        kind="manual",
        status="success",
    )
    await scenario.run(caller=replace(scenario.caller, source_id="other"))
    result = await scan(scenario)
    assert result["call"]["queued_count"] == 1
    assert result["call"]["scanned_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detail,category",
    [
        ("CRON AUTH USER_INFO IS EXPIRED", "other"),
        ("workflow sapId does not match target user", "other"),
        ("技能任务结果不可用", "other"),
        ("authentication", "auth_expired"),
    ],
)
async def test_batch_exclusion_rules_also_apply_to_ordinary(
    scenario,
    detail,
    category,
):
    await scenario.run(detail=detail, category=category)
    result = await scan(scenario)
    assert result["call"]["queued_count"] == 0
    assert result["call"]["skipped_count"] == 1


@pytest.mark.asyncio
async def test_repeat_and_concurrent_calls_do_not_enqueue_original_twice(
    scenario,
):
    await scenario.run()
    first = DailyFailureRetry(scenario.db, clock=scenario.clock)
    second = DailyFailureRetry(scenario.db, clock=scenario.clock)
    calls = [
        await first.calls.submit("bank"),
        await second.calls.submit("bank"),
    ]
    await asyncio.gather(
        first.process(calls[0]["call_id"]),
        second.process(calls[1]["call_id"]),
    )
    count = scenario.db.conn.execute(
        "SELECT COUNT(*) FROM swe_cron_execution_runs",
    ).fetchone()[0]
    assert count == 2
    rows = [await first.calls.get("bank", call["call_id"]) for call in calls]
    assert sum(row["queued_count"] for row in rows) == 1


@pytest.mark.asyncio
async def test_retry_failure_can_continue_beyond_native_limit_and_cross_day(
    scenario,
):
    original = await scenario.run()
    for attempt in range(1, 5):
        result = await scan(scenario)
        assert result["call"]["queued_count"] == 1
        item = next(
            item for item in result["items"] if item["decision"] == "queued"
        )
        row = await scenario.db.fetch_one(
            "SELECT * FROM swe_cron_execution_runs WHERE execution_id=%s",
            (item["execution"]["execution_id"],),
        )
        marker = json.loads(row["dispatch_meta_json"])["daily_failure_retry"]
        assert marker["retry_count"] == attempt
        assert marker["root_execution_id"] == original["execution_id"]
        if attempt == 2:
            scenario.now[0] += timedelta(days=1)
        await scenario.finish(row)


@pytest.mark.asyncio
async def test_cutoff_and_queue_high_water_are_fixed_per_call(scenario):
    service = DailyFailureRetry(scenario.db, clock=scenario.clock)
    call = await service.calls.submit("bank")
    scenario.now[0] += timedelta(seconds=1)
    await scenario.run()
    await service.process(call["call_id"])
    assert (await service.calls.get("bank", call["call_id"]))[
        "scanned_count"
    ] == 0
    assert (await scan(scenario))["call"]["queued_count"] == 1


def test_retry_spread_is_stable_and_uses_the_full_hour_window():
    call = {
        "call_id": "f4c08118-1ca8-47e5-a921-6f12b11f5ee5",
        "cutoff_at": "2026-10-27T01:00:00+00:00",
    }
    rows = [
        {
            "tenant_id": "tenant",
            "source_id": "bank",
            "agent_id": "default",
            "job_id": f"job-{index}",
        }
        for index in range(64)
    ]
    accepted_at = datetime.fromisoformat(call["cutoff_at"])
    due_at = [retry_not_before_at(call, row) for row in rows]

    assert due_at[0] == retry_not_before_at(call, rows[0])
    assert all(
        accepted_at <= value < accepted_at + timedelta(hours=1)
        for value in due_at
    )
    assert len(set(due_at)) > 10

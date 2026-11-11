# -*- coding: utf-8 -*-
"""批关系、故障恢复、事务 fencing 和执行条件的补跑边界。"""

import json
from datetime import timedelta

import pytest

from tests.unit.scheduler.test_daily_failure_retry import scenario, scan
from scheduler.app.services.cron.managed.daily_retry_service import (
    DailyFailureRetry,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("attempts,queued", [(1, 0), (2, 0), (3, 1)])
async def test_batch_waits_for_native_retry_budget(scenario, attempts, queued):
    _, _, source = await scenario.batch(attempts=attempts)
    result = await scan(scenario)
    assert result["call"]["queued_count"] == queued
    if queued:
        item = next(
            item for item in result["items"] if item["decision"] == "queued"
        )
        assert item["previous_execution_id"] == source["execution_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("children", [False, True])
async def test_batch_reruns_only_failed_member_and_paused_entry_is_allowed(
    scenario,
    children,
):
    created, batch_id, source = await scenario.batch(
        attempts=3,
        children=children,
    )
    await scenario.db.execute(
        "UPDATE swe_cron_job_states SET dispatch_state='batch_paused' WHERE job_id=%s",
        (created["job_id"],),
    )
    before = scenario.db.conn.execute(
        "SELECT COUNT(*) FROM swe_cron_managed_trigger_receipts",
    ).fetchone()[0]
    result = await scan(scenario)
    assert result["call"]["queued_count"] == 1
    item = next(
        item for item in result["items"] if item["decision"] == "queued"
    )
    row = await scenario.db.fetch_one(
        "SELECT * FROM swe_cron_execution_runs WHERE execution_id=%s",
        (item["execution"]["execution_id"],),
    )
    assert row["job_id"] == source["job_id"]
    assert row["tenant_id"] == source["tenant_id"]
    assert (
        scenario.db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_managed_trigger_receipts",
        ).fetchone()[0]
        == before
    )
    await scenario.finish(row, status="success")
    old = await scenario.db.fetch_one(
        "SELECT is_final FROM swe_cron_executions WHERE id=%s",
        (source["history_execution_id"],),
    )
    assert old["is_final"] == 0
    member = await scenario.db.fetch_one(
        "SELECT current_execution_id,business_attempt FROM swe_cron_managed_batch_members WHERE batch_id=%s AND job_id=%s",
        (batch_id, source["job_id"]),
    )
    assert member["current_execution_id"] == row["execution_id"]
    assert member["business_attempt"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["disabled", "deleted", "paused", "not_ready"],
)
async def test_non_executable_jobs_are_skipped(scenario, change):
    row = await scenario.run()
    if change == "disabled":
        await scenario.db.execute("UPDATE swe_cron_jobs_v2 SET enabled=0")
    elif change == "deleted":
        await scenario.db.execute(
            "UPDATE swe_cron_jobs_v2 SET deleted_at=%s",
            (scenario.clock(),),
        )
    elif change == "paused":
        await scenario.db.execute(
            "UPDATE swe_cron_job_states SET pause_reason='auto_unread_threshold'",
        )
    else:
        await scenario.db.execute(
            "UPDATE swe_cron_job_states SET management_status='updating'",
        )
    result = await scan(scenario)
    assert result["call"]["queued_count"] == 0
    assert result["call"]["skipped_count"] == 1


@pytest.mark.asyncio
async def test_unknown_execution_outcome_is_not_a_confirmed_failure(scenario):
    row = await scenario.run(category="outcome_unknown")
    await scenario.db.execute(
        "UPDATE swe_cron_execution_runs SET error_code='cron_execution_lease_expired' WHERE execution_id=%s",
        (row["execution_id"],),
    )
    result = await scan(scenario)
    assert result["items"][0]["reason"] == "cron_daily_retry_outcome_unknown"


@pytest.mark.asyncio
async def test_partial_item_failure_rolls_back_then_continues(
    scenario,
    monkeypatch,
    caplog,
):
    from scheduler.app.services.cron.managed import (
        daily_retry_service as module,
    )

    first = await scenario.run()
    await scenario.run()
    original = module.enqueue_retry

    async def broken(tx, call, row, *args):
        result = await original(tx, call, row, *args)
        if row["execution_id"] == first["execution_id"]:
            raise RuntimeError("private-sql private-token")
        return result

    monkeypatch.setattr(module, "enqueue_retry", broken)
    result = await scan(scenario)
    assert result["call"]["status"] == "completed_with_errors"
    assert result["call"]["queued_count"] == 1
    assert result["call"]["failed_count"] == 1
    assert result["call"]["error_code"] == "cron_daily_retry_items_failed"
    assert (
        scenario.db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 3
    )
    assert "private-sql" not in caplog.text
    assert "private-token" not in caplog.text


@pytest.mark.asyncio
async def test_expired_worker_cannot_commit_enqueue_or_cursor(
    scenario,
    monkeypatch,
):
    from scheduler.app.services.cron.managed import (
        daily_retry_service as module,
    )

    await scenario.run()
    service = DailyFailureRetry(scenario.db, clock=scenario.clock)
    call = await service.calls.submit("bank")
    original = module.enqueue_retry

    async def expire(*args):
        result = await original(*args)
        scenario.now[0] += timedelta(seconds=181)
        return result

    monkeypatch.setattr(module, "enqueue_retry", expire)
    with pytest.raises(RuntimeError, match="cron_daily_retry_lease_lost"):
        await service.process(call["call_id"])
    assert (
        scenario.db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_execution_runs",
        ).fetchone()[0]
        == 1
    )
    assert (await service.calls.get("bank", call["call_id"]))[
        "scanned_count"
    ] == 0
    monkeypatch.setattr(module, "enqueue_retry", original)
    await DailyFailureRetry(scenario.db, clock=scenario.clock).process(
        call["call_id"],
    )
    assert (await service.calls.get("bank", call["call_id"]))[
        "queued_count"
    ] == 1


@pytest.mark.asyncio
async def test_new_worker_continues_persisted_page_without_duplicates(
    scenario,
):
    for _ in range(3):
        await scenario.run()
    service = DailyFailureRetry(scenario.db, clock=scenario.clock, page_size=1)
    call = await service.calls.submit("bank")
    await service.process(call["call_id"])
    other = DailyFailureRetry(scenario.db, clock=scenario.clock, page_size=1)
    for _ in range(3):
        await other.process(call["call_id"])
    result = await other.detail("bank", call["call_id"])
    assert result["call"]["status"] == "completed"
    assert (
        result["call"]["scanned_count"] == result["call"]["queued_count"] == 3
    )
    assert len(result["items"]) == 3


@pytest.mark.asyncio
async def test_model_is_resolved_outside_any_business_transaction(
    scenario,
    monkeypatch,
):
    from scheduler.app.services.cron.managed import executions

    row = await scenario.run()
    spec = json.loads(row["spec_json"])
    spec["task_type"] = "agent"
    spec["model_slot"] = None
    await scenario.db.execute(
        "UPDATE swe_cron_jobs_v2 SET task_type='agent',spec_json=%s,definition_version=2",
        (json.dumps(spec),),
    )
    seen = []

    async def model(_current, _spec):
        assert not scenario.db.lock.locked()
        seen.append(True)
        return {"provider_id": "selected-provider", "model": "selected-model"}

    monkeypatch.setattr(executions, "resolve_agent_model", model)
    result = await scan(scenario)
    assert result["call"]["queued_count"] == 1
    assert seen == [True]


@pytest.mark.asyncio
async def test_page_failure_is_recorded_and_can_resume_after_backoff(
    scenario,
    monkeypatch,
):
    from scheduler.app.services.cron.managed import (
        daily_retry_service as module,
    )

    await scenario.run()
    service = DailyFailureRetry(scenario.db, clock=scenario.clock)
    call = await service.calls.submit("bank")
    original = module.candidates

    async def unavailable(*_args):
        raise RuntimeError("private-db-password")

    monkeypatch.setattr(module, "candidates", unavailable)
    with pytest.raises(RuntimeError, match="private-db-password"):
        await service.process(call["call_id"])
    failed = await service.calls.get("bank", call["call_id"])
    assert failed["status"] == "queued"
    assert failed["error_code"] == "cron_daily_retry_scan_failed"
    assert failed["scanned_count"] == 0
    assert not await service.calls.pending()
    scenario.now[0] += timedelta(seconds=61)
    monkeypatch.setattr(module, "candidates", original)
    await service.process(call["call_id"])
    recovered = await service.calls.get("bank", call["call_id"])
    assert recovered["status"] == "completed"
    assert recovered["error_code"] is None
    assert recovered["queued_count"] == 1


@pytest.mark.asyncio
async def test_batch_daily_retry_can_continue_beyond_native_budget(scenario):
    _, batch_id, original = await scenario.batch(attempts=3, children=True)
    for count in range(1, 5):
        result = await scan(scenario)
        assert result["call"]["queued_count"] == 1
        item = next(
            value for value in result["items"] if value["decision"] == "queued"
        )
        assert item["retry_count"] == count
        assert item["root_execution_id"] is not None
        row = await scenario.db.fetch_one(
            "SELECT * FROM swe_cron_execution_runs WHERE execution_id=%s",
            (item["execution"]["execution_id"],),
        )
        await scenario.finish(row)
    member = await scenario.db.fetch_one(
        "SELECT * FROM swe_cron_managed_batch_members WHERE batch_id=%s AND job_id=%s",
        (batch_id, original["job_id"]),
    )
    assert member["business_attempt"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("settled", [False, True])
@pytest.mark.parametrize(
    "error_type",
    ["Prompt", "Business,Prompt", "Business", None],
)
async def test_async_failure_uses_confirmation_day_and_waits_for_settlement(
    scenario,
    settled,
    error_type,
):
    from scheduler.app.services.cron.managed.settlement import (
        ExecutionSettlement,
    )

    yesterday = scenario.now[0] - timedelta(days=1)
    scenario.now[0] = yesterday
    row = await scenario.run(status="success")
    await scenario.db.execute(
        "UPDATE swe_cron_execution_runs SET settled_status=NULL,settled_at=NULL",
    )
    await scenario.db.execute(
        "UPDATE swe_cron_executions SET async_status=%s,error_type=%s",
        ("error" if settled else "pending", error_type),
    )
    scenario.now[0] += timedelta(days=1)
    await ExecutionSettlement(scenario.db, clock=scenario.clock).tick()
    result = await scan(scenario)
    assert result["call"]["queued_count"] == int(
        settled and "Prompt" in (error_type or ""),
    )
    if settled and "Prompt" in (error_type or ""):
        assert (
            result["items"][0]["previous_execution_id"] == row["execution_id"]
        )


@pytest.mark.asyncio
async def test_locked_candidate_must_still_belong_to_requested_source(
    scenario,
    monkeypatch,
):
    from scheduler.app.services.cron.managed import (
        daily_retry_service as module,
    )

    row = await scenario.run()
    wrong_call = DailyFailureRetry(scenario.db, clock=scenario.clock)
    call = await wrong_call.calls.submit("other")

    async def wrong_source(*_args):
        return [row]

    monkeypatch.setattr(module, "candidates", wrong_source)
    await wrong_call.process(call["call_id"])
    assert (await wrong_call.calls.get("other", call["call_id"]))[
        "queued_count"
    ] == 0


@pytest.mark.asyncio
async def test_daily_retry_respects_current_batch_pause(scenario):
    _, batch, _ = await scenario.batch(attempts=3)
    await scenario.db.execute(
        "UPDATE swe_cron_managed_trigger_receipts SET batch_paused=1 WHERE batch_id=%s",
        (batch,),
    )
    result = await scan(scenario)
    assert result["call"]["queued_count"] == 0
    assert any(
        item["reason"] == "cron_daily_retry_batch_paused"
        for item in result["items"]
    )
    await scenario.db.execute(
        "UPDATE swe_cron_managed_trigger_receipts SET batch_paused=0 WHERE batch_id=%s",
        (batch,),
    )
    assert (await scan(scenario))["call"]["queued_count"] == 1

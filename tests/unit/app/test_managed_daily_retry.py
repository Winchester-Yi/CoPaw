# -*- coding: utf-8 -*-
"""补跑从队列进入 SWE 后的新历史、最终标识和运行态集成验证。"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.unit.scheduler.test_daily_failure_retry import scenario, scan
from swe.app.crons.managed_results import ExecutionWriter, ExecutionOutcome
from swe.app.crons.managed_history import notification_fields


async def retry_row(scenario):
    result = await scan(scenario)
    item = next(
        item for item in result["items"] if item["decision"] == "queued"
    )
    row = await scenario.db.fetch_one(
        "SELECT * FROM swe_cron_execution_runs WHERE execution_id=%s",
        (item["execution"]["execution_id"],),
    )
    return row, result["call"]


def key(row):
    return tuple(
        row[name] for name in ("tenant_id", "source_id", "agent_id", "job_id")
    )


def advance_to_retry_due(scenario, row):
    scenario.now[0] = datetime.fromisoformat(row["not_before_at"])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["success", "error"])
async def test_new_history_is_marked_and_old_history_final_changes_atomically(
    scenario,
    status,
):
    original = await scenario.run()
    row, call = await retry_row(scenario)
    assert (
        scenario.db.conn.execute(
            "SELECT is_final FROM swe_cron_executions",
        ).fetchone()[0]
        == 1
    )
    await scenario.finish(row, status=status)
    history = await scenario.db.fetch_all(
        "SELECT * FROM swe_cron_executions ORDER BY id",
    )
    assert [item["is_final"] for item in history] == [0, 1]
    assert [item["status"] for item in history] == ["error", status]
    marker = json.loads(history[-1]["meta"])["daily_failure_retry"]
    assert marker["previous_execution_id"] == original["execution_id"]
    assert marker["call_id"] == call["call_id"]
    assert marker["retry_count"] == 1
    state = await scenario.db.fetch_one("SELECT * FROM swe_cron_job_states")
    assert state["last_status"] == status
    assert state["unread_execution_count"] == (1 if status == "success" else 0)


@pytest.mark.asyncio
async def test_only_previous_history_in_same_chain_changes(scenario):
    first = await scenario.run()
    latest = await scenario.run(job_id=first["job_id"])
    row, _ = await retry_row(scenario)
    await scenario.finish(row, status="success")
    history = await scenario.db.fetch_all(
        "SELECT id,is_final FROM swe_cron_executions ORDER BY id",
    )
    assert [item["is_final"] for item in history] == [1, 0, 1]
    assert history[1]["id"] == latest["history_execution_id"]


@pytest.mark.asyncio
async def test_history_update_failure_rolls_back_insert_and_state(
    scenario,
    monkeypatch,
):
    await scenario.run()
    row, _ = await retry_row(scenario)
    advance_to_retry_due(scenario, row)
    writer = ExecutionWriter(scenario.db)
    await writer.accept(row["execution_id"], key(row))
    assert await writer.start(row["execution_id"], key(row), "owner")
    original = scenario.db.execute

    async def fail(sql, params=None):
        if sql.startswith("UPDATE swe_cron_executions SET is_final=0"):
            raise ConnectionError("history final update failed")
        return await original(sql, params)

    monkeypatch.setattr(scenario.db, "execute", fail)
    with pytest.raises(ConnectionError):
        await writer.complete(
            row["execution_id"],
            key(row),
            "owner",
            ExecutionOutcome("success"),
        )
    assert (
        scenario.db.conn.execute(
            "SELECT COUNT(*) FROM swe_cron_executions",
        ).fetchone()[0]
        == 1
    )
    assert (await writer.get(row["execution_id"], key(row)))[
        "status"
    ] == "running"


@pytest.mark.asyncio
async def test_newer_scheduled_success_skips_waiting_retry_without_overwriting_state(
    scenario,
):
    original = await scenario.run()
    row, _ = await retry_row(scenario)
    await scenario.run(job_id=original["job_id"], status="success")
    advance_to_retry_due(scenario, row)
    writer = ExecutionWriter(scenario.db)
    await writer.accept(row["execution_id"], key(row))
    assert await writer.start(row["execution_id"], key(row), "owner") is None
    assert (await writer.get(row["execution_id"], key(row)))[
        "status"
    ] == "skipped"
    assert (
        await scenario.db.fetch_one(
            "SELECT last_status FROM swe_cron_job_states",
        )
    )["last_status"] == "success"


@pytest.mark.asyncio
async def test_manual_success_does_not_skip_waiting_retry(scenario):
    original = await scenario.run()
    row, _ = await retry_row(scenario)
    await scenario.run(
        job_id=original["job_id"],
        kind="manual",
        status="success",
    )
    advance_to_retry_due(scenario, row)
    writer = ExecutionWriter(scenario.db)
    await writer.accept(row["execution_id"], key(row))
    assert await writer.start(row["execution_id"], key(row), "owner")


@pytest.mark.asyncio
async def test_cross_identity_previous_reference_is_not_modified(scenario):
    original = await scenario.run()
    row, _ = await retry_row(scenario)
    unrelated = await scenario.run()
    meta = json.loads(row["dispatch_meta_json"])
    meta["previous_execution_id"] = unrelated["execution_id"]
    meta["daily_failure_retry"]["previous_execution_id"] = unrelated[
        "execution_id"
    ]
    await scenario.db.execute(
        "UPDATE swe_cron_execution_runs SET dispatch_meta_json=%s WHERE execution_id=%s",
        (json.dumps(meta), row["execution_id"]),
    )
    with pytest.raises(ValueError, match="cron_daily_retry_history"):
        await scenario.finish(row, status="success")
    histories = await scenario.db.fetch_all(
        "SELECT is_final FROM swe_cron_executions",
    )
    assert [item["is_final"] for item in histories] == [1, 1]


@pytest.mark.parametrize("finished_after_target", [True, False])
def test_retry_notification_preserves_original_target_time(
    finished_after_target,
):
    from datetime import datetime

    due = datetime(2026, 10, 27, 2, tzinfo=timezone.utc)
    now = due + timedelta(hours=1 if finished_after_target else -1)
    row = {
        "kind": "ordinary",
        "started_at": now,
        "scheduled_fire_at": due,
        "dispatch_meta_json": json.dumps(
            {
                "daily_failure_retry": {
                    "notification_at": due.isoformat(),
                    "notification_timezone": "Asia/Shanghai",
                },
            },
        ),
    }
    spec = {
        "task_type": "agent",
        "schedule": {"timezone": "Asia/Shanghai"},
        "meta": {"notification_delay_minutes": 30},
    }
    fields = notification_fields(row, spec, ExecutionOutcome("success"), now)
    assert fields["notification_due_at"] == datetime(2026, 10, 27, 10)


@pytest.mark.asyncio
async def test_started_retry_is_not_interrupted_or_overwrites_newer_success(
    scenario,
):
    original = await scenario.run()
    row, _ = await retry_row(scenario)
    advance_to_retry_due(scenario, row)
    writer = ExecutionWriter(scenario.db)
    await writer.accept(row["execution_id"], key(row))
    assert await writer.start(row["execution_id"], key(row), "owner")
    spec = json.loads(row["spec_json"])
    spec["runtime"]["max_concurrency"] = 2
    await scenario.db.execute(
        "UPDATE swe_cron_jobs_v2 SET spec_json=%s",
        (json.dumps(spec),),
    )
    await scenario.run(job_id=original["job_id"], status="success")
    assert await writer.complete(
        row["execution_id"],
        key(row),
        "owner",
        ExecutionOutcome("error", error_code="cron_execution_failed"),
    )
    state = await scenario.db.fetch_one("SELECT * FROM swe_cron_job_states")
    assert state["latest_scheduled_preview"] == "report"
    assert state["last_status"] == "success"
    assert state["last_error"] is None
    assert (await writer.get(row["execution_id"], key(row)))[
        "status"
    ] == "error"


@pytest.mark.asyncio
async def test_long_execution_metadata_cannot_remove_daily_chain_identity(
    scenario,
):
    from swe.app.crons.managed_history import history_metadata

    await scenario.run()
    row, call = await retry_row(scenario)
    row["source_id"] = "\\" * 128
    row["agent_id"] = "\\" * 128
    text = history_metadata(
        row,
        ExecutionOutcome("error", execution_meta={"description": "x" * 980}),
    )
    meta = json.loads(text)
    assert len(text) <= 2048
    assert meta["source_id"] == row["source_id"]
    assert meta["agent_id"] == row["agent_id"]
    assert meta["daily_failure_retry"]["call_id"] == call["call_id"]


@pytest.mark.asyncio
async def test_daily_success_still_obeys_unread_auto_pause(
    scenario,
    monkeypatch,
):
    from types import SimpleNamespace
    from swe.app.crons import managed_results

    await scenario.run()
    row, _ = await retry_row(scenario)
    monkeypatch.setattr(
        managed_results,
        "resolve_cron_unread_auto_pause_config",
        lambda _: SimpleNamespace(enabled=True, threshold=1),
    )
    await scenario.finish(row, status="success")
    state = await scenario.db.fetch_one("SELECT * FROM swe_cron_job_states")
    assert state["unread_execution_count"] == 1
    assert state["pause_reason"] == "auto_unread_threshold"
    assert (await scan(scenario))["call"]["queued_count"] == 0

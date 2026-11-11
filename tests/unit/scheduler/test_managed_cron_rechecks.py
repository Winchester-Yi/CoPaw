# -*- coding: utf-8 -*-
import json

import pytest

from scheduler.app.services.cron.managed.definition import normalize_spec
from scheduler.app.services.cron.managed.identity import CallerIdentity
from tests.unit.scheduler.test_managed_cron_management import create_payload

CALLER = CallerIdentity("alice", "bank", "default", "alice")


def test_recheck_rule_normalizes_preserves_and_clears():
    raw = create_payload()["spec"]
    raw["recheck"] = {"delay_seconds": 60, "instruction": " 核对结果 "}
    saved = normalize_spec(raw, CALLER, "job-1")
    assert saved["recheck"] == {"delay_seconds": 60, "instruction": "核对结果"}
    current = {
        "spec_json": json.dumps(saved),
        "creator_user_id": "alice",
        "enabled": True,
    }
    del raw["recheck"]
    assert (
        normalize_spec(raw, CALLER, "job-1", current)["recheck"]
        == saved["recheck"]
    )
    raw["recheck"] = None
    assert normalize_spec(raw, CALLER, "job-1", current)["recheck"] is None


@pytest.mark.parametrize("delay", [True, 0, 59, 604801, "60"])
def test_recheck_rejects_invalid_delay(delay):
    raw = create_payload()["spec"]
    raw["recheck"] = {"delay_seconds": delay, "instruction": "核对结果"}
    with pytest.raises(ValueError, match="invalid_cron_recheck"):
        normalize_spec(raw, CALLER, "job-1")


def recheck_database():
    from tests.unit.scheduler.test_managed_cron_queries import QueryDatabase

    db = QueryDatabase()
    db.add("job-1")
    db.add("other-job", tenant="bob")
    db.conn.executescript(
        """
        CREATE TABLE swe_cron_recheck_tasks (
            recheck_id TEXT PRIMARY KEY, parent_execution_id TEXT,
            tenant_id TEXT, source_id TEXT, agent_id TEXT, job_id TEXT,
            run_at TEXT, started_at TEXT, status TEXT, enqueued_state_version INTEGER
        );
        CREATE TABLE swe_cron_managed_dispatch_events (
            execution_id TEXT, tenant_id TEXT, source_id TEXT, agent_id TEXT,
            job_id TEXT, event_type TEXT, details_json TEXT,
            occurred_at TEXT, event_order INTEGER
        );
        INSERT INTO swe_cron_recheck_tasks VALUES
            ('r1','p1','alice','bank','default','job-1',
             '2026-11-09 10:00:00',NULL,'queued',0),
            ('r2','p2','alice','bank','default','job-1',
             '2026-11-09 10:01:00','2026-11-09 10:01:03','running',0),
            ('private','p3','bob','bank','default','other-job',
             '2026-11-09 10:00:00',NULL,'queued',0);
        INSERT INTO swe_cron_managed_dispatch_events VALUES
            ('completed','alice','bank','default','job-1','recheck_terminal',
             '{"parent_execution_id":"done","status":"success","run_at":"2026-11-09T09:00:00+00:00"}',
             '2026-11-09 09:03:00',1),
            ('ordinary','alice','bank','default','job-1','success','{}',
             '2026-11-09 09:04:00',2);
        """,
    )
    return db


@pytest.mark.asyncio
async def test_recheck_list_separates_active_history_and_tenant():
    from scheduler.app.services.cron.managed.recheck_query import page_rechecks

    db = recheck_database()
    active = await page_rechecks(db, CALLER, view="active", limit=1)
    assert [item["recheck_id"] for item in active["items"]] == ["r1"]
    page2 = await page_rechecks(
        db,
        CALLER,
        view="active",
        limit=1,
        cursor=active["next_cursor"],
    )
    assert [item["recheck_id"] for item in page2["items"]] == ["r2"]
    assert page2["next_cursor"] is None
    with pytest.raises(ValueError, match="invalid_cron_cursor"):
        await page_rechecks(
            db,
            CALLER,
            view="history",
            limit=1,
            cursor=active["next_cursor"],
        )
    history = await page_rechecks(db, CALLER, view="history", limit=2)
    assert [item["recheck_id"] for item in history["items"]] == ["completed"]
    assert history["items"][0]["finished_at"] == "2026-11-09T09:03:00+00:00"
    db.conn.close()


@pytest.mark.asyncio
async def test_recheck_list_keeps_running_and_history_after_deletion():
    from scheduler.app.services.cron.managed.recheck_query import page_rechecks

    db = recheck_database()
    db.conn.execute("UPDATE swe_cron_jobs_v2 SET deleted_at='2026-11-09'")
    active = await page_rechecks(db, CALLER, view="active", limit=5)
    assert [item["recheck_id"] for item in active["items"]] == ["r2"]
    history = await page_rechecks(db, CALLER, view="history", limit=5)
    assert history["items"][0]["recheck_id"] == "completed"
    assert history["items"][0]["chat_id"] is None
    db.conn.close()


@pytest.mark.asyncio
async def test_due_recheck_claim_cancels_if_disabled(monkeypatch):
    from datetime import datetime
    from scheduler.app.services.cron.managed import recheck_worker

    now = datetime.fromisoformat("2026-11-09 12:00:00")
    row = {
        "status": "queued",
        "recheck_id": "r1",
        "version": 1,
        "dispatch_attempt": 0,
        "enqueued_state_version": 0,
        "next_dispatch_at": now,
    }
    calls = []

    async def record(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(recheck_worker, "_terminal", record)
    worker = recheck_worker.RecheckWorker(None, None, 1)
    monkeypatch.setattr(worker, "_write", record)
    definition = {"enabled": True, "deleted_at": None}
    state = {"pause_reason": None, "queue_invalidation_version": 0}

    assert await worker._claim(None, row, definition, state, now) == "execute"
    assert calls[0][1]["status"] == "dispatching"
    assert calls[0][1]["dispatch_attempt"] == 1

    definition["enabled"] = False
    assert await worker._claim(None, row, definition, state, now) is None
    assert calls[-1][0][-2:] == ("cancelled", "cron_job_disabled_before_start")

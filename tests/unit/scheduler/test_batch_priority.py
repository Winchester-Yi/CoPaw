from scheduler.app.services.cron.dispatch_intent_service import (
    compute_batch_dispatch_order,
)
import json
from unittest.mock import AsyncMock
import pytest
from datetime import datetime


def test_user_priority_precedes_branch_and_heat():
    rows = [
        dict(job_id="hot", tenant_id="a", viewer_heat_score=999),
        dict(
            job_id="branch",
            tenant_id="b",
            viewer_heat_score=10,
            payload={"dispatch_priority": {"branch_rank": 1}},
        ),
        dict(
            job_id="user",
            tenant_id="c",
            viewer_heat_score=0,
            payload={"dispatch_priority": {"user_rank": 1}},
        ),
    ]
    assert [r["job_id"] for r in compute_batch_dispatch_order(rows)] == [
        "user",
        "branch",
        "hot",
    ]


def test_equal_user_rank_uses_branch_rank_before_heat():
    rows = [
        dict(
            job_id="a",
            viewer_heat_score=500,
            payload={"dispatch_priority": {"user_rank": 1}},
        ),
        dict(
            job_id="b",
            viewer_heat_score=0,
            payload={"dispatch_priority": {"user_rank": 1, "branch_rank": 1}},
        ),
    ]
    assert [r["job_id"] for r in compute_batch_dispatch_order(rows)] == [
        "b",
        "a",
    ]


@pytest.mark.asyncio
async def test_batch_snapshot_overrides_later_parent_policy(monkeypatch):
    from scheduler.app.services.cron import dispatch_intent_service as module

    definitions = [
        {
            "id": "parent",
            "tenant_id": "bob",
            "source_id": "s",
            "bbk_id": "121",
            "meta": json.dumps(
                {"batch_dispatch_priority": {"user_ids": ["bob"]}}
            ),
        },
        {
            "id": "child",
            "tenant_id": "alice",
            "source_id": "s",
            "bbk_id": "110",
            "meta": "{}",
        },
    ]
    db = AsyncMock()
    db.fetch_all.return_value = definitions
    monkeypatch.setattr(module, "get_db_connection", lambda: db)
    monkeypatch.setattr(
        module,
        "_fetch_viewer_heat_scores",
        AsyncMock(return_value={"parent": 999, "child": 0}),
    )
    jobs = [
        {
            "job_id": d["id"],
            "tenant_id": d["tenant_id"],
            "source_id": "s",
            "payload": {"keep": "value"},
        }
        for d in definitions
    ]
    rows = (
        await module.CronDispatchIntentService()._build_ordered_execution_rows(
            parent_job_id="parent",
            jobs=jobs,
            due_at=datetime(2026, 9, 8),
            priority_policy={"user_ids": ["alice"], "branch_ids": ["121"]},
        )
    )
    assert [r["job_id"] for r in rows] == ["child", "parent"]
    assert rows[0]["payload"]["dispatch_priority"]["basis"] == "user"
    assert rows[1]["payload"]["dispatch_priority"]["basis"] == "branch"
    assert jobs[0]["payload"] == {"keep": "value"}

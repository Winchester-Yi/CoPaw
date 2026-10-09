"""Batch creation freezes workflow configuration identity and model pool."""

import json
from datetime import datetime, timezone

import pytest

from scheduler.app.services.cron.workflow_binding import (
    resolve_workflow_batch_jobs,
)
from scheduler.app.services.cron.scheduling_service import (
    _base_execution_payload,
    _build_execution_callback_kwargs,
    _prepare_parent_callback_context,
)
from scheduler.app.services.cron import scheduling_service
from swe.app.routers.internal import _build_dispatch_callback_meta


class _Db:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    async def fetch_all(self, query, params):
        self.queries.append((query, params))
        return self.rows


@pytest.mark.asyncio
async def test_workflow_jobs_share_one_frozen_config_and_agent_is_untouched():
    db = _Db(
        [
            {
                "binding_id": "binding-1",
                "source_id": "RMASSIST",
                "current_version": 3,
                "enabled": 1,
                "config_json": json.dumps(
                    {"provider_id": "provider-a", "model_id": "model-a"},
                ),
            },
        ],
    )
    jobs = [
        {
            "id": "parent",
            "task_type": "workflow",
            "source_id": "RMASSIST",
            "workflow_binding_id": "binding-1",
        },
        {
            "job_id": "child",
            "task_type": "workflow",
            "source_id": "RMASSIST",
            "workflow_binding_id": "binding-1",
        },
        {"job_id": "agent", "task_type": "agent", "provider_id": "old"},
    ]

    await resolve_workflow_batch_jobs(jobs, db)

    assert len(db.queries) == 1
    assert db.queries[0][1] == ("binding-1",)
    assert jobs[0]["workflow_config_version"] == 3
    assert jobs[1]["provider_id"] == "provider-a"
    assert jobs[1]["model_id"] == "model-a"
    assert jobs[2] == {
        "job_id": "agent",
        "task_type": "agent",
        "provider_id": "old",
    }


@pytest.mark.asyncio
async def test_workflow_job_without_published_binding_fails_closed():
    db = _Db([])

    with pytest.raises(RuntimeError, match="binding"):
        await resolve_workflow_batch_jobs(
            [
                {
                    "id": "parent",
                    "task_type": "workflow",
                    "source_id": "RMASSIST",
                    "workflow_binding_id": "missing",
                },
            ],
            db,
        )


def test_frozen_workflow_version_reaches_swe_dispatch_metadata():
    payload = _base_execution_payload(
        {
            "tenant_id": "user-1",
            "source_id": "RMASSIST",
            "task_type": "workflow",
            "workflow_config_version": 3,
        },
        job_id="job-1",
        provider_id="p",
        model_id="m",
        scheduled="2026-09-24T09:00:00+08:00",
    )
    callback = _build_execution_callback_kwargs(
        {
            "tenant_id": "user-1",
            "source_id": "RMASSIST",
            "job_id": "job-1",
            "id": 12,
            "batch_id": "batch-1",
            "attempt_count": 2,
            "provider_id": "p",
            "model_id": "m",
        },
        payload,
    )
    callback["callback_source"] = "dispatch_service"
    metadata = _build_dispatch_callback_meta(callback)

    assert payload["workflow_config_version"] == 3
    assert callback["workflow_config_version"] == 3
    assert metadata["workflow_config_version"] == 3


@pytest.mark.asyncio
async def test_parent_callback_uses_config_model_over_stale_timer_model(
    monkeypatch,
):
    db = _Db(
        [
            {
                "binding_id": "binding-1",
                "source_id": "RMASSIST",
                "current_version": 4,
                "enabled": 1,
                "config_json": json.dumps(
                    {"provider_id": "new-provider", "model_id": "new-model"},
                ),
            },
        ],
    )
    parent = {
        "id": "job-1",
        "tenant_id": "user-1",
        "source_id": "RMASSIST",
        "task_type": "workflow",
        "workflow_binding_id": "binding-1",
        "meta": json.dumps({"broadcast_dispatch_intents_enabled": True}),
    }

    async def load_parent(**_kwargs):
        return parent

    async def load_children(_parent):
        return []

    monkeypatch.setattr(
        scheduling_service, "_load_parent_callback_job", load_parent
    )
    monkeypatch.setattr(
        scheduling_service, "_fetch_batch_child_jobs", load_children
    )
    monkeypatch.setattr(scheduling_service, "get_db_connection", lambda: db)

    context = await _prepare_parent_callback_context(
        params={
            "job_id": "job-1",
            "tenant_id": "user-1",
            "source_id": "RMASSIST",
            "provider_id": "old-provider",
            "model_id": "old-model",
            "scheduled_fire_at": "2026-09-24T09:00:00Z",
        },
        headers=None,
        now_utc=datetime(2026, 9, 24, 9, tzinfo=timezone.utc),
    )

    assert (context.provider_id, context.model_id) == (
        "new-provider",
        "new-model",
    )
    assert context.parent["workflow_config_version"] == 4

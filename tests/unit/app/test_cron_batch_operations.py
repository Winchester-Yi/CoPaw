import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from src.swe.app.crons.batch_operations import BatchPriority, manager_identity
from src.swe.app.crons.batch_operations import patch_priority
from src.swe.app.crons.models import CronJobSpec, JobsFile


def test_only_primary_branches_and_unique_users_are_accepted():
    assert BatchPriority(
        branch_ids=["121", "110"], user_ids=["alice", "bob"]
    ).branch_ids == ["121", "110"]
    with pytest.raises(ValidationError):
        BatchPriority(branch_ids=["111"])
    with pytest.raises(ValidationError):
        BatchPriority(user_ids=["alice", " alice "])


@pytest.mark.parametrize(
    "headers,status",
    [
        ({}, 403),
        ({"x-user-role": "user", "x-user-id": "a", "x-source-id": "s"}, 403),
        ({"x-user-role": "manager"}, 400),
    ],
)
def test_batch_operations_require_manager_and_source(headers, status):
    request = Request(
        {
            "type": "http",
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        }
    )
    with pytest.raises(HTTPException) as exc:
        manager_identity(request)
    assert exc.value.status_code == status


def test_manager_identity_is_source_scoped():
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"x-user-role", b"manager"),
                (b"x-user-id", b"alice"),
                (b"x-source-id", b"source-a"),
            ],
        }
    )
    assert manager_identity(request) == ("source-a", "alice")


def test_priority_patch_preserves_current_definition_and_other_metadata():
    job = CronJobSpec(
        id="parent",
        name="newly edited name",
        source_id="source-a",
        enabled=False,
        meta={"task_chat_id": "chat-1"},
        task_type="text",
        text="hello",
        schedule={"type": "cron", "cron": "0 9 * * *"},
        dispatch={
            "channel": "console",
            "target": {"user_id": "alice", "session_id": "s"},
        },
    )
    jobs = JobsFile(jobs=[job])
    changed, updated = patch_priority(
        jobs, "parent", "source-a", BatchPriority(user_ids=["alice"])
    )
    assert changed
    assert updated.name == "newly edited name"
    assert updated.enabled is False
    assert updated.meta["task_chat_id"] == "chat-1"
    assert updated.meta["batch_dispatch_priority"]["user_ids"] == ["alice"]
    with pytest.raises(HTTPException):
        patch_priority(jobs, "parent", "another-source", BatchPriority())
    from src.swe.app.crons.api import (
        _preserve_batch_dispatch_meta_on_save,
        _build_broadcast_job,
    )

    edited = _preserve_batch_dispatch_meta_on_save(job, updated)
    assert edited.meta["batch_dispatch_priority"]["user_ids"] == ["alice"]
    child = _build_broadcast_job(
        updated,
        job_id="child",
        target_tenant_id="bob",
        target_tenant_name="Bob",
        target_bbk_id="110",
        source_id="source-a",
        cron="0 9 * * *",
        timezone_name="UTC",
        offset_minutes=0,
        model_slot=None,
        model_slot_fallback_reason="",
        enable_batch_dispatch=True,
    )
    assert "batch_dispatch_priority" not in child.meta


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_status", [200, 503])
async def test_priority_save_waits_for_definition_sync(
    monkeypatch, sync_status
):
    import asyncio
    import json
    import httpx
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.swe.app.crons import batch_operations as ops
    from src.swe.app.crons.monitor_sync_client import MonitorSyncClient

    jobs = JobsFile(
        jobs=[
            CronJobSpec(
                id="parent",
                name="job",
                source_id="source-a",
                tenant_id="alice",
                task_type="text",
                text="hello",
                schedule={"type": "cron", "cron": "0 9 * * *"},
                dispatch={
                    "channel": "console",
                    "target": {"user_id": "alice", "session_id": "s"},
                },
            )
        ]
    )
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(sync_status, json={"synced": sync_status == 200})

    sync = MonitorSyncClient(base_url="http://monitor.test")
    sync._client = httpx.AsyncClient(
        base_url="http://monitor.test",
        transport=httpx.MockTransport(transport),
    )

    async def mutate(mutator):
        changed, job = mutator(jobs)
        return changed, job, 0

    mgr = SimpleNamespace(
        _lock=asyncio.Lock(),
        _mutate_jobs_file_locked=mutate,
        _monitor_sync_client=sync,
        _agent_id="default",
    )
    store = SimpleNamespace(
        finish_task=AsyncMock(), record_task_failed=AsyncMock()
    )
    monkeypatch.setattr(
        ops,
        "_get_source_job_or_404",
        AsyncMock(side_effect=lambda *_: jobs.jobs[0]),
    )
    monkeypatch.setattr(
        ops,
        "_claim_dispatch_mode_operation",
        AsyncMock(return_value=(store, SimpleNamespace(task_id="operation"))),
    )
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"x-user-role", b"manager"),
                (b"x-user-id", b"alice"),
                (b"x-source-id", b"source-a"),
            ],
        }
    )
    try:
        if sync_status == 200:
            result = await ops.save_priority(
                request, "parent", BatchPriority(branch_ids=["121"]), mgr
            )
            assert result.meta["batch_dispatch_priority"]["branch_ids"] == [
                "121"
            ]
        else:
            with pytest.raises(HTTPException) as exc:
                await ops.save_priority(
                    request, "parent", BatchPriority(branch_ids=["121"]), mgr
                )
            assert exc.value.status_code == 502
        assert jobs.jobs[0].meta["batch_dispatch_priority"]["branch_ids"] == [
            "121"
        ]
        assert json.loads(requests[0]["meta"])["batch_dispatch_priority"][
            "branch_ids"
        ] == ["121"]
    finally:
        await sync._client.aclose()


def test_non_manager_is_rejected_before_workspace_resolution():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.swe.app.crons import batch_operations as ops

    app = FastAPI()
    app.include_router(ops.router)

    def unavailable_manager():
        raise AssertionError("Unauthorized call resolved a workspace")

    app.dependency_overrides[ops.get_cron_manager] = unavailable_manager
    response = TestClient(app).put(
        "/cron/jobs/parent/batch-dispatch/priority",
        json={"user_ids": [], "branch_ids": []},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,body", [("failures", None), ("retry", {"candidates": []})]
)
async def test_scheduler_proxy_needs_manager_identity_but_no_token(
    monkeypatch, action, body
):
    import httpx
    from src.swe.app.crons import batch_operations as ops

    monkeypatch.delenv("SWE_INTERNAL_TOKEN", raising=False)
    monkeypatch.setattr(
        ops, "get_scheduler_api_url", lambda: "http://scheduler.test"
    )
    calls = []

    def handle(request):
        calls.append(request)
        assert "X-Internal-Token" not in request.headers
        assert request.headers["X-Source-Id"] == "source-a"
        assert request.headers["X-User-Id"] == "admin"
        return httpx.Response(200, json={"ok": True})

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        ops.httpx,
        "AsyncClient",
        lambda **kwargs: factory(
            transport=httpx.MockTransport(handle), **kwargs
        ),
    )
    scope = {
        "type": "http",
        "query_string": b"",
        "headers": [
            (b"x-user-role", b"manager"),
            (b"x-source-id", b"source-a"),
            (b"x-user-id", b"admin"),
        ],
    }
    assert await ops._proxy(Request(scope), "batch", action, body) == {
        "ok": True
    }
    assert len(calls) == 1
    with pytest.raises(HTTPException) as exc:
        await ops._proxy(
            Request({**scope, "headers": []}), "batch", action, body
        )
    assert exc.value.status_code == 403
    assert len(calls) == 1

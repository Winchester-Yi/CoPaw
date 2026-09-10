from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [True, False])
async def test_resume_repairs_only_its_batch_timer_before_state_change(
    monkeypatch, paused
):
    from src.swe.app.crons import batch_operations as ops

    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "headers": [
                (b"x-user-role", b"manager"),
                (b"x-user-id", b"admin"),
                (b"x-source-id", b"source-a"),
            ],
        }
    )
    job = SimpleNamespace(
        id="parent",
        tenant_id="owner",
        source_id="source-a",
        meta={
            "broadcast_dispatch_intents_enabled": True,
            "batch_dispatch_external_job_id": "batch-42",
            "external_job_id": "normal-41",
        },
    )
    adapter = SimpleNamespace(resume_job=AsyncMock())
    mgr = SimpleNamespace(
        get_job=AsyncMock(return_value=job), _scheduler_adapter=adapter
    )
    operation_store = SimpleNamespace(
        finish_task=AsyncMock(), record_task_failed=AsyncMock()
    )
    monkeypatch.setattr(
        ops,
        "_claim_dispatch_mode_operation",
        AsyncMock(
            return_value=(operation_store, SimpleNamespace(task_id="op"))
        ),
    )
    proxy = AsyncMock(return_value={"paused": paused, "version": 2})
    monkeypatch.setattr(ops, "request_run_state", proxy)
    result = await ops.save_batch_run_state(
        request,
        job.id,
        ops.BatchRunStateUpdate(paused=paused, expected_version=1),
        mgr,
    )
    assert result["paused"] is paused
    if paused:
        adapter.resume_job.assert_not_awaited()
    else:
        adapter.resume_job.assert_awaited_once_with("batch-42")
        proxy.reset_mock()
        adapter.resume_job.side_effect = RuntimeError(
            "external scheduler unavailable"
        )
        with pytest.raises(HTTPException) as exc:
            await ops.save_batch_run_state(
                request,
                job.id,
                ops.BatchRunStateUpdate(paused=False, expected_version=2),
                mgr,
            )
        assert exc.value.status_code == 502
        proxy.assert_not_awaited()

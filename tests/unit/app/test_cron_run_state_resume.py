import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request


@pytest.fixture
def state_change(monkeypatch):
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
    events = []
    adapter = SimpleNamespace(
        resume_job=AsyncMock(side_effect=lambda _: events.append("resume")),
        pause_job=AsyncMock(side_effect=lambda _: events.append("pause")),
    )
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
    proxy = AsyncMock(return_value={"paused": True, "version": 2})
    proxy.side_effect = (
        lambda *args, **kwargs: events.append("state") or proxy.return_value
    )
    monkeypatch.setattr(ops, "request_run_state", proxy)

    async def change(paused, version=1):
        return await ops.save_batch_run_state(
            request,
            job.id,
            ops.BatchRunStateUpdate(paused=paused, expected_version=version),
            mgr,
        )

    return SimpleNamespace(
        change=change,
        job=job,
        mgr=mgr,
        adapter=adapter,
        proxy=proxy,
        store=operation_store,
        events=events,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("paused", [True, False])
async def test_state_change_orders_internal_gate_and_only_its_batch_timer(
    state_change, paused
):
    ctx = state_change
    ctx.proxy.return_value = {"paused": paused, "version": 2}
    result = await ctx.change(paused)
    assert result["paused"] is paused
    if paused:
        ctx.adapter.pause_job.assert_awaited_once_with("batch-42")
        ctx.adapter.resume_job.assert_not_awaited()
        assert ctx.events == ["state", "pause"]
    else:
        ctx.adapter.resume_job.assert_awaited_once_with("batch-42")
        ctx.adapter.pause_job.assert_not_awaited()
        assert ctx.events == ["resume", "state"]
        ctx.proxy.reset_mock()
        ctx.adapter.resume_job.side_effect = RuntimeError(
            "external scheduler unavailable"
        )
        with pytest.raises(HTTPException) as exc:
            await ctx.change(False, 2)
        assert exc.value.status_code == 502
        ctx.proxy.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_pause_failure_keeps_internal_pause_and_can_retry(
    state_change,
):
    ctx = state_change
    ctx.adapter.pause_job.side_effect = RuntimeError("unavailable")
    with pytest.raises(HTTPException) as exc:
        await ctx.change(True)
    assert exc.value.status_code == 502
    assert "内部批调度已暂停" in exc.value.detail
    ctx.proxy.assert_awaited_once()
    assert ctx.proxy.await_args.kwargs["body"]["paused"] is True
    ctx.adapter.resume_job.assert_not_awaited()
    ctx.store.finish_task.assert_not_awaited()
    ctx.store.record_task_failed.assert_awaited_once()
    ctx.adapter.pause_job.side_effect = None
    assert (await ctx.change(True, 2))["paused"] is True
    assert ctx.adapter.pause_job.await_count == 2
    assert all(
        call.args == ("batch-42",)
        for call in ctx.adapter.pause_job.await_args_list
    )
    assert all(
        call.kwargs["body"]["paused"] for call in ctx.proxy.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [409, 503])
async def test_internal_pause_failure_never_touches_external_timer(
    state_change, status
):
    ctx = state_change
    ctx.proxy.side_effect = HTTPException(status, "state update rejected")
    with pytest.raises(HTTPException) as exc:
        await ctx.change(True)
    assert exc.value.status_code == status
    ctx.adapter.pause_job.assert_not_awaited()
    ctx.adapter.resume_job.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["missing-id", "no-adapter", "noop", "normal-id"]
)
async def test_pause_never_falls_back_to_the_ordinary_timer(
    state_change, invalid
):
    from src.swe.app.crons.scheduler_adapter import NoopSchedulerAdapter

    ctx = state_change
    if invalid == "missing-id":
        ctx.job.meta.pop("batch_dispatch_external_job_id")
    elif invalid == "normal-id":
        ctx.job.meta["batch_dispatch_external_job_id"] = "normal-41"
    elif invalid == "noop":
        ctx.mgr._scheduler_adapter = NoopSchedulerAdapter()
    else:
        ctx.mgr._scheduler_adapter = None
    with pytest.raises(HTTPException) as exc:
        await ctx.change(True)
    assert exc.value.status_code == 409
    assert "内部批调度已暂停" in exc.value.detail
    ctx.proxy.assert_awaited_once()
    ctx.adapter.pause_job.assert_not_awaited()
    ctx.adapter.resume_job.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
async def test_pause_posts_only_batch_id_to_external_platform(
    state_change, monkeypatch, accepted
):
    from src.swe.app.crons.scheduler_adapter import RealSchedulerAdapter

    ctx = state_change
    ctx.job.meta.update(
        batch_dispatch_external_job_id="42", external_job_id="41"
    )
    ctx.mgr._scheduler_adapter = RealSchedulerAdapter(
        base_url="https://scheduler.example",
        job_group=1,
        author="test",
        alarm_email="",
        client_no="test-client",
        client_key="test-key",
        client_remark="test",
    )
    requests = []

    def respond(request):
        requests.append(request)
        ctx.events.append("external")
        return httpx.Response(
            200, json={"code": 200 if accepted else 500, "msg": "test"}
        )

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(
            **kwargs, transport=httpx.MockTransport(respond)
        ),
    )
    if accepted:
        assert (await ctx.change(True))["paused"] is True
        ctx.store.finish_task.assert_awaited_once_with("op")
    else:
        with pytest.raises(HTTPException) as exc:
            await ctx.change(True)
        assert exc.value.status_code == 502
        assert "内部批调度已暂停" in exc.value.detail
        ctx.store.finish_task.assert_not_awaited()
    assert ctx.events == ["state", "external"]
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == (
        "https://scheduler.example/job-admin/v2/update-job-run-states"
    )
    assert json.loads(requests[0].content) == {
        "id": 42,
        "runFlag": 0,
        "clientNo": "test-client",
        "clientKey": "test-key",
        "clientRemark": "test",
    }

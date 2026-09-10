from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "marker", ["batch_managed_external_callback", "unknown_skip"]
)
async def test_unrecognized_skip_is_not_accepted_or_immediately_retried(
    monkeypatch, marker
):
    from scheduler.app.services.cron import scheduling_service as service

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"skipped": marker})
    )
    factory = httpx.AsyncClient
    monkeypatch.setattr(
        service.httpx,
        "AsyncClient",
        lambda **kwargs: factory(transport=transport, **kwargs),
    )
    client = service.SweCronCallbackClient(base_url="http://swe")
    with pytest.raises(service.SweCronCallbackOutcomeUnknownError):
        await client.dispatch_job(
            tenant_id="t",
            source_id="s",
            agent_id="a",
            job_id="j",
            dispatch_intent_id=1,
            dispatch_batch_id="b",
            dispatch_attempt=1,
        )


@pytest.mark.asyncio
async def test_callback_explicit_disabled_is_not_an_accepted_execution(
    monkeypatch,
):
    from scheduler.app.services.cron import scheduling_service as service

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"status": "ok", "skipped": "job_disabled"},
            request=request,
        )
    )
    factory = httpx.AsyncClient
    monkeypatch.setattr(
        service.httpx,
        "AsyncClient",
        lambda **kwargs: factory(transport=transport, **kwargs),
    )
    client = service.SweCronCallbackClient(base_url="http://swe")
    assert (
        await client.dispatch_job(
            tenant_id="t",
            source_id="s",
            agent_id="a",
            job_id="j",
            dispatch_intent_id=1,
            dispatch_batch_id="b",
            dispatch_attempt=1,
        )
        == "job_disabled"
    )


@pytest.mark.asyncio
async def test_scheduler_does_not_send_paused_claim_or_count_it_as_failure():
    from scheduler.app.services.cron.scheduling_service import (
        CronSchedulingService,
    )

    store = SimpleNamespace(
        prepare_handoff=AsyncMock(return_value="paused"),
        fail_intent=AsyncMock(),
    )
    callback = SimpleNamespace(dispatch_job=AsyncMock())
    service = CronSchedulingService(
        dispatch_store=store, callback_client=callback
    )
    assert (
        await service._dispatch_one(
            {"id": 1, "intent_role": "child"}, datetime.now(timezone.utc)
        )
        is False
    )
    callback.dispatch_job.assert_not_awaited()
    store.fail_intent.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [None, "job_disabled"])
async def test_post_callback_persistence_error_does_not_retry_accepted_or_skipped_work(
    outcome,
):
    from scheduler.app.services.cron.scheduling_service import (
        CronSchedulingService,
    )

    store = SimpleNamespace(
        prepare_handoff=AsyncMock(return_value="ready"),
        mark_intent_dispatched=AsyncMock(
            side_effect=RuntimeError("database unavailable")
        ),
        settle_callback_skip=AsyncMock(
            side_effect=RuntimeError("database unavailable")
        ),
        fail_intent=AsyncMock(return_value=True),
        update_batch_counts=AsyncMock(),
    )
    callback = SimpleNamespace(dispatch_job=AsyncMock(return_value=outcome))
    service = CronSchedulingService(
        dispatch_store=store, callback_client=callback
    )
    row = dict(
        id=7,
        intent_role="child",
        batch_id="batch",
        job_id="job",
        tenant_id="tenant",
        source_id="source",
        attempt_count=1,
        claim_token="token",
    )
    assert (
        await service._dispatch_one(row, datetime.now(timezone.utc)) is False
    )
    store.fail_intent.assert_not_awaited()

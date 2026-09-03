# -*- coding: utf-8 -*-
"""Scheduler cron scheduling service tests."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from scheduler.app import _app as scheduler_app
from scheduler.app.routers import cron as scheduler_cron
from scheduler.app.services.cron import scheduling_service as service_module
from scheduler.app.services.cron.scheduling_service import (
    CronSchedulingService,
    SweCronCallbackClient,
    WorkerScope,
    WorkerStrategy,
)


class _DispatchStore:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = list(rows)
        self.claims: list[dict[str, Any]] = []
        self.dispatched: list[dict[str, Any]] = []
        self.unknown_dispatches: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []
        self.child_batches: list[dict[str, Any]] = []
        self.execution_batches: list[dict[str, Any]] = []
        self.batch_upserts: list[dict[str, Any]] = []
        self.batch_count_updates: list[dict[str, Any]] = []
        self.parent_intents: list[dict[str, Any]] = []
        self.capacity: list[dict[str, Any]] = []
        self.stale_recoveries: list[dict[str, Any]] = []
        self.scope_leases: list[dict[str, Any]] = []
        self.scope_lease_allowed = True
        self.recovered_running_count: int | None = None
        self.scopes: list[WorkerScope] = [
            WorkerScope(
                source_id="source-a",
                provider_id="default",
                model_id="default",
            ),
        ]
        self.strategy = WorkerStrategy(
            strategy_id="unit",
            min_workers=1,
            baseline_workers=1,
            max_workers=1,
            adjust_interval_seconds=300,
            feedback_window_seconds=300,
            stale_execution_seconds=7800,
            error_rate_rules=[],
        )
        self.latest_capacity: dict[str, Any] | None = {
            "effective_workers": 1,
            "created_at": datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc),
        }
        self.latest_capacity_reads = 0
        self.feedback = {
            "pending_count": 0,
            "claimed_count": 0,
            "running_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "latency_p95_ms": 0,
        }

    async def claim_due_intents(self, **kwargs):
        self.claims.append(kwargs)
        return list(self.rows[: kwargs["limit"]])

    async def mark_intent_dispatched(self, **kwargs):
        self.dispatched.append(kwargs)
        return True

    async def mark_intent_dispatch_unknown(self, **kwargs):
        self.unknown_dispatches.append(kwargs)
        return True

    async def complete_intent(self, **kwargs):
        self.completed.append(kwargs)
        return True

    async def complete_from_execution(self, **kwargs):
        self.completed.append(kwargs)
        return True

    async def fail_intent(self, **kwargs):
        self.failed.append(kwargs)
        return True

    async def enqueue_parent_intent(self, **kwargs):
        self.parent_intents.append(kwargs)
        return len(self.parent_intents)

    async def enqueue_child_intents(self, **kwargs):
        self.child_batches.append(kwargs)
        return [100 + index for index, _ in enumerate(kwargs["child_jobs"])]

    async def upsert_dispatch_batch(self, **kwargs):
        self.batch_upserts.append(kwargs)

    async def enqueue_batch_execution_intents(self, **kwargs):
        self.execution_batches.append(kwargs)
        return [200 + index for index, _ in enumerate(kwargs["jobs"])]

    async def update_batch_counts(self, **kwargs):
        self.batch_count_updates.append(kwargs)

    async def list_dispatch_scopes(self, **kwargs):
        return list(self.scopes)

    async def resolve_worker_strategy(self, **kwargs):
        return self.strategy

    async def get_latest_worker_capacity(self, **kwargs):
        self.latest_capacity_reads += 1
        return self.latest_capacity

    async def acquire_scope_lease(self, **kwargs):
        self.scope_leases.append(kwargs)
        return self.scope_lease_allowed

    async def summarize_recent_completion_feedback(self, **kwargs):
        return dict(self.feedback)

    async def recover_stale_dispatched_intents(self, **kwargs):
        self.stale_recoveries.append(kwargs)
        if self.recovered_running_count is not None:
            self.feedback["running_count"] = self.recovered_running_count
        return 1

    async def record_worker_capacity(self, **kwargs):
        self.capacity.append(kwargs)
        self.latest_capacity = {
            "effective_workers": kwargs["effective_workers"],
            "created_at": kwargs.get("recorded_at")
            or datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc),
        }


class _CallbackClient:
    def __init__(
        self,
        *,
        fail: bool = False,
        error: Exception | None = None,
    ) -> None:
        self.fail = fail
        self.error = error
        self.requests: list[dict[str, Any]] = []

    async def dispatch_job(self, **kwargs):
        self.requests.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.fail:
            raise RuntimeError("callback failed")


def test_swe_callback_client_prefers_swe_internal_token(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SWE_INTERNAL_TOKEN", "swe-token")
    monkeypatch.setenv("SCHEDULER_SWE_INTERNAL_TOKEN", "scheduler-token")

    client = SweCronCallbackClient(base_url="http://swe.local")

    assert client._internal_token == "swe-token"


@pytest.mark.asyncio
async def test_swe_callback_client_forwards_passthrough_headers(
    monkeypatch,
) -> None:
    requests: list[dict[str, Any]] = []

    class _Response:
        status_code = 200
        text = ""

    class _AsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, *, json, headers):
            requests.append({"url": url, "json": json, "headers": headers})
            return _Response()

    monkeypatch.setattr(service_module.httpx, "AsyncClient", _AsyncClient)
    client = SweCronCallbackClient(
        base_url="http://fallback-swe.local",
        internal_token="scheduler-token",
    )

    await client.dispatch_job(
        tenant_id="tenant-a",
        source_id="source-a",
        agent_id="default",
        job_id="child-1",
        scope_id="tenant-a-source-a",
        from_id="tenant-a",
        dispatch_intent_id=7,
        dispatch_batch_id="batch-1",
        dispatch_attempt=2,
        swe_server_domain="http://swe.local",
        passthrough_headers={
            "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
            "X-B3-Spanid": "32befd146889a61a",
            "X-Internal-Token": "attacker-token",
        },
    )

    assert requests[0]["headers"] == {
        "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
        "X-B3-Spanid": "32befd146889a61a",
        "X-Internal-Token": "Bearer scheduler-token",
    }
    assert requests[0]["url"] == "http://swe.local/api/internal/cron/callback"
    assert requests[0]["json"]["scopeId"] == "tenant-a-source-a"
    assert requests[0]["json"]["fromId"] == "tenant-a"


@pytest.mark.asyncio
async def test_swe_callback_client_classifies_read_timeout_as_unknown(
    monkeypatch,
) -> None:
    class _AsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            raise service_module.httpx.ReadTimeout("")

    monkeypatch.setattr(service_module.httpx, "AsyncClient", _AsyncClient)
    client = SweCronCallbackClient(base_url="http://swe.local")

    unknown_error = service_module.SweCronCallbackOutcomeUnknownError
    with pytest.raises(unknown_error) as exc_info:
        await client.dispatch_job(
            tenant_id="tenant-a",
            source_id="source-a",
            agent_id="default",
            job_id="child-1",
            dispatch_intent_id=7,
            dispatch_batch_id="batch-1",
            dispatch_attempt=1,
        )

    assert exc_info.value.error_type == "ReadTimeout"
    assert exc_info.value.error_message


@pytest.mark.asyncio
async def test_swe_callback_client_keeps_connect_failure_retryable(
    monkeypatch,
) -> None:
    class _AsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            raise service_module.httpx.ConnectError("connection refused")

    monkeypatch.setattr(service_module.httpx, "AsyncClient", _AsyncClient)
    client = SweCronCallbackClient(base_url="http://swe.local")

    with pytest.raises(service_module.httpx.ConnectError):
        await client.dispatch_job(
            tenant_id="tenant-a",
            source_id="source-a",
            agent_id="default",
            job_id="child-1",
            dispatch_intent_id=7,
            dispatch_batch_id="batch-1",
            dispatch_attempt=1,
        )


@pytest.mark.asyncio
async def test_swe_callback_client_keeps_http_rejection_retryable(
    monkeypatch,
) -> None:
    class _Response:
        status_code = 503
        text = "unavailable"

    class _AsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return _Response()

    monkeypatch.setattr(service_module.httpx, "AsyncClient", _AsyncClient)
    client = SweCronCallbackClient(base_url="http://swe.local")

    with pytest.raises(RuntimeError, match="status=503"):
        await client.dispatch_job(
            tenant_id="tenant-a",
            source_id="source-a",
            agent_id="default",
            job_id="child-1",
            dispatch_intent_id=7,
            dispatch_batch_id="batch-1",
            dispatch_attempt=1,
        )


def test_scheduler_app_lifespan_wires_scheduler_loop() -> None:
    source = inspect.getsource(scheduler_app.lifespan)

    assert "cron-scheduler-service" in source
    assert "run_loop" in source
    assert "cron_scheduling_runtime_enabled" in source


def test_scheduler_runtime_config_ignores_non_scheduler_stale_env(
    monkeypatch,
) -> None:
    from scheduler.app.services.cron import scheduling_service as module

    monkeypatch.delenv(
        "SCHEDULER_CRON_DISPATCHED_STALE_SECONDS",
        raising=False,
    )

    monkeypatch.setenv("OTHER_CRON_DISPATCHED_STALE_SECONDS", "9")

    assert module.configured_dispatched_stale_seconds() == 7800


def test_extract_model_identity_accepts_flat_provider_model_fields() -> None:
    assert service_module._extract_model_identity(
        {
            "provider_id": "dashscope",
            "model_id": "qwen-max",
            "meta": {},
        },
    ) == ("dashscope", "qwen-max")
    assert service_module._extract_model_identity(
        {
            "meta": {
                "provider_id": "openai",
                "model_id": "gpt-5",
            },
        },
    ) == ("openai", "gpt-5")


@pytest.mark.asyncio
async def test_child_dispatch_calls_swe_callback_and_marks_dispatched() -> (
    None
):
    store = _DispatchStore(
        [
            {
                "id": 1,
                "batch_id": "batch-1",
                "intent_role": "child",
                "tenant_id": "tenant-b",
                "source_id": "source-a",
                "agent_id": "default",
                "job_id": "child-1",
            },
        ],
    )
    callback = _CallbackClient()
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=callback,
        worker_id="scheduler-1",
        effective_workers=1,
    )

    await service.dispatch_ready_once(
        now_utc=datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc),
    )

    assert store.claims[0]["limit"] == 1
    assert callback.requests == [
        {
            "tenant_id": "tenant-b",
            "source_id": "source-a",
            "scope_id": "tenant-b-source-a",
            "from_id": "tenant-b",
            "agent_id": "default",
            "job_id": "child-1",
            "dispatch_attempt": 1,
            "execution_key": "child-1:batch-1:1",
            "dispatch_intent_id": 1,
            "dispatch_batch_id": "batch-1",
            "parent_scheduled_fire_at": "",
            "provider_id": "default",
            "model_id": "default",
        },
    ]
    assert store.dispatched[0]["intent_id"] == 1
    assert store.dispatched[0]["details"]["dispatch_attempt"] == 1
    assert store.completed == []


@pytest.mark.asyncio
async def test_child_dispatch_passes_attempt_count_to_swe_callback() -> None:
    store = _DispatchStore(
        [
            {
                "id": 11,
                "batch_id": "batch-2",
                "intent_role": "child",
                "tenant_id": "tenant-b",
                "source_id": "source-a",
                "agent_id": "default",
                "job_id": "child-2",
                "attempt_count": 2,
            },
        ],
    )
    callback = _CallbackClient()
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=callback,
        worker_id="scheduler-1",
        effective_workers=1,
    )

    await service.dispatch_ready_once(
        now_utc=datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc),
    )

    assert callback.requests[0]["dispatch_attempt"] == 2
    assert store.dispatched[0]["details"]["dispatch_attempt"] == 2


@pytest.mark.asyncio
async def test_child_dispatch_forwards_passthrough_headers_to_swe_callback() -> (
    None
):
    store = _DispatchStore(
        [
            {
                "id": 1,
                "batch_id": "batch-1",
                "intent_role": "child",
                "tenant_id": "tenant-a",
                "source_id": "source-a",
                "agent_id": "default",
                "job_id": "child-1",
                "attempt_count": 1,
                "provider_id": "openai",
                "model_id": "gpt-5",
                "payload": {
                    "parent_scheduled_fire_at": "2026-07-01T01:00:00+00:00",
                    "passthrough_headers": {
                        "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
                        "X-B3-Spanid": "32befd146889a61a",
                    },
                },
            },
        ],
    )
    callback = _CallbackClient()
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=callback,
        worker_id="scheduler-1",
        effective_workers=1,
    )

    dispatched = await service.dispatch_ready_once(
        now_utc=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )

    assert dispatched == 1
    assert callback.requests[0]["passthrough_headers"] == {
        "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
        "X-B3-Spanid": "32befd146889a61a",
    }


@pytest.mark.asyncio
async def test_parent_intent_dispatches_to_swe_callback_like_child() -> None:
    store = _DispatchStore(
        [
            {
                "id": 2,
                "batch_id": "batch-1",
                "intent_role": "parent",
                "tenant_id": "tenant-a",
                "source_id": "source-a",
                "agent_id": "default",
                "job_id": "parent-1",
                "payload": {
                    "parent_scheduled_fire_at": "2026-07-01T01:00:00+00:00",
                },
            },
        ],
    )
    callback = _CallbackClient()
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=callback,
        worker_id="scheduler-1",
        effective_workers=1,
    )

    await service.dispatch_ready_once(
        now_utc=datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc),
    )

    assert callback.requests == [
        {
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "scope_id": "tenant-a-source-a",
            "from_id": "tenant-a",
            "agent_id": "default",
            "job_id": "parent-1",
            "dispatch_attempt": 1,
            "execution_key": "parent-1:batch-1:2",
            "dispatch_intent_id": 2,
            "dispatch_batch_id": "batch-1",
            "parent_scheduled_fire_at": "2026-07-01T01:00:00+00:00",
            "provider_id": "default",
            "model_id": "default",
        },
    ]
    assert store.child_batches == []
    assert store.completed == []
    assert store.dispatched[0]["intent_id"] == 2


@pytest.mark.asyncio
async def test_callback_failure_schedules_retry_instead_of_completing() -> (
    None
):
    store = _DispatchStore(
        [
            {
                "id": 3,
                "batch_id": "batch-1",
                "intent_role": "child",
                "tenant_id": "tenant-b",
                "source_id": "source-a",
                "agent_id": "default",
                "job_id": "child-1",
            },
        ],
    )
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=_CallbackClient(fail=True),
        worker_id="scheduler-1",
        effective_workers=1,
        retry_delay_seconds=120,
    )

    await service.dispatch_ready_once(
        now_utc=datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc),
    )

    assert store.dispatched == []
    assert store.failed[0]["intent_id"] == 3
    assert store.failed[0]["retry_delay_seconds"] == 120
    assert "callback failed" in store.failed[0]["error"]


@pytest.mark.asyncio
async def test_callback_unknown_outcome_stays_dispatched_without_retry(
) -> None:
    store = _DispatchStore(
        [
            {
                "id": 3,
                "batch_id": "batch-1",
                "intent_role": "child",
                "tenant_id": "tenant-b",
                "source_id": "source-a",
                "agent_id": "default",
                "job_id": "child-1",
            },
        ],
    )
    transport_error = service_module.httpx.ReadTimeout("")
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=_CallbackClient(
            error=service_module.SweCronCallbackOutcomeUnknownError(
                transport_error,
            ),
        ),
        worker_id="scheduler-1",
        effective_workers=1,
    )

    dispatched = await service.dispatch_ready_once(
        now_utc=datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc),
    )

    assert dispatched == 1
    assert store.failed == []
    assert store.dispatched == []
    assert store.unknown_dispatches[0]["intent_id"] == 3
    assert store.unknown_dispatches[0]["details"]["callback_outcome"] == (
        "unknown"
    )
    assert store.unknown_dispatches[0]["details"]["error_type"] == (
        "ReadTimeout"
    )
    assert store.unknown_dispatches[0]["details"]["error"]


@pytest.mark.asyncio
async def test_capacity_adjustment_is_interval_gated_and_separate() -> None:
    store = _DispatchStore([])
    store.strategy = WorkerStrategy(
        strategy_id="unit",
        min_workers=1,
        baseline_workers=1,
        max_workers=4,
        adjust_interval_seconds=300,
        feedback_window_seconds=300,
        stale_execution_seconds=7800,
        error_rate_rules=[
            {
                "min_error_rate": 0,
                "max_error_rate": 0,
                "operation": "add",
                "value": 1,
                "reason": "stable_success",
            },
        ],
    )
    store.latest_capacity = {
        "effective_workers": 1,
        "created_at": datetime(2026, 7, 1, 0, 50, tzinfo=timezone.utc),
    }
    store.feedback = {
        "pending_count": 5,
        "claimed_count": 0,
        "running_count": 1,
        "success_count": 4,
        "failure_count": 0,
        "latency_p95_ms": 100,
    }
    callback = _CallbackClient()
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=callback,
        worker_id="scheduler-1",
        baseline_workers=1,
        max_workers=4,
        effective_workers=1,
        capacity_adjust_interval_seconds=300,
    )
    first = datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc)

    adjusted = await service.adjust_worker_capacity_if_due(now_utc=first)
    skipped = await service.adjust_worker_capacity_if_due(
        now_utc=first + timedelta(seconds=299),
    )

    assert adjusted is True
    assert skipped is False
    assert len(store.scope_leases) == 1
    assert store.scope_leases[0]["worker_id"] == "scheduler-1"
    assert service.effective_workers == 2
    assert store.capacity[0]["effective_workers"] == 2
    assert store.capacity[0]["decision_reason"] == "stable_success"
    assert callback.requests == []
    assert store.claims == []


@pytest.mark.asyncio
async def test_capacity_adjustment_rechecks_interval_after_acquiring_lease(
) -> None:
    store = _DispatchStore([])
    store.latest_capacity = {
        "effective_workers": 1,
        "created_at": datetime(2026, 7, 1, 0, 50, tzinfo=timezone.utc),
    }
    now = datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc)

    async def acquire_scope_lease(**kwargs):
        store.scope_leases.append(kwargs)
        store.latest_capacity = {
            "effective_workers": 2,
            "created_at": now - timedelta(seconds=1),
        }
        return True

    store.acquire_scope_lease = acquire_scope_lease
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=_CallbackClient(),
        worker_id="scheduler-2",
    )

    adjusted = await service.adjust_worker_capacity_if_due(now_utc=now)

    assert adjusted is False
    assert len(store.scope_leases) == 1
    assert store.latest_capacity_reads == 2
    assert store.capacity == []


@pytest.mark.asyncio
async def test_capacity_loop_runs_while_dispatch_loop_is_blocked() -> None:
    service = CronSchedulingService(
        dispatch_store=_DispatchStore([]),
        callback_client=_CallbackClient(),
    )
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()
    capacity_checked = asyncio.Event()
    stop_event = asyncio.Event()

    async def blocked_dispatch(**_kwargs):
        dispatch_started.set()
        await release_dispatch.wait()
        return 0

    async def check_capacity(**_kwargs):
        capacity_checked.set()
        stop_event.set()
        return False

    service.dispatch_ready_once = blocked_dispatch
    service.adjust_worker_capacity_if_due = check_capacity
    loop_task = asyncio.create_task(service.run_loop(stop_event=stop_event))

    try:
        await asyncio.wait_for(dispatch_started.wait(), timeout=1)
        await asyncio.wait_for(capacity_checked.wait(), timeout=1)
    finally:
        release_dispatch.set()
        stop_event.set()
        await asyncio.wait_for(loop_task, timeout=1)


def test_capacity_loop_default_check_interval_is_sixty_seconds() -> None:
    parameter = inspect.signature(CronSchedulingService.run_loop).parameters[
        "capacity_interval_seconds"
    ]

    assert parameter.default == 60


@pytest.mark.asyncio
async def test_dispatch_loop_failure_does_not_stop_capacity_loop() -> None:
    service = CronSchedulingService(
        dispatch_store=_DispatchStore([]),
        callback_client=_CallbackClient(),
    )
    capacity_checked = asyncio.Event()
    stop_event = asyncio.Event()

    async def failed_dispatch(**_kwargs):
        raise RuntimeError("dispatch unavailable")

    async def check_capacity(**_kwargs):
        capacity_checked.set()
        stop_event.set()
        return False

    service.dispatch_ready_once = failed_dispatch
    service.adjust_worker_capacity_if_due = check_capacity

    await asyncio.wait_for(service.run_loop(stop_event=stop_event), timeout=1)

    assert capacity_checked.is_set()


@pytest.mark.asyncio
async def test_capacity_loop_failure_does_not_stop_dispatch_loop() -> None:
    service = CronSchedulingService(
        dispatch_store=_DispatchStore([]),
        callback_client=_CallbackClient(),
    )
    capacity_started = asyncio.Event()
    dispatch_checked = asyncio.Event()
    stop_event = asyncio.Event()

    async def check_dispatch(**_kwargs):
        await capacity_started.wait()
        dispatch_checked.set()
        stop_event.set()
        return 0

    async def failed_capacity(**_kwargs):
        capacity_started.set()
        raise RuntimeError("capacity unavailable")

    service.dispatch_ready_once = check_dispatch
    service.adjust_worker_capacity_if_due = failed_capacity

    await asyncio.wait_for(service.run_loop(stop_event=stop_event), timeout=1)

    assert dispatch_checked.is_set()


@pytest.mark.asyncio
async def test_capacity_adjustment_skips_scope_without_lease() -> None:
    store = _DispatchStore([])
    store.scope_lease_allowed = False
    store.latest_capacity = None
    store.feedback = {
        **store.feedback,
        "pending_count": 3,
        "success_count": 2,
        "failure_count": 0,
    }
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=_CallbackClient(),
        worker_id="scheduler-2",
        baseline_workers=1,
        max_workers=4,
    )

    adjusted = await service.adjust_worker_capacity_if_due(
        now_utc=datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc),
    )

    assert adjusted is False
    assert store.scope_leases
    assert store.capacity == []


@pytest.mark.asyncio
async def test_dispatch_ready_only_claims_available_worker_slots() -> None:
    store = _DispatchStore(
        [
            {
                "id": 4,
                "batch_id": "batch-1",
                "intent_role": "child",
                "tenant_id": "tenant-b",
                "source_id": "source-a",
                "agent_id": "default",
                "job_id": "child-1",
            },
            {
                "id": 5,
                "batch_id": "batch-1",
                "intent_role": "child",
                "tenant_id": "tenant-c",
                "source_id": "source-a",
                "agent_id": "default",
                "job_id": "child-2",
            },
        ],
    )
    store.feedback = {
        **store.feedback,
        "claimed_count": 1,
        "running_count": 1,
    }
    store.strategy = WorkerStrategy(
        strategy_id="unit",
        min_workers=1,
        baseline_workers=3,
        max_workers=3,
        adjust_interval_seconds=300,
        feedback_window_seconds=300,
        stale_execution_seconds=7800,
        error_rate_rules=[],
    )
    store.latest_capacity = {
        "effective_workers": 3,
        "created_at": datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc),
    }
    callback = _CallbackClient()
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=callback,
        worker_id="scheduler-1",
        max_workers=3,
        effective_workers=3,
    )

    dispatched = await service.dispatch_ready_once(
        now_utc=datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc),
    )

    assert dispatched == 1
    assert store.scope_leases[0]["source_id"] == "source-a"
    assert store.scope_leases[0]["provider_id"] == "default"
    assert store.scope_leases[0]["model_id"] == "default"
    assert store.claims[0]["limit"] == 1
    assert len(callback.requests) == 1


@pytest.mark.asyncio
async def test_dispatch_ready_skips_scope_without_lease() -> None:
    store = _DispatchStore(
        [
            {
                "id": 6,
                "batch_id": "batch-1",
                "intent_role": "child",
                "tenant_id": "tenant-b",
                "source_id": "source-a",
                "agent_id": "default",
                "job_id": "child-1",
            },
        ],
    )
    store.latest_capacity = None
    store.scope_lease_allowed = False
    callback = _CallbackClient()
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=callback,
        worker_id="scheduler-1",
        effective_workers=1,
    )

    dispatched = await service.dispatch_ready_once(
        now_utc=datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc),
    )

    assert dispatched == 0
    assert store.scope_leases
    assert store.capacity == []
    assert store.claims == []
    assert callback.requests == []


@pytest.mark.asyncio
async def test_dispatch_ready_recovers_stale_dispatched_before_capacity_gate() -> (
    None
):
    store = _DispatchStore(
        [
            {
                "id": 6,
                "batch_id": "batch-1",
                "intent_role": "child",
                "tenant_id": "tenant-b",
                "source_id": "source-a",
                "agent_id": "default",
                "job_id": "child-1",
            },
        ],
    )
    store.feedback = {
        **store.feedback,
        "running_count": 1,
    }
    store.latest_capacity = None
    store.recovered_running_count = 0
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=_CallbackClient(),
        worker_id="scheduler-1",
        effective_workers=1,
    )

    dispatched = await service.dispatch_ready_once(
        now_utc=datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc),
        source_ids=["source-a"],
    )

    assert dispatched == 1
    assert store.stale_recoveries[0]["source_ids"] == ["source-a"]
    assert store.claims[0]["limit"] == 1


@pytest.mark.asyncio
async def test_execution_completion_marks_intent_and_dispatches_next() -> None:
    store = _DispatchStore([])
    callback = _CallbackClient()
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=callback,
        worker_id="scheduler-1",
        effective_workers=1,
    )

    await service.handle_execution_recorded(
        execution_id=42,
        status="success",
        meta={
            "cron_dispatch": {
                "intent_id": 7,
                "batch_id": "batch-1",
                "dispatch_attempt": 1,
            },
        },
        completed_at=datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc),
    )

    assert store.completed[0]["intent_id"] == 7
    assert store.completed[0]["execution_id"] == 42
    assert store.completed[0]["expected_attempt_count"] == 1
    assert store.claims, "completion should immediately try to refill capacity"


@pytest.mark.asyncio
async def test_execution_failure_keeps_error_message_for_retry_log() -> None:
    store = _DispatchStore([])
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=_CallbackClient(),
        worker_id="scheduler-1",
        effective_workers=1,
    )

    await service.handle_execution_recorded(
        execution_id=43,
        status="failed",
        meta={"cron_dispatch": {"intent_id": 8, "batch_id": "batch-1"}},
        error_message="provider timeout",
        completed_at=datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc),
    )

    assert store.completed[0]["intent_id"] == 8
    assert store.completed[0]["error"] == "provider timeout"


@pytest.mark.asyncio
async def test_execution_failure_uses_configured_retry_delay() -> None:
    store = _DispatchStore([])
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=_CallbackClient(),
        worker_id="scheduler-1",
        effective_workers=1,
        retry_delay_seconds=45,
    )

    await service.handle_execution_recorded(
        execution_id=44,
        status="failed",
        meta={"cron_dispatch": {"intent_id": 9, "batch_id": "batch-1"}},
        completed_at=datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc),
    )

    assert store.completed[0]["retry_delay_seconds"] == 45


@pytest.mark.asyncio
async def test_scheduler_tick_does_not_scan_due_parent_jobs(
    monkeypatch,
) -> None:
    from scheduler.app.services.cron import scheduling_service as module

    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    store = _DispatchStore([])
    callback = _CallbackClient()
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=callback,
        worker_id="scheduler-1",
        effective_workers=1,
    )

    async def _fail_fetch_due_parent_jobs(**_kwargs):
        raise AssertionError("scheduler must not scan parent jobs")

    monkeypatch.setattr(
        module,
        "_fetch_due_parent_jobs",
        _fail_fetch_due_parent_jobs,
    )

    result = await service.run_scheduler_once(
        now_utc=datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc),
    )

    assert result["queued_parent_intents"] == 0
    assert result["dispatched_intents"] == 0
    assert store.parent_intents == []
    assert store.claims
    assert callback.requests == []


@pytest.mark.asyncio
async def test_scheduler_tick_dispatches_due_pending_retry_intents() -> None:
    store = _DispatchStore(
        [
            {
                "id": 1,
                "batch_id": "batch-1",
                "intent_role": "child",
                "tenant_id": "tenant-b",
                "source_id": "source-a",
                "agent_id": "default",
                "job_id": "child-1",
            },
        ],
    )
    callback = _CallbackClient()
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=callback,
        worker_id="scheduler-1",
        effective_workers=1,
    )

    result = await service.run_scheduler_once(
        now_utc=datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc),
    )

    assert result["dispatched_intents"] == 1
    assert store.claims[0]["limit"] == 1
    assert callback.requests[0]["job_id"] == "child-1"


def test_dispatch_batch_id_is_stable_per_scheduled_fire() -> None:
    parent = {"id": "parent-1"}
    first_fire = datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc)
    duplicate_first_fire = datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc)
    next_day_fire = datetime(2026, 7, 2, 1, 0, tzinfo=timezone.utc)

    first_batch_id = service_module._build_dispatch_batch_id(
        parent,
        first_fire,
    )

    assert (
        service_module._build_dispatch_batch_id(parent, duplicate_first_fire)
        == first_batch_id
    )
    assert (
        service_module._build_dispatch_batch_id(parent, next_day_fire)
        != first_batch_id
    )


@pytest.mark.asyncio
async def test_parent_callback_creates_batch_and_execution_intents(
    monkeypatch,
) -> None:
    from scheduler.app.services.cron import scheduling_service as module

    store = _DispatchStore([])
    callback = _CallbackClient()
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=callback,
        worker_id="scheduler-1",
        effective_workers=2,
    )
    fire_at = datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc)

    async def _fake_fetch_parent_job_for_callback(**_kwargs):
        return {
            "id": "parent-1",
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "agent_id": "default",
            "meta": '{"broadcast_dispatch_intents_enabled": true}',
        }

    async def _fake_fetch_batch_child_jobs(_parent):
        return [
            {
                "tenant_id": "tenant-b",
                "source_id": "source-a",
                "agent_id": "default",
                "job_id": "child-1",
                "provider_id": "openai",
                "model_id": "gpt-5",
            },
        ]

    monkeypatch.setattr(
        module,
        "_fetch_parent_job_for_callback",
        _fake_fetch_parent_job_for_callback,
    )
    monkeypatch.setattr(
        module,
        "_fetch_batch_child_jobs",
        _fake_fetch_batch_child_jobs,
    )

    result = await service.handle_parent_callback(
        params={
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "agent_id": "default",
            "job_id": "parent-1",
            "scheduled_fire_at": fire_at.isoformat(),
            "provider_id": "dashscope",
            "model_id": "qwen-max",
        },
        now_utc=fire_at,
    )

    assert result["enqueued_intents"] == 2
    assert store.batch_upserts[0]["parent_job_id"] == "parent-1"
    assert store.batch_upserts[0]["provider_id"] == "dashscope"
    assert store.batch_upserts[0]["model_id"] == "qwen-max"
    jobs = store.execution_batches[0]["jobs"]
    assert {job["job_id"] for job in jobs} == {"parent-1", "child-1"}
    jobs_by_id = {job["job_id"]: job for job in jobs}
    assert jobs_by_id["parent-1"]["provider_id"] == "dashscope"
    assert jobs_by_id["parent-1"]["model_id"] == "qwen-max"
    assert jobs_by_id["child-1"]["provider_id"] == "openai"
    assert jobs_by_id["child-1"]["model_id"] == "gpt-5"
    assert all(
        job["payload"]["parent_scheduled_fire_at"] == fire_at.isoformat()
        for job in jobs
    )
    assert store.claims, "parent callback should immediately try to dispatch"


@pytest.mark.asyncio
async def test_parent_callback_dispatches_using_current_batch_model_scope(
    monkeypatch,
) -> None:
    from scheduler.app.services.cron import scheduling_service as module

    store = _DispatchStore([])
    store.latest_capacity = None
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=_CallbackClient(),
        worker_id="scheduler-1",
        effective_workers=2,
    )
    fire_at = datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc)

    async def _fake_fetch_parent_job_for_callback(**_kwargs):
        return {
            "id": "parent-1",
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "agent_id": "default",
            "meta": '{"broadcast_dispatch_intents_enabled": true}',
        }

    async def _fake_fetch_batch_child_jobs(_parent):
        return []

    monkeypatch.setattr(
        module,
        "_fetch_parent_job_for_callback",
        _fake_fetch_parent_job_for_callback,
    )
    monkeypatch.setattr(
        module,
        "_fetch_batch_child_jobs",
        _fake_fetch_batch_child_jobs,
    )

    await service.handle_parent_callback(
        params={
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "agent_id": "default",
            "job_id": "parent-1",
            "scheduled_fire_at": fire_at.isoformat(),
            "provider_id": "dashscope",
            "model_id": "qwen-max",
        },
        now_utc=fire_at,
    )

    assert store.capacity[0]["source_id"] == "source-a"
    assert store.capacity[0]["provider_id"] == "dashscope"
    assert store.capacity[0]["model_id"] == "qwen-max"
    assert store.claims[0]["provider_id"] == "dashscope"
    assert store.claims[0]["model_id"] == "qwen-max"


@pytest.mark.asyncio
async def test_dispatch_ready_without_real_scopes_does_not_register_default_capacity() -> (
    None
):
    store = _DispatchStore([])
    store.scopes = []
    store.latest_capacity = None
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=_CallbackClient(),
        worker_id="scheduler-1",
        effective_workers=1,
    )

    dispatched = await service.dispatch_ready_once(
        now_utc=datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc),
    )

    assert dispatched == 0
    assert store.capacity == []
    assert store.claims == []


@pytest.mark.asyncio
async def test_parent_callback_child_model_identity_falls_back_to_parent(
    monkeypatch,
) -> None:
    from scheduler.app.services.cron import scheduling_service as module

    store = _DispatchStore([])
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=_CallbackClient(),
        worker_id="scheduler-1",
        effective_workers=2,
    )
    fire_at = datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc)

    async def _fake_fetch_parent_job_for_callback(**_kwargs):
        return {
            "id": "parent-1",
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "agent_id": "default",
            "meta": '{"broadcast_dispatch_intents_enabled": true}',
        }

    async def _fake_fetch_batch_child_jobs(_parent):
        return [
            {
                "tenant_id": "tenant-b",
                "source_id": "source-a",
                "agent_id": "default",
                "job_id": "child-1",
            },
        ]

    monkeypatch.setattr(
        module,
        "_fetch_parent_job_for_callback",
        _fake_fetch_parent_job_for_callback,
    )
    monkeypatch.setattr(
        module,
        "_fetch_batch_child_jobs",
        _fake_fetch_batch_child_jobs,
    )

    await service.handle_parent_callback(
        params={
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "agent_id": "default",
            "job_id": "parent-1",
            "scheduled_fire_at": fire_at.isoformat(),
            "provider_id": "dashscope",
            "model_id": "qwen-max",
        },
        now_utc=fire_at,
    )

    jobs_by_id = {
        job["job_id"]: job for job in store.execution_batches[0]["jobs"]
    }
    assert jobs_by_id["child-1"]["provider_id"] == "dashscope"
    assert jobs_by_id["child-1"]["model_id"] == "qwen-max"


@pytest.mark.asyncio
async def test_parent_callback_adds_b3_headers_to_execution_intents(
    monkeypatch,
) -> None:
    from scheduler.app.services.cron import scheduling_service as module

    store = _DispatchStore([])
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=_CallbackClient(),
        worker_id="scheduler-1",
        effective_workers=2,
    )
    fire_at = datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc)

    async def _fake_fetch_parent_job_for_callback(**_kwargs):
        return {
            "id": "parent-1",
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "agent_id": "default",
            "meta": '{"broadcast_dispatch_intents_enabled": true}',
        }

    async def _fake_fetch_batch_child_jobs(_parent):
        return [
            {
                "tenant_id": "tenant-b",
                "source_id": "source-a",
                "agent_id": "default",
                "job_id": "child-1",
            },
        ]

    monkeypatch.setattr(
        module,
        "_fetch_parent_job_for_callback",
        _fake_fetch_parent_job_for_callback,
    )
    monkeypatch.setattr(
        module,
        "_fetch_batch_child_jobs",
        _fake_fetch_batch_child_jobs,
    )

    await service.handle_parent_callback(
        params={
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "agent_id": "default",
            "job_id": "parent-1",
            "scheduled_fire_at": fire_at.isoformat(),
        },
        headers={
            "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
            "X-B3-Spanid": "32befd146889a61a",
            "X-B3-BusinessId": "LQ1303LMES-WEB",
        },
        now_utc=fire_at,
    )

    jobs = store.execution_batches[0]["jobs"]
    assert all(
        job["payload"]["passthrough_headers"]
        == {
            "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
            "X-B3-Spanid": "32befd146889a61a",
            "X-B3-BusinessId": "LQ1303LMES-WEB",
        }
        for job in jobs
    )


@pytest.mark.asyncio
async def test_scheduler_parent_callback_passes_headers_to_service() -> None:
    captured: dict[str, Any] = {}

    class _Service:
        async def handle_parent_callback(self, **kwargs):
            captured.update(kwargs)
            return {
                "batch_id": "batch-1",
                "parent_job_id": "parent-1",
                "child_count": 0,
                "enqueued_intents": 0,
                "dispatched_intents": 0,
            }

    response = await scheduler_cron.scheduler_parent_callback(
        request=SimpleNamespace(
            headers={
                "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
                "X-B3-Spanid": "32befd146889a61a",
            },
        ),
        body={"job_id": "parent-1"},
        scheduling_service=_Service(),
    )

    assert response.batch_id == "batch-1"
    assert captured["params"] == {"job_id": "parent-1"}
    assert captured["headers"] == {
        "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
        "X-B3-Spanid": "32befd146889a61a",
    }


@pytest.mark.asyncio
async def test_parent_callback_adds_batch_offset_to_trigger_time(
    monkeypatch,
) -> None:
    from scheduler.app.services.cron import scheduling_service as module

    store = _DispatchStore([])
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=_CallbackClient(),
        worker_id="scheduler-1",
        effective_workers=2,
    )
    trigger_at = datetime(2026, 7, 1, 21, 0, tzinfo=timezone.utc)
    expected_fire_at = trigger_at + timedelta(minutes=240)

    async def _fake_fetch_parent_job_for_callback(**_kwargs):
        return {
            "id": "parent-1",
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "agent_id": "default",
            "meta": '{"broadcast_dispatch_intents_enabled": true}',
        }

    async def _fake_fetch_batch_child_jobs(_parent):
        return []

    monkeypatch.setattr(
        module,
        "_fetch_parent_job_for_callback",
        _fake_fetch_parent_job_for_callback,
    )
    monkeypatch.setattr(
        module,
        "_fetch_batch_child_jobs",
        _fake_fetch_batch_child_jobs,
    )

    await service.handle_parent_callback(
        params={
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "agent_id": "default",
            "job_id": "parent-1",
            "trigger_time": trigger_at.isoformat(),
            "batch_dispatch_offset_minutes": 240,
            "swe_server_domain": "http://tenant-swe.local",
        },
        now_utc=trigger_at,
    )

    assert store.batch_upserts[0]["scheduled_fire_at"] == expected_fire_at
    jobs = store.execution_batches[0]["jobs"]
    assert [job["job_id"] for job in jobs] == ["parent-1"]
    assert (
        jobs[0]["payload"]["parent_scheduled_fire_at"]
        == expected_fire_at.isoformat()
    )
    assert jobs[0]["payload"]["swe_server_domain"] == "http://tenant-swe.local"


def test_parent_callback_uses_upcoming_fire_inside_batch_prefire_window() -> (
    None
):
    callback_received_at = datetime(
        2026,
        7,
        15,
        8,
        0,
        1,
        tzinfo=timezone.utc,
    )

    scheduled_fire_at = service_module._callback_scheduled_fire_at(
        {"batch_dispatch_offset_minutes": 240},
        {"cron_expr": "0 20 * * *", "timezone": "Asia/Shanghai"},
        callback_received_at,
    )

    assert scheduled_fire_at == datetime(
        2026,
        7,
        15,
        12,
        0,
        tzinfo=timezone.utc,
    )


@pytest.mark.asyncio
async def test_parent_callback_job_param_prefire_uses_upcoming_fire(
    monkeypatch,
) -> None:
    from scheduler.app.services.cron import scheduling_service as module

    store = _DispatchStore([])
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=_CallbackClient(),
        worker_id="scheduler-1",
        effective_workers=1,
    )
    callback_received_at = datetime(
        2026,
        7,
        15,
        8,
        0,
        1,
        tzinfo=timezone.utc,
    )
    expected_fire_at = datetime(
        2026,
        7,
        15,
        12,
        0,
        tzinfo=timezone.utc,
    )

    async def _fake_fetch_parent_job_for_callback(**_kwargs):
        return {
            "id": "parent-1",
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "agent_id": "default",
            "cron_expr": "0 20 * * *",
            "timezone": "Asia/Shanghai",
            "meta": '{"broadcast_dispatch_intents_enabled": true}',
        }

    async def _fake_fetch_batch_child_jobs(_parent):
        return []

    monkeypatch.setattr(
        module,
        "_fetch_parent_job_for_callback",
        _fake_fetch_parent_job_for_callback,
    )
    monkeypatch.setattr(
        module,
        "_fetch_batch_child_jobs",
        _fake_fetch_batch_child_jobs,
    )
    job_param = base64.urlsafe_b64encode(
        json.dumps(
            {
                "tenant_id": "tenant-a",
                "source_id": "source-a",
                "agent_id": "default",
                "job_id": "parent-1",
                "batch_dispatch_offset_minutes": 240,
            },
        ).encode("utf-8"),
    ).decode("ascii")

    await service.handle_parent_callback(
        params={"jobParam": job_param},
        now_utc=callback_received_at,
    )

    assert store.batch_upserts[0]["scheduled_fire_at"] == expected_fire_at
    parent_intent = store.execution_batches[0]["jobs"][0]
    assert (
        parent_intent["payload"]["parent_scheduled_fire_at"]
        == expected_fire_at.isoformat()
    )


def test_parent_callback_uses_upcoming_fire_across_midnight() -> None:
    callback_received_at = datetime(
        2026,
        7,
        15,
        13,
        0,
        1,
        tzinfo=timezone.utc,
    )

    scheduled_fire_at = service_module._callback_scheduled_fire_at(
        {"batch_dispatch_offset_minutes": 240},
        {"cron_expr": "0 1 * * *", "timezone": "Asia/Shanghai"},
        callback_received_at,
    )

    assert scheduled_fire_at == datetime(
        2026,
        7,
        15,
        17,
        0,
        tzinfo=timezone.utc,
    )


def test_parent_callback_keeps_previous_fire_after_prefire_window() -> None:
    callback_received_at = datetime(
        2026,
        7,
        15,
        12,
        5,
        tzinfo=timezone.utc,
    )

    scheduled_fire_at = service_module._callback_scheduled_fire_at(
        {"batch_dispatch_offset_minutes": 240},
        {"cron_expr": "0 20 * * *", "timezone": "Asia/Shanghai"},
        callback_received_at,
    )

    assert scheduled_fire_at == datetime(
        2026,
        7,
        15,
        12,
        0,
        tzinfo=timezone.utc,
    )


@pytest.mark.asyncio
async def test_parent_callback_rejects_stale_dispatch_intent_meta(
    monkeypatch,
) -> None:
    from scheduler.app.services.cron import scheduling_service as module

    store = _DispatchStore([])
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=_CallbackClient(),
        worker_id="scheduler-1",
        effective_workers=1,
    )
    fire_at = datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc)

    async def _fake_fetch_parent_job_for_callback(**_kwargs):
        return {
            "id": "parent-1",
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "agent_id": "default",
            "meta": '{"dispatch_intents_enabled": true}',
        }

    monkeypatch.setattr(
        module,
        "_fetch_parent_job_for_callback",
        _fake_fetch_parent_job_for_callback,
    )

    with pytest.raises(RuntimeError, match="batch parent dispatch disabled"):
        await service.handle_parent_callback(
            params={
                "tenant_id": "tenant-a",
                "source_id": "source-a",
                "agent_id": "default",
                "job_id": "parent-1",
                "scheduled_fire_at": fire_at.isoformat(),
            },
            now_utc=fire_at,
        )

    assert store.batch_upserts == []


@pytest.mark.asyncio
async def test_worker_strategy_adjusts_by_terminal_error_rate() -> None:
    store = _DispatchStore([])
    store.strategy = WorkerStrategy(
        strategy_id="halve-on-errors",
        min_workers=1,
        baseline_workers=4,
        max_workers=8,
        adjust_interval_seconds=60,
        feedback_window_seconds=300,
        stale_execution_seconds=7800,
        error_rate_rules=[
            {
                "min_error_rate": 0.5,
                "operation": "multiply",
                "value": 0.5,
                "reason": "high_error_rate",
            },
        ],
    )
    store.latest_capacity = {
        "effective_workers": 4,
        "created_at": datetime(2026, 7, 1, 0, 58, tzinfo=timezone.utc),
    }
    store.feedback = {
        "pending_count": 10,
        "claimed_count": 0,
        "running_count": 4,
        "success_count": 2,
        "failure_count": 2,
        "latency_p95_ms": 0,
    }
    service = CronSchedulingService(
        dispatch_store=store,
        callback_client=_CallbackClient(),
        worker_id="scheduler-1",
    )

    adjusted = await service.adjust_worker_capacity_if_due(
        now_utc=datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc),
    )

    assert adjusted is True
    assert store.capacity[0]["previous_workers"] == 4
    assert store.capacity[0]["effective_workers"] == 2
    assert store.capacity[0]["error_rate"] == 0.5
    assert store.capacity[0]["decision_reason"] == "high_error_rate"

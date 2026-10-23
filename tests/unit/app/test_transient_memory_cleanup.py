# -*- coding: utf-8 -*-
"""Lifecycle cleanup remains cancellable and isolates store failures."""

import asyncio
import contextvars
import gc
import time
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.transient_memory_cleanup import TransientMemoryCleanup


def test_default_cleanup_interval_is_one_minute():
    assert TransientMemoryCleanup()._interval_seconds == 60.0


@pytest.mark.asyncio
async def test_cleanup_continues_after_failure_and_can_retry():
    calls = []

    async def failing():
        calls.append("failing")
        raise RuntimeError("store failed")

    async def healthy():
        calls.append("healthy")

    cleaner = TransientMemoryCleanup(cleaners=(failing, healthy), resetters=())
    await cleaner.cleanup_once()
    await cleaner.cleanup_once()
    assert calls == ["failing", "healthy", "failing", "healthy"]
    assert cleaner.cleanup_errors_total == 2


@pytest.mark.asyncio
async def test_start_stop_idempotent_and_restart_has_no_orphan_task():
    cleaned = asyncio.Event()

    async def clean():
        cleaned.set()

    cleaner = TransientMemoryCleanup(
        cleaners=(clean,),
        resetters=(),
        interval_seconds=0.001,
    )
    await cleaner.start()
    task = cleaner._task
    await cleaner.start()
    assert cleaner._task is task
    await asyncio.wait_for(cleaned.wait(), 1)
    await cleaner.stop()
    await cleaner.stop()
    assert task.done()
    assert cleaner._task is None
    cleaned.clear()
    await cleaner.start()
    await asyncio.wait_for(cleaned.wait(), 1)
    await cleaner.stop()


@pytest.mark.asyncio
async def test_stop_cancels_in_progress_cleanup_without_resetting_stores():
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    reset_calls = []

    async def clean():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def reset():
        reset_calls.append("reset")

    cleaner = TransientMemoryCleanup(
        cleaners=(clean,),
        resetters=(reset,),
        interval_seconds=0.001,
    )
    await cleaner.start()
    await asyncio.wait_for(entered.wait(), 1)
    await cleaner.stop()
    assert cancelled.is_set()
    assert reset_calls == []
    await cleaner.reset()
    assert reset_calls == ["reset"]


@pytest.mark.asyncio
async def test_reset_isolates_errors():
    calls = []

    async def failing():
        raise RuntimeError("reset failed")

    async def healthy():
        calls.append("reset")

    cleaner = TransientMemoryCleanup(cleaners=(), resetters=(failing, healthy))
    await cleaner.reset()
    assert calls == ["reset"]
    assert cleaner.cleanup_errors_total == 1


@pytest.mark.asyncio
async def test_stop_propagates_callers_cancellation_during_cleanup_teardown():
    entered = asyncio.Event()
    tearing_down = asyncio.Event()

    async def clean():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            tearing_down.set()
            await asyncio.Event().wait()

    cleaner = TransientMemoryCleanup(cleaners=(clean,), interval_seconds=0.001)
    await cleaner.start()
    await asyncio.wait_for(entered.wait(), 1)
    stopping = asyncio.create_task(cleaner.stop())
    await asyncio.wait_for(tearing_down.wait(), 1)
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert cleaner._task is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_producer",
    [None, "manager", "pool", "pending-summary"],
)
async def test_shutdown_stops_sweeper_before_producers_and_resets_after(
    monkeypatch,
    failed_producer,
):
    from swe.agents.tools import background_process
    from swe.agents.memory import base_memory_manager
    from swe.app import _app as app_module

    order = []

    async def record(name):
        order.append(name)
        return name != failed_producer

    cleaner = SimpleNamespace(
        stop=lambda: record("sweeper-stop"),
        reset=lambda: record("reset"),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(transient_memory_cleanup=cleaner),
    )
    monkeypatch.setattr(
        background_process.managed_background_process_manager,
        "stop_all",
        lambda: None,
    )
    monkeypatch.setattr(
        app_module,
        "runtime_diagnostic_manager",
        SimpleNamespace(
            stop=AsyncMock(),
            set_workspace_metrics=lambda _: None,
        ),
    )
    monkeypatch.setattr(app_module, "close_trace_manager", AsyncMock())
    monkeypatch.setattr(app_module, "shutdown_global_tracer", lambda: None)
    monkeypatch.setattr(app_module, "stop_service_heartbeat", AsyncMock())
    monkeypatch.setattr(
        app_module,
        "_stop_multi_agent_manager",
        lambda _: record("manager"),
    )
    monkeypatch.setattr(
        app_module,
        "_stop_tenant_workspace_pool",
        lambda _: record("pool"),
    )
    monkeypatch.setattr(app_module, "shutdown_logger", lambda: None)
    monkeypatch.setattr(
        base_memory_manager,
        "summary_task_stats",
        lambda: {"pending": int(failed_producer == "pending-summary")},
    )
    await app_module._shutdown_lifespan_resources(app, None)
    expected = ["sweeper-stop", "manager", "pool"]
    if failed_producer is None:
        expected.append("reset")
    assert order == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["multi_agent_manager", "tenant_workspace_pool"],
)
async def test_producer_stop_reports_failure(kind):
    from swe.app import _app as app_module

    helper = getattr(app_module, f"_stop_{kind}")
    app = SimpleNamespace(state=SimpleNamespace())
    assert await helper(app) is True
    producer = SimpleNamespace(stop_all=AsyncMock(side_effect=RuntimeError()))
    setattr(app.state, kind, producer)
    assert await helper(app) is False
    producer.stop_all = AsyncMock()
    assert await helper(app) is True


@pytest.mark.asyncio
async def test_default_cleanup_wires_actual_stores_and_summary_registry(
    monkeypatch,
):
    from swe.agents.memory import base_memory_manager as summaries
    from swe.agents.memory.reme_light_memory_manager import (
        ReMeLightMemoryManager,
    )
    from swe.app import console_push_store as push
    from swe.app.suggestions import store

    await push.reset()
    await store.reset()
    manager = object.__new__(ReMeLightMemoryManager)
    summaries.BaseMemoryManager.__init__(manager, "/unused", "cleanup-test")
    monkeypatch.setattr(summaries, "SUMMARY_RESULT_TTL_SECONDS", 0)
    record = manager._make_summary_result(
        1,
        "cleanup-scope",
        "completed",
        time.monotonic(),
        "done",
    )
    manager._summary_results[1] = record
    manager._summary_result_bytes = record.payload_bytes
    monkeypatch.setattr(push, "_MAX_AGE_SECONDS", 0)
    monkeypatch.setattr(store, "_MAX_AGE_SECONDS", 0)
    try:
        assert await push.append("s", "push", tenant_id="cleanup-test")
        await store.store_suggestions("s", ["next"], "cleanup-test")
        await store.store_qa_content("c", "q", "a", "cleanup-test", 0)
        cleaner = TransientMemoryCleanup()
        await cleaner.cleanup_once()
        assert push.get_stats()["entries"] == 0
        assert store.get_stats()["entries"] == 0
        assert store.get_stats()["qa"]["entries"] == 0
        assert not manager._summary_results
        assert manager._summary_result_bytes == 0
        assert cleaner.cleanup_errors_total == 0
        await cleaner.reset()
        assert push.get_stats()["expired_total"] == 0
        assert store.get_stats()["expired_total"] == 0
    finally:
        await push.reset()
        await store.reset()


@pytest.mark.asyncio
async def test_sweeper_does_not_hold_unknown_request_context():
    class RequestState:
        pass

    variable = contextvars.ContextVar("cleanup-request-state")
    state = RequestState()
    ref = weakref.ref(state)
    token = variable.set(state)
    cleaner = TransientMemoryCleanup(interval_seconds=3600)
    try:
        await cleaner.start()
        variable.reset(token)
        del state
        gc.collect()
        assert ref() is None
    finally:
        await cleaner.stop()


@pytest.mark.asyncio
async def test_background_services_start_one_sweeper(monkeypatch):
    from swe.app import _app as app_module
    from swe.app import transient_memory_cleanup
    from swe.security.tool_guard import watcher

    cleaner = SimpleNamespace(start=AsyncMock())
    monkeypatch.setattr(
        transient_memory_cleanup,
        "TransientMemoryCleanup",
        lambda: cleaner,
    )
    monkeypatch.setattr(
        watcher,
        "ToolGuardConfigWatcher",
        lambda: SimpleNamespace(start=AsyncMock()),
    )
    monkeypatch.setattr(app_module, "start_service_heartbeat", AsyncMock())
    monkeypatch.setattr(
        app_module,
        "CronNotificationWorker",
        lambda **_: SimpleNamespace(start=lambda: None),
    )
    monkeypatch.setattr(
        app_module,
        "runtime_diagnostic_manager",
        SimpleNamespace(
            start=AsyncMock(),
            set_workspace_metrics=lambda _: None,
        ),
    )
    manager = SimpleNamespace(
        start_workspace_cleanup_loop=AsyncMock(),
        workspace_cache_metrics=lambda: {},
    )
    app = SimpleNamespace(state=SimpleNamespace())
    await app_module._start_lifespan_background_services(app, manager)
    assert app.state.transient_memory_cleanup is cleaner
    cleaner.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_background_start_failure_still_runs_shutdown(
    monkeypatch,
):
    from swe.app import _app as app_module
    from swe.app import auth

    app = SimpleNamespace(state=SimpleNamespace())
    for name in (
        "_configure_async_thread_pools",
        "ensure_default_agent_exists",
        "_initialize_source_system_config",
        "_initialize_source_tools",
        "_initialize_continuous_governance",
    ):
        monkeypatch.setattr(app_module, name, lambda *a, **k: None)
    monkeypatch.setattr(auth, "auto_register_from_env", lambda: None)
    for name in (
        "_reset_scope_sensitive_runtime_state",
        "_initialize_tracing_manager",
        "_initialize_workflow_config_store",
        "_initialize_approval_audit_store",
        "_initialize_skill_scan_history",
        "_initialize_cron_broadcast_children_store",
        "_initialize_cron_broadcast_task_store",
        "_register_wealth_plan_store",
        "_initialize_skill_readiness",
        "_initialize_database_backed_modules",
    ):
        monkeypatch.setattr(app_module, name, AsyncMock())
    monkeypatch.setattr(
        app_module,
        "_initialize_runtime_managers",
        AsyncMock(return_value=(None, None)),
    )
    monkeypatch.setattr(
        app_module,
        "_initialize_database_connection",
        AsyncMock(return_value=None),
    )
    shutdown = AsyncMock()
    monkeypatch.setattr(app_module, "_shutdown_lifespan_resources", shutdown)
    monkeypatch.setattr(
        app_module,
        "_start_lifespan_background_services",
        AsyncMock(side_effect=RuntimeError("partial background start")),
    )
    with pytest.raises(RuntimeError, match="partial background start"):
        async with app_module.lifespan(app):
            pytest.fail("startup should fail")
    shutdown.assert_awaited_once_with(app, None)

# -*- coding: utf-8 -*-
"""恢复巡检单轮上限与轮询间隔的环境变量读取与装配。"""

import asyncio
from types import SimpleNamespace

import pytest

from scheduler.app.services.cron.managed import runtime


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 32),
        ("", 32),
        ("  ", 32),
        ("1", 1),
        ("64", 64),
        ("200", 200),
        (" 48 ", 48),
        ("0", 32),
        ("201", 32),
        ("-5", 32),
        ("abc", 32),
        ("32.5", 32),
    ],
)
def test_managed_recovery_limit_reads_env(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(runtime.MANAGED_RECOVERY_LIMIT_ENV, raising=False)
    else:
        monkeypatch.setenv(runtime.MANAGED_RECOVERY_LIMIT_ENV, raw)
    assert runtime.managed_recovery_limit() == expected


@pytest.mark.asyncio
async def test_create_managed_worker_passes_env_limit(monkeypatch):
    captured = {}

    class FakeWorker:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    async def _ready(db):
        return None

    async def _rechecks(db):
        return False

    monkeypatch.setenv(runtime.MANAGED_RECOVERY_LIMIT_ENV, "96")
    monkeypatch.setattr(runtime, "assert_managed_schema_ready", _ready)
    monkeypatch.setattr(runtime, "assert_managed_board_schema_ready", _ready)
    monkeypatch.setattr(
        runtime, "enable_managed_capacity_accounting", lambda: None
    )
    monkeypatch.setattr(
        runtime,
        "get_cron_scheduling_service",
        lambda: SimpleNamespace(bind_managed_feedback=lambda feedback: None),
    )
    monkeypatch.setattr(runtime, "ManagedCapacityFeedback", lambda db: None)
    monkeypatch.setattr(runtime, "prepare_recheck_storage", _rechecks)
    monkeypatch.setattr(runtime, "ManagedSweClient", lambda base: None)
    monkeypatch.setattr(runtime, "ManagedSweExecutionClient", lambda base: None)
    monkeypatch.setattr(runtime, "ManagedPlatformClient", lambda config: None)
    monkeypatch.setattr(runtime, "platform_config", lambda: {})
    monkeypatch.setattr(runtime, "ManagedCapacityGate", lambda legacy: None)
    monkeypatch.setattr(
        runtime,
        "BatchRetryPolicy",
        lambda fetch: SimpleNamespace(read_overrides=None),
    )
    monkeypatch.setattr(runtime, "ManagedRecoveryWorker", FakeWorker)

    worker = await runtime.create_managed_worker(object())

    assert isinstance(worker, FakeWorker)
    assert captured["limit"] == 96


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 2),
        ("", 2),
        ("1", 1),
        ("5", 5),
        ("60", 60),
        (" 3 ", 3),
        ("0", 2),
        ("61", 2),
        ("-1", 2),
        ("abc", 2),
    ],
)
def test_managed_recovery_interval_reads_env(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(runtime.MANAGED_RECOVERY_INTERVAL_ENV, raising=False)
    else:
        monkeypatch.setenv(runtime.MANAGED_RECOVERY_INTERVAL_ENV, raw)
    assert runtime.managed_recovery_interval() == expected


@pytest.mark.asyncio
async def test_lifespan_passes_interval_env(monkeypatch):
    seen = {}

    class FakeWorker:
        async def run_loop(self, *, stop_event, interval_seconds):
            seen["interval"] = interval_seconds
            await stop_event.wait()

    app = SimpleNamespace(
        state=SimpleNamespace(
            managed_cron_runtime_prepared=True,
            managed_cron_worker=FakeWorker(),
        )
    )
    monkeypatch.setenv(runtime.MANAGED_RECOVERY_INTERVAL_ENV, "5")

    async with runtime.managed_lifespan(app):
        await asyncio.sleep(0.01)
        assert seen["interval"] == 5
        assert app.state.managed_cron_runtime_ready is True

    assert app.state.managed_cron_runtime_ready is False

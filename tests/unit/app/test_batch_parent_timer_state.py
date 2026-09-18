from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.unit.app.test_external_cron_scope_refresh import (
    CronManager,
    CapturingSchedulerAdapter,
    _JobsRepo,
    _sample_job,
)


@pytest.mark.asyncio
async def test_disabled_parent_keeps_batch_wakeup_timer_enabled(monkeypatch):
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    adapter = CapturingSchedulerAdapter()
    job = _sample_job(external_id="42").model_copy(update={"enabled": False})
    manager = CronManager(
        repo=_JobsRepo([job]),
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id="tenant-a",
        scheduler_adapter=adapter,
    )
    manager._resolve_batch_dispatch_execution_model_params = lambda spec: {}
    await manager._sync_batch_dispatch_scheduler_job(
        job, offset_window_hours=4
    )
    states = [
        payload["runFlag"]
        for path, payload in adapter.requests
        if path.endswith("/update-job-run-states")
    ]
    assert states[-1] == 1


@pytest.mark.asyncio
async def test_enable_requires_persisted_control_initialization(monkeypatch):
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    adapter = CapturingSchedulerAdapter()
    job = _sample_job(external_id="42")
    manager = CronManager(
        repo=_JobsRepo([job]),
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id="tenant-a",
        scheduler_adapter=adapter,
    )
    manager._monitor_sync_client = None
    manager._resolve_batch_dispatch_execution_model_params = lambda spec: {}
    with pytest.raises(RuntimeError, match="定义同步未启用"):
        await manager.enable_batch_dispatch_for_parent(job.id)

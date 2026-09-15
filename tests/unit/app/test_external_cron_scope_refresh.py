# -*- coding: utf-8 -*-
"""外部调度平台 scope 兼容与存量刷新回归测试。"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any

import pytest

from swe.app.crons.manager import CronManager
from swe.app.crons.api import _build_broadcast_job
from swe.app.crons.models import (
    CronJobRequest,
    CronJobSpec,
    DispatchSpec,
    DispatchTarget,
    JobRuntimeSpec,
    JobsFile,
    ScheduleSpec,
)
from swe.app.crons.scheduler_adapter import RealSchedulerAdapter
from swe.app.routers import internal as internal_router
from swe.app.source_system_config.models import (
    EffectiveSourceSystemConfig,
    SourceSystemConfig,
)
from swe.config.context import encode_scope_id
from swe.providers import provider_manager as provider_manager_module
from swe.providers.models import ModelSlotConfig


class CapturingSchedulerAdapter(RealSchedulerAdapter):
    """捕获外部调度请求，避免测试访问真实平台。"""

    def __init__(self) -> None:
        super().__init__(
            base_url="http://scheduler.local",
            job_group=1,
            author="swe",
            alarm_email="",
            client_no="client",
            client_key="key",
            client_remark="remark",
        )
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def _post(self, path: str, payload: dict[str, Any]) -> dict:
        self.requests.append((path, payload))
        if path.endswith("/add-job"):
            return {"content": "1001"}
        return {"content": "ok"}


def _decode_job_param(value: str) -> dict[str, Any]:
    return json.loads(base64.urlsafe_b64decode(value))


def _sample_job(*, external_id: str | None = None) -> CronJobSpec:
    meta: dict[str, Any] = {}
    if external_id:
        meta["external_job_id"] = external_id
    return CronJobSpec(
        id="job-1",
        name="每日巡检",
        enabled=True,
        tenant_id="tenant-a",
        source_id="source-a",
        scope_id=encode_scope_id("tenant-a", "source-a"),
        schedule=ScheduleSpec(
            type="cron",
            cron="0 9 * * *",
            timezone="Asia/Shanghai",
        ),
        task_type="agent",
        request=CronJobRequest(input=[{"content": [{"text": "ping"}]}]),
        dispatch=DispatchSpec(
            type="channel",
            channel="console",
            target=DispatchTarget(user_id="user-a", session_id="session-a"),
        ),
        runtime=JobRuntimeSpec(timeout_seconds=30),
        meta=meta,
    )


def _batch_dispatch_child_job(
    *,
    external_id: str | None = None,
    dispatch_enabled: bool = True,
) -> CronJobSpec:
    job = _sample_job(external_id=external_id)
    meta = dict(job.meta or {})
    meta.update(
        {
            "broadcast_source_job_id": "parent-job",
            "broadcast_dispatch_intents_enabled": dispatch_enabled,
        },
    )
    return job.model_copy(
        update={
            "id": "child-job",
            "tenant_id": "tenant-b",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-b", "source-a"),
            "meta": meta,
        },
    )


class _JobsRepo:
    _path = "jobs.json"

    def __init__(self, jobs: list[CronJobSpec] | None = None) -> None:
        self.jobs = list(jobs or [])

    async def list_jobs(self) -> list[CronJobSpec]:
        return list(self.jobs)

    async def load(self) -> JobsFile:
        return JobsFile(jobs=list(self.jobs))

    async def save(self, jobs_file: JobsFile) -> None:
        self.jobs = list(jobs_file.jobs)

    async def get_job(self, job_id: str) -> CronJobSpec | None:
        for job in self.jobs:
            if job.id == job_id:
                return job
        return None


class _StaticSourceSystemConfigService:
    def __init__(self, raw_config: dict[str, Any] | None = None) -> None:
        self.raw_config = SourceSystemConfig.model_validate(raw_config or {})

    async def resolve_config(
        self,
        source_id: str,
    ) -> EffectiveSourceSystemConfig:
        return EffectiveSourceSystemConfig(
            source_id=source_id,
            config=self.raw_config.merged_with_defaults(),
            raw_config=self.raw_config,
            version=1,
        )


def test_build_broadcast_job_uses_target_tenant_and_current_source() -> None:
    """广播任务必须写入目标租户和当前 source 对应的 runtime scope。"""
    source_job = _sample_job()

    target_job = _build_broadcast_job(
        source_job,
        job_id="job-broadcast",
        target_tenant_id="tenant-b",
        target_tenant_name="目标租户",
        target_bbk_id="3301",
        source_id="source-a",
        cron="0 8 * * *",
        timezone_name="Asia/Shanghai",
        offset_minutes=60,
        model_slot=source_job.model_slot,
        model_slot_fallback_reason="",
    )

    assert target_job.tenant_id == "tenant-b"
    assert target_job.source_id == "source-a"
    assert target_job.scope_id == encode_scope_id("tenant-b", "source-a")
    assert target_job.tenant_name == "目标租户"
    assert target_job.bbk_id == "3301"
    assert target_job.dispatch.target.user_id == "tenant-b"
    assert target_job.request is not None
    assert target_job.request.user_id == "tenant-b"


def test_build_broadcast_job_can_enable_batch_dispatch() -> None:
    source_job = _sample_job()

    target_job = _build_broadcast_job(
        source_job,
        job_id="job-broadcast",
        target_tenant_id="tenant-b",
        target_tenant_name=None,
        target_bbk_id=None,
        source_id="source-a",
        cron="0 8 * * *",
        timezone_name="Asia/Shanghai",
        offset_minutes=0,
        model_slot=source_job.model_slot,
        model_slot_fallback_reason="",
        enable_batch_dispatch=True,
    )

    assert target_job.meta["broadcast_dispatch_intents_enabled"] is True


def test_build_broadcast_job_strips_parent_batch_flag_when_disabled() -> None:
    source_job = _sample_job().model_copy(
        update={"meta": {"broadcast_dispatch_intents_enabled": True}},
    )

    target_job = _build_broadcast_job(
        source_job,
        job_id="job-broadcast",
        target_tenant_id="tenant-b",
        target_tenant_name=None,
        target_bbk_id=None,
        source_id="source-a",
        cron="0 8 * * *",
        timezone_name="Asia/Shanghai",
        offset_minutes=0,
        model_slot=source_job.model_slot,
        model_slot_fallback_reason="",
        enable_batch_dispatch=False,
    )

    assert "broadcast_dispatch_intents_enabled" not in target_job.meta


@pytest.mark.asyncio
async def test_scheduler_payload_uses_logical_tenant_and_source() -> None:
    """调度平台展示和回调参数必须保留原始业务租户和来源。"""
    adapter = CapturingSchedulerAdapter()

    ext_id = await adapter.register_job(
        tenant_id="tenant-a",
        source_id="source-a",
        agent_id="default",
        task_type="job",
        job_id="job-1",
        job_name="每日巡检",
        cron="0 9 * * *",
        callback_url="http://swe.local/api/internal/cron/callback",
    )

    assert ext_id == "1001"
    add_path, payload = adapter.requests[0]
    assert add_path == "/job-admin/v2/add-job"
    assert (
        payload["jobDesc"] == "[SWE] tenant-a/source-a/default/job - 每日巡检"
    )
    job_param = _decode_job_param(payload["jobParam"])
    assert job_param == {
        "tenant_id": "tenant-a",
        "source_id": "source-a",
        "agent_id": "default",
        "task_type": "job",
        "job_id": "job-1",
        "scopeId": "tenant-a-source-a",
        "fromId": "tenant-a",
    }


@pytest.mark.asyncio
async def test_source_cleanup_scheduler_payload_uses_source_only_job_name() -> (
    None
):
    """source 级 cleanup payload 应使用 source 维度 jobDesc 和默认 scopeId。"""
    adapter = CapturingSchedulerAdapter()

    ext_id = await adapter.register_job(
        tenant_id="tenant-a",
        source_id="source-a",
        agent_id="",
        task_type="cleanup",
        job_id="_source_task_session_cleanup",
        job_name="task_session_cleanup",
        cron="30 2 * * *",
        callback_url="http://swe.local/api/internal/cron/callback",
        from_id="alice",
        source_level=True,
    )

    assert ext_id == "1001"
    add_path, payload = adapter.requests[0]
    assert add_path == "/job-admin/v2/add-job"
    assert payload["jobDesc"] == "[SWE] source-a/task_session_cleanup"
    job_param = _decode_job_param(payload["jobParam"])
    assert job_param == {
        "tenant_id": "tenant-a",
        "source_id": "source-a",
        "agent_id": "",
        "task_type": "cleanup",
        "job_id": "_source_task_session_cleanup",
        "scopeId": "tenant-a-source-a",
        "fromId": "alice",
    }


@pytest.mark.asyncio
async def test_scheduler_payload_keeps_full_normalized_cron() -> None:
    """行外注册时不能截断转换后的 cron 表达式。"""
    adapter = CapturingSchedulerAdapter()

    await adapter.register_job(
        tenant_id="tenant-a",
        source_id="source-a",
        agent_id="default",
        task_type="job",
        job_id="job-1",
        job_name="daily-window",
        cron="0 9,10,11,12,13 * * *",
        callback_url="http://swe.local/api/internal/cron/callback",
    )

    _, payload = adapter.requests[0]
    assert payload["jobCron"] == "0 0 9,10,11,12,13 * * ?"


@pytest.mark.asyncio
async def test_cron_manager_system_jobs_do_not_register_cleanup(
    tmp_path,
) -> None:
    """tenant CronManager 初始化系统任务时不再注册 source 级清理任务。"""
    adapter = CapturingSchedulerAdapter()

    class FakeRepo:
        _path = tmp_path / "jobs.json"

        async def list_jobs(self):
            return []

    manager = CronManager(
        repo=FakeRepo(),
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id=encode_scope_id("tenant-a", "source-a"),
        scheduler_adapter=adapter,
        source_system_config_service=_StaticSourceSystemConfigService(
            {
                "cron_task_session_cleanup": {
                    "enabled": True,
                    "retention_days": 30,
                    "cron": "30 2 * * *",
                },
            },
        ),
    )

    await manager._register_system_jobs()

    paths = [path for path, _ in adapter.requests]
    assert "/job-admin/v2/add-job" not in paths


@pytest.mark.asyncio
async def test_scheduler_payload_converts_weekdays_without_mutating_input() -> (
    None
):
    """仅外部 payload 转换星期编号，内部 cron 值保持标准 crontab 语义。"""
    adapter = CapturingSchedulerAdapter()
    cron = "0 9 * * 1-5"

    await adapter.register_job(
        tenant_id="tenant-a",
        source_id="source-a",
        agent_id="default",
        task_type="job",
        job_id="job-1",
        job_name="weekday-window",
        cron=cron,
        callback_url="http://swe.local/api/internal/cron/callback",
    )

    _, payload = adapter.requests[0]
    assert cron == "0 9 * * 1-5"
    assert payload["jobCron"] == "0 0 9 ? * 2-6"


@pytest.mark.asyncio
async def test_callback_resolves_runtime_scope_from_tenant_and_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回调用原始 tenant/source 推导 scope 后再找 CronManager。"""
    observed: dict[str, Any] = {}

    async def fake_get_cron_manager(manager, tenant_id: str, agent_id: str):
        observed["lookup"] = (tenant_id, agent_id)

        class FakeCronManager:
            async def run_job(
                self,
                job_id: str,
                *,
                is_manual: bool = True,
                source_id: str | None = None,
            ) -> None:
                observed["run_job"] = job_id
                observed["is_manual"] = is_manual
                observed["source_id"] = source_id

        return FakeCronManager()

    monkeypatch.setattr(
        internal_router,
        "_get_cron_manager",
        fake_get_cron_manager,
    )

    params = {
        "tenant_id": "tenant-a",
        "source_id": "source-a",
        "agent_id": "default",
        "task_type": "job",
        "job_id": "job-1",
    }
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(multi_agent_manager=object()),
        ),
    )

    response = await internal_router.internal_cron_callback(
        request=request,
        body={
            "jobParam": base64.urlsafe_b64encode(
                json.dumps(params).encode(),
            ).decode(),
        },
    )

    assert response == {"status": "ok", "task_type": "job"}
    assert observed == {
        "lookup": (encode_scope_id("tenant-a", "source-a"), "default"),
        "run_job": "job-1",
        "is_manual": False,
        "source_id": "source-a",
    }


@pytest.mark.asyncio
async def test_callback_runs_source_cleanup_without_using_tenant_scope() -> (
    None
):
    """清理回调只按 source_id 定位清理范围。"""
    observed: dict[str, Any] = {}

    class FakeSourceScheduler:
        async def run_task_session_cleanup(self, *, source_id: str):
            observed["source_id"] = source_id
            return {"enabled": True, "source_id": source_id}

    params = {
        "tenant_id": "tenant-a",
        "source_id": "source-a",
        "agent_id": "",
        "task_type": "cleanup",
        "job_id": "_source_task_session_cleanup",
        "scopeId": "tenant-a-source-a",
        "fromId": "tenant-a",
    }
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                source_system_task_scheduler=FakeSourceScheduler(),
            ),
        ),
    )

    response = await internal_router.internal_cron_callback(
        request=request,
        body={
            "jobParam": base64.urlsafe_b64encode(
                json.dumps(params).encode(),
            ).decode(),
        },
    )

    assert response == {
        "status": "ok",
        "task_type": "cleanup",
    }
    assert observed == {"source_id": "source-a"}


@pytest.mark.asyncio
async def test_callback_runs_source_archive_maintenance_without_tenant_scope() -> (
    None
):
    observed: dict[str, Any] = {}

    class FakeSourceScheduler:
        async def run_archive_maintenance(self, *, source_id: str):
            observed["source_id"] = source_id
            return {"enabled": True, "source_id": source_id}

    params = {
        "tenant_id": "tenant-a",
        "source_id": "source-a",
        "agent_id": "",
        "task_type": "archive_maintenance",
        "job_id": "_source_archive_maintenance",
        "scopeId": "tenant-a-source-a",
        "fromId": "tenant-a",
    }
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                source_system_task_scheduler=FakeSourceScheduler(),
            ),
        ),
    )

    response = await internal_router.internal_cron_callback(
        request=request,
        body={
            "jobParam": base64.urlsafe_b64encode(
                json.dumps(params).encode(),
            ).decode(),
        },
    )

    assert response == {
        "status": "ok",
        "task_type": "archive_maintenance",
    }
    assert observed == {"source_id": "source-a"}


@pytest.mark.asyncio
async def test_refresh_external_jobs_updates_existing_external_binding(
    tmp_path,
) -> None:
    """存量 external_job_id 任务刷新时必须调用 update-job。"""
    adapter = CapturingSchedulerAdapter()

    class FakeRepo:
        _path = tmp_path / "jobs.json"

        async def list_jobs(self):
            return [_sample_job(external_id="42")]

    manager = CronManager(
        repo=FakeRepo(),
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id=encode_scope_id("tenant-a", "source-a"),
        scheduler_adapter=adapter,
    )

    result = await manager.refresh_external_jobs()

    assert result["updated"] == 1
    assert result["registered"] == 0
    update_path, payload = adapter.requests[0]
    assert update_path == "/job-admin/v2/update-job"
    assert payload["id"] == 42
    assert payload["jobDesc"].startswith("[SWE] tenant-a/source-a/default/job")
    assert _decode_job_param(payload["jobParam"])["source_id"] == "source-a"


@pytest.mark.asyncio
async def test_batch_dispatch_child_does_not_register_external_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    adapter = CapturingSchedulerAdapter()
    repo = _JobsRepo()
    manager = CronManager(
        repo=repo,
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id=encode_scope_id("tenant-b", "source-a"),
        scheduler_adapter=adapter,
    )

    await manager.create_or_replace_job(_batch_dispatch_child_job())

    assert repo.jobs[0].meta.get("external_job_id") in (None, "")
    assert [path for path, _ in adapter.requests] == []


@pytest.mark.asyncio
async def test_batch_dispatch_parent_registers_normal_swe_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    monkeypatch.setenv("SWE_SCHEDULER_API_URL", "http://scheduler.local/api")
    monkeypatch.setenv("SWE_SERVER_DOMAIN", "http://swe.local")
    adapter = CapturingSchedulerAdapter()
    repo = _JobsRepo()
    parent = _sample_job().model_copy(
        update={
            "meta": {
                "broadcast_dispatch_intents_enabled": True,
            },
        },
    )
    manager = CronManager(
        repo=repo,
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id=encode_scope_id("tenant-a", "source-a"),
        scheduler_adapter=adapter,
    )

    await manager.create_or_replace_job(parent)

    assert repo.jobs[0].meta["external_job_id"] == "1001"
    add_path, payload = adapter.requests[0]
    assert add_path == "/job-admin/v2/add-job"
    assert (
        payload["jobAddress"] == "http://swe.local/api/internal/cron/callback"
    )
    job_param = _decode_job_param(payload["jobParam"])
    assert job_param["job_id"] == "job-1"
    assert "callback_token" not in job_param


@pytest.mark.asyncio
async def test_enable_batch_dispatch_registers_separate_scheduler_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock
    from swe.app.crons import batch_run_state_client

    monkeypatch.setattr(
        batch_run_state_client, "initialize_run_state", AsyncMock()
    )
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    monkeypatch.setenv("SWE_SCHEDULER_API_URL", "http://scheduler.local/api")
    monkeypatch.setenv("SWE_SERVER_DOMAIN", "http://swe.local")
    monkeypatch.setattr(
        provider_manager_module.ProviderManager,
        "_resolve_effective_provider_tenant_id",
        staticmethod(lambda tenant_id: tenant_id or "default"),
    )
    monkeypatch.setattr(
        provider_manager_module.ProviderManager,
        "_get_tenant_root_path",
        staticmethod(lambda tenant_id: f"/unused/{tenant_id}/providers"),
    )
    monkeypatch.setattr(
        provider_manager_module.ProviderManager,
        "_read_active_model_from_root",
        staticmethod(
            lambda _root: ModelSlotConfig(
                provider_id="dashscope",
                model="qwen-max",
            ),
        ),
    )
    adapter = CapturingSchedulerAdapter()
    parent = _sample_job(external_id="42")
    repo = _JobsRepo([parent])
    manager = CronManager(
        repo=repo,
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id=encode_scope_id("tenant-a", "source-a"),
        scheduler_adapter=adapter,
    )

    saved = await manager.enable_batch_dispatch_for_parent(
        "job-1",
        offset_window_hours=4,
    )

    meta = saved.meta or {}
    assert meta["external_job_id"] == "42"
    assert meta["batch_dispatch_external_job_id"] == "1001"
    assert meta["broadcast_dispatch_intents_enabled"] is True
    assert meta["batch_dispatch_offset_minutes"] == 240
    assert (repo.jobs[0].meta or {}) == meta

    normal_update = next(
        payload
        for path, payload in adapter.requests
        if path == "/job-admin/v2/update-job" and payload.get("id") == 42
    )
    assert (
        normal_update["jobAddress"]
        == "http://swe.local/api/internal/cron/callback"
    )

    batch_add = next(
        payload
        for path, payload in adapter.requests
        if path == "/job-admin/v2/add-job"
        and payload["jobAddress"]
        == "http://scheduler.local/api/scheduler/cron/callback"
    )
    assert "[批调度]" in batch_add["jobDesc"]
    assert batch_add["jobCron"] == "0 0 5 * * ?"
    job_param = _decode_job_param(batch_add["jobParam"])
    assert job_param["job_id"] == "job-1"
    assert job_param["batch_dispatch_offset_minutes"] == 240
    assert job_param["batch_dispatch_parent_cron"] == "0 9 * * *"
    assert job_param["swe_server_domain"] == "http://swe.local"
    assert job_param["provider_id"] == "dashscope"
    assert job_param["model_id"] == "qwen-max"

    run_states = [
        payload
        for path, payload in adapter.requests
        if path == "/job-admin/v2/update-job-run-states"
    ]
    assert any(
        payload["id"] == 42 and payload["runFlag"] == 0
        for payload in run_states
    )
    assert any(
        payload["id"] == 1001 and payload["runFlag"] == 1
        for payload in run_states
    )


@pytest.mark.asyncio
async def test_update_enabled_batch_dispatch_parent_refreshes_batch_scheduler_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    monkeypatch.setenv("SWE_SCHEDULER_API_URL", "http://scheduler.local/api")
    monkeypatch.setenv("SWE_SERVER_DOMAIN", "http://swe.local")
    adapter = CapturingSchedulerAdapter()
    parent = _sample_job(external_id="42").model_copy(
        update={
            "meta": {
                "external_job_id": "42",
                "batch_dispatch_external_job_id": "1001",
                "broadcast_dispatch_intents_enabled": True,
                "batch_dispatch_offset_window_hours": 4,
                "batch_dispatch_offset_minutes": 240,
            },
        },
    )
    updated_parent = parent.model_copy(
        update={
            "schedule": ScheduleSpec(
                type="cron",
                cron="30 10 * * *",
                timezone="Asia/Shanghai",
            ),
        },
    )
    repo = _JobsRepo([parent])
    manager = CronManager(
        repo=repo,
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id=encode_scope_id("tenant-a", "source-a"),
        scheduler_adapter=adapter,
    )

    await manager.create_or_replace_job(updated_parent)

    normal_update = next(
        payload
        for path, payload in adapter.requests
        if path == "/job-admin/v2/update-job" and payload.get("id") == 42
    )
    assert _decode_job_param(normal_update["jobParam"])["job_id"] == "job-1"

    batch_update = next(
        payload
        for path, payload in adapter.requests
        if path == "/job-admin/v2/update-job" and payload.get("id") == 1001
    )
    assert (
        batch_update["jobAddress"]
        == "http://scheduler.local/api/scheduler/cron/callback"
    )
    assert batch_update["jobCron"] == "0 30 6 * * ?"
    batch_job_param = _decode_job_param(batch_update["jobParam"])
    assert batch_job_param["job_id"] == "job-1"
    assert batch_job_param["batch_dispatch_offset_minutes"] == 240
    assert batch_job_param["batch_dispatch_parent_cron"] == "30 10 * * *"

    run_states = [
        payload
        for path, payload in adapter.requests
        if path == "/job-admin/v2/update-job-run-states"
    ]
    assert any(
        payload["id"] == 42 and payload["runFlag"] == 0
        for payload in run_states
    )
    assert any(
        payload["id"] == 1001 and payload["runFlag"] == 1
        for payload in run_states
    )
    assert repo.jobs[0].meta["batch_dispatch_cron"] == "30 6 * * *"


@pytest.mark.asyncio
async def test_disable_batch_dispatch_resumes_normal_and_pauses_batch_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    monkeypatch.setenv("SWE_SCHEDULER_API_URL", "http://scheduler.local/api")
    monkeypatch.setenv("SWE_SERVER_DOMAIN", "http://swe.local")
    adapter = CapturingSchedulerAdapter()
    parent = _sample_job(external_id="42").model_copy(
        update={
            "meta": {
                "external_job_id": "42",
                "batch_dispatch_external_job_id": "1001",
                "broadcast_dispatch_intents_enabled": True,
                "batch_dispatch_offset_minutes": 240,
            },
        },
    )
    repo = _JobsRepo([parent])
    manager = CronManager(
        repo=repo,
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id=encode_scope_id("tenant-a", "source-a"),
        scheduler_adapter=adapter,
    )

    saved = await manager.disable_batch_dispatch_for_parent("job-1")

    meta = saved.meta or {}
    assert meta["external_job_id"] == "42"
    assert meta["batch_dispatch_external_job_id"] == "1001"
    assert "broadcast_dispatch_intents_enabled" not in meta
    assert "batch_dispatch_offset_minutes" not in meta
    assert (repo.jobs[0].meta or {}) == meta

    normal_update = next(
        payload
        for path, payload in adapter.requests
        if path == "/job-admin/v2/update-job" and payload.get("id") == 42
    )
    assert (
        normal_update["jobAddress"]
        == "http://swe.local/api/internal/cron/callback"
    )

    run_states = [
        payload
        for path, payload in adapter.requests
        if path == "/job-admin/v2/update-job-run-states"
    ]
    assert any(
        payload["id"] == 42 and payload["runFlag"] == 1
        for payload in run_states
    )
    assert any(
        payload["id"] == 1001 and payload["runFlag"] == 0
        for payload in run_states
    )


@pytest.mark.asyncio
async def test_restore_external_ids_updates_batch_disabled_parent_to_swe_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    monkeypatch.setenv("SWE_SERVER_DOMAIN", "http://swe.local")
    adapter = CapturingSchedulerAdapter()
    parent = _sample_job(external_id="42")
    repo = _JobsRepo([parent])
    manager = CronManager(
        repo=repo,
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id=encode_scope_id("tenant-a", "source-a"),
        scheduler_adapter=adapter,
    )

    await manager._restore_external_job_ids()

    update_path, payload = adapter.requests[0]
    assert update_path == "/job-admin/v2/update-job"
    assert payload["id"] == 42
    assert (
        payload["jobAddress"] == "http://swe.local/api/internal/cron/callback"
    )


@pytest.mark.asyncio
async def test_restore_external_ids_updates_rolled_back_child_to_swe_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    monkeypatch.setenv("SWE_SERVER_DOMAIN", "http://swe.local")
    adapter = CapturingSchedulerAdapter()
    child = _sample_job(external_id="42").model_copy(
        update={
            "id": "child-job",
            "tenant_id": "tenant-b",
            "source_id": "source-a",
            "scope_id": encode_scope_id("tenant-b", "source-a"),
            "meta": {
                "external_job_id": "42",
                "broadcast_source_job_id": "parent-job",
            },
        },
    )
    repo = _JobsRepo([child])
    manager = CronManager(
        repo=repo,
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id=encode_scope_id("tenant-b", "source-a"),
        scheduler_adapter=adapter,
    )

    await manager._restore_external_job_ids()

    update_path, payload = adapter.requests[0]
    assert update_path == "/job-admin/v2/update-job"
    assert payload["id"] == 42
    assert (
        payload["jobAddress"] == "http://swe.local/api/internal/cron/callback"
    )


@pytest.mark.asyncio
async def test_batch_dispatch_child_uses_normal_scheduler_when_feature_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", raising=False)
    adapter = CapturingSchedulerAdapter()
    repo = _JobsRepo()
    manager = CronManager(
        repo=repo,
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id=encode_scope_id("tenant-b", "source-a"),
        scheduler_adapter=adapter,
    )

    await manager.create_or_replace_job(_batch_dispatch_child_job())

    assert repo.jobs[0].meta["external_job_id"] == "1001"
    paths = [path for path, _ in adapter.requests]
    assert paths[0] == "/job-admin/v2/add-job"
    assert "/job-admin/v2/add-job" in paths
    assert any(
        path == "/job-admin/v2/update-job-run-states"
        and payload["runFlag"] == 1
        for path, payload in adapter.requests
    )


@pytest.mark.asyncio
async def test_batch_dispatch_child_registers_scheduler_after_mode_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    monkeypatch.setenv("SWE_SERVER_DOMAIN", "http://swe.local")
    adapter = CapturingSchedulerAdapter()
    existing = _batch_dispatch_child_job()
    repo = _JobsRepo([existing])
    manager = CronManager(
        repo=repo,
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id=encode_scope_id("tenant-b", "source-a"),
        scheduler_adapter=adapter,
    )
    updated = existing.model_copy(
        update={"meta": {"broadcast_source_job_id": "parent-job"}},
    )

    await manager.create_or_replace_job(updated)

    assert repo.jobs[0].meta["external_job_id"] == "1001"
    assert adapter.requests[0][0] == "/job-admin/v2/add-job"
    assert any(
        path == "/job-admin/v2/update-job-run-states"
        and payload["runFlag"] == 1
        for path, payload in adapter.requests
    )


@pytest.mark.asyncio
async def test_batch_dispatch_child_pauses_existing_external_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    adapter = CapturingSchedulerAdapter()
    existing = _batch_dispatch_child_job(
        external_id="42",
        dispatch_enabled=False,
    )
    repo = _JobsRepo([existing])
    manager = CronManager(
        repo=repo,
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id=encode_scope_id("tenant-b", "source-a"),
        scheduler_adapter=adapter,
    )

    updated = existing.model_copy(
        update={
            "meta": {
                "broadcast_source_job_id": "parent-job",
                "broadcast_dispatch_intents_enabled": True,
            },
        },
    )
    await manager.create_or_replace_job(updated)

    assert repo.jobs[0].meta["external_job_id"] == "42"
    assert [path for path, _ in adapter.requests] == [
        "/job-admin/v2/update-job-run-states",
    ]
    assert adapter.requests[0][1]["id"] == 42
    assert adapter.requests[0][1]["runFlag"] == 0


@pytest.mark.asyncio
async def test_register_missing_pauses_existing_batch_dispatch_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    adapter = CapturingSchedulerAdapter()
    repo = _JobsRepo([_batch_dispatch_child_job(external_id="42")])
    manager = CronManager(
        repo=repo,
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id=encode_scope_id("tenant-b", "source-a"),
        scheduler_adapter=adapter,
    )

    result = await manager.register_missing_external_jobs()

    assert result["updated"] == 1
    assert [path for path, _ in adapter.requests] == [
        "/job-admin/v2/update-job-run-states",
    ]
    assert adapter.requests[0][1]["id"] == 42
    assert adapter.requests[0][1]["runFlag"] == 0


async def test_restore_external_ids_skips_batch_dispatch_child_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    adapter = CapturingSchedulerAdapter()
    repo = _JobsRepo([_batch_dispatch_child_job()])
    manager = CronManager(
        repo=repo,
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id=encode_scope_id("tenant-b", "source-a"),
        scheduler_adapter=adapter,
    )

    await manager._restore_external_job_ids()

    assert adapter.requests == []


@pytest.mark.asyncio
async def test_restore_external_ids_pauses_batch_dispatch_child_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "1")
    adapter = CapturingSchedulerAdapter()
    repo = _JobsRepo([_batch_dispatch_child_job(external_id="42")])
    manager = CronManager(
        repo=repo,
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id=encode_scope_id("tenant-b", "source-a"),
        scheduler_adapter=adapter,
    )

    await manager._restore_external_job_ids()

    assert [path for path, _ in adapter.requests] == [
        "/job-admin/v2/update-job-run-states",
    ]
    assert adapter.requests[0][1]["id"] == 42
    assert adapter.requests[0][1]["runFlag"] == 0


@pytest.mark.asyncio
async def test_delete_job_uses_persisted_external_binding_when_state_missing(
    tmp_path,
) -> None:
    """删除任务时即使内存状态缺失，也必须使用持久化的调度平台绑定。"""
    adapter = CapturingSchedulerAdapter()
    job = _sample_job(external_id="42")

    class FakeRepo:
        _path = tmp_path / "jobs.json"

        def __init__(self) -> None:
            self.jobs = [job]

        async def load(self) -> JobsFile:
            return JobsFile(jobs=list(self.jobs))

        async def save(self, jobs_file: JobsFile) -> None:
            self.jobs = list(jobs_file.jobs)

        async def get_job(self, job_id: str) -> CronJobSpec | None:
            for saved_job in self.jobs:
                if saved_job.id == job_id:
                    return saved_job
            return None

    repo = FakeRepo()
    manager = CronManager(
        repo=repo,
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id=encode_scope_id("tenant-a", "source-a"),
        scheduler_adapter=adapter,
    )

    assert manager.get_state(job.id).external_job_id is None

    deleted = await manager.delete_job(job.id)

    assert deleted is True
    assert repo.jobs == []
    paths = [path for path, _ in adapter.requests]
    assert "/job-admin/v2/update-job" in paths
    assert "/job-admin/v2/update-job-run-states" in paths

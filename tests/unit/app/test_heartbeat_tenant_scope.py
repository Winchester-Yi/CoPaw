# -*- coding: utf-8 -*-
"""租户感知 heartbeat 配置访问的回归测试。"""

# pylint: disable=protected-access
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from swe.app.crons import heartbeat as heartbeat_module
from swe.app.crons.heartbeat import run_heartbeat_once
from swe.app.crons.manager import CronManager
from swe.app.crons.models import (
    CronJobRequest,
    CronJobSpec,
    DispatchSpec,
    DispatchTarget,
    JobRuntimeSpec,
    JobsFile,
    ScheduleSpec,
)
from swe.config import config as config_module
from swe.config import utils as config_utils
from swe.config.llm_workload import (
    LLM_WORKLOAD_CHAT,
    LLM_WORKLOAD_CRON,
    bind_llm_workload,
    get_current_llm_workload,
)
from swe.config.context import (
    encode_scope_id,
    get_current_scope_id,
    get_current_source_id,
    get_current_tenant_id,
)


@pytest.mark.asyncio
async def test_run_heartbeat_once_uses_longer_default_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, Any] = {}
    workspace_dir = tmp_path / "tenant-a" / "workspaces" / "default"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "HEARTBEAT.md").write_text("ping", encoding="utf-8")

    class FakeRunner:
        async def stream_query(self, request):
            observed["request"] = request
            observed["workload"] = get_current_llm_workload()
            yield {"type": "message", "text": "pong"}

    class FakeChannelManager:
        async def send_event(self, **kwargs) -> None:
            observed["dispatch"] = kwargs

    async def fake_wait_for(awaitable, timeout):
        observed["timeout"] = timeout
        return await awaitable

    monkeypatch.setattr(heartbeat_module.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(
        heartbeat_module,
        "get_heartbeat_config",
        lambda agent_id=None, *, tenant_id=None: SimpleNamespace(
            active_hours=None,
            target="main",
        ),
    )

    await run_heartbeat_once(
        runner=FakeRunner(),
        channel_manager=FakeChannelManager(),
        agent_id="default",
        tenant_id="tenant-a",
        workspace_dir=workspace_dir,
    )

    assert observed["timeout"] == 7200
    assert observed["workload"] == LLM_WORKLOAD_CRON
    assert observed["request"]["execution_origin"] == "scheduled"
    assert get_current_llm_workload() == LLM_WORKLOAD_CHAT


def test_get_heartbeat_config_uses_tenant_scoped_agent_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, str | None] = {}

    def fake_load_agent_config(
        agent_id: str,
        config_path=None,
        *,
        tenant_id: str | None = None,
    ) -> SimpleNamespace:
        del config_path
        observed["agent_id"] = agent_id
        observed["tenant_id"] = tenant_id
        return SimpleNamespace(
            heartbeat=SimpleNamespace(enabled=True, every="5m"),
        )

    monkeypatch.setattr(
        config_utils,
        "load_agent_config",
        fake_load_agent_config,
    )

    hb = config_utils.get_heartbeat_config(
        "default",
        tenant_id="tenant-a",
    )

    assert observed == {
        "agent_id": "default",
        "tenant_id": "tenant-a",
    }
    assert hb.enabled is True
    assert hb.every == "5m"


def test_update_last_dispatch_saves_tenant_scoped_agent_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}
    agent_config = SimpleNamespace(last_dispatch=None)

    def fake_load_agent_config(
        agent_id: str,
        config_path=None,
        *,
        tenant_id: str | None = None,
    ) -> SimpleNamespace:
        del config_path
        observed["load"] = (agent_id, tenant_id)
        return agent_config

    def fake_save_agent_config(
        agent_id: str,
        config,
        config_path=None,
        *,
        tenant_id: str | None = None,
    ) -> None:
        del config_path
        observed["save"] = (agent_id, tenant_id, config.last_dispatch)

    monkeypatch.setattr(
        config_utils,
        "load_agent_config",
        fake_load_agent_config,
    )
    monkeypatch.setattr(
        config_utils,
        "save_agent_config",
        fake_save_agent_config,
    )

    config_utils.update_last_dispatch(
        channel="console",
        user_id="user-a",
        session_id="session-a",
        agent_id="default",
        tenant_id="tenant-a",
    )

    assert observed["load"] == ("default", "tenant-a")
    saved_agent_id, saved_tenant_id, last_dispatch = observed["save"]
    assert saved_agent_id == "default"
    assert saved_tenant_id == "tenant-a"
    assert last_dispatch.channel == "console"
    assert last_dispatch.user_id == "user-a"
    assert last_dispatch.session_id == "session-a"


@pytest.mark.asyncio
async def test_cron_manager_register_heartbeat_uses_runtime_tenant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, Any] = {}

    async def submit(tenant, source, agent, kind):
        observed["submission"] = (tenant, source, agent, kind)
        return {"submission_status": "accepted"}

    class FakeSchedulerAdapter:
        async def register_job(self, **kwargs):
            observed["register_job"] = kwargs
            return "ext-heartbeat"

        async def resume_job(self, external_id: str):
            observed["resume_job"] = external_id

    class FakeRepo:
        _path = tmp_path / "jobs.json"

    manager = CronManager(
        repo=FakeRepo(),
        runner=object(),
        channel_manager=object(),
        agent_id="default",
        tenant_id=encode_scope_id("tenant-a", "source-a"),
        scheduler_adapter=FakeSchedulerAdapter(),
    )

    monkeypatch.setattr(
        "swe.app.crons.system_bridge.submit_agent_refresh",
        submit,
    )

    await manager.register_heartbeat()

    assert observed["submission"] == (
        "tenant-a",
        "source-a",
        "default",
        "heartbeat",
    )
    assert "register_job" not in observed
    assert observed.get("resume_job") is None


@pytest.mark.asyncio
async def test_cron_manager_heartbeat_callback_binds_cron_workload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}
    manager = CronManager(
        repo=object(),
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id="tenant-a",
    )

    async def fake_run_heartbeat_once(**kwargs: Any) -> None:
        observed["tenant_id"] = kwargs["tenant_id"]
        observed["workload"] = get_current_llm_workload()

    monkeypatch.setattr(
        heartbeat_module,
        "run_heartbeat_once",
        fake_run_heartbeat_once,
    )

    with bind_llm_workload(LLM_WORKLOAD_CHAT):
        await manager.run_heartbeat()
        assert get_current_llm_workload() == LLM_WORKLOAD_CHAT

    assert observed["tenant_id"] == "tenant-a"
    assert observed["workload"] == LLM_WORKLOAD_CRON


@pytest.mark.asyncio
async def test_cron_manager_heartbeat_callback_rehydrates_scope_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """heartbeat 后台执行必须从 runtime scope 还原完整三元组。"""
    observed: dict[str, Any] = {}
    scope_id = encode_scope_id("tenant-a", "source-a")
    manager = CronManager(
        repo=object(),
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id=scope_id,
    )

    async def fake_run_heartbeat_once(**kwargs: Any) -> None:
        observed["passed_tenant_id"] = kwargs["tenant_id"]
        observed["tenant_id"] = get_current_tenant_id()
        observed["source_id"] = get_current_source_id()
        observed["scope_id"] = get_current_scope_id()

    monkeypatch.setattr(
        heartbeat_module,
        "run_heartbeat_once",
        fake_run_heartbeat_once,
    )

    await manager.run_heartbeat()

    assert observed == {
        "passed_tenant_id": scope_id,
        "tenant_id": "tenant-a",
        "source_id": "source-a",
        "scope_id": scope_id,
    }


@pytest.mark.asyncio
async def test_cron_manager_prefetch_targets_use_runtime_scope() -> None:
    """后台 auth 预热必须使用 canonical scope 读取租户密钥。"""
    scope_id = encode_scope_id("tenant-a", "source-a")

    class FakeRepo:
        async def list_jobs(self):
            return [
                CronJobSpec(
                    id="prefetch-job",
                    name="Prefetch Job",
                    enabled=True,
                    tenant_id="tenant-a",
                    source_id="source-a",
                    scope_id=f"scope.v1.{scope_id}",
                    schedule=ScheduleSpec(
                        type="cron",
                        cron="0 0 * * *",
                        timezone="UTC",
                    ),
                    task_type="agent",
                    request=CronJobRequest(
                        input=[{"content": [{"text": "ping"}]}],
                    ),
                    dispatch=DispatchSpec(
                        type="channel",
                        channel="console",
                        target=DispatchTarget(
                            user_id="user-a",
                            session_id="session-a",
                        ),
                        meta={"workspace_dir": "/tmp/ws"},
                    ),
                    runtime=JobRuntimeSpec(timeout_seconds=30),
                ),
            ]

    manager = CronManager(
        repo=FakeRepo(),
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id=scope_id,
    )

    targets = (
        await manager._collect_prefetch_targets()
    )  # pylint: disable=protected-access

    assert targets == {
        (
            scope_id,
            "/tmp/ws",
            "user-a",
            "source-a",
            scope_id,
        ),
    }


@pytest.mark.asyncio
async def test_cron_manager_register_missing_external_jobs_persists_runtime_scope(
    tmp_path: Path,
) -> None:
    """补注册外部调度任务时必须保留可解析的 runtime scope。"""
    scope_id = encode_scope_id("tenant-a", "source-a")
    observed: dict[str, Any] = {}
    job = CronJobSpec(
        id="job-1",
        name="Job One",
        enabled=True,
        tenant_id="tenant-a",
        source_id="source-a",
        schedule=ScheduleSpec(
            type="cron",
            cron="0 0 * * *",
            timezone="UTC",
        ),
        task_type="agent",
        request=CronJobRequest(input=[{"content": [{"text": "ping"}]}]),
        dispatch=DispatchSpec(
            type="channel",
            channel="console",
            target=DispatchTarget(
                user_id="user-a",
                session_id="session-a",
            ),
        ),
        runtime=JobRuntimeSpec(timeout_seconds=30),
    )
    jobs_file = JobsFile(jobs=[job])

    class FakeRepo:
        _path = tmp_path / "jobs.json"

        async def list_jobs(self):
            return list(jobs_file.jobs)

        async def load(self):
            return jobs_file

        async def save(self, updated: JobsFile):
            observed["saved_jobs"] = updated.jobs

    class FakeSchedulerAdapter:
        async def register_job(self, **kwargs):
            observed["register_job"] = kwargs
            return "ext-job-1"

        async def resume_job(self, external_id: str):
            observed["resume_job"] = external_id

        async def pause_job(self, external_id: str):
            observed["pause_job"] = external_id

    manager = CronManager(
        repo=FakeRepo(),
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        agent_id="default",
        tenant_id=scope_id,
        scheduler_adapter=FakeSchedulerAdapter(),
    )

    result = await manager.register_missing_external_jobs()

    assert result["registered"] == 1
    assert observed["register_job"]["tenant_id"] == "tenant-a"
    assert observed["register_job"]["source_id"] == "source-a"
    assert observed["register_job"]["job_id"] == "job-1"
    assert observed["resume_job"] == "ext-job-1"
    saved_job = observed["saved_jobs"][0]
    assert saved_job.tenant_id == "tenant-a"
    assert saved_job.source_id == "source-a"
    assert manager._get_job_runtime_tenant_id(saved_job) == scope_id
    assert saved_job.meta["external_job_id"] == "ext-job-1"


@pytest.mark.asyncio
async def test_run_heartbeat_once_loads_last_dispatch_from_runtime_tenant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, Any] = {}
    workspace_dir = tmp_path / "tenant-a" / "workspaces" / "default"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "HEARTBEAT.md").write_text("ping", encoding="utf-8")

    class FakeRunner:
        async def stream_query(self, request):
            observed["request"] = request
            yield {"type": "message", "text": "pong"}

    class FakeChannelManager:
        async def send_event(
            self,
            *,
            channel,
            user_id,
            session_id,
            event,
            meta,
        ) -> None:
            observed["dispatch"] = (
                channel,
                user_id,
                session_id,
                event,
                meta,
            )

    def fake_get_heartbeat_config(
        agent_id: str | None = None,
        *,
        tenant_id: str | None = None,
    ) -> SimpleNamespace:
        observed["heartbeat_lookup"] = (agent_id, tenant_id)
        return SimpleNamespace(active_hours=None, target="last")

    def fake_load_agent_config(
        agent_id: str,
        config_path=None,
        *,
        tenant_id: str | None = None,
    ) -> SimpleNamespace:
        del config_path
        observed["last_dispatch_lookup"] = (agent_id, tenant_id)
        return SimpleNamespace(
            last_dispatch=SimpleNamespace(
                channel="console",
                user_id="user-a",
                session_id="session-a",
            ),
        )

    monkeypatch.setattr(
        heartbeat_module,
        "get_heartbeat_config",
        fake_get_heartbeat_config,
    )
    monkeypatch.setattr(
        config_module,
        "load_agent_config",
        fake_load_agent_config,
    )

    await run_heartbeat_once(
        runner=FakeRunner(),
        channel_manager=FakeChannelManager(),
        agent_id="default",
        tenant_id="tenant-a",
        workspace_dir=workspace_dir,
    )

    assert observed["heartbeat_lookup"] == ("default", "tenant-a")
    assert observed["last_dispatch_lookup"] == ("default", "tenant-a")
    assert observed["dispatch"] == (
        "console",
        "user-a",
        "session-a",
        {"type": "message", "text": "pong"},
        {},
    )

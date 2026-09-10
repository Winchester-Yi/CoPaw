# -*- coding: utf-8 -*-
"""Scheduled Run Boundary source system config binding tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from unittest.mock import AsyncMock, patch

import pytest

from swe.app.crons.manager import CronManager
from swe.app.crons.models import (
    CronJobRequest,
    CronJobSpec,
    DispatchSpec,
    DispatchTarget,
    JobRuntimeSpec,
    ScheduleSpec,
)
from swe.app.routers.dream_logs import (
    ArchiveItem,
    DreamArchiveMaintenanceResult,
)
from swe.app.source_system_config.models import (
    EffectiveSourceSystemConfig,
    SourceSystemConfig,
)
from swe.app.source_system_config.runtime import (
    bind_source_system_config,
    get_current_source_system_config,
    resolve_tool_result_compact_config,
)
from swe.app.source_system_config.service import (
    SourceSystemConfigDataInvalid,
    SourceSystemConfigUnavailable,
)
from swe.app.workspace.tenant_pool import TenantWorkspacePool
from swe.app.workspace.workspace import Workspace
from swe.app.multi_agent_manager import MultiAgentManager
from swe.config.context import encode_scope_id
from swe.config.config import ToolResultCompactConfig


def _build_effective_config(
    source_id: str = "portal",
) -> EffectiveSourceSystemConfig:
    raw_config = SourceSystemConfig.model_validate(
        {"feature_switches": {"scheduled_boundary": True}},
    )
    return EffectiveSourceSystemConfig(
        source_id=source_id,
        config=raw_config.merged_with_defaults(),
        raw_config=raw_config,
        version=1,
    )


def _build_tool_compact_disabled_config(
    source_id: str = "portal",
) -> EffectiveSourceSystemConfig:
    raw_config = SourceSystemConfig.model_validate(
        {"tool_result_compact": {"enabled": False}},
    )
    return EffectiveSourceSystemConfig(
        source_id=source_id,
        config=raw_config.merged_with_defaults(),
        raw_config=raw_config,
        version=1,
    )


def _build_agent_job(
    *,
    source_id: str | None = "portal",
    scope_id: str | None = None,
) -> CronJobSpec:
    return CronJobSpec(
        id="scheduled-job",
        name="scheduled job",
        enabled=True,
        tenant_id="tenant-a",
        source_id=source_id,
        scope_id=scope_id,
        schedule=ScheduleSpec(cron="* * * * *"),
        task_type="agent",
        request=CronJobRequest(
            input=[{"content": [{"type": "text", "text": "ping"}]}],
        ),
        dispatch=DispatchSpec(
            channel="console",
            target=DispatchTarget(user_id="user-a", session_id="session-a"),
            meta={},
        ),
        runtime=JobRuntimeSpec(timeout_seconds=60),
    )


class _RecordingSourceConfigService:
    def __init__(
        self,
        *,
        effective: EffectiveSourceSystemConfig | None = None,
        error: Exception | None = None,
    ) -> None:
        self.effective = effective or _build_effective_config()
        self.error = error
        self.calls: list[str] = []

    async def resolve_config(
        self,
        source_id: str,
        *,
        force_refresh: bool = False,
    ) -> EffectiveSourceSystemConfig:
        del force_refresh
        self.calls.append(source_id)
        if self.error is not None:
            raise self.error
        return self.effective


class _RecordingContinuousGovernanceService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.archive_calls: list[dict[str, object]] = []
        self.delete_archive_calls: list[dict[str, object]] = []
        self.audit_calls: list[dict[str, object]] = []

    async def upsert_workspace_governance_record_with_health(
        self,
        **kwargs,
    ) -> None:
        self.calls.append(kwargs)

    async def upsert_archive_items(self, **kwargs) -> None:
        self.archive_calls.append(kwargs)

    async def delete_archive_items(self, **kwargs) -> None:
        self.delete_archive_calls.append(kwargs)

    async def upsert_cleanup_audit(self, record) -> None:
        self.audit_calls.append(record)


@pytest.mark.asyncio
async def test_execute_once_binds_source_config_across_job_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _RecordingSourceConfigService(
        effective=_build_effective_config("portal"),
    )
    manager = CronManager(
        repo=object(),
        runner=object(),
        channel_manager=object(),
        source_system_config_service=service,
    )
    observed: dict[str, str | None] = {}

    async def fake_execute(_job, **_kwargs):
        current = get_current_source_system_config()
        observed["execute"] = None if current is None else current.source_id
        return SimpleNamespace(
            trace_id="",
            output_preview="",
            input_snapshot=None,
            executor_leader="",
            execution_meta=None,
        )

    async def fake_success(_job, _execution_key=""):
        current = get_current_source_system_config()
        observed["success"] = None if current is None else current.source_id

    async def fake_finalize(**_kwargs):
        current = get_current_source_system_config()
        observed["finalize"] = None if current is None else current.source_id

    monkeypatch.setattr(
        manager,
        "_executor",
        SimpleNamespace(execute=fake_execute),
    )
    monkeypatch.setattr(manager, "_handle_success_notifications", fake_success)
    monkeypatch.setattr(manager, "_finalize_execution_state", fake_finalize)

    await manager._execute_once(
        _build_agent_job(),
    )  # pylint: disable=protected-access

    assert service.calls == ["portal"]
    assert observed == {
        "execute": "portal",
        "success": "portal",
        "finalize": "portal",
    }
    assert get_current_source_system_config() is None


@pytest.mark.asyncio
async def test_execute_once_passes_dispatch_meta_to_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = CronManager(
        repo=object(),
        runner=object(),
        channel_manager=object(),
    )
    observed: dict[str, object | None] = {}

    async def fake_execute(_job, **kwargs):
        observed["dispatch_meta"] = kwargs.get("dispatch_meta")
        return SimpleNamespace(
            trace_id="trace-1",
            output_preview="",
            input_snapshot=None,
            executor_leader="",
            execution_meta=None,
        )

    async def fake_success(_job, _execution_key=""):
        return None

    async def fake_finalize(**_kwargs):
        return None

    dispatch_meta = {
        "passthrough_headers": {
            "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
        },
        "b3_trace_id": "8267fd70bacf497704fec30eaa353979",
    }
    monkeypatch.setattr(
        manager,
        "_executor",
        SimpleNamespace(execute=fake_execute),
    )
    monkeypatch.setattr(manager, "_handle_success_notifications", fake_success)
    monkeypatch.setattr(manager, "_finalize_execution_state", fake_finalize)

    await manager._execute_once(  # pylint: disable=protected-access
        _build_agent_job(),
        dispatch_meta=dispatch_meta,
    )

    assert observed["dispatch_meta"] == dispatch_meta


@pytest.mark.asyncio
async def test_execute_once_falls_back_to_scope_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope_id = encode_scope_id("tenant-a", "scope-source")
    service = _RecordingSourceConfigService(
        effective=_build_effective_config("scope-source"),
    )
    manager = CronManager(
        repo=object(),
        runner=object(),
        channel_manager=object(),
        source_system_config_service=service,
    )
    observed: dict[str, str | None] = {}

    async def fake_execute(_job, **_kwargs):
        current = get_current_source_system_config()
        observed["execute"] = None if current is None else current.source_id
        return SimpleNamespace(
            trace_id="",
            output_preview="",
            input_snapshot=None,
            executor_leader="",
            execution_meta=None,
        )

    async def fake_success(_job, _execution_key=""):
        return None

    async def fake_finalize(**_kwargs):
        return None

    monkeypatch.setattr(
        manager,
        "_executor",
        SimpleNamespace(execute=fake_execute),
    )
    monkeypatch.setattr(manager, "_handle_success_notifications", fake_success)
    monkeypatch.setattr(manager, "_finalize_execution_state", fake_finalize)

    await manager._execute_once(  # pylint: disable=protected-access
        _build_agent_job(source_id=None, scope_id=scope_id),
    )

    assert service.calls == ["scope-source"]
    assert observed["execute"] == "scope-source"
    assert get_current_source_system_config() is None


@pytest.mark.asyncio
async def test_execute_once_keeps_legacy_source_less_run_unbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _RecordingSourceConfigService()
    manager = CronManager(
        repo=object(),
        runner=object(),
        channel_manager=object(),
        source_system_config_service=service,
    )
    observed: dict[str, str | None] = {}

    async def fake_execute(_job, **_kwargs):
        current = get_current_source_system_config()
        observed["execute"] = None if current is None else current.source_id
        return SimpleNamespace(
            trace_id="",
            output_preview="",
            input_snapshot=None,
            executor_leader="",
            execution_meta=None,
        )

    async def fake_success(_job, _execution_key=""):
        return None

    async def fake_finalize(**_kwargs):
        return None

    monkeypatch.setattr(
        manager,
        "_executor",
        SimpleNamespace(execute=fake_execute),
    )
    monkeypatch.setattr(manager, "_handle_success_notifications", fake_success)
    monkeypatch.setattr(manager, "_finalize_execution_state", fake_finalize)

    await manager._execute_once(  # pylint: disable=protected-access
        _build_agent_job(source_id=None, scope_id=None),
    )

    assert service.calls == []
    assert observed["execute"] is None
    assert get_current_source_system_config() is None


@pytest.mark.asyncio
async def test_execute_once_clears_inherited_request_source_when_unbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _RecordingSourceConfigService()
    manager = CronManager(
        repo=object(),
        runner=object(),
        channel_manager=object(),
        source_system_config_service=service,
    )
    observed: dict[str, str | None] = {}

    async def fake_execute(_job, **_kwargs):
        current = get_current_source_system_config()
        observed["execute"] = None if current is None else current.source_id
        return SimpleNamespace(
            trace_id="",
            output_preview="",
            input_snapshot=None,
            executor_leader="",
            execution_meta=None,
        )

    async def fake_success(_job, _execution_key=""):
        current = get_current_source_system_config()
        observed["success"] = None if current is None else current.source_id

    async def fake_finalize(**_kwargs):
        current = get_current_source_system_config()
        observed["finalize"] = None if current is None else current.source_id

    monkeypatch.setattr(
        manager,
        "_executor",
        SimpleNamespace(execute=fake_execute),
    )
    monkeypatch.setattr(manager, "_handle_success_notifications", fake_success)
    monkeypatch.setattr(manager, "_finalize_execution_state", fake_finalize)

    with bind_source_system_config(_build_effective_config("request-source")):
        await manager._execute_once(  # pylint: disable=protected-access
            _build_agent_job(source_id=None, scope_id=None),
        )

    assert service.calls == []
    assert observed == {
        "execute": None,
        "success": None,
        "finalize": None,
    }
    assert get_current_source_system_config() is None


@pytest.mark.asyncio
async def test_execute_once_keeps_sourced_run_unbound_when_service_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = CronManager(
        repo=object(),
        runner=object(),
        channel_manager=object(),
    )
    observed: dict[str, str | None] = {}

    async def fake_execute(_job, **_kwargs):
        current = get_current_source_system_config()
        observed["execute"] = None if current is None else current.source_id
        return SimpleNamespace(
            trace_id="",
            output_preview="",
            input_snapshot=None,
            executor_leader="",
            execution_meta=None,
        )

    async def fake_success(_job, _execution_key=""):
        return None

    async def fake_finalize(**_kwargs):
        return None

    monkeypatch.setattr(
        manager,
        "_executor",
        SimpleNamespace(execute=fake_execute),
    )
    monkeypatch.setattr(manager, "_handle_success_notifications", fake_success)
    monkeypatch.setattr(manager, "_finalize_execution_state", fake_finalize)

    await manager._execute_once(
        _build_agent_job(),
    )  # pylint: disable=protected-access

    assert observed["execute"] is None
    assert get_current_source_system_config() is None


@pytest.mark.asyncio
async def test_run_job_manual_clears_request_source_for_legacy_unbound_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _build_agent_job(source_id=None, scope_id=None)
    manager = CronManager(
        repo=SimpleNamespace(get_job=AsyncMock(return_value=job)),
        runner=object(),
        channel_manager=object(),
    )
    observed: dict[str, str | None] = {}
    created_tasks: list[asyncio.Task[None]] = []
    original_create_task = asyncio.create_task

    async def fake_execute(_job, **_kwargs):
        current = get_current_source_system_config()
        observed["execute"] = None if current is None else current.source_id
        return SimpleNamespace(
            trace_id="",
            output_preview="",
            input_snapshot=None,
            executor_leader="",
            execution_meta=None,
        )

    async def fake_success(_job, _execution_key=""):
        current = get_current_source_system_config()
        observed["success"] = None if current is None else current.source_id

    async def fake_finalize(**_kwargs):
        current = get_current_source_system_config()
        observed["finalize"] = None if current is None else current.source_id

    def capture_create_task(coro, *, name=None):
        task = original_create_task(coro, name=name)
        created_tasks.append(task)
        return task

    monkeypatch.setattr(
        manager,
        "_executor",
        SimpleNamespace(execute=fake_execute),
    )
    monkeypatch.setattr(manager, "_handle_success_notifications", fake_success)
    monkeypatch.setattr(manager, "_finalize_execution_state", fake_finalize)
    monkeypatch.setattr(
        manager,
        "_ensure_persisted_task_binding",
        AsyncMock(return_value=job),
    )
    monkeypatch.setattr(asyncio, "create_task", capture_create_task)

    with bind_source_system_config(_build_effective_config("request-source")):
        await manager.run_job(job.id)

    assert len(created_tasks) == 1
    await created_tasks[0]

    assert observed == {
        "execute": None,
        "success": None,
        "finalize": None,
    }
    assert get_current_source_system_config() is None


@pytest.mark.asyncio
async def test_run_job_uses_callback_source_for_legacy_unbound_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _build_agent_job(source_id=None, scope_id=None)
    service = _RecordingSourceConfigService(
        effective=_build_tool_compact_disabled_config("callback-source"),
    )
    manager = CronManager(
        repo=SimpleNamespace(get_job=AsyncMock(return_value=job)),
        runner=object(),
        channel_manager=object(),
        source_system_config_service=service,
    )
    observed: dict[str, str | None] = {}
    created_tasks: list[asyncio.Task[None]] = []
    original_create_task = asyncio.create_task

    async def fake_execute(_job, **_kwargs):
        current = get_current_source_system_config()
        observed["execute"] = None if current is None else current.source_id
        resolved = resolve_tool_result_compact_config(
            ToolResultCompactConfig(enabled=True),
        )
        observed["tool_result_compact_enabled"] = str(resolved.enabled)
        return SimpleNamespace(
            trace_id="",
            output_preview="",
            input_snapshot=None,
            executor_leader="",
            execution_meta=None,
        )

    async def fake_success(_job, _execution_key=""):
        current = get_current_source_system_config()
        observed["success"] = None if current is None else current.source_id

    async def fake_finalize(**_kwargs):
        current = get_current_source_system_config()
        observed["finalize"] = None if current is None else current.source_id

    def capture_create_task(coro, *, name=None):
        task = original_create_task(coro, name=name)
        created_tasks.append(task)
        return task

    monkeypatch.setattr(
        manager,
        "_executor",
        SimpleNamespace(execute=fake_execute),
    )
    monkeypatch.setattr(manager, "_handle_success_notifications", fake_success)
    monkeypatch.setattr(manager, "_finalize_execution_state", fake_finalize)
    monkeypatch.setattr(
        manager,
        "_ensure_persisted_task_binding",
        AsyncMock(return_value=job),
    )
    monkeypatch.setattr(asyncio, "create_task", capture_create_task)

    await manager.run_job(
        job.id,
        is_manual=False,
        source_id="callback-source",
        dispatch_meta={"external_execution_id": "execution-1"},
    )

    assert len(created_tasks) == 1
    await created_tasks[0]

    assert service.calls == ["callback-source"]
    assert observed == {
        "execute": "callback-source",
        "tool_result_compact_enabled": "False",
        "success": "callback-source",
        "finalize": "callback-source",
    }
    assert get_current_source_system_config() is None


@pytest.mark.asyncio
async def test_execute_once_raises_when_source_config_resolution_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _RecordingSourceConfigService(
        error=SourceSystemConfigUnavailable("source config unavailable"),
    )
    manager = CronManager(
        repo=object(),
        runner=object(),
        channel_manager=object(),
        source_system_config_service=service,
    )

    async def fake_finalize(**_kwargs):
        assert get_current_source_system_config() is None

    monkeypatch.setattr(manager, "_finalize_execution_state", fake_finalize)

    with pytest.raises(SourceSystemConfigUnavailable):
        await manager._execute_once(
            _build_agent_job(),
        )  # pylint: disable=protected-access

    assert service.calls == ["portal"]
    assert get_current_source_system_config() is None


@pytest.mark.asyncio
async def test_execute_once_raises_when_source_config_data_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _RecordingSourceConfigService(
        error=SourceSystemConfigDataInvalid("invalid source config"),
    )
    manager = CronManager(
        repo=object(),
        runner=object(),
        channel_manager=object(),
        source_system_config_service=service,
    )

    async def fake_finalize(**_kwargs):
        assert get_current_source_system_config() is None

    monkeypatch.setattr(manager, "_finalize_execution_state", fake_finalize)

    with pytest.raises(SourceSystemConfigDataInvalid):
        await manager._execute_once(
            _build_agent_job(),
        )  # pylint: disable=protected-access

    assert service.calls == ["portal"]
    assert get_current_source_system_config() is None


@pytest.mark.asyncio
async def test_run_heartbeat_binds_source_config_from_runtime_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope_id = encode_scope_id("tenant-a", "source-a")
    service = _RecordingSourceConfigService(
        effective=_build_effective_config("source-a"),
    )
    manager = CronManager(
        repo=object(),
        runner=SimpleNamespace(workspace_dir=None, _workspace=None),
        channel_manager=object(),
        tenant_id=scope_id,
        source_system_config_service=service,
    )
    observed: dict[str, str | None] = {}

    async def fake_run_heartbeat_once(**_kwargs):
        current = get_current_source_system_config()
        observed["source_id"] = None if current is None else current.source_id

    monkeypatch.setattr(
        "swe.app.crons.heartbeat.run_heartbeat_once",
        fake_run_heartbeat_once,
    )

    await manager.run_heartbeat()

    assert service.calls == ["source-a"]
    assert observed["source_id"] == "source-a"
    assert get_current_source_system_config() is None


@pytest.mark.asyncio
async def test_run_dream_binds_source_config_from_runtime_scope() -> None:
    scope_id = encode_scope_id("tenant-a", "source-a")
    service = _RecordingSourceConfigService(
        effective=_build_effective_config("source-a"),
    )
    observed: dict[str, str | None] = {}

    async def fake_dream_memory(**_kwargs):
        current = get_current_source_system_config()
        observed["source_id"] = None if current is None else current.source_id

    runner = SimpleNamespace(
        workspace_dir=None,
        _workspace=None,
        memory_manager=SimpleNamespace(dream_memory=fake_dream_memory),
    )
    manager = CronManager(
        repo=object(),
        runner=runner,
        channel_manager=object(),
        tenant_id=scope_id,
        source_system_config_service=service,
    )

    await manager.run_dream()

    assert service.calls == ["source-a"]
    assert observed["source_id"] == "source-a"
    assert get_current_source_system_config() is None


@pytest.mark.asyncio
async def test_run_dream_dual_writes_new_records_without_archive_maintenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope_id = encode_scope_id("tenant-a", "source-a")
    workspace_dir = tmp_path / scope_id / "workspaces" / "default"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "dream_logs.json").write_text(
        json.dumps({"records": [{"id": "existing-record"}]}),
        encoding="utf-8",
    )
    governance_service = _RecordingContinuousGovernanceService()

    async def fake_dream_memory(**_kwargs):
        (workspace_dir / "dream_logs.json").write_text(
            json.dumps(
                {
                    "records": [
                        {"id": "existing-record"},
                        {"id": "new-record", "status": "kept"},
                    ],
                },
            ),
            encoding="utf-8",
        )

    archive_maintenance_called = False

    def record_archive_maintenance(*_args, **_kwargs):
        nonlocal archive_maintenance_called
        archive_maintenance_called = True

    monkeypatch.setattr(
        "swe.app.routers.dream_logs.run_dream_archive_maintenance",
        record_archive_maintenance,
    )
    runner = SimpleNamespace(
        workspace_dir=workspace_dir,
        _workspace=None,
        memory_manager=SimpleNamespace(dream_memory=fake_dream_memory),
    )
    manager = CronManager(
        repo=object(),
        runner=runner,
        channel_manager=object(),
        agent_id="default",
        tenant_id=scope_id,
        continuous_governance_service=governance_service,
    )

    await manager.run_dream()

    assert len(governance_service.calls) == 1
    call = cast(dict[str, Any], governance_service.calls[0])
    assert call["source_id"] == "source-a"
    assert call["target_user_id"] == "tenant-a"
    assert call["target_agent_id"] == "default"
    assert call["record"]["id"] == "new-record"
    assert archive_maintenance_called is True
    assert governance_service.archive_calls == []
    assert governance_service.delete_archive_calls == []
    assert governance_service.audit_calls == []


@pytest.mark.asyncio
async def test_run_dream_skips_governance_dual_write_for_overlong_agent_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cron agent 标识超过读模型字段长度时不执行治理 DB 双写。"""
    scope_id = encode_scope_id("tenant-a", "source-a")
    workspace_dir = tmp_path / scope_id / "workspaces" / "default"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "dream_logs.json").write_text(
        json.dumps({"records": []}),
        encoding="utf-8",
    )
    governance_service = _RecordingContinuousGovernanceService()

    async def fake_dream_memory(**_kwargs):
        (workspace_dir / "dream_logs.json").write_text(
            json.dumps({"records": [{"id": "new-record"}]}),
            encoding="utf-8",
        )

    monkeypatch.setattr(
        "swe.app.routers.dream_logs.run_dream_archive_maintenance",
        lambda *_args, **_kwargs: DreamArchiveMaintenanceResult(
            archived_items=[
                ArchiveItem(
                    id="archive-1",
                    original_path="old.md",
                    archive_path="governance/archive/files/archive-1",
                    size_bytes=7,
                    mtime="2026-05-24T09:00:00Z",
                    archived_at="2026-05-25T09:00:00Z",
                    archived_by="dream_cron",
                    archive_reason="dream_auto_mtime_3_days",
                ),
            ],
        ),
    )
    runner = SimpleNamespace(
        workspace_dir=workspace_dir,
        _workspace=None,
        memory_manager=SimpleNamespace(dream_memory=fake_dream_memory),
    )
    manager = CronManager(
        repo=object(),
        runner=runner,
        channel_manager=object(),
        agent_id="a" * 129,
        tenant_id=scope_id,
        continuous_governance_service=governance_service,
    )

    await manager.run_dream()

    assert governance_service.calls == []
    assert governance_service.archive_calls == []


def test_workspace_registers_source_config_service_for_cron_manager(
    tmp_path: Path,
) -> None:
    service = object()
    governance_service = object()
    workspace = Workspace(
        agent_id="default",
        workspace_dir=str(tmp_path / "tenant-a" / "workspaces" / "default"),
        tenant_id="tenant-a",
        source_system_config_service=service,
        continuous_governance_service=governance_service,
    )
    workspace._service_manager.services["runner"] = (
        object()
    )  # pylint: disable=protected-access

    cron_descriptor = workspace._service_manager.descriptors[
        "cron_manager"
    ]  # pylint: disable=protected-access
    init_args = cron_descriptor.init_args(workspace)

    assert init_args["source_system_config_service"] is service
    assert init_args["continuous_governance_service"] is governance_service


@pytest.mark.asyncio
async def test_multi_agent_manager_passes_source_config_service_to_workspace(
    tmp_path: Path,
) -> None:
    service = object()
    governance_service = object()
    manager = MultiAgentManager(
        source_system_config_service=service,
        continuous_governance_service=governance_service,
    )
    workspace_dir = tmp_path / "tenant-a" / "workspaces" / "default"

    with patch.object(
        MultiAgentManager,
        "_load_agent_config_for_tenant",
        return_value=SimpleNamespace(
            agents=SimpleNamespace(
                profiles={
                    "default": SimpleNamespace(
                        workspace_dir=str(workspace_dir),
                    ),
                },
            ),
        ),
    ):
        fake_workspace = Mock()
        fake_workspace.start = AsyncMock()
        fake_workspace.set_manager = Mock()
        with patch(
            "swe.app.multi_agent_manager.Workspace",
            return_value=fake_workspace,
        ) as workspace_cls:
            await manager.get_agent("default", tenant_id="tenant-a")

    workspace_cls.assert_called_once_with(
        agent_id="default",
        workspace_dir=str(workspace_dir),
        tenant_id="tenant-a",
        source_system_config_service=service,
        continuous_governance_service=governance_service,
    )


@pytest.mark.asyncio
async def test_tenant_workspace_pool_passes_source_config_service_to_workspace(
    tmp_path: Path,
) -> None:
    service = object()
    governance_service = object()
    pool = TenantWorkspacePool(
        tmp_path / "tenants",
        source_system_config_service=service,
        continuous_governance_service=governance_service,
    )
    fake_workspace = SimpleNamespace()

    with patch(
        "swe.app.workspace.tenant_pool.Workspace",
        return_value=fake_workspace,
    ) as workspace_cls:
        workspace = await pool.get_or_create("tenant-a", "default")

    assert workspace is fake_workspace
    workspace_cls.assert_called_once_with(
        agent_id="default",
        workspace_dir=str(
            tmp_path / "tenants" / "tenant-a" / "workspaces" / "default",
        ),
        tenant_id="tenant-a",
        source_system_config_service=service,
        continuous_governance_service=governance_service,
    )

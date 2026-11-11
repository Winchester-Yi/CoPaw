# -*- coding: utf-8 -*-
"""系统执行复用原业务，但不得吞掉失败或把梦境和归档重新绑在一起。"""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import sys

import pytest

from swe.app.crons import heartbeat
from swe.app.crons.manager import CronManager
from swe.config.context import encode_scope_id


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
async def test_heartbeat_timeout_only_propagates_for_managed_execution(
    tmp_path,
    monkeypatch,
    managed,
):
    (tmp_path / "HEARTBEAT.md").write_text("检查", encoding="utf-8")
    monkeypatch.setattr(
        heartbeat,
        "get_heartbeat_config",
        lambda *a, **k: SimpleNamespace(active_hours=None, target="main"),
    )
    monkeypatch.setattr(
        "swe.config.config.load_agent_config",
        lambda *a, **k: SimpleNamespace(last_dispatch=None),
    )

    timeout = AsyncMock(side_effect=TimeoutError())

    async def stream(_):
        yield await timeout()

    kwargs = {
        "runner": SimpleNamespace(stream_query=stream),
        "channel_manager": None,
        "agent_id": "default",
        "workspace_dir": tmp_path,
        "managed_execution": managed,
    }
    if managed:
        with pytest.raises(TimeoutError):
            await heartbeat.run_heartbeat_once(**kwargs)
    else:
        await heartbeat.run_heartbeat_once(**kwargs)


@pytest.mark.asyncio
async def test_heartbeat_failed_response_is_consumed_then_reported(
    tmp_path,
    monkeypatch,
):
    from agentscope_runtime.engine.schemas.agent_schemas import RunStatus

    (tmp_path / "HEARTBEAT.md").write_text("检查", encoding="utf-8")
    monkeypatch.setattr(
        heartbeat,
        "get_heartbeat_config",
        lambda *a, **k: SimpleNamespace(active_hours=None, target="main"),
    )
    monkeypatch.setattr(
        "swe.config.config.load_agent_config",
        lambda *a, **k: SimpleNamespace(last_dispatch=None),
    )
    consumed = []

    async def stream(_):
        yield SimpleNamespace(object="response", status=RunStatus.Failed)
        consumed.append(True)

    with pytest.raises(RuntimeError, match="system_heartbeat_failed"):
        await heartbeat.run_heartbeat_once(
            runner=SimpleNamespace(stream_query=stream),
            channel_manager=None,
            agent_id="default",
            workspace_dir=tmp_path,
            managed_execution=True,
        )
    assert consumed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("include_archive", [False, True])
async def test_new_dream_excludes_archive_but_legacy_default_keeps_it(
    tmp_path,
    monkeypatch,
    include_archive,
):
    manager = object.__new__(CronManager)
    manager._resolve_runtime_context = lambda: (
        tmp_path,
        "user",
        "source",
        encode_scope_id("user", "source"),
        "user",
    )
    manager._resolve_scheduled_run_source_system_config = AsyncMock(
        return_value=None,
    )
    manager._bind_scheduled_run_source_system_config = lambda _: nullcontext()
    manager._load_dream_record_ids = AsyncMock(return_value=set())
    manager._runner = SimpleNamespace(
        memory_manager=SimpleNamespace(dream_memory=AsyncMock()),
    )
    manager._continuous_governance_target_agent_id = lambda: "default"
    manager._dual_write_dream_records = AsyncMock()
    manager._continuous_governance_service = None
    archive = Mock(return_value=None)
    monkeypatch.setitem(
        sys.modules,
        "swe.app.routers.dream_logs",
        SimpleNamespace(
            run_dream_archive_maintenance=archive,
            dual_write_dream_archive_maintenance_result=AsyncMock(),
        ),
    )
    if include_archive:
        await manager.run_dream()
    else:
        await manager.run_dream(include_archive_maintenance=False)
    manager._runner.memory_manager.dream_memory.assert_awaited_once()
    manager._dual_write_dream_records.assert_awaited_once()
    assert archive.call_count == int(include_archive)

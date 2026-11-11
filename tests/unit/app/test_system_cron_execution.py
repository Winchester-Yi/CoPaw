# -*- coding: utf-8 -*-
"""系统业务只开始一次，配置关闭即时生效，已开始后的超时保留待核实。"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from swe.app.crons.system_execution import SystemExecutionService
from tests.unit.app.test_system_cron_results import accepted_run
from tests.unit.scheduler.test_system_cron_management import (
    CONFIG,
    system_setup,
)


@pytest.mark.asyncio
async def test_cancel_during_preparation_releases_known_not_started_execution(
    system_setup,
):
    writer, store, job, run, _ = await accepted_run(system_setup)
    entered = asyncio.Event()

    async def prepare(_):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        SystemExecutionService(
            writer,
            AsyncMock(return_value=CONFIG),
            prepare,
        ).execute(run, job),
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = await store.get(run["execution_id"])
    assert result["status"] == "error" and result["started_at"] is None
    assert result["error_code"] == "system_resource_prepare_cancelled"
    assert (await system_setup[1].get(job["job_id"]))[
        "active_execution_id"
    ] is None


@pytest.mark.asyncio
async def test_cancel_running_action_persists_unknown_before_shutdown_returns(
    system_setup,
):
    writer, store, job, run, _ = await accepted_run(system_setup)
    entered = asyncio.Event()

    async def action():
        entered.set()
        await asyncio.Event().wait()

    service = SystemExecutionService(
        writer,
        AsyncMock(return_value=CONFIG),
        AsyncMock(return_value=action),
    )
    task = asyncio.create_task(service.execute(run, job))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = await store.get(run["execution_id"])
    assert result["status"] == "unknown" and result["finished_at"] is None
    assert (await system_setup[1].get(job["job_id"]))[
        "active_execution_id"
    ] == run["execution_id"]


@pytest.mark.asyncio
async def test_two_instances_prepare_but_only_one_can_start_business(
    system_setup,
):
    writer, store, job, run, _ = await accepted_run(system_setup)
    action = AsyncMock(return_value={"sessions_cleaned": 2})
    arrived = 0
    both = asyncio.Event()

    async def prepare(_):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both.set()
        await both.wait()
        return action

    service = SystemExecutionService(
        writer,
        AsyncMock(return_value=CONFIG),
        prepare,
    )
    await asyncio.gather(service.execute(run, job), service.execute(run, job))
    action.assert_awaited_once()
    assert (await store.get(run["execution_id"]))["status"] == "success"


@pytest.mark.asyncio
async def test_config_disabled_during_resource_preparation_stops_actual_action(
    system_setup,
):
    writer, store, job, run, _ = await accepted_run(system_setup)
    action = AsyncMock()
    read = AsyncMock(side_effect=[CONFIG, {**CONFIG, "enabled": False}])
    await SystemExecutionService(
        writer,
        read,
        AsyncMock(return_value=action),
    ).execute(run, job)
    action.assert_not_awaited()
    result = await store.get(run["execution_id"])
    assert result["status"] == "skipped" and result["started_at"] is None


@pytest.mark.asyncio
async def test_already_disabled_does_not_initialize_workspace(system_setup):
    writer, store, job, run, _ = await accepted_run(system_setup)
    prepare = AsyncMock()
    await SystemExecutionService(
        writer,
        AsyncMock(return_value={**CONFIG, "enabled": False}),
        prepare,
    ).execute(run, job)
    prepare.assert_not_awaited()
    assert (await store.get(run["execution_id"]))["status"] == "skipped"


@pytest.mark.asyncio
async def test_resource_failure_is_known_not_started_and_releases_identity(
    system_setup,
):
    writer, store, job, run, _ = await accepted_run(system_setup)
    prepare = AsyncMock(side_effect=RuntimeError("private password"))
    await SystemExecutionService(
        writer,
        AsyncMock(return_value=CONFIG),
        prepare,
    ).execute(run, job)
    result = await store.get(run["execution_id"])
    assert result["status"] == "error" and result["started_at"] is None
    assert result["error_code"] == "system_resource_prepare_failed"


@pytest.mark.asyncio
async def test_business_timeout_keeps_identity_blocked_without_automatic_retry(
    system_setup,
):
    writer, store, job, run, _ = await accepted_run(system_setup)
    action = AsyncMock(side_effect=TimeoutError())
    service = SystemExecutionService(
        writer,
        AsyncMock(return_value=CONFIG),
        AsyncMock(return_value=action),
    )
    await service.execute(run, job)
    result = await store.get(run["execution_id"])
    assert result["status"] == "unknown" and result["finished_at"] is None
    assert (await system_setup[1].get(job["job_id"]))[
        "active_execution_id"
    ] == run["execution_id"]
    await service.execute(run, job)
    action.assert_awaited_once()


@pytest.mark.asyncio
async def test_source_partial_result_saves_counters_not_private_errors(
    system_setup,
):
    writer, store, job, run, _ = await accepted_run(system_setup)
    action = AsyncMock(
        return_value={
            "scopes_seen": 2,
            "scopes_failed": 1,
            "sessions_cleaned": 3,
            "errors": [{"error": "private"}],
        },
    )
    await SystemExecutionService(
        writer,
        AsyncMock(return_value=CONFIG),
        AsyncMock(return_value=action),
    ).execute(run, job)
    result = await store.get(run["execution_id"])
    assert result["status"] == "partial"
    assert "private" not in result["result_json"]

# -*- coding: utf-8 -*-
"""Tests for headers added at the CronManager.run_job boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

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


def _build_agent_job() -> CronJobSpec:
    return CronJobSpec(
        id="scheduled-job",
        name="scheduled job",
        enabled=True,
        tenant_id="tenant-a",
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


async def _run_and_capture_dispatch_meta(
    monkeypatch: pytest.MonkeyPatch,
    *,
    is_manual: bool,
    dispatch_meta: dict[str, Any] | None,
    job: CronJobSpec | None = None,
) -> tuple[CronJobSpec, dict[str, Any] | None]:
    job = job or _build_agent_job()
    manager = CronManager(
        repo=SimpleNamespace(get_job=AsyncMock(return_value=job)),
        runner=object(),
        channel_manager=object(),
    )
    execute_once = AsyncMock()
    created_tasks: list[asyncio.Task[None]] = []
    original_create_task = asyncio.create_task

    def capture_create_task(coro, *, name=None):
        task = original_create_task(coro, name=name)
        created_tasks.append(task)
        return task

    monkeypatch.setattr(
        manager,
        "_ensure_persisted_task_binding",
        AsyncMock(return_value=job),
    )
    monkeypatch.setattr(manager, "_execute_once", execute_once)
    monkeypatch.setattr(asyncio, "create_task", capture_create_task)

    await manager.run_job(
        job.id,
        is_manual=is_manual,
        dispatch_meta=dispatch_meta,
    )
    await created_tasks[0]

    return job, execute_once.await_args.kwargs["dispatch_meta"]


@pytest.mark.asyncio
async def test_scheduled_run_requires_an_execution_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _build_agent_job()
    manager = CronManager(
        repo=SimpleNamespace(get_job=AsyncMock(return_value=job)),
        runner=object(),
        channel_manager=object(),
    )
    ensure_binding = AsyncMock(return_value=job)
    monkeypatch.setattr(
        manager,
        "_ensure_persisted_task_binding",
        ensure_binding,
    )

    with pytest.raises(RuntimeError, match="execution identity"):
        await manager.run_job(job.id, is_manual=False)
    ensure_binding.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduled_run_preserves_headers_overrides_cron_job_id_and_copies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = {
        "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
        "cron_job_id": "untrusted-job-id",
    }
    dispatch_meta = {
        "passthrough_headers": headers,
        "parent_scheduled_fire_at": "2026-07-31T01:00:00Z",
    }

    job, observed = await _run_and_capture_dispatch_meta(
        monkeypatch,
        is_manual=False,
        dispatch_meta=dispatch_meta,
    )

    assert observed == {
        "cron_is_manual": False,
        "passthrough_headers": {
            "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
            "cron_job_id": job.id,
        },
        "parent_scheduled_fire_at": "2026-07-31T01:00:00Z",
    }
    assert observed is not dispatch_meta
    assert observed["passthrough_headers"] is not headers
    assert dispatch_meta == {
        "passthrough_headers": {
            "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
            "cron_job_id": "untrusted-job-id",
        },
        "parent_scheduled_fire_at": "2026-07-31T01:00:00Z",
    }


@pytest.mark.asyncio
async def test_run_preserves_persisted_headers_without_execution_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted_headers = {"Authorization": "Bearer persisted-token"}
    persisted_meta = {"passthrough_headers": persisted_headers}
    job = _build_agent_job()
    job.dispatch.meta = persisted_meta

    _, observed = await _run_and_capture_dispatch_meta(
        monkeypatch,
        is_manual=True,
        dispatch_meta=None,
        job=job,
    )

    assert observed.pop("cron_execution_key").startswith(f"manual:{job.id}:")
    assert observed == {
        "cron_is_manual": True,
        "passthrough_headers": {
            "Authorization": "Bearer persisted-token",
            "cron_job_id": job.id,
        },
    }
    assert job.dispatch.meta is persisted_meta
    assert job.dispatch.meta["passthrough_headers"] is persisted_headers
    assert persisted_meta == {
        "passthrough_headers": {"Authorization": "Bearer persisted-token"},
    }


@pytest.mark.asyncio
async def test_run_normalizes_cron_job_id_header_case_and_merges_copies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted_headers = {
        "Authorization": "Bearer persisted-token",
        "X-Shared": "persisted",
        "CRON_JOB_ID": "persisted-untrusted-job-id",
    }
    persisted_meta = {"passthrough_headers": persisted_headers}
    execution_headers = {
        "authorization": "Bearer execution-token",
        "X-Shared": "execution",
        "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
        "Cron_Job_Id": "execution-untrusted-job-id",
    }
    execution_meta = {
        "passthrough_headers": execution_headers,
        "scheduled_fire_at": "2026-07-31T01:00:00Z",
    }
    job = _build_agent_job()
    job.dispatch.meta = persisted_meta

    _, observed = await _run_and_capture_dispatch_meta(
        monkeypatch,
        is_manual=False,
        dispatch_meta=execution_meta,
        job=job,
    )

    assert observed == {
        "cron_is_manual": False,
        "passthrough_headers": {
            "authorization": "Bearer execution-token",
            "X-Shared": "execution",
            "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
            "cron_job_id": job.id,
        },
        "scheduled_fire_at": "2026-07-31T01:00:00Z",
    }
    assert [
        key
        for key in observed["passthrough_headers"]
        if str(key).casefold() == "cron_job_id"
    ] == ["cron_job_id"]
    assert job.dispatch.meta is persisted_meta
    assert job.dispatch.meta["passthrough_headers"] is persisted_headers
    assert persisted_meta == {
        "passthrough_headers": {
            "Authorization": "Bearer persisted-token",
            "X-Shared": "persisted",
            "CRON_JOB_ID": "persisted-untrusted-job-id",
        },
    }
    assert execution_meta == {
        "passthrough_headers": {
            "authorization": "Bearer execution-token",
            "X-Shared": "execution",
            "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
            "Cron_Job_Id": "execution-untrusted-job-id",
        },
        "scheduled_fire_at": "2026-07-31T01:00:00Z",
    }


@pytest.mark.asyncio
async def test_manual_run_preserves_headers_overrides_cron_job_id_and_copies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = {
        "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
        "cron_job_id": "untrusted-job-id",
    }
    dispatch_meta = {
        "passthrough_headers": headers,
        "parent_scheduled_fire_at": "2026-07-31T01:00:00Z",
    }

    job, observed = await _run_and_capture_dispatch_meta(
        monkeypatch,
        is_manual=True,
        dispatch_meta=dispatch_meta,
    )

    assert observed.pop("cron_execution_key").startswith(f"manual:{job.id}:")
    assert observed == {
        "cron_is_manual": True,
        "passthrough_headers": {
            "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
            "cron_job_id": job.id,
        },
        "parent_scheduled_fire_at": "2026-07-31T01:00:00Z",
    }
    assert observed is not dispatch_meta
    assert observed["passthrough_headers"] is not headers
    assert dispatch_meta == {
        "passthrough_headers": {
            "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
            "cron_job_id": "untrusted-job-id",
        },
        "parent_scheduled_fire_at": "2026-07-31T01:00:00Z",
    }


@pytest.mark.asyncio
async def test_manual_text_task_gets_a_unique_delivery_execution_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _build_agent_job().model_copy(
        update={
            "task_type": "text",
            "request": None,
            "text": "scheduled text",
            "meta": {
                "creator_user_id": "user-a",
                "task_session_id": "task-session-a",
            },
        },
    )

    _, observed = await _run_and_capture_dispatch_meta(
        monkeypatch,
        is_manual=True,
        dispatch_meta=None,
        job=job,
    )

    assert observed is not None
    assert observed["cron_execution_key"].startswith(
        f"manual:{job.id}:",
    )

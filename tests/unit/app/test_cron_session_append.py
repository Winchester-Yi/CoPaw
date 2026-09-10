# -*- coding: utf-8 -*-
"""Cron session delta and execution-key regression coverage."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from swe.app.crons.executor import CronExecutor, _build_cron_execution_key
from swe.app.crons.models import (
    CronJobRequest,
    CronJobSpec,
    DispatchSpec,
    DispatchTarget,
    ScheduleSpec,
)
from swe.app.cron_result_metrics import (
    CRON_EXECUTION_KEY_CONFLICT_TOTAL,
    get_cron_result_metrics,
    reset_cron_result_metrics_for_test,
)
from swe.app.runner.runner import AgentRunner, _build_cron_append_state
from swe.app.runner.session import SafeJSONSession


def _entry(role: str, text: str, timestamp: str) -> dict[str, Any]:
    return {
        "role": role,
        "content": [{"type": "text", "text": text}],
        "timestamp": timestamp,
    }


def _agent_state(entries: list[Any]) -> dict[str, Any]:
    return {
        "memory": {"content": entries},
    }


def test_cron_appends_only_request_delta_and_keeps_existing_agent_fields() -> (
    None
):
    existing: dict[str, Any] = {
        "agent": {
            "model": {"name": "manual-model"},
            "memory": {"content": [_entry("user", "u1", "t1")]},
        },
        "task_runs": [],
    }
    current: dict[str, Any] = _agent_state(
        [_entry("user", "u2", "t2"), _entry("assistant", "a2", "t2")],
    )

    merged, old, new, stripped, committed = _build_cron_append_state(
        existing,
        current,
        None,
        execution_key="job-1:fire-1:session-1",
    )

    assert committed is True
    assert old == existing["agent"]["memory"]["content"]
    assert new == current["memory"]["content"]
    assert stripped == 0
    assert merged["agent"]["model"] == {"name": "manual-model"}
    assert merged["agent"]["memory"]["content"] == old + new
    assert merged["task_runs"][0]["execution_key"] == (
        "job-1:fire-1:session-1"
    )


def test_same_execution_key_is_idempotent() -> None:
    execution_key = "job-1:fire-1:session-1"
    current: dict[str, Any] = _agent_state([_entry("user", "u1", "t1")])
    existing, *_ = _build_cron_append_state(
        {"agent": {"memory": {"content": []}}, "task_runs": []},
        current,
        None,
        execution_key=execution_key,
    )

    merged, old, new, stripped, committed = _build_cron_append_state(
        existing,
        current,
        None,
        execution_key=execution_key,
    )

    assert committed is False
    assert merged is existing
    assert old == existing["agent"]["memory"]["content"]
    assert new == current["memory"]["content"]
    assert stripped == 0


def test_replayed_cron_append_reports_no_session_commit() -> None:
    """An idempotent replay must not be reported as a newly committed run."""
    execution_key = "job-1:fire-1:session-1"
    current_state = _agent_state([_entry("user", "input", "t1")])
    existing, *_ = _build_cron_append_state(
        {"agent": {"memory": {"content": []}}, "task_runs": []},
        current_state,
        None,
        execution_key=execution_key,
    )
    session_execution = SimpleNamespace(
        state=existing,
        commit_state=AsyncMock(),
    )
    agent = SimpleNamespace(
        state_dict=lambda: current_state,
        _request_context={"cron_execution_key": execution_key},
    )

    committed = asyncio.run(
        AgentRunner()._save_cron_session_state(  # pylint: disable=protected-access
            agent,
            "session-1",
            "user-1",
            session_execution=session_execution,
        ),
    )

    assert committed is False
    session_execution.commit_state.assert_not_awaited()


def test_same_execution_key_without_input_hash_raises_conflict() -> None:
    """无法证明旧记录输入一致时，不能把它当作合法重放。"""
    execution_key = "job-1:fire-1:session-1"
    reset_cron_result_metrics_for_test()
    existing: dict[str, Any] = {
        "agent": {"memory": {"content": [_entry("user", "old", "t1")]}},
        "task_runs": [{"execution_key": execution_key}],
    }

    with pytest.raises(RuntimeError, match="execution_key_conflict"):
        _build_cron_append_state(
            existing,
            _agent_state([_entry("user", "new", "t2")]),
            None,
            execution_key=execution_key,
        )

    assert get_cron_result_metrics()[CRON_EXECUTION_KEY_CONFLICT_TOTAL] == 1


def test_same_execution_key_with_different_input_raises_conflict() -> None:
    """同一 key 只能重放同一输入，不能静默吞掉不同任务。"""
    execution_key = "job-1:fire-1:session-1"
    reset_cron_result_metrics_for_test()
    first = _agent_state([_entry("user", "first", "t1")])
    existing, *_ = _build_cron_append_state(
        {"agent": {"memory": {"content": []}}, "task_runs": []},
        first,
        None,
        execution_key=execution_key,
    )

    with __import__("pytest").raises(RuntimeError, match="execution_key_conflict"):
        _build_cron_append_state(
            existing,
            _agent_state([_entry("user", "second", "t2")]),
            None,
            execution_key=execution_key,
        )
    assert get_cron_result_metrics()[CRON_EXECUTION_KEY_CONFLICT_TOTAL] == 1


def test_task_run_records_execution_identity_and_input_hash() -> None:
    """每个任务运行记录必须带可定位的执行身份与输入指纹。"""
    merged, *_ = _build_cron_append_state(
        {"agent": {"memory": {"content": []}}, "task_runs": []},
        _agent_state([_entry("user", "input", "t1")]),
        None,
        execution_key="job-1:fire-1:session-1",
        job_id="job-1",
        session_id="session-1",
        user_id="user-1",
    )

    assert merged["task_runs"][0] == {
        **merged["task_runs"][0],
        "job_id": "job-1",
        "session_id": "session-1",
        "user_id": "user-1",
        "execution_key": "job-1:fire-1:session-1",
        "input_hash": merged["task_runs"][0]["input_hash"],
    }


@pytest.mark.asyncio
async def test_concurrent_cron_appends_preserve_messages_and_deduplicate_replay(
    tmp_path: Path,
) -> None:
    """同一 session 的并发执行不能丢消息，也不能重复追加同一执行键。"""
    session = SafeJSONSession(save_dir=str(tmp_path))

    async def append(text: str, execution_key: str) -> None:
        current_state = _agent_state([_entry("user", text, execution_key)])
        async with session.execution("session-1", "user-1") as transaction:
            merged, *_rest, should_commit = _build_cron_append_state(
                transaction.state,
                current_state,
                None,
                execution_key=execution_key,
                job_id="job-1",
                session_id="session-1",
                user_id="user-1",
            )
            if should_commit:
                await transaction.commit_state(merged)

    await asyncio.gather(
        append("first", "job-1:fire-1:session-1"),
        append("second", "job-1:fire-2:session-1"),
        append("first", "job-1:fire-1:session-1"),
    )

    state = await session.get_session_state_dict("session-1", "user-1")
    messages = state["agent"]["memory"]["content"]
    assert sorted(message["content"][0]["text"] for message in messages) == [
        "first",
        "second",
    ]
    assert sorted(run["execution_key"] for run in state["task_runs"]) == [
        "job-1:fire-1:session-1",
        "job-1:fire-2:session-1",
    ]


def test_cron_strips_internal_follow_up_before_append() -> None:
    internal = _entry("assistant", "internal", "t2")
    internal["metadata"] = {"swe_internal_follow_up": True}
    visible = _entry("assistant", "visible", "t2")
    existing: dict[str, Any] = {
        "agent": {"memory": {"content": [_entry("user", "u1", "t1")]}},
        "task_runs": [],
    }
    current: dict[str, Any] = _agent_state([[internal, []], [visible, []]])

    merged, old, new, stripped, committed = _build_cron_append_state(
        existing,
        current,
        None,
        execution_key="job-1:fire-2:session-1",
    )

    assert committed is True
    assert stripped == 1
    assert old == existing["agent"]["memory"]["content"]
    assert new == [[visible, []]]
    assert merged["agent"]["memory"]["content"] == old + [[visible, []]]
    assert merged["task_runs"][0]["memory_start"] == 1
    assert merged["task_runs"][0]["memory_end"] == 2


def test_execution_key_uses_scheduler_identity_not_worker_time() -> None:
    assert (
        _build_cron_execution_key(
            job_id="job-1",
            target_session_id="session-1",
            dispatch_meta={
                "scheduled_fire_at": "2026-08-26T09:00:00Z",
            },
        )
        == "job-1:2026-08-26T09:00:00Z:session-1"
    )
    assert (
        _build_cron_execution_key(
            job_id="job-1",
            target_session_id="session-1",
            dispatch_meta={
                "batch_id": "batch-1",
                "intent_id": 7,
            },
        )
        == "job-1:batch-1:7:session-1"
    )
    assert (
        _build_cron_execution_key(
            job_id="job-1",
            target_session_id="session-1",
            dispatch_meta={"cron_is_manual": True},
        )
        == ""
    )


def test_manual_cron_requests_have_distinct_persistence_receipts() -> None:
    """Manual runs may share an idempotency key but never a cleanup receipt."""
    executor = CronExecutor(runner=object(), channel_manager=object())
    job = CronJobSpec(
        id="job-1",
        name="manual task",
        schedule=ScheduleSpec(cron="* * * * *"),
        task_type="agent",
        request=CronJobRequest(input=[]),
        dispatch=DispatchSpec(
            target=DispatchTarget(user_id="user-1", session_id="session-1"),
        ),
    )

    first = executor._build_agent_request(  # pylint: disable=protected-access
        job,
        "user-1",
        "session-1",
        dispatch_meta={"cron_is_manual": True},
    )
    second = executor._build_agent_request(  # pylint: disable=protected-access
        job,
        "user-1",
        "session-1",
        dispatch_meta={"cron_is_manual": True},
    )

    assert "cron_execution_key" not in first
    assert first["cron_persistence_key"] != second["cron_persistence_key"]

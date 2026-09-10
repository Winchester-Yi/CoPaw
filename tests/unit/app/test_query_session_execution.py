# -*- coding: utf-8 -*-
"""Regression coverage for the query-scoped JSON session transaction."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock

import pytest
from agentscope.message import Msg

from swe.app.runner.query_attempt import (
    _save_and_close_session_execution,
    stream_query_after_preflight,
)
from swe.app.runner.query_contracts import (
    QueryPersistenceResult,
    _QueryPreflight,
)
from swe.app.answer_turn.models import TurnIdentity, TurnStatus
from swe.app.runner.runner import AgentRunner
from swe.app.runner.session import SafeJSONSession


class _RecordingExecution:
    def __init__(self, session: "_RecordingSession") -> None:
        self._session = session
        self.state: dict[str, Any] = {}
        self._state_dirty = False

    @property
    def has_uncommitted_state(self) -> bool:
        return self._state_dirty

    def mark_state_dirty(self) -> None:
        self._state_dirty = True

    async def commit_state(self, state: dict[str, Any]) -> None:
        self.state = state
        self._state_dirty = False
        self._session.commit_count += 1
        self._session.committed_states.append(dict(state))

    async def close(self) -> None:
        self._session.in_execution = False


class _RecordingSession:
    def __init__(self) -> None:
        self.execution_entries = 0
        self.commit_count = 0
        self.committed_states: list[dict[str, Any]] = []
        self.in_execution = False
        self.active_execution: _RecordingExecution | None = None
        self.execution_timeouts: list[float | None] = []

    @asynccontextmanager
    async def execution(
        self,
        _session_id: str,
        user_id: str = "",
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[_RecordingExecution]:
        del user_id
        self.execution_entries += 1
        self.execution_timeouts.append(timeout_seconds)
        self.in_execution = True
        execution = _RecordingExecution(self)
        self.active_execution = execution
        try:
            yield execution
        finally:
            self.in_execution = False
            self.active_execution = None


class _RecordingCoordinator:
    def __init__(self) -> None:
        self.outcomes: list[Any] = []

    async def settle(self, outcome: Any) -> bool:
        self.outcomes.append(outcome)
        return True


class _SnapshotAgent:
    def __init__(self) -> None:
        self.memory = SimpleNamespace(add=self._add_to_memory)
        self.memory_entries: list[str] = []

    async def _add_to_memory(self, message: Msg) -> None:
        self.memory_entries.append(message.get_text_content())

    def state_dict(self) -> dict[str, list[str]]:
        return {"memory_entries": list(self.memory_entries)}


@pytest.mark.asyncio
async def test_session_cleanup_reports_commit_result_and_assistant_count() -> (
    None
):
    """会话清理应向调用方报告提交结果与 assistant 数量。"""
    session = _RecordingSession()
    execution = _RecordingExecution(session)
    assistant = SimpleNamespace(role="assistant")
    user = SimpleNamespace(role="user")
    runtime = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        session_execution=execution,
        agent=SimpleNamespace(
            memory=SimpleNamespace(content=[(user, True), (assistant, True)]),
        ),
        session_state_commit_attempted=False,
        session_state_committed=False,
    )

    class Owner:
        async def _save_state_during_cleanup(self, **kwargs: Any) -> bool:
            await kwargs["runtime"].session_execution.commit_state(
                {"agent": {"memory": {"content": [{"role": "assistant"}]}}},
            )
            return True

    request = SimpleNamespace(user_id="user-1")
    await _save_and_close_session_execution(
        Owner(),
        cleanup_runtime=runtime,
        cleanup_state_loaded=True,
        retry_state=SimpleNamespace(prev_agent=None),
        request=request,
        session_id="session-1",
        session_execution=execution,
    )

    assert runtime.persistence_result == QueryPersistenceResult(
        session_id="session-1",
        user_id="user-1",
        assistant_message_count=1,
        commit_attempted=True,
        committed=True,
        commit_error=None,
    )


@pytest.mark.asyncio
async def test_session_cleanup_does_not_report_commit_when_cleanup_is_noop() -> (
    None
):
    """A skipped state save must not be reported as a committed session."""
    recorded: list[QueryPersistenceResult] = []

    class Owner:
        async def _save_state_during_cleanup(
            self,
            **_kwargs: Any,
        ) -> bool | None:
            return None

        def _record_query_persistence_result(
            self,
            _request: Any,
            result: QueryPersistenceResult,
        ) -> None:
            recorded.append(result)

    execution = SimpleNamespace(
        has_uncommitted_state=False,
        close=AsyncMock(),
    )
    runtime = SimpleNamespace(
        agent=SimpleNamespace(
            memory=SimpleNamespace(
                content=[(SimpleNamespace(role="assistant"), True)],
            ),
        ),
        session_state_commit_attempted=False,
        session_state_committed=False,
    )

    await _save_and_close_session_execution(
        Owner(),
        cleanup_runtime=runtime,
        cleanup_state_loaded=False,
        retry_state=SimpleNamespace(prev_agent=None),
        request=SimpleNamespace(user_id="user-1"),
        session_id="session-1",
        session_execution=execution,
    )

    assert recorded == [
        QueryPersistenceResult(
            session_id="session-1",
            user_id="user-1",
            assistant_message_count=0,
            commit_attempted=False,
            committed=False,
            commit_error="session_state_not_persisted",
        ),
    ]


@pytest.mark.asyncio
async def test_real_json_session_cleanup_commits_assistant_state(
    tmp_path: Path,
) -> None:
    """真实 SafeJSONSession 提交后，cleanup 回执必须确认已落盘。"""
    session = SafeJSONSession(save_dir=str(tmp_path))
    recorded: list[QueryPersistenceResult] = []
    assistant = SimpleNamespace(role="assistant")

    class Owner:
        async def _save_state_during_cleanup(self, **kwargs: Any) -> bool:
            await kwargs["runtime"].session_execution.commit_state(
                {
                    "agent": {
                        "memory": {"content": [{"role": "assistant"}]},
                    },
                },
            )
            return True

        def _record_query_persistence_result(
            self,
            _request: Any,
            result: QueryPersistenceResult,
        ) -> None:
            recorded.append(result)

    request = SimpleNamespace(user_id="user-1", skip_history=False)
    async with session.execution("session-1", "user-1") as execution:
        runtime = SimpleNamespace(
            session_id="session-1",
            user_id="user-1",
            session_execution=execution,
            agent=SimpleNamespace(
                memory=SimpleNamespace(content=[(assistant, True)]),
            ),
            session_state_commit_attempted=False,
            session_state_committed=False,
        )
        await _save_and_close_session_execution(
            Owner(),
            cleanup_runtime=runtime,
            cleanup_state_loaded=True,
            retry_state=SimpleNamespace(prev_agent=None),
            request=request,
            session_id="session-1",
            session_execution=execution,
        )

    assert recorded == [
        QueryPersistenceResult(
            session_id="session-1",
            user_id="user-1",
            assistant_message_count=1,
            commit_attempted=True,
            committed=True,
        ),
    ]
    assert (await session.get_session_state_dict("session-1", "user-1"))[
        "agent"
    ]["memory"]["content"] == [{"role": "assistant"}]


@pytest.mark.asyncio
async def test_session_cleanup_counts_only_this_cron_persistence_receipt() -> (
    None
):
    """An assistant from an older run cannot satisfy this run's receipt."""
    recorded: list[QueryPersistenceResult] = []

    class Owner:
        async def _save_state_during_cleanup(self, **_kwargs: Any) -> bool:
            return True

        def _record_query_persistence_result(
            self,
            _request: Any,
            result: QueryPersistenceResult,
        ) -> None:
            recorded.append(result)

    execution = SimpleNamespace(
        state={
            "agent": {"memory": {"content": [{"role": "assistant"}]}},
            "task_runs": [
                {
                    "persistence_key": "older-receipt",
                    "memory_start": 0,
                    "memory_end": 1,
                },
            ],
        },
        close=AsyncMock(),
    )
    runtime = SimpleNamespace(
        agent=SimpleNamespace(memory=SimpleNamespace(content=[])),
        session_state_commit_attempted=False,
        session_state_committed=False,
    )

    await _save_and_close_session_execution(
        Owner(),
        cleanup_runtime=runtime,
        cleanup_state_loaded=True,
        retry_state=SimpleNamespace(prev_agent=None),
        request=SimpleNamespace(
            user_id="user-1",
            cron_persistence_key="current-receipt",
        ),
        session_id="session-1",
        session_execution=execution,
    )

    assert recorded[0].committed is True
    assert recorded[0].assistant_message_count == 0


@pytest.mark.asyncio
async def test_session_cleanup_records_commit_failure_when_close_also_fails() -> (
    None
):
    """关闭 session 失败不能吞掉已知的提交失败回执。"""
    recorded: list[QueryPersistenceResult] = []

    class Owner:
        async def _save_state_during_cleanup(self, **_kwargs: Any) -> None:
            raise RuntimeError("commit failed")

        def _record_query_persistence_result(
            self,
            _request: Any,
            result: QueryPersistenceResult,
        ) -> None:
            recorded.append(result)

    async def _close() -> None:
        raise RuntimeError("close failed")

    execution = SimpleNamespace(close=_close)
    request = SimpleNamespace(user_id="user-1", skip_history=False)

    with pytest.raises(RuntimeError, match="commit failed"):
        await _save_and_close_session_execution(
            Owner(),
            cleanup_runtime=None,
            cleanup_state_loaded=False,
            retry_state=SimpleNamespace(prev_agent=None),
            request=request,
            session_id="session-1",
            session_execution=execution,
        )

    assert recorded == [
        QueryPersistenceResult(
            session_id="session-1",
            user_id="user-1",
            assistant_message_count=0,
            commit_attempted=True,
            committed=False,
            commit_error="commit failed",
        ),
    ]


@pytest.mark.asyncio
async def test_session_cleanup_propagates_keyboard_interrupt_from_close() -> (
    None
):
    """Cancellation-like BaseException from close must not be swallowed."""

    class Owner:
        async def _save_state_during_cleanup(self, **_kwargs: Any) -> None:
            raise RuntimeError("commit failed")

    async def _close() -> None:
        raise KeyboardInterrupt

    execution = SimpleNamespace(close=_close)

    with pytest.raises(KeyboardInterrupt):
        await _save_and_close_session_execution(
            Owner(),
            cleanup_runtime=None,
            cleanup_state_loaded=False,
            retry_state=SimpleNamespace(prev_agent=None),
            request=SimpleNamespace(user_id="user-1"),
            session_id="session-1",
            session_execution=execution,
        )


@pytest.mark.asyncio
async def test_session_cleanup_ignores_close_failure_after_commit() -> None:
    """A close failure cannot turn an already committed query into an error."""
    recorded: list[QueryPersistenceResult] = []
    lifecycle: list[str] = []

    class Owner:
        async def _save_state_during_cleanup(self, **_kwargs: Any) -> bool:
            return True

        def _record_query_persistence_result(
            self,
            _request: Any,
            result: QueryPersistenceResult,
        ) -> None:
            lifecycle.append("record")
            recorded.append(result)

    async def _close() -> None:
        lifecycle.append("close")
        raise RuntimeError("close failed")

    execution = SimpleNamespace(
        state={"agent": {"memory": {"content": []}}},
        close=_close,
    )

    await _save_and_close_session_execution(
        Owner(),
        cleanup_runtime=None,
        cleanup_state_loaded=True,
        retry_state=SimpleNamespace(prev_agent=SimpleNamespace()),
        request=SimpleNamespace(user_id="user-1"),
        session_id="session-1",
        session_execution=execution,
    )

    assert recorded == [
        QueryPersistenceResult(
            session_id="session-1",
            user_id="user-1",
            assistant_message_count=0,
            commit_attempted=True,
            committed=True,
        ),
    ]
    assert lifecycle == ["close", "record"]


@pytest.mark.asyncio
async def test_session_cleanup_reports_matching_task_run_as_idempotent_replay() -> (
    None
):
    """A scheduler replay consumes its existing persisted assistant result."""
    recorded: list[QueryPersistenceResult] = []

    class Owner:
        async def _save_state_during_cleanup(self, **_kwargs: Any) -> bool:
            return False

        def _record_query_persistence_result(
            self,
            _request: Any,
            result: QueryPersistenceResult,
        ) -> None:
            recorded.append(result)

    execution = SimpleNamespace(
        state={
            "agent": {
                "memory": {
                    "content": [
                        {"role": "user", "content": "input"},
                        {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "persisted output"},
                            ],
                        },
                    ],
                },
            },
            "task_runs": [
                {
                    "execution_key": "execution-1",
                    "memory_start": 0,
                    "memory_end": 2,
                    "cron_delivery_version": 1,
                    "output_delivery_completed": True,
                },
            ],
        },
        close=AsyncMock(),
    )

    await _save_and_close_session_execution(
        Owner(),
        cleanup_runtime=None,
        cleanup_state_loaded=True,
        retry_state=SimpleNamespace(prev_agent=SimpleNamespace()),
        request=SimpleNamespace(
            user_id="user-1",
            cron_execution_key="execution-1",
            cron_persistence_key="replay-receipt",
        ),
        session_id="session-1",
        session_execution=execution,
    )

    assert recorded == [
        QueryPersistenceResult(
            session_id="session-1",
            user_id="user-1",
            assistant_message_count=1,
            commit_attempted=False,
            committed=True,
            idempotent_replay=True,
            persisted_assistant_content=[
                {"type": "text", "text": "persisted output"},
            ],
            output_delivery_replay_supported=True,
            output_delivery_completed=True,
        ),
    ]
    execution.close.assert_awaited_once()


def test_agent_runner_returns_persistence_result_for_scheduled_query() -> None:
    """Cron 仅能读取自身 query 的提交结果，不能读取其他任务状态。"""
    runner = AgentRunner()
    result = QueryPersistenceResult(
        session_id="session-1",
        user_id="user-1",
        assistant_message_count=1,
        commit_attempted=True,
        committed=True,
    )
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        execution_origin="scheduled",
        cron_execution_key="job-1:fire-1",
        cron_persistence_key="receipt-1",
    )

    runner._record_query_persistence_result(request, result)

    assert (
        runner.get_query_persistence_result(
            session_id="session-1",
            user_id="user-1",
            execution_key="receipt-1",
        )
        == result
    )
    assert (
        runner.get_query_persistence_result(
            session_id="session-1",
            user_id="user-1",
            execution_key="receipt-1",
        )
        is None
    )


@pytest.mark.asyncio
async def test_agent_runner_persists_cron_output_delivery_receipt(
    tmp_path: Path,
) -> None:
    """Final output delivery is recorded in the persisted task-run state."""
    runner = AgentRunner()
    runner.session = SafeJSONSession(str(tmp_path))
    async with runner.session.execution(
        "session-1",
        user_id="user-1",
    ) as execution:
        state = execution.state
        state["task_runs"] = [
            {
                "execution_key": "execution-1",
                "persistence_key": "receipt-1",
                "output_delivery_completed": False,
            },
        ]
        await execution.commit_state(state)

    await runner.mark_cron_output_delivery_completed(
        session_id="session-1",
        user_id="user-1",
        execution_key="execution-1",
        persistence_key="receipt-1",
    )

    state = await runner.session.get_session_state_dict(
        "session-1",
        user_id="user-1",
    )
    assert state["task_runs"][0]["output_delivery_completed"] is True


def test_agent_runner_discards_expired_persistence_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unconsumed Cron receipts expire instead of retaining session state."""
    now = {"value": 100.0}
    monkeypatch.setattr(
        "swe.app.runner.runner.time.monotonic",
        lambda: now["value"],
    )
    runner = AgentRunner()
    result = QueryPersistenceResult(
        session_id="session-1",
        user_id="user-1",
        assistant_message_count=1,
        commit_attempted=True,
        committed=True,
    )
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        execution_origin="scheduled",
        cron_persistence_key="receipt-1",
    )

    runner._record_query_persistence_result(request, result)
    now["value"] = 161.0

    assert (
        runner.get_query_persistence_result(
            session_id="session-1",
            user_id="user-1",
            execution_key="receipt-1",
        )
        is None
    )
    assert runner._cron_persistence_results == {}


def test_agent_runner_bounds_unconsumed_persistence_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle Runner cannot retain an unbounded number of late receipts."""
    monkeypatch.setattr(
        "swe.app.runner.runner.CRON_PERSISTENCE_RESULT_MAX_ENTRIES",
        2,
    )
    runner = AgentRunner()
    result = QueryPersistenceResult(
        session_id="session-1",
        user_id="user-1",
        assistant_message_count=1,
        commit_attempted=True,
        committed=True,
    )

    for index in range(3):
        runner._record_query_persistence_result(
            SimpleNamespace(
                session_id="session-1",
                user_id="user-1",
                execution_origin="scheduled",
                cron_persistence_key=f"receipt-{index}",
            ),
            result,
        )

    assert set(runner._cron_persistence_results) == {
        ("session-1", "user-1", "receipt-1"),
        ("session-1", "user-1", "receipt-2"),
    }


def test_agent_runner_does_not_cache_regular_query_persistence_result() -> (
    None
):
    """普通查询没有 Cron 消费者，不能把 cleanup 回执留在 Runner 中。"""
    runner = AgentRunner()
    result = QueryPersistenceResult(
        session_id="session-1",
        user_id="user-1",
        assistant_message_count=1,
        commit_attempted=True,
        committed=True,
    )
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        execution_origin="interactive",
        cron_execution_key="",
    )

    runner._record_query_persistence_result(request, result)

    assert (
        runner.get_query_persistence_result(
            session_id="session-1",
            user_id="user-1",
        )
        is None
    )


class _RetryableError(RuntimeError):
    status_code = 429


class _RecordingOwner:
    agent_id = "test-agent"
    tenant_id = None

    def __init__(self) -> None:
        self.session = _RecordingSession()
        self.attempts = 0
        self.short_mutation_count = 0
        self.expected_retry_state: dict[str, list[str]] | None = None
        self.fail_runtime_on_retry = False
        self.first_agent: _SnapshotAgent | None = None

    def _request_file_url_network(self, _request: Any) -> None:
        return None

    async def _start_query_trace(
        self,
        _request: Any,
        _msgs: list[Any],
    ) -> None:
        return None

    def _new_query_turn_outcome(self) -> Any:
        return SimpleNamespace(task_completed=False)

    def _new_retry_state(self) -> Any:
        return SimpleNamespace(
            agent=None,
            prev_agent=None,
            session_state_loaded=False,
            prev_session_state_loaded=False,
            task_completed=False,
        )

    def _load_query_retry_settings(
        self,
        _config: Any = None,
    ) -> tuple[int, int, float, float]:
        return 2, 1, 0.0, 0.0

    def _new_query_attempt_input(self, **kwargs: Any) -> Any:
        return SimpleNamespace(**kwargs)

    def _new_query_attempt_state(self) -> Any:
        return SimpleNamespace(
            runtime=None,
            runtime_start=None,
            session_state_loaded=False,
            should_return=False,
            succeeded=False,
            session_title_task_started=False,
        )

    async def _stream_retry_backoff_notice(self, **_kwargs: Any):
        if _kwargs.get("emit_test_message"):
            yield None

    async def _stream_single_query_attempt(
        self,
        *,
        attempt_input: Any,
        outcome: Any,
        retry_state: Any,
        attempt_state: Any,
    ):
        del outcome
        self.attempts += 1
        agent = _SnapshotAgent()
        attempt_state.runtime = SimpleNamespace(
            agent=agent,
            skip_history=False,
            user_id="user-1",
            session_id="session-1",
            session_execution=attempt_input.session_execution,
        )
        attempt_state.session_state_loaded = True
        retry_state.agent = agent
        retry_state.session_state_loaded = True
        if self.attempts == 1:
            self.first_agent = agent
            self.expected_retry_state = {
                "memory_entries": ["请求频率超限，正在重试 (1/1)..."],
            }
            raise _RetryableError("rate limited")
        if self.fail_runtime_on_retry:
            attempt_state.runtime = None
            attempt_state.session_state_loaded = False
            raise RuntimeError("runtime preparation failed")
        assert retry_state.agent_state_snapshot == self.expected_retry_state
        attempt_state.succeeded = True
        if getattr(attempt_state, "emit_test_message", False):
            yield None

    def _should_retry(
        self,
        retry_attempt: int,
        max_retry_attempts: int,
        _exc: BaseException,
    ) -> bool:
        return retry_attempt < max_retry_attempts - 1

    async def _raise_console_model_call_failed_if_needed(
        self,
        **_kwargs: Any,
    ) -> None:
        if self.fail_runtime_on_retry:
            return None
        raise AssertionError("the retry should succeed")

    async def _handle_query_error(self, **_kwargs: Any) -> None:
        if self.fail_runtime_on_retry:
            return None
        raise AssertionError("the retry should succeed")

    async def _stream_retryable_query_error(self, **kwargs: Any):
        from swe.app.runner.query_attempt import stream_retryable_query_error

        async for (
            item
        ) in stream_retryable_query_error(  # pylint: disable=missing-kwoa
            self,
            **kwargs,
        ):
            yield item

    async def save_job_session_state(
        self,
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        self.short_mutation_count += 1

    async def _save_state_during_cleanup(
        self,
        *,
        runtime: Any,
        fallback_agent: Any = None,
        **_kwargs: Any,
    ) -> None:
        assert self.session.in_execution
        if self.fail_runtime_on_retry:
            assert fallback_agent is self.first_agent
            await self.session.active_execution.commit_state({"agent": {}})
            return
        assert runtime.session_execution is self.session.active_execution
        await runtime.session_execution.commit_state({"agent": {}})

    async def _handle_query_cancelled(self, **_kwargs: Any) -> None:
        raise AssertionError("the retry should not be cancelled")

    async def _cleanup_query_resources(self, **_kwargs: Any) -> None:
        assert not self.session.in_execution

    async def _cleanup_blocked_runtime_start(
        self,
        _runtime_start: Any,
    ) -> None:
        return None

    async def _store_qa_content_if_needed(self, **_kwargs: Any) -> None:
        return None

    async def _end_trace_if_needed(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_retry_uses_one_transaction_and_writes_only_final_state() -> (
    None
):
    owner = _RecordingOwner()
    request = SimpleNamespace(user_id="user-1")

    outputs = [
        item
        async for item in stream_query_after_preflight(
            owner,
            msgs=[],
            request=request,
            query="hello",
            session_id="session-1",
            preflight=_QueryPreflight(),
        )
    ]

    assert len(outputs) == 1
    assert owner.attempts == 2
    assert owner.session.execution_entries == 1
    assert owner.session.commit_count == 1
    assert owner.short_mutation_count == 0


@pytest.mark.asyncio
async def test_runner_reports_one_completed_outcome_with_coordinator_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _RecordingCoordinator()
    identity = TurnIdentity.create(chat_id="chat-1", msgid="msg-1")
    runner = AgentRunner(
        agent_id="test-agent",
        answer_turn_coordinator=coordinator,
    )
    runner._query_execution = None

    async def stream_entry(*_args: Any, **_kwargs: Any):
        yield Msg(name="Friday", role="assistant", content="answer"), True

    monkeypatch.setattr(runner, "_stream_query_entry", stream_entry)
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        execution_origin="scheduled",
        channel_meta={"answer_turn_identity": identity},
    )

    events = [
        event async for event in runner.query_handler([], request=request)
    ]

    assert len(events) == 1
    assert request.channel_meta["turn_id"] == identity.turn_id
    assert [outcome.status for outcome in coordinator.outcomes] == [
        TurnStatus.COMPLETED,
    ]
    assert coordinator.outcomes[0].identity is identity


@pytest.mark.asyncio
async def test_scheduled_query_uses_one_third_of_existing_job_timeout_for_lock() -> (
    None
):
    owner = _RecordingOwner()
    request = SimpleNamespace(
        user_id="user-1",
        execution_origin="scheduled",
        cron_timeout_seconds=60,
    )

    async for _item in stream_query_after_preflight(
        owner,
        msgs=[],
        request=request,
        query="hello",
        session_id="session-1",
        preflight=_QueryPreflight(),
    ):
        pass

    assert owner.session.execution_timeouts == [20.0]


@pytest.mark.asyncio
async def test_retry_runtime_failure_commits_previous_agent_once_before_rethrowing() -> (
    None
):
    owner = _RecordingOwner()
    owner.fail_runtime_on_retry = True

    with pytest.raises(RuntimeError, match="runtime preparation failed"):
        async for _item in stream_query_after_preflight(
            owner,
            msgs=[],
            request=SimpleNamespace(user_id="user-1"),
            query="hello",
            session_id="session-1",
            preflight=_QueryPreflight(),
        ):
            pass

    assert owner.session.commit_count == 1
    assert owner.short_mutation_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [asyncio.TimeoutError(), asyncio.CancelledError()],
)
async def test_session_commit_timeout_or_cancel_propagates(
    failure: BaseException,
) -> None:
    from swe.app.runner import query_cleanup

    async def fail_save(*_args: Any, **_kwargs: Any) -> None:
        raise failure

    runtime = SimpleNamespace(
        tenant_hooks=SimpleNamespace(),
        agent_config=SimpleNamespace(),
        hook_overlay=SimpleNamespace(),
        agent=SimpleNamespace(),
        session_id="session-1",
        skip_history=False,
        user_id="user-1",
        session_execution=SimpleNamespace(),
    )
    owner = SimpleNamespace(save_job_session_state=fail_save)

    with pytest.raises(type(failure)):
        await query_cleanup.save_state_during_cleanup(
            owner,
            runtime=runtime,
            session_state_loaded=True,
            cleanup_timeout=1.0,
            hook_config_enabled=lambda *_args: False,
        )


@pytest.mark.asyncio
async def test_admission_command_and_denial_cleanup_run_in_one_session_transaction(
    monkeypatch,
) -> None:
    from swe.app.runner.query_execution import admission

    session = _RecordingSession()
    events: list[str] = []

    class Owner:
        def __init__(self, preflight: _QueryPreflight) -> None:
            self.session = session
            self._preflight = preflight

        async def _prepare_query_preflight(
            self,
            **_kwargs: Any,
        ) -> _QueryPreflight:
            return self._preflight

        async def _start_query_trace(self, *_args: Any) -> None:
            return None

        async def _end_trace_if_needed(self, *_args: Any) -> None:
            return None

        async def _cleanup_denied_session_memory(
            self,
            *_args: Any,
            **_kwargs: Any,
        ) -> None:
            assert session.in_execution
            events.append("denial cleanup")

    async def command_path(*_args: Any, **_kwargs: Any):
        assert session.in_execution
        events.append("command")
        yield Msg(name="Friday", role="assistant", content="done"), True

    monkeypatch.setattr(admission, "run_command_path", command_path)
    request = SimpleNamespace(session_id="session-1", user_id="user-1")
    async for _item in admission.stream_admission(
        Owner(_QueryPreflight()),
        [],
        request=request,
        query="/new",
        session_id="session-1",
        user_id="user-1",
    ):
        pass
    async for _item in admission.stream_admission(
        Owner(
            _QueryPreflight(
                response=Msg(
                    name="Friday",
                    role="assistant",
                    content="denied",
                ),
                cleanup_denied_memory=True,
            ),
        ),
        [],
        request=request,
        query="no",
        session_id="session-1",
        user_id="user-1",
    ):
        pass

    assert session.execution_entries == 2
    assert events == ["command", "denial cleanup"]


@pytest.mark.asyncio
async def test_admission_persists_user_anchor_before_preflight_and_agent_start(
    monkeypatch,
) -> None:
    from swe.app.runner.query_execution import admission

    session = _RecordingSession()
    events: list[str] = []

    class Owner:
        def __init__(self) -> None:
            self.session = session

        async def _prepare_query_preflight(self, **_kwargs: Any):
            events.append("preflight")
            assert session.active_execution is not None
            assert (
                session.active_execution.state["turn_states"]["msg-1"][
                    "status"
                ]
                == "admitted"
            )
            return _QueryPreflight(
                response=Msg(
                    name="Friday",
                    role="assistant",
                    content="blocked",
                ),
                cleanup_denied_memory=True,
            )

        async def _cleanup_denied_session_memory(self, **_kwargs: Any) -> None:
            events.append("cleanup")

    msg = Msg(name="user-1", role="user", content="hello")
    msg.id = "msg-1"
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel_meta={"msgid": "msg-1"},
    )
    async for _item in admission.stream_admission(
        Owner(),
        [msg],
        request=request,
        query="hello",
        session_id="session-1",
        user_id="user-1",
    ):
        pass

    assert events == ["preflight", "cleanup"]
    assert session.commit_count == 1
    assert (
        session.committed_states[0]["turn_states"]["msg-1"]["message"][
            "content"
        ]
        == "hello"
    )


@pytest.mark.asyncio
async def test_admission_does_not_persist_control_command_as_user_turn(
    monkeypatch,
) -> None:
    from swe.app.runner.query_execution import admission

    session = _RecordingSession()

    class Owner:
        def __init__(self) -> None:
            self.session = session

        async def _prepare_query_preflight(self, **_kwargs: Any):
            return _QueryPreflight()

        async def _start_query_trace(self, *_args: Any) -> None:
            return None

        async def _end_trace_if_needed(self, *_args: Any) -> None:
            return None

    async def command_path(*_args: Any, **_kwargs: Any):
        yield Msg(name="Friday", role="assistant", content="done"), True

    monkeypatch.setattr(admission, "run_command_path", command_path)
    command = Msg(name="user-1", role="user", content="/new")
    command.id = "command-msg-1"
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel_meta={"msgid": "command-msg-1"},
    )

    async for _item in admission.stream_admission(
        Owner(),
        [command],
        request=request,
        query="/new",
        session_id="session-1",
        user_id="user-1",
    ):
        pass

    assert session.commit_count == 0
    assert session.active_execution is None


def test_admitted_user_anchor_is_removed_from_loaded_agent_memory_before_reply() -> (
    None
):
    from swe.app.runner.session_lifecycle import discard_admitted_user_anchor

    anchor = Msg(name="user-1", role="user", content="hello")
    anchor.id = "msg-1"
    earlier = Msg(name="Friday", role="assistant", content="previous")
    memory = SimpleNamespace(content=[(earlier, []), (anchor, [])])
    agent = SimpleNamespace(memory=memory)

    assert discard_admitted_user_anchor(agent, "msg-1") is True
    assert memory.content == [(earlier, [])]


def test_mark_stopped_turn_state_marks_last_displayable_assistant_message() -> (
    None
):
    from swe.app.runner.session_lifecycle import mark_stopped_turn_state

    state: dict[str, Any] = {
        "agent": {
            "memory": {
                "content": [
                    [
                        {
                            "id": "user-1",
                            "role": "user",
                            "content": "hello",
                        },
                        [],
                    ],
                    [
                        {
                            "id": "assistant-1",
                            "role": "assistant",
                            "content": [{"type": "text", "text": "partial"}],
                        },
                        [],
                    ],
                ],
            },
        },
    }

    mark_stopped_turn_state(state, "user-1")

    assert state["turn_states"]["user-1"]["status"] == "stopped"
    assert (
        state["agent"]["memory"]["content"][1][0]["metadata"]["turn_status"]
        == "stopped"
    )


def test_mark_terminal_turn_state_records_public_terminal_outcome() -> None:
    from swe.app.runner.session_lifecycle import mark_terminal_turn_state

    state: dict[str, Any] = {
        "turn_states": {
            "turn-1": {"status": "admitted", "chat_id": "chat-1"},
        },
    }

    mark_terminal_turn_state(state, "turn-1", "completed")

    assert state["turn_states"]["turn-1"] == {
        "status": "completed",
        "chat_id": "chat-1",
    }


@pytest.mark.asyncio
async def test_runner_persists_completed_turn_outcome_after_execution() -> (
    None
):
    from swe.app.answer_turn.models import TurnIdentity, TurnOutcome

    class _Session:
        def __init__(self) -> None:
            self.state = {
                "turn_states": {
                    "msg-1": {"status": "admitted", "chat_id": "chat-1"},
                },
            }

        async def mutate_session_state(
            self,
            session_id: str,
            mutator,
            *,
            user_id: str,
        ) -> dict:
            assert (session_id, user_id) == ("session-1", "user-1")
            self.state = mutator(self.state)
            return self.state

    identity = TurnIdentity("chat-1", "msg-1", "turn-1")
    runner = object.__new__(AgentRunner)
    runner.session = _Session()
    runner._answer_turn_runtimes = {
        identity: (
            SimpleNamespace(session_id="session-1", user_id="user-1"),
            None,
        ),
    }

    await runner.persist_outcome(TurnOutcome.completed(identity))

    assert (
        runner.session.state["turn_states"]["msg-1"]["status"] == "completed"
    )


@pytest.mark.asyncio
async def test_runner_persists_stopped_outcome_through_active_execution() -> (
    None
):
    from swe.app.answer_turn.models import TurnIdentity, TurnOutcome

    class _Execution:
        is_active = True

        def __init__(self) -> None:
            self.state = {
                "turn_states": {
                    "msg-1": {"status": "stopping", "chat_id": "chat-1"},
                },
            }
            self.commit_calls = 0

        async def commit_state(self, state: dict[str, Any]) -> None:
            self.commit_calls += 1
            assert state is self.state

    class _Session:
        async def mutate_session_state(
            self,
            *_args: Any,
            **_kwargs: Any,
        ) -> None:
            raise AssertionError(
                "active execution must not use short-lock mutation",
            )

    identity = TurnIdentity("chat-1", "msg-1", "turn-1")
    execution = _Execution()
    runner = object.__new__(AgentRunner)
    runner.session = _Session()
    runner._answer_turn_runtimes = {
        identity: (
            SimpleNamespace(
                session_id="session-1",
                user_id="user-1",
                agent=SimpleNamespace(memory=SimpleNamespace(content=[])),
            ),
            execution,
        ),
    }

    await runner.persist_outcome(TurnOutcome.cancelled(identity))

    assert execution.commit_calls == 1
    assert execution.state["turn_states"]["msg-1"]["status"] == "stopped"


def test_mark_stopped_agent_memory_marks_last_assistant_message() -> None:
    from swe.app.runner.session_lifecycle import mark_stopped_agent_memory

    anchor = Msg(name="user-1", role="user", content="hello")
    anchor.id = "msg-1"
    assistant = Msg(name="Friday", role="assistant", content="partial")
    agent = SimpleNamespace(
        memory=SimpleNamespace(content=[(anchor, []), (assistant, [])]),
    )

    assert mark_stopped_agent_memory(agent, "msg-1") is True
    assert assistant.metadata["turn_status"] == "stopped"


@pytest.mark.asyncio
async def test_admission_releases_transaction_before_normal_query_resource_cleanup() -> (
    None
):
    from swe.app.runner.query_execution import admission

    class Owner(_RecordingOwner):
        async def _prepare_query_preflight(
            self,
            **_kwargs: Any,
        ) -> _QueryPreflight:
            return _QueryPreflight()

        async def _stream_query_after_preflight(
            self,
            msgs: list[Any],
            **kwargs: Any,
        ):
            async for (
                item
            ) in stream_query_after_preflight(  # pylint: disable=missing-kwoa
                self,
                msgs=msgs,
                **kwargs,
            ):
                yield item

    owner = Owner()
    request = SimpleNamespace(user_id="user-1", channel_meta={})

    outputs = [
        item
        async for item in admission.stream_admission(
            owner,
            [],
            request=request,
            query="hello",
            session_id="session-1",
            user_id="user-1",
        )
    ]

    assert len(outputs) == 1
    assert owner.session.commit_count == 1


@pytest.mark.asyncio
async def test_runtime_setup_failure_commits_pending_model_detail_without_agent() -> (
    None
):
    class Owner(_RecordingOwner):
        def _load_query_retry_settings(self, _config: Any = None):
            return 1, 0, 0.0, 0.0

        async def _stream_single_query_attempt(self, **_kwargs: Any):
            if _kwargs.get("emit_test_message"):
                yield None
            raise RuntimeError("runtime setup failed")

        def _should_retry(self, *_args: Any) -> bool:
            return False

        async def _raise_console_model_call_failed_if_needed(
            self,
            **kwargs: Any,
        ) -> None:
            transaction = kwargs["session_execution"]
            transaction.state["model_failure_detail"] = {"message": "failed"}
            transaction.mark_state_dirty()

        async def _handle_query_error(self, **_kwargs: Any) -> None:
            return None

        async def _save_state_during_cleanup(self, **kwargs: Any) -> None:
            assert kwargs["runtime"] is None
            assert kwargs["fallback_agent"] is None

    owner = Owner()
    request = SimpleNamespace(user_id="user-1")

    with pytest.raises(RuntimeError, match="runtime setup failed"):
        async for _item in stream_query_after_preflight(
            owner,
            msgs=[],
            request=request,
            query="hello",
            session_id="session-1",
            preflight=_QueryPreflight(),
        ):
            pass

    assert owner.session.commit_count == 1
    assert owner.session.committed_states == [
        {"model_failure_detail": {"message": "failed"}},
    ]
    assert owner.session.active_execution is None


@pytest.mark.asyncio
async def test_admission_without_session_preserves_legacy_command_call_shape(
    monkeypatch,
) -> None:
    """Hook-runtime command fakes do not accept a transaction keyword."""
    from swe.app.runner.query_execution import admission

    class Owner:
        session = None

        async def _prepare_query_preflight(
            self,
            *,
            session_id: str,
            user_id: str,
            query: str | None,
            request: Any,
        ) -> _QueryPreflight:
            assert (session_id, user_id, query, request) == (
                "session-1",
                "user-1",
                "/history",
                request,
            )
            return _QueryPreflight()

        async def _start_query_trace(self, *_args: Any) -> None:
            return None

        async def _end_trace_if_needed(self, *_args: Any) -> None:
            return None

    async def legacy_command_path(request: Any, msgs: list[Any], owner: Owner):
        assert request.session_id == "session-1"
        assert msgs == []
        assert isinstance(owner, Owner)
        yield Msg(name="Friday", role="assistant", content="done"), True

    monkeypatch.setattr(admission, "run_command_path", legacy_command_path)
    request = SimpleNamespace(session_id="session-1", user_id="user-1")
    outputs = [
        item
        async for item in admission.stream_admission(
            Owner(),
            [],
            request=request,
            query="/history",
            session_id="session-1",
            user_id="user-1",
        )
    ]

    assert outputs[-1][0].get_text_content() == "done"


@pytest.mark.asyncio
async def test_preflight_hook_overlay_reads_transaction_snapshot_without_short_getter(
    monkeypatch,
    tmp_path,
) -> None:
    from swe.agents.hook_runtime.models import HookConfig
    from swe.app.runner import query_preflight
    from swe.app.runner.runner import AgentRunner

    runner = AgentRunner(agent_id="test-agent", workspace_dir=tmp_path)
    transaction = SimpleNamespace(
        read_state=AsyncMock(
            return_value={"hook_overlay": {"once_executed": {"tx": True}}},
        ),
    )
    runner.session = SimpleNamespace(
        get_session_state_dict=AsyncMock(
            side_effect=AssertionError("short session getter must not run"),
        ),
    )
    captured: dict[str, Any] = {}

    async def prepare(owner, **kwargs):
        captured["owner"] = owner
        captured.update(kwargs)
        return _QueryPreflight(
            hook_overlay=await owner._load_query_preflight_overlay(
                session_id="session-1",
                user_id="user-1",
                session_execution=kwargs["session_execution"],
            ),
        )

    monkeypatch.setattr(query_preflight, "prepare_query_preflight", prepare)
    preflight = await runner._prepare_query_preflight(
        session_id="session-1",
        user_id="user-1",
        query="hello",
        request=SimpleNamespace(channel_meta={}),
        session_execution=transaction,
    )

    assert captured["owner"] is runner
    assert captured["session_execution"] is transaction
    assert preflight.hook_overlay.once_executed == {"tx": True}
    transaction.read_state.assert_awaited_once_with()
    runner.session.get_session_state_dict.assert_not_awaited()


@pytest.mark.asyncio
async def test_prompt_hook_snapshot_reads_transaction_state_without_short_getter() -> (
    None
):
    from swe.app.runner.runner import (
        _capture_persisted_runner_conversation_snapshot,
    )

    persisted_state = {
        "agent": {
            "memory": {
                "content": [
                    [
                        Msg(
                            name="user",
                            role="user",
                            content="previous question",
                        ).to_dict(),
                        [],
                    ],
                ],
            },
        },
    }
    transaction = SimpleNamespace(
        read_state=AsyncMock(return_value=persisted_state),
    )
    session = SimpleNamespace(get_session_state_dict=AsyncMock())
    runner = SimpleNamespace(session=session)
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        skip_history=False,
    )

    snapshot = await _capture_persisted_runner_conversation_snapshot(
        request=request,
        runner=runner,
        session_execution=transaction,
    )

    assert snapshot["messages"] == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "previous question"}],
        },
    ]
    transaction.read_state.assert_awaited_once_with()
    session.get_session_state_dict.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduled_retry_restores_in_memory_snapshot_without_loading_history() -> (
    None
):
    from swe.app.runner import session_lifecycle

    class Agent:
        def __init__(self) -> None:
            self.loaded_state: dict[str, Any] | None = None

        def load_state_dict(self, state: dict[str, Any]) -> None:
            self.loaded_state = state

    transaction = SimpleNamespace(
        read_state=AsyncMock(
            side_effect=AssertionError(
                "cron must not load transaction history",
            ),
        ),
    )
    owner = SimpleNamespace(
        session=SimpleNamespace(
            load_session_state=AsyncMock(
                side_effect=AssertionError("cron must not load disk history"),
            ),
        ),
    )
    retry_agent = Agent()
    retry_snapshot = {"memory_entries": ["retry notice"]}

    await session_lifecycle.get_state_loaded(
        owner,
        retry_agent,
        "session-1",
        session_state_loaded=False,
        skip_history=True,
        user_id="user-1",
        coerce_session_id=lambda value: value,
        coerce_user_id=lambda value: value,
        session_execution=transaction,
        retry_state_snapshot=retry_snapshot,
    )

    assert retry_agent.loaded_state == retry_snapshot
    transaction.read_state.assert_not_awaited()
    owner.session.load_session_state.assert_not_awaited()

    first_attempt_agent = Agent()
    await session_lifecycle.get_state_loaded(
        owner,
        first_attempt_agent,
        "session-1",
        session_state_loaded=False,
        skip_history=True,
        user_id="user-1",
        coerce_session_id=lambda value: value,
        coerce_user_id=lambda value: value,
        session_execution=transaction,
    )

    assert first_attempt_agent.loaded_state is None
    transaction.read_state.assert_not_awaited()
    owner.session.load_session_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_start_hook_snapshot_reads_transaction_state_without_short_getter(
    monkeypatch,
    tmp_path,
) -> None:
    from swe.agents.hook_runtime.models import (
        CommandHookHandlerConfig,
        HookConfig,
        HookEventName,
        HookMatcherGroupConfig,
        HookSessionOverlay,
    )
    from swe.app.runner.runner import _emit_runner_hook

    transaction = SimpleNamespace(
        read_state=AsyncMock(
            return_value={
                "agent": {
                    "memory": {
                        "content": [
                            [
                                Msg(
                                    name="user",
                                    role="user",
                                    content="previous question",
                                ).to_dict(),
                                [],
                            ],
                        ],
                    },
                },
            },
        ),
    )
    session = SimpleNamespace(
        get_session_state_dict=AsyncMock(
            side_effect=AssertionError("short session getter must not run"),
        ),
    )
    runner = SimpleNamespace(
        session=session,
        workspace_dir=tmp_path,
        tenant_id=None,
        agent_id="test-agent",
    )
    payloads: list[dict[str, Any]] = []

    async def fake_execute_handler_result(handler, context, *, workspace_dir):
        del workspace_dir
        payloads.append(context.to_handler_payload())
        from swe.agents.hook_runtime.models import HookHandlerResult

        return HookHandlerResult(handler_id=handler.id, order=0)

    monkeypatch.setattr(
        "swe.agents.hook_runtime.runtime.execute_handler",
        fake_execute_handler_result,
    )
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        channel_meta={},
        skip_history=False,
    )
    hooks = HookConfig(
        enabled=True,
        events={
            HookEventName.SESSION_START: [
                HookMatcherGroupConfig(
                    hooks=[
                        CommandHookHandlerConfig(
                            id="start-policy",
                            command="unused",
                            includeConversationSnapshot=True,
                        ),
                    ],
                ),
            ],
        },
    )

    await _emit_runner_hook(
        HookEventName.SESSION_START,
        request=request,
        runner=runner,
        tenant_hooks=hooks,
        agent_config=SimpleNamespace(hooks=HookConfig()),
        overlay=HookSessionOverlay(),
        session_execution=transaction,
    )

    assert payloads[0]["conversation_snapshot"] == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "previous question"}],
        },
    ]
    transaction.read_state.assert_awaited_once_with()
    session.get_session_state_dict.assert_not_awaited()

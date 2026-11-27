# -*- coding: utf-8 -*-
"""复核修复项的真实 MySQL 回归测试；只指向一次性 *_test 数据库。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from swe.database import DatabaseConfig, DatabaseConnection
from swe.observability.projector import InboxProjector
from swe.observability.store import ObservabilityStore

_DB_HOST = os.getenv("SWE_AGENTOBS_TEST_DB_HOST")
pytestmark = pytest.mark.skipif(
    not _DB_HOST,
    reason="set SWE_AGENTOBS_TEST_DB_HOST to a disposable MySQL *_test database",
)


def _test_db_config() -> DatabaseConfig:
    database = os.getenv("SWE_AGENTOBS_TEST_DB_NAME", "")
    if not database.endswith("_test"):
        raise RuntimeError(
            "Agent observability integration DB name must end in _test",
        )
    return DatabaseConfig(
        host=_DB_HOST or "",
        port=int(os.getenv("SWE_AGENTOBS_TEST_DB_PORT", "3306")),
        user=os.getenv("SWE_AGENTOBS_TEST_DB_USER", "root"),
        password=os.getenv("SWE_AGENTOBS_TEST_DB_PASSWORD", ""),
        database=database,
        min_connections=1,
        max_connections=3,
        ssl_disabled=True,
        time_zone="+00:00",
        acquire_timeout=5,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
        cleanup_timeout=1,
    )


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _event(
    *,
    run_id: str,
    event_name: str,
    phase: str,
    status: str,
    occurred_at: datetime,
    started_at: datetime,
    ended_at: datetime | None = None,
    source_id: str,
    tenant_id: str = "tenant-a",
    local_seq: int = 0,
    **extra,
) -> dict:
    event = {
        "schema_version": 1,
        "event_id": str(uuid4()),
        "event_name": event_name,
        "occurred_at": _iso(occurred_at),
        "producer_id": f"review-fix:{uuid4()}",
        "obs_run_id": run_id,
        "run_started_at": _iso(started_at),
        "capture_enabled": True,
        "entity_kind": "run",
        "entity_id": run_id,
        "phase_key": phase,
        "local_seq": local_seq,
        "scope": {
            "service_unit_id": "swe",
            "source_id": source_id,
            "tenant_id": tenant_id,
            "agent_id": "default",
        },
        "business_ref": {
            "chat_id": f"chat-{run_id}",
            "root_question_msgid": f"question-{run_id}",
            "session_id": f"session-{run_id}",
        },
        "started_at": _iso(started_at),
        "status": status,
        **extra,
    }
    if ended_at is not None:
        event["ended_at"] = _iso(ended_at)
        event["terminal_fact_source"] = "business_settlement"
        event["metrics"] = {"duration_us": 1234}
    return event


def _attempt_event(
    run_id: str,
    attempt_id: str,
    started_at: datetime,
    *,
    source_id: str,
    attempt_index: int = 1,
) -> dict:
    event = _event(
        run_id=run_id,
        event_name="attempt.started",
        phase="started",
        status="running",
        occurred_at=started_at,
        started_at=started_at,
        source_id=source_id,
    )
    event.update(
        {
            "entity_kind": "attempt",
            "entity_id": attempt_id,
            "obs_attempt_id": attempt_id,
            "attempt_index": attempt_index,
            "attempt_started_at": _iso(started_at),
        },
    )
    return event


def _span_event(
    run_id: str,
    attempt_id: str,
    span_id: str,
    *,
    event_name: str,
    span_type: str,
    operation: str,
    started_at: datetime,
    ended_at: datetime | None,
    source_id: str,
    **extra,
) -> dict:
    event = _event(
        run_id=run_id,
        event_name=event_name,
        phase="started" if ended_at is None else "ended",
        status="running" if ended_at is None else "succeeded",
        occurred_at=started_at if ended_at is None else ended_at,
        started_at=started_at,
        ended_at=ended_at,
        source_id=source_id,
    )
    event.update(
        {
            "entity_kind": "span",
            "entity_id": span_id,
            "obs_attempt_id": attempt_id,
            "obs_span_id": span_id,
            "span_type": span_type,
            "operation": operation,
            "kind": "INTERNAL",
            "name": "registered.tool" if span_type == "Tool" else "model.call",
            **extra,
        },
    )
    if ended_at is not None:
        event["metrics"] = {"duration_us": 100}
        event["quality"] = {
            "start_observed": True,
            "end_observed": True,
        }
    return event


class _Harness:
    def __init__(self, connection, store, projector):
        self.connection = connection
        self.store = store
        self.projector = projector

    async def insert(self, event: dict) -> int:
        async with self.connection.transaction() as session:
            return int(
                await session.insert(
                    "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                    (json.dumps(event, separators=(",", ":")),),
                ),
            )

    async def project(self) -> None:
        await self.projector.project_batch()

    async def inbox_state(self, ingest_id: int) -> dict:
        async with self.connection.acquire() as session:
            return await session.fetch_one(
                "SELECT processing_state, reason_code "
                "FROM swe_agentobs_inbox WHERE ingest_id=%s",
                (ingest_id,),
            )


@pytest.fixture
async def harness():
    connection = DatabaseConnection(_test_db_config())
    await connection.connect()
    try:
        store = ObservabilityStore(connection)
        assert (await store.readiness())["ready"]
        yield _Harness(
            connection,
            store,
            InboxProjector(store, poll_seconds=0.01),
        )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_poison_row_is_isolated_and_later_events_progress(harness):
    now = datetime.now(timezone.utc)
    started = now - timedelta(seconds=3)
    source = f"review-fix-{uuid4()}"
    run_a, run_b, run_c = (str(uuid4()) for _ in range(3))

    bad = _event(
        run_id=run_b,
        event_name="run.accepted",
        phase="accepted",
        status="accepted",
        occurred_at=started,
        started_at=started,
        source_id=source,
    )
    bad["started_at"] = "not-a-date"

    ids = [
        await harness.insert(
            _event(
                run_id=run_a,
                event_name="run.accepted",
                phase="accepted",
                status="accepted",
                occurred_at=started,
                started_at=started,
                source_id=source,
            ),
        ),
        await harness.insert(bad),
        await harness.insert(
            _event(
                run_id=run_b,
                event_name="run.accepted",
                phase="accepted",
                status="accepted",
                occurred_at=started,
                started_at=started,
                source_id=source,
            ),
        ),
    ]
    await harness.project()

    states = [await harness.inbox_state(item) for item in ids]
    assert states[0]["processing_state"] == 1
    assert states[1]["processing_state"] == 2
    assert states[1]["reason_code"] == "invalid_started_at"
    assert states[2]["processing_state"] == 1

    later = await harness.insert(
        _event(
            run_id=run_c,
            event_name="run.accepted",
            phase="accepted",
            status="accepted",
            occurred_at=started,
            started_at=started,
            source_id=source,
        ),
    )
    await harness.project()
    assert (await harness.inbox_state(later))["processing_state"] == 1


def _model_span(
    run_id,
    span_id,
    source,
    started,
    *,
    operation="request_attempt",
    parent=None,
    index=1,
    tokens=None,
    status="succeeded",
):
    event = _event(
        run_id=run_id,
        event_name="span.ended",
        phase="ended",
        status=status,
        occurred_at=started + timedelta(seconds=1),
        started_at=started,
        ended_at=started + timedelta(seconds=1),
        source_id=source,
        entity_kind="span",
        entity_id=span_id,
        obs_span_id=span_id,
        span_type="LLM",
        operation=operation,
        kind="CLIENT",
        name=(
            "model.request_attempt"
            if operation == "request_attempt"
            else "model.call"
        ),
        provider_key="openai",
        request_model_key="requested",
        response_model_key="actual",
        model_key_source="response",
        streaming=True,
        finish_reason="stop",
        quality={"start_observed": True, "end_observed": True},
    )
    event["metrics"].update(tokens or {})
    if operation == "request_attempt":
        event.update(parent_obs_span_id=parent, request_attempt_index=index)
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize("first_input", [10, None])
async def test_model_summary_deduplicates_independent_usage_and_repairs_late_start(
    harness,
    first_input,
):
    started = datetime.now(timezone.utc)
    source = f"request-fields-{uuid4()}"
    run, logical, first, second = (str(uuid4()) for _ in range(4))
    await harness.insert(
        _model_span(
            run,
            logical,
            source,
            started,
            operation="chat",
            tokens={"input_tokens": 999, "output_tokens": 999},
        ),
    )
    first_event = _model_span(
        run,
        first,
        source,
        started,
        parent=logical,
        status="failed",
        tokens={"input_tokens": first_input, "output_tokens": 0},
    )
    await harness.insert(first_event)
    await harness.insert({**first_event, "event_id": str(uuid4())})
    second_event = _model_span(
        run,
        second,
        source,
        started,
        parent=logical,
        index=2,
        status="cancelled",
        tokens={"input_tokens": 20, "first_chunk_us": 35},
    )
    await harness.insert(second_event)
    await harness.project()
    summary = await harness.store.get_run(
        service_unit_id="swe",
        run_id=run,
        source_ids=[source],
        tenant_id="tenant-a",
    )
    assert summary["llm_calls"] == 1
    assert summary["request_attempt_count"] == 2
    assert summary["input_tokens"] == 20 + (first_input or 0)
    assert summary["input_tokens_known_requests"] == (
        1 if first_input is None else 2
    )
    assert summary["output_tokens"] == 0
    assert summary["output_tokens_known_requests"] == 1
    spans = {
        span["span_id"]: span
        for span in await harness.store.list_spans(run_id=run)
    }
    assert spans[first]["total_tokens"] == first_input
    assert spans[second]["status"] == "cancelled"
    assert spans[second]["request_attempt_index"] == 2
    assert spans[second]["time_to_first_chunk_us"] == 35
    assert spans[second]["total_tokens"] is None
    assert spans[second]["quality"] == "partial"

    start = {
        key: value
        for key, value in second_event.items()
        if key
        not in {
            "ended_at",
            "metrics",
            "response_model_key",
            "finish_reason",
            "quality",
        }
    }
    start.update(
        event_id=str(uuid4()),
        event_name="span.started",
        phase_key="started",
        status="running",
        occurred_at=start["started_at"],
        model_key_source="request_fallback",
    )
    await harness.insert(start)
    await harness.project()
    spans = {
        span["span_id"]: span
        for span in await harness.store.list_spans(run_id=run)
    }
    assert spans[second]["quality"] == "observed"
    assert spans[second]["model_key_source"] == "response"
    assert spans[second]["request_attempt_index"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_zero", [False, True])
async def test_model_summary_historical_null_and_confirmed_zero(
    harness,
    explicit_zero,
):
    started = datetime.now(timezone.utc)
    source = f"request-coverage-{uuid4()}"
    run, logical = (str(uuid4()) for _ in range(2))
    event = _model_span(
        run,
        logical,
        source,
        started,
        operation="chat",
        tokens={"input_tokens": 999},
        status="cancelled",
    )
    if explicit_zero:
        event["no_request_attempts"] = True
    await harness.insert(event)
    await harness.project()
    summary = await harness.store.get_run(
        service_unit_id="swe",
        run_id=run,
        source_ids=[source],
    )
    assert summary["llm_calls"] == 1
    assert summary["request_attempt_count"] == (0 if explicit_zero else None)
    assert summary["input_tokens_known_requests"] == (
        0 if explicit_zero else None
    )
    assert summary["input_tokens"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("end_first", [False, True])
@pytest.mark.parametrize("mismatch", ["type", "operation", "ordinal"])
async def test_conflicting_model_span_lifecycle_shape_is_rejected(
    harness,
    end_first,
    mismatch,
):
    source = f"request-conflict-{uuid4()}"
    start_time = datetime.now(timezone.utc)
    run, span = str(uuid4()), str(uuid4())
    end = _model_span(run, span, source, start_time)
    start = {
        key: value
        for key, value in end.items()
        if key not in {"ended_at", "metrics", "quality"}
    }
    start.update(
        event_id=str(uuid4()),
        event_name="span.started",
        phase_key="started",
        status="running",
        occurred_at=end["started_at"],
    )
    if mismatch == "type":
        start["span_type"] = "Agent"
    elif mismatch == "operation":
        start.update(operation="chat", name="model.call")
    else:
        start["request_attempt_index"] = 2
    first, conflicting = (end, start) if end_first else (start, end)
    accepted_id = await harness.insert(first)
    conflict_id = await harness.insert(conflicting)
    await harness.project()
    assert (await harness.inbox_state(accepted_id))["processing_state"] == 1
    assert await harness.inbox_state(conflict_id) == {
        "processing_state": 3,
        "reason_code": "event_conflict",
    }
    (projected,) = await harness.store.list_spans(run_id=run)
    assert projected["quality"] == "partial"
    for field in ("span_type", "operation", "status"):
        assert projected[field] == first[field]
    if first["span_type"] == "LLM" and first["operation"] == "request_attempt":
        assert (
            projected["request_attempt_index"]
            == first["request_attempt_index"]
        )


@pytest.mark.asyncio
async def test_real_sql_row_rejection_rolls_back_failed_event_only(
    harness,
    monkeypatch,
):
    source = f"sql-row-error-{uuid4()}"
    started = datetime.now(timezone.utc)
    runs = [str(uuid4()) for _ in range(4)]
    events = [
        _model_span(run, str(uuid4()), source, started, operation="chat")
        for run in runs
    ]
    events[1]["request_model_key"] = {"tool_key": "bad-type"}
    failed_id = events[2]["event_id"]
    original = harness.projector._project_entity

    async def project_with_one_constraint_failure(session, event, metadata):
        if event["event_id"] == failed_id:
            await session.execute(
                "INSERT INTO swe_agentobs_inbox (processing_state) VALUES (42)",
            )
        await original(session, event, metadata)

    monkeypatch.setattr(
        harness.projector,
        "_project_entity",
        project_with_one_constraint_failure,
    )
    ids = [await harness.insert(event) for event in events]
    while await harness.projector.project_batch():
        pass
    states = [await harness.inbox_state(ingest_id) for ingest_id in ids]
    assert [state["processing_state"] for state in states] == [1, 2, 2, 1]
    assert states[1]["reason_code"] == "field_too_long"
    assert states[2]["reason_code"] == "projection_sql_error:3819"
    async with harness.connection.acquire() as session:
        row = await session.fetch_one(
            "SELECT event_id FROM swe_agentobs_events WHERE event_id=%s",
            (failed_id,),
        )
    assert row is None


@pytest.mark.asyncio
async def test_attempt_index_conflict_is_rejected(harness):
    now = datetime.now(timezone.utc)
    started = now - timedelta(seconds=3)
    source = f"review-fix-{uuid4()}"
    run_id = str(uuid4())
    first_id, second_id = str(uuid4()), str(uuid4())

    await harness.insert(
        _attempt_event(run_id, first_id, started, source_id=source),
    )
    await harness.project()
    second = await harness.insert(
        _attempt_event(run_id, second_id, started, source_id=source),
    )
    await harness.project()

    state = await harness.inbox_state(second)
    assert state["processing_state"] == 3
    async with harness.connection.acquire() as session:
        rows = await session.fetch_all(
            "SELECT attempt_id FROM swe_agentobs_attempts WHERE run_id=%s",
            (run_id,),
        )
    assert [row["attempt_id"] for row in rows] == [first_id]


@pytest.mark.asyncio
async def test_span_end_backfills_run_aggregates(harness):
    now = datetime.now(timezone.utc)
    started = now - timedelta(seconds=3)
    ended = now - timedelta(seconds=1)
    source = f"review-fix-{uuid4()}"
    run_id = str(uuid4())
    attempt_id = str(uuid4())
    tool_span, llm_span = str(uuid4()), str(uuid4())

    await harness.insert(
        _event(
            run_id=run_id,
            event_name="run.accepted",
            phase="accepted",
            status="accepted",
            occurred_at=started,
            started_at=started,
            source_id=source,
        ),
    )
    await harness.insert(
        _attempt_event(run_id, attempt_id, started, source_id=source),
    )
    for span_id, span_type, operation, extra in (
        (
            tool_span,
            "Tool",
            "execute",
            {
                "tool": {
                    "tool_key": "read_file",
                    "tool_call_id": "call-1",
                    "tool_execution_id": str(uuid4()),
                    "executor_kind": "agent_tool",
                },
            },
        ),
        (
            llm_span,
            "LLM",
            "chat",
            {
                "model": {
                    "provider_key": "provider-demo",
                    "request_model_key": "model-demo",
                },
            },
        ),
    ):
        await harness.insert(
            _span_event(
                run_id,
                attempt_id,
                span_id,
                event_name="span.started",
                span_type=span_type,
                operation=operation,
                started_at=started,
                ended_at=None,
                source_id=source,
                **extra,
            ),
        )
        await harness.insert(
            _span_event(
                run_id,
                attempt_id,
                span_id,
                event_name="span.ended",
                span_type=span_type,
                operation=operation,
                started_at=started,
                ended_at=ended,
                source_id=source,
                **extra,
            ),
        )
    await harness.project()

    async with harness.connection.acquire() as session:
        row = await session.fetch_one(
            "SELECT span_count, error_span_count, tool_calls, llm_calls "
            "FROM swe_agentobs_runs WHERE run_id=%s",
            (run_id,),
        )
    assert row["span_count"] == 2
    assert row["error_span_count"] == 0
    assert row["tool_calls"] == 1
    assert row["llm_calls"] == 1


@pytest.mark.asyncio
async def test_tenant_filter_isolates_runs(harness):
    now = datetime.now(timezone.utc)
    started = now - timedelta(seconds=3)
    source = f"review-fix-{uuid4()}"
    run_a, run_b = str(uuid4()), str(uuid4())

    for run_id, tenant in ((run_a, "tenant-a"), (run_b, "tenant-b")):
        await harness.insert(
            _event(
                run_id=run_id,
                event_name="run.accepted",
                phase="accepted",
                status="accepted",
                occurred_at=started,
                started_at=started,
                source_id=source,
                tenant_id=tenant,
            ),
        )
    await harness.project()

    rows = await harness.store.list_runs(
        service_unit_id="swe",
        source_ids=[source],
        tenant_id="tenant-a",
        limit=10,
        start_at=started - timedelta(seconds=1),
        end_at=now + timedelta(seconds=1),
    )
    assert [row["run_id"] for row in rows] == [run_a]

    assert (
        await harness.store.get_run(
            service_unit_id="swe",
            run_id=run_b,
            source_ids=[source],
            tenant_id="tenant-a",
        )
        is None
    )
    assert (
        await harness.store.get_run(
            service_unit_id="swe",
            run_id=run_b,
            source_ids=[source],
            tenant_id="tenant-b",
        )
        is not None
    )


@pytest.mark.asyncio
async def test_repeated_run_state_is_not_dropped(harness):
    now = datetime.now(timezone.utc)
    started = now - timedelta(seconds=5)
    source = f"review-fix-{uuid4()}"
    run_id = str(uuid4())

    await harness.insert(
        _event(
            run_id=run_id,
            event_name="run.accepted",
            phase="accepted",
            status="accepted",
            occurred_at=started,
            started_at=started,
            source_id=source,
        ),
    )
    for offset, phase, status in (
        (1, "state:running", "running"),
        (2, "state:waiting_approval", "waiting_approval"),
        (3, "state:running", "running"),
    ):
        await harness.insert(
            _event(
                run_id=run_id,
                event_name="run.state",
                phase=phase,
                status=status,
                occurred_at=started + timedelta(seconds=offset),
                started_at=started,
                source_id=source,
                local_seq=offset,
            ),
        )
    await harness.project()

    async with harness.connection.acquire() as session:
        row = await session.fetch_one(
            "SELECT status FROM swe_agentobs_runs WHERE run_id=%s",
            (run_id,),
        )
    assert row["status"] == "running"

    await harness.insert(
        _event(
            run_id=run_id,
            event_name="run.ended",
            phase="ended",
            status="succeeded",
            occurred_at=started + timedelta(seconds=6),
            started_at=started,
            ended_at=started + timedelta(seconds=6),
            source_id=source,
        ),
    )
    await harness.project()
    async with harness.connection.acquire() as session:
        row = await session.fetch_one(
            "SELECT status FROM swe_agentobs_runs WHERE run_id=%s",
            (run_id,),
        )
    assert row["status"] == "succeeded"

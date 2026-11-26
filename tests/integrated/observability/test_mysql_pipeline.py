# -*- coding: utf-8 -*-
"""Real MySQL contract test; point only at a disposable *_test database."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from io import StringIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
import httpx
from fastapi import FastAPI, Request

from swe.database import DatabaseConfig, DatabaseConnection
from swe.observability import Observability, Scope, bind_context
from swe.observability.emitter import JsonlEmitter
from swe.observability.projector import InboxProjector
from swe.observability.query import router as observability_router
from swe.observability import runner_lifecycle
from swe.observability.models import ObservabilityContext
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


def _event(
    *,
    run_id: str,
    event_name: str,
    phase: str,
    status: str,
    occurred_at: datetime,
    started_at: datetime,
    ended_at: datetime | None,
    source_id: str = "agentobs-integration",
) -> dict:
    occurred = occurred_at.isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )
    started = started_at.isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )
    return {
        "schema_version": 1,
        "event_id": str(uuid4()),
        "event_name": event_name,
        "occurred_at": occurred,
        "producer_id": f"mysql-test:{uuid4()}",
        "obs_run_id": run_id,
        "run_started_at": started,
        "capture_enabled": True,
        "entity_kind": "run",
        "entity_id": run_id,
        "phase_key": phase,
        "local_seq": 0,
        "scope": {
            "service_unit_id": "swe",
            "source_id": source_id,
            "tenant_id": "integration-user",
            "agent_id": "default",
        },
        "business_ref": {
            "chat_id": f"chat-{run_id}",
            "root_question_msgid": f"question-{run_id}",
            "session_id": f"session-{run_id}",
        },
        "started_at": started,
        **(
            {
                "ended_at": ended_at.isoformat(
                    timespec="microseconds",
                ).replace(
                    "+00:00",
                    "Z",
                ),
                "status": status,
                "terminal_fact_source": "business_settlement",
                "metrics": {"duration_us": 1234},
            }
            if ended_at
            else {"status": status}
        ),
    }


async def _verify_partial_run_ordering(store, ingest, started, ended):
    run_id = str(uuid4())
    running_at = started + timedelta(milliseconds=500)
    running_ingest_id = await ingest(
        _event(
            run_id=run_id,
            event_name="run.state",
            phase="running",
            status="running",
            occurred_at=running_at,
            started_at=started,
            ended_at=None,
        ),
    )
    late_accepted_ingest_id = await ingest(
        _event(
            run_id=run_id,
            event_name="run.accepted",
            phase="accepted",
            status="accepted",
            occurred_at=started - timedelta(seconds=1),
            started_at=started,
            ended_at=None,
        ),
    )
    rows = await store.list_runs(
        service_unit_id="swe",
        source_ids=["agentobs-integration"],
        limit=10,
        start_at=started - timedelta(seconds=1),
        end_at=ended + timedelta(seconds=1),
    )
    projected = next(row for row in rows if row["run_id"] == run_id)
    assert projected["status"] == "running"
    assert projected["quality"] == "partial"

    waiting_at = started + timedelta(seconds=1)
    for phase, status, occurred_at in (
        ("waiting-approval", "waiting_approval", waiting_at),
        ("running-late", "running", running_at),
        ("running-same-time", "running", waiting_at),
    ):
        await ingest(
            _event(
                run_id=run_id,
                event_name="run.state",
                phase=phase,
                status=status,
                occurred_at=occurred_at,
                started_at=started,
                ended_at=None,
            ),
        )
    rows = await store.list_runs(
        service_unit_id="swe",
        source_ids=["agentobs-integration"],
        limit=10,
        start_at=started - timedelta(seconds=1),
        end_at=ended + timedelta(seconds=1),
    )
    projected = next(row for row in rows if row["run_id"] == run_id)
    assert projected["status"] == "waiting_approval"
    assert projected["quality"] == "partial"

    await ingest(
        _event(
            run_id=run_id,
            event_name="run.ended",
            phase="ended",
            status="succeeded",
            occurred_at=ended,
            started_at=started,
            ended_at=ended,
        ),
    )
    rows = await store.list_runs(
        service_unit_id="swe",
        source_ids=["agentobs-integration"],
        limit=10,
        start_at=started - timedelta(seconds=1),
        end_at=ended + timedelta(seconds=1),
    )
    projected = next(row for row in rows if row["run_id"] == run_id)
    assert projected["status"] == "succeeded"
    assert projected["quality"] == "observed"
    return running_ingest_id, late_accepted_ingest_id


async def _ingest_tool_span(connection, ingest, run_id, started, ended):
    span_id = str(uuid4())
    tool_metadata = {
        "tool_key": "read_file",
        "tool_call_id": "call-integration-1",
        "tool_execution_id": str(uuid4()),
        "executor_kind": "agent_tool",
        "timeout_budget_ms": 2500,
    }
    common = {
        "entity_kind": "span",
        "entity_id": span_id,
        "obs_span_id": span_id,
        "span_type": "Tool",
        "operation": "execute",
        "kind": "INTERNAL",
        "name": "registered.tool",
        "tool": tool_metadata,
    }
    started_event = _event(
        run_id=run_id,
        event_name="span.started",
        phase="started",
        status="running",
        occurred_at=started,
        started_at=started,
        ended_at=None,
    )
    started_event.update(common)
    started_ingest_id = await ingest(started_event)
    ended_event = _event(
        run_id=run_id,
        event_name="span.ended",
        phase="ended",
        status="failed",
        occurred_at=ended,
        started_at=started,
        ended_at=ended,
    )
    ended_event.update(
        {
            **common,
            "quality": {"start_observed": True, "end_observed": True},
            "technical_status": "OK",
            "has_error": False,
            "error_type": "permission_denied",
            "phase": "tool_execution",
        },
    )
    ended_ingest_id = await ingest(ended_event)
    async with connection.acquire() as session:
        rows = await session.fetch_all(
            "SELECT ingest_id,event_id,processing_state,reason_code "
            "FROM swe_agentobs_inbox WHERE ingest_id IN (%s,%s)",
            (started_ingest_id, ended_ingest_id),
        )
    assert len(rows) == 2
    assert all(row["processing_state"] == 1 for row in rows), rows
    return span_id, tool_metadata


async def _ingest_llm_span(connection, ingest, run_id, started, ended):
    span_id = str(uuid4())
    common = {
        "entity_kind": "span",
        "entity_id": span_id,
        "obs_span_id": span_id,
        "span_type": "LLM",
        "operation": "chat",
        "kind": "CLIENT",
        "name": "model.call",
        "provider_key": "provider",
        "request_model_key": "model",
        "streaming": False,
    }
    started_event = _event(
        run_id=run_id,
        event_name="span.started",
        phase="started",
        status="running",
        occurred_at=started,
        started_at=started,
        ended_at=None,
    )
    started_event.update(common)
    await ingest(started_event)
    ended_event = _event(
        run_id=run_id,
        event_name="span.ended",
        phase="ended",
        status="succeeded",
        occurred_at=ended,
        started_at=started,
        ended_at=ended,
    )
    ended_event.update(
        {
            **common,
            "quality": {"start_observed": True, "end_observed": True},
            "metrics": {"input_tokens": 1, "output_tokens": 1},
            "technical_status": "OK",
            "has_error": False,
        },
    )
    await ingest(ended_event)
    return span_id


async def _ingest_subagent_span(
    connection,
    ingest,
    run_id,
    parent_span_id,
    started,
    ended,
):
    span_id = str(uuid4())
    subagent = {
        "mode": "background",
        "execution_agent_key": "plan-researcher",
    }
    common = {
        "entity_kind": "span",
        "entity_id": span_id,
        "obs_span_id": span_id,
        "parent_obs_span_id": parent_span_id,
        "span_type": "SubAgent",
        "operation": "agent.execute",
        "kind": "SERVER",
        "name": "agent.execute",
        "subagent": subagent,
    }
    started_event = _event(
        run_id=run_id,
        event_name="span.started",
        phase="started",
        status="running",
        occurred_at=started,
        started_at=started,
        ended_at=None,
    )
    started_event.update(common)
    started_ingest_id = await ingest(started_event)
    ended_event = _event(
        run_id=run_id,
        event_name="span.ended",
        phase="ended",
        status="partial",
        occurred_at=ended,
        started_at=started,
        ended_at=ended,
    )
    ended_event.update(
        {
            **common,
            "subagent": {
                **subagent,
                "business_subagent_key": "subagent-integration-1",
                "result_status": "partial",
            },
            "quality": {"start_observed": True, "end_observed": True},
        },
    )
    ended_ingest_id = await ingest(ended_event)
    async with connection.acquire() as session:
        rows = await session.fetch_all(
            "SELECT processing_state FROM swe_agentobs_inbox "
            "WHERE ingest_id IN (%s,%s)",
            (started_ingest_id, ended_ingest_id),
        )
    assert len(rows) == 2
    assert all(row["processing_state"] == 1 for row in rows), rows
    return span_id, parent_span_id


async def _ingest_guard_span(connection, ingest, run_id, started, ended):
    span_id = str(uuid4())
    tool_metadata = {
        "tool_key": "execute_shell_command",
        "tool_call_id": "guard-call-1",
        "executor_kind": "agent_tool",
    }
    common = {
        "entity_kind": "span",
        "entity_id": span_id,
        "obs_span_id": span_id,
        "span_type": "Guard",
        "operation": "tool",
        "kind": "INTERNAL",
        "name": "registered.tool",
        "tool": tool_metadata,
    }
    started_event = _event(
        run_id=run_id,
        event_name="span.started",
        phase="started",
        status="running",
        occurred_at=started,
        started_at=started,
        ended_at=None,
    )
    started_event.update(common)
    await ingest(started_event)
    ended_event = _event(
        run_id=run_id,
        event_name="span.ended",
        phase="ended",
        status="rejected",
        occurred_at=ended,
        started_at=started,
        ended_at=ended,
    )
    ended_event.update(
        {
            **common,
            "quality": {"start_observed": True, "end_observed": True},
            "technical_status": "OK",
            "has_error": False,
            "phase": "tool_guard",
        },
    )
    ingest_id = await ingest(ended_event)
    async with connection.acquire() as session:
        row = await session.fetch_one(
            "SELECT processing_state FROM swe_agentobs_inbox "
            "WHERE ingest_id=%s",
            (ingest_id,),
        )
    assert row and row["processing_state"] == 1
    return span_id


async def _read_duration_pages(client, headers, started, ended):
    rows = []
    cursor = None
    start = (started - timedelta(seconds=1)).isoformat()
    end = (ended + timedelta(seconds=1)).isoformat()
    for page_index in range(100):
        params = {
            "sort_by": "duration_us",
            "limit": 2,
            "start": start,
            "end": end,
        }
        if cursor:
            params["cursor"] = cursor
        page = await client.get(
            "/agent-observability/runs",
            headers=headers,
            params=params,
        )
        assert page.status_code == 200, page.text
        body = page.json()
        rows.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return rows
        if page_index == 0:
            changed_filter = await client.get(
                "/agent-observability/runs",
                headers=headers,
                params={**params, "cursor": cursor, "status": "failed"},
            )
            assert changed_filter.status_code == 400
    raise AssertionError("duration cursor did not terminate")


async def _read_admin_multi_source_pages(client, headers, started, ended):
    rows = []
    cursor = None
    for _ in range(100):
        params = [
            ("source_id", "agentobs-integration"),
            ("source_id", "agentobs-other-source"),
            ("limit", "2"),
            ("start", (started - timedelta(seconds=1)).isoformat()),
            ("end", (ended + timedelta(seconds=1)).isoformat()),
        ]
        if cursor:
            params.append(("cursor", cursor))
        response = await client.get(
            "/agent-observability/runs",
            headers={**headers, "X-User-Role": "admin"},
            params=params,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        rows.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return rows
    raise AssertionError("admin multi-Source cursor did not terminate")


async def _ingest_terminal_runs(ingest, run_ids, source_id, started, ended):
    for run_id in run_ids:
        await ingest(
            _event(
                run_id=run_id,
                event_name="run.ended",
                phase="ended",
                status="succeeded",
                occurred_at=ended,
                started_at=started,
                ended_at=ended,
                source_id=source_id,
            ),
        )


async def _seed_multi_source_runs(ingest, started, ended):
    tied_ids = [str(uuid4()) for _ in range(3)]
    other_source_ids = [str(uuid4()) for _ in range(3)]
    await _ingest_terminal_runs(
        ingest,
        tied_ids,
        "agentobs-integration",
        started,
        ended,
    )
    await _ingest_terminal_runs(
        ingest,
        other_source_ids,
        "agentobs-other-source",
        started,
        ended,
    )
    return tied_ids, other_source_ids


async def _ingest_malformed_terminal(ingest, started, ended):
    event = _event(
        run_id=str(uuid4()),
        event_name="run.ended",
        phase="ended",
        status="failed",
        occurred_at=ended,
        started_at=started,
        ended_at=ended,
    )
    event.pop("status")
    return await ingest(event)


async def _assert_approval_run_api(store, run_id, approval_ids):
    api = FastAPI()
    api.state.agent_observability_store = store

    @api.middleware("http")
    async def bind_source(request, call_next):
        request.state.source_id = "agentobs-integration"
        request.state.effective_tenant_id = "user-a"
        return await call_next(request)

    api.include_router(observability_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://agentobs.test",
    ) as client:
        detail = await client.get(
            f"/agent-observability/runs/{run_id}",
            headers={"X-User-Role": "manager"},
        )
        events = await client.get(
            f"/agent-observability/runs/{run_id}/events",
            headers={"X-User-Role": "manager"},
        )
        performance = await client.get(
            "/agent-observability/performance",
            headers={"X-User-Role": "manager"},
        )
    assert (
        detail.status_code
        == events.status_code
        == performance.status_code
        == 200
    )
    performance_body = performance.json()
    assert performance_body["population_source"] == "observed_ended_spans"
    assert "active_duration_us" in performance_body
    assert "agent_loops" in performance_body
    body = detail.json()
    assert body["run"]["status"] == "succeeded"
    attempts = body["attempts"]
    assert [attempt["status"] for attempt in attempts] == [
        *["waiting_approval"] * len(approval_ids),
        "succeeded",
    ]
    for index, attempt in enumerate(attempts[1:], start=1):
        assert (
            attempt["predecessor_attempt_id"]
            == attempts[index - 1]["attempt_id"]
        )
    event_rows = events.json()["items"]
    run_terminal_events = [
        item for item in event_rows if item["event_name"] == "run.ended"
    ]
    assert len(run_terminal_events) == 1
    for index, approval_id in enumerate(approval_ids):
        approval_events = [
            item for item in event_rows if item["entity_id"] == approval_id
        ]
        requested = next(
            item
            for item in approval_events
            if item["event_name"] == "approval.requested"
        )
        resolved = next(
            item
            for item in approval_events
            if item["event_name"] == "approval.resolved"
        )
        assert requested["attempt_id"] == attempts[index]["attempt_id"]
        assert resolved["attempt_id"] == attempts[index + 1]["attempt_id"]
        assert requested["occurred_at"] == resolved["metadata"]["started_at"]


async def _assert_query_candidate_budget(api, client, headers):
    store = api.state.agent_observability_store
    original_count = store.count_run_candidates

    async def over_budget(**_kwargs):
        return 50_001

    store.count_run_candidates = over_budget
    try:
        response = await client.get(
            "/agent-observability/runs",
            headers=headers,
        )
    finally:
        store.count_run_candidates = original_count
    assert response.status_code == 422
    assert response.json()["detail"] == "range_too_large"


async def _assert_authorized_run_list(
    api,
    run_id,
    started,
    ended,
    tied_duration_run_ids,
    other_source_run_ids,
):
    headers = {
        "X-User-Role": "manager",
        "X-Source-Id": "agentobs-integration",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://agentobs.test",
    ) as client:
        response = await client.get(
            "/agent-observability/runs",
            headers=headers,
            params={
                "start": (started - timedelta(seconds=1)).isoformat(),
                "end": (ended + timedelta(seconds=1)).isoformat(),
            },
        )
        exact = await client.get(
            "/agent-observability/runs",
            headers=headers,
            params={
                "run_id": run_id,
                "agent_id": "default",
                "status": "succeeded",
                "min_duration_ms": 1,
            },
        )
        exact_session = await client.get(
            "/agent-observability/runs",
            headers=headers,
            params={"session_id": f"session-{run_id}"},
        )
        admin_multi_source = await client.get(
            "/agent-observability/runs",
            headers={**headers, "X-User-Role": "admin"},
            params=[
                ("source_id", "agentobs-integration"),
                ("source_id", "agentobs-other-source"),
            ],
        )
        multi_source_pages = await _read_admin_multi_source_pages(
            client,
            headers,
            started,
            ended,
        )
        denied_source = await client.get(
            "/agent-observability/runs",
            headers=headers,
            params={"source_id": "agentobs-other-source"},
        )
        duration_sorted = await client.get(
            "/agent-observability/runs",
            headers=headers,
            params={
                "sort_by": "duration_us",
                "start": (started - timedelta(seconds=1)).isoformat(),
                "end": (ended + timedelta(seconds=1)).isoformat(),
            },
        )
        duration_pages = await _read_duration_pages(
            client,
            headers,
            started,
            ended,
        )
        await _assert_query_candidate_budget(api, client, headers)
    assert response.status_code == 200
    run = next(
        item for item in response.json()["items"] if item["run_id"] == run_id
    )
    assert run["status"] == "succeeded"
    assert exact.status_code == 200
    assert [item["run_id"] for item in exact.json()["items"]] == [run_id]
    assert exact_session.status_code == 200
    assert [item["run_id"] for item in exact_session.json()["items"]] == [
        run_id,
    ]
    assert admin_multi_source.status_code == 200
    assert {
        row["source_key"] for row in admin_multi_source.json()["items"]
    } == {"agentobs-integration", "agentobs-other-source"}
    assert denied_source.status_code == 403
    multi_source_ids = [row["run_id"] for row in multi_source_pages]
    expected_multi_source_ids = [
        run_id,
        *tied_duration_run_ids,
        *other_source_run_ids,
    ]
    assert set(expected_multi_source_ids).issubset(multi_source_ids)
    assert len(multi_source_ids) == len(set(multi_source_ids))
    assert multi_source_ids == [
        row["run_id"]
        for row in sorted(
            multi_source_pages,
            key=lambda row: (row["started_at"], row["run_id"]),
            reverse=True,
        )
    ]
    assert {row["source_key"] for row in multi_source_pages} == {
        "agentobs-integration",
        "agentobs-other-source",
    }
    assert duration_sorted.status_code == 200
    duration_rows = duration_sorted.json()["items"]
    assert any(row["run_id"] == run_id for row in duration_rows)
    assert all(
        row["ended_at"] is not None and row["duration_us"] is not None
        for row in duration_rows
    )
    assert [row["duration_us"] for row in duration_rows] == sorted(
        (row["duration_us"] for row in duration_rows),
        reverse=True,
    )
    paged_ids = [row["run_id"] for row in duration_pages]
    assert len(paged_ids) == len(set(paged_ids))
    assert [row["duration_us"] for row in duration_pages] == sorted(
        (row["duration_us"] for row in duration_pages),
        reverse=True,
    )
    tied_rows = [
        row for row in duration_pages if row["run_id"] in tied_duration_run_ids
    ]
    assert {row["run_id"] for row in tied_rows} == set(tied_duration_run_ids)
    assert [row["run_id"] for row in tied_rows] == sorted(
        tied_duration_run_ids,
        reverse=True,
    )


async def _assert_authorized_tool_detail(api, run_id, span_id, tool_metadata):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://agentobs.test",
    ) as client:
        response = await client.get(
            f"/agent-observability/runs/{run_id}",
            headers={
                "X-User-Role": "manager",
                "X-Source-Id": "agentobs-integration",
            },
        )
        search_response = await client.get(
            f"/agent-observability/runs/{run_id}/spans/search",
            headers={
                "X-User-Role": "manager",
                "X-Source-Id": "agentobs-integration",
            },
            params={"q": "read_file", "limit": 1},
        )
        error_response = await client.get(
            f"/agent-observability/runs/{run_id}/spans/search",
            headers={
                "X-User-Role": "manager",
                "X-Source-Id": "agentobs-integration",
            },
            params={"q": "read_file", "errors_only": "true", "limit": 1},
        )
        cursor_page = await client.get(
            f"/agent-observability/runs/{run_id}/spans/search",
            headers={
                "X-User-Role": "manager",
                "X-Source-Id": "agentobs-integration",
            },
            params={"q": "execute", "limit": 1},
        )
        changed_search_cursor = await client.get(
            f"/agent-observability/runs/{run_id}/spans/search",
            headers={
                "X-User-Role": "manager",
                "X-Source-Id": "agentobs-integration",
            },
            params={
                "q": "read_file",
                "limit": 1,
                "cursor": cursor_page.json()["next_cursor"],
            },
        )
    assert response.status_code == 200
    span = next(
        item for item in response.json()["spans"] if item["span_id"] == span_id
    )
    assert span["tool"] == tool_metadata
    assert span["status"] == "failed"
    assert span["technical_status"] == "OK"
    assert span["has_error"] == 0
    assert "metadata_text" not in span
    assert search_response.status_code == 200
    assert [item["span_id"] for item in search_response.json()["items"]] == [
        span_id,
    ]
    assert (
        search_response.json()["items"][0]["tool"]["tool_key"] == "read_file"
    )
    assert error_response.status_code == 200
    assert [item["span_id"] for item in error_response.json()["items"]] == [
        span_id,
    ]
    assert cursor_page.status_code == 200
    assert cursor_page.json()["next_cursor"]
    assert changed_search_cursor.status_code == 400


async def _assert_authorized_subagent_detail(
    api,
    run_id,
    span_id,
    parent_span_id,
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://agentobs.test",
    ) as client:
        response = await client.get(
            f"/agent-observability/runs/{run_id}",
            headers={
                "X-User-Role": "manager",
                "X-Source-Id": "agentobs-integration",
            },
        )
        cross_source = await client.get(
            f"/agent-observability/runs/{run_id}",
            headers={
                "X-User-Role": "manager",
                "X-Source-Id": "agentobs-other-source",
            },
        )
        search = await client.get(
            f"/agent-observability/runs/{run_id}/spans/search",
            params={"q": "agent.execute", "limit": 100},
            headers={
                "X-User-Role": "manager",
                "X-Source-Id": "agentobs-integration",
            },
        )
        ancestors = await client.get(
            f"/agent-observability/runs/{run_id}/spans/{span_id}/ancestors",
            params={"limit": 64},
            headers={
                "X-User-Role": "manager",
                "X-Source-Id": "agentobs-integration",
            },
        )
    assert response.status_code == 200
    assert cross_source.status_code == 404
    assert search.status_code == 200
    assert span_id in {item["span_id"] for item in search.json()["items"]}
    assert ancestors.status_code == 200
    ancestor_items = ancestors.json()["items"]
    assert [item["span_id"] for item in ancestor_items[:2]] == [
        span_id,
        parent_span_id,
    ]
    body = response.json()
    assert body["run"]["status"] == "succeeded"
    span = next(item for item in body["spans"] if item["span_id"] == span_id)
    assert span["span_type"] == "SubAgent"
    assert span["status"] == "partial"
    assert (
        span["subagent"]["business_subagent_key"] == "subagent-integration-1"
    )
    assert span["subagent"]["execution_agent_key"] == "plan-researcher"
    assert span["subagent"]["result_status"] == "partial"
    assert span["parent_span_id"] == parent_span_id


async def _assert_authorized_guard_detail(api, run_id, span_id):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://agentobs.test",
    ) as client:
        response = await client.get(
            f"/agent-observability/runs/{run_id}",
            headers={
                "X-User-Role": "manager",
                "X-Source-Id": "agentobs-integration",
            },
        )
    assert response.status_code == 200
    span = next(
        item for item in response.json()["spans"] if item["span_id"] == span_id
    )
    assert span["span_type"] == "Guard"
    assert span["status"] == "rejected"
    assert span["tool"] == {
        "tool_key": "execute_shell_command",
        "tool_call_id": "guard-call-1",
        "executor_kind": "agent_tool",
    }


async def _ingest_partial_span(store, ingest, started):
    run_id = str(uuid4())
    span_id = str(uuid4())
    event = _event(
        run_id=run_id,
        event_name="span.started",
        phase="started",
        status="running",
        occurred_at=started,
        started_at=started,
        ended_at=None,
    )
    event.update(
        {
            "entity_kind": "span",
            "entity_id": span_id,
            "obs_span_id": span_id,
            "span_type": "Agent",
            "operation": "execute",
            "kind": "SERVER",
            "name": "agent.execute",
        },
    )
    await ingest(event)
    rows = await store.list_spans(run_id=run_id)
    assert len(rows) == 1
    assert rows[0]["quality"] == "partial"
    assert rows[0]["ended_at"] is None


async def _assert_authorized_asset_detail(api, run_id, span_id):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://agentobs.test",
    ) as client:
        first = await client.get(
            f"/agent-observability/runs/{run_id}/assets",
            params={"span_id": span_id},
            headers={
                "X-User-Role": "manager",
                "X-Source-Id": "agentobs-integration",
            },
        )
        assert first.status_code == 200
        first_page = first.json()
        assert len(first_page["items"]) == 32
        assert first_page["quality"] == "partial"
        assert first_page["next_cursor"]
        second = await client.get(
            f"/agent-observability/runs/{run_id}/assets",
            params={
                "span_id": span_id,
                "cursor": first_page["next_cursor"],
            },
            headers={
                "X-User-Role": "manager",
                "X-Source-Id": "agentobs-integration",
            },
        )
    assert second.status_code == 200
    second_page = second.json()
    assert len(second_page["items"]) == 1
    assert second_page["quality"] == "complete"
    assert second_page["next_cursor"] is None
    items = first_page["items"] + second_page["items"]
    assert len({item["asset_ref"] for item in items}) == 33
    assert all(
        item
        == {
            "asset_ref": item["asset_ref"],
            "span_id": span_id,
            "asset_type": "tool",
            "asset_id": "source_echo",
            "asset_version": "7",
            "asset_digest": "sha256:integration-digest",
            "asset_source": "source_catalog_snapshot",
        }
        for item in items
    )


async def _ingest_tool_asset(ingest, run_id, span_id, started, ended):
    for _ in range(33):
        event = _event(
            run_id=run_id,
            event_name="asset.used",
            phase="used",
            status="accepted",
            occurred_at=ended,
            started_at=started,
            ended_at=ended,
        )
        event.update(
            {
                "entity_kind": "asset",
                "entity_id": str(uuid4()),
                "parent_obs_span_id": span_id,
                "asset": {
                    "asset_type": "tool",
                    "asset_id": "source_echo",
                    "asset_version": "7",
                    "asset_digest": "sha256:integration-digest",
                    "asset_source": "source_catalog_snapshot",
                },
            },
        )
        await ingest(event)


async def _ingest_user_question_evidence(
    ingest,
    run_id,
    span_id,
    started,
    ended,
):
    event = _event(
        run_id=run_id,
        event_name="span.event",
        phase="event",
        status="succeeded",
        occurred_at=ended,
        started_at=started,
        ended_at=ended,
    )
    event.update(
        {
            "entity_kind": "span",
            "entity_id": span_id,
            "phase_key": "event",
            "local_seq": 1,
            "obs_span_id": span_id,
            "span_type": "LLM",
            "operation": "chat",
            "name": "model.call",
            "event_type": "evidence.linked",
            "chat_id": f"chat-{run_id}",
            "message_id": f"question-{run_id}",
            "content_type": "user_question",
            "saved_state": "available",
            "private_prompt": "must never escape the locator projection",
        },
    )
    await ingest(event)


async def _assert_projected_user_question_evidence(store, run_id, span_id):
    refs = await store.list_span_evidence_refs(
        run_id=run_id,
        span_id=span_id,
        source_ids=["agentobs-integration"],
    )
    assert refs == [
        {
            "chat_id": f"chat-{run_id}",
            "message_id": f"question-{run_id}",
            "content_type": "user_question",
            "saved_state": "available",
            "evidence_ref": refs[0]["evidence_ref"],
        },
    ]
    assert "private_prompt" not in json.dumps(refs)


async def _ingest_tool_asset_and_question_evidence(
    ingest,
    run_id,
    tool_span_id,
    llm_span_id,
    started,
    ended,
):
    await _ingest_tool_asset(ingest, run_id, tool_span_id, started, ended)
    await _ingest_user_question_evidence(
        ingest,
        run_id,
        llm_span_id,
        started,
        ended,
    )


async def _assert_run_projection(store, run_id, started, ended):
    rows = await store.list_runs(
        service_unit_id="swe",
        source_ids=["agentobs-integration"],
        limit=10,
        start_at=started - timedelta(seconds=1),
        end_at=ended + timedelta(seconds=1),
    )
    projected = next(row for row in rows if row["run_id"] == run_id)
    assert projected["status"] == "succeeded"
    assert projected["ended_at"] is not None


@pytest.mark.asyncio
async def test_manual_inbox_to_mysql_projection_and_authorized_run_list(
    monkeypatch,
):
    monkeypatch.setenv(
        "SWE_AGENTOBS_CURSOR_SECRET",
        "integration-cursor-secret-at-least-32",
    )
    connection = DatabaseConnection(_test_db_config())
    await connection.connect()
    try:
        store = ObservabilityStore(connection)
        assert (await store.readiness())["ready"]

        projector = InboxProjector(store, poll_seconds=0.01)
        now = datetime.now(timezone.utc)
        started = now - timedelta(seconds=3)
        ended = now - timedelta(seconds=1)
        run_id = str(uuid4())

        async def ingest(event: dict) -> int:
            async with connection.transaction() as session:
                ingest_id = await session.insert(
                    "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                    (json.dumps(event, separators=(",", ":")),),
                )
            for _ in range(10):
                await projector.project_batch()
                async with connection.acquire() as session:
                    state = await session.fetch_one(
                        "SELECT processing_state FROM swe_agentobs_inbox "
                        "WHERE ingest_id=%s",
                        (ingest_id,),
                    )
                if state and state["processing_state"] != 0:
                    break
            assert state and state["processing_state"] != 0
            return int(ingest_id)

        ended_ingest_id = await ingest(
            _event(
                run_id=run_id,
                event_name="run.ended",
                phase="ended",
                status="succeeded",
                occurred_at=ended,
                started_at=started,
                ended_at=ended,
            ),
        )
        accepted_ingest_id = await ingest(
            _event(
                run_id=run_id,
                event_name="run.accepted",
                phase="accepted",
                status="accepted",
                occurred_at=started,
                started_at=started,
                ended_at=None,
            ),
        )
        running_ingest_id, late_accepted_ingest_id = (
            await _verify_partial_run_ordering(
                store,
                ingest,
                started,
                ended,
            )
        )

        await _ingest_partial_span(store, ingest, started)

        llm_span_id = await _ingest_llm_span(
            connection,
            ingest,
            run_id,
            started,
            ended,
        )
        tool_span_id, tool_metadata = await _ingest_tool_span(
            connection,
            ingest,
            run_id,
            started,
            ended,
        )
        await _ingest_tool_asset_and_question_evidence(
            ingest,
            run_id,
            tool_span_id,
            llm_span_id,
            started,
            ended,
        )
        subagent_span_id, parent_span_id = await _ingest_subagent_span(
            connection,
            ingest,
            run_id,
            tool_span_id,
            started,
            ended,
        )
        guard_span_id = await _ingest_guard_span(
            connection,
            ingest,
            run_id,
            started,
            ended,
        )

        malformed_ingest_id = await _ingest_malformed_terminal(
            ingest,
            started,
            ended,
        )
        tied_duration_run_ids, other_source_run_ids = (
            await _seed_multi_source_runs(
                ingest,
                started,
                ended,
            )
        )

        await _assert_run_projection(store, run_id, started, ended)

        api = FastAPI()
        api.state.agent_observability_store = store

        @api.middleware("http")
        async def bind_test_source(request: Request, call_next):
            request.state.source_id = request.headers.get("X-Source-Id")
            request.state.effective_tenant_id = "integration-user"
            return await call_next(request)

        api.include_router(observability_router)
        await _assert_authorized_run_list(
            api,
            run_id,
            started,
            ended,
            tied_duration_run_ids,
            other_source_run_ids,
        )
        await _assert_authorized_tool_detail(
            api,
            run_id,
            tool_span_id,
            tool_metadata,
        )
        await _assert_authorized_asset_detail(api, run_id, tool_span_id)
        await _assert_projected_user_question_evidence(
            store,
            run_id,
            llm_span_id,
        )
        await _assert_authorized_subagent_detail(
            api,
            run_id,
            subagent_span_id,
            parent_span_id,
        )
        await _assert_authorized_guard_detail(api, run_id, guard_span_id)

        async with connection.acquire() as session:
            inbox_rows = await session.fetch_all(
                "SELECT processing_state,payload_text FROM swe_agentobs_inbox "
                "WHERE ingest_id IN (%s,%s,%s,%s,%s) ORDER BY ingest_id",
                (
                    ended_ingest_id,
                    accepted_ingest_id,
                    running_ingest_id,
                    late_accepted_ingest_id,
                    malformed_ingest_id,
                ),
            )
        assert len(inbox_rows) == 5
        assert sum(row["processing_state"] == 1 for row in inbox_rows) == 4
        assert sum(row["processing_state"] == 2 for row in inbox_rows) == 1
        assert all(row["payload_text"] is None for row in inbox_rows)
    finally:
        await connection.close()


async def _emit_source_tool_snapshot_asset(
    monkeypatch,
    run_id,
    started,
    tmp_path,
):
    import hashlib
    from swe.agents.source_tools import (
        SourceToolRuntime,
        build_source_tool_function,
    )
    from swe.app.source_tools.models import SourceToolVersion
    from swe.observability.context import bind_context
    from swe.observability.instrumentation import Observability, bind_tool_span
    from swe.observability.models import ObservabilityContext, Scope
    from swe.observability.config import EmitterConfig

    script = (
        'TOOL_NAME = "source_echo"\n'
        "async def execute(arguments, context):\n"
        '    return {"ok": arguments["ok"]}\n'
    )
    version = SourceToolVersion(
        name="source_echo",
        version=7,
        description="test source tool",
        json_schema={"type": "object"},
        required_env=(),
        content_digest=hashlib.sha256(script.encode("utf-8")).hexdigest(),
        script=script,
        created_at=1,
        created_by="integration-test",
    )

    async def execute_source_snapshot(*_args):
        return {"ok": True}

    monkeypatch.setattr(
        "swe.agents.source_tools._run_source_tool",
        execute_source_snapshot,
    )
    stream = StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    scope = Scope(
        "swe",
        "agentobs-integration",
        "integration-user",
        "default",
    )
    context = ObservabilityContext(
        obs_run_id=run_id,
        run_started_at=started.isoformat(timespec="microseconds").replace(
            "+00:00",
            "Z",
        ),
        capture_enabled=True,
        scope=scope,
    )
    observer = Observability(emitter=emitter)
    try:
        with bind_context(context):
            span = observer.span(
                "Tool",
                "execute",
                name="registered.tool",
                tool={
                    "tool_key": version.name,
                    "tool_call_id": "call-source-tool-asset",
                    "tool_execution_id": str(uuid4()),
                    "executor_kind": "agent_tool",
                },
            )
            assert span is not None
            with bind_tool_span(span):
                response = await build_source_tool_function(
                    version,
                    SourceToolRuntime(
                        tenant_id="integration-user",
                        source_id=None,
                        workspace_dir=tmp_path,
                    ),
                )(ok=True)
            span.end(status="succeeded")
        assert emitter.close(timeout=1) == 0
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        return span.span_id, version, response.content, events
    finally:
        emitter.close()


@pytest.mark.asyncio
async def test_source_tool_snapshot_asset_reaches_authorized_mysql_api(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv(
        "SWE_AGENTOBS_CURSOR_SECRET",
        "integration-cursor-secret-at-least-32",
    )
    connection = DatabaseConnection(_test_db_config())
    await connection.connect()
    try:
        store = ObservabilityStore(connection)
        assert (await store.readiness())["ready"]
        projector = InboxProjector(store, poll_seconds=0.01)
        run_id = str(uuid4())
        started = datetime.now(timezone.utc) - timedelta(seconds=2)
        ended = datetime.now(timezone.utc)

        async def ingest(event):
            async with connection.transaction() as session:
                ingest_id = await session.insert(
                    "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                    (json.dumps(event, separators=(",", ":")),),
                )
            for _ in range(10):
                await projector.project_batch()
                async with connection.acquire() as session:
                    state = await session.fetch_one(
                        "SELECT processing_state FROM swe_agentobs_inbox "
                        "WHERE ingest_id=%s",
                        (ingest_id,),
                    )
                if state and state["processing_state"] != 0:
                    break
            assert state and state["processing_state"] == 1

        await ingest(
            _event(
                run_id=run_id,
                event_name="run.ended",
                phase="ended",
                status="succeeded",
                occurred_at=ended,
                started_at=started,
                ended_at=ended,
            ),
        )
        span_id, version, response_content, producer_events = (
            await _emit_source_tool_snapshot_asset(
                monkeypatch,
                run_id,
                started,
                tmp_path,
            )
        )
        assert response_content == {"ok": True}
        assert any(
            event["event_name"] == "asset.used" for event in producer_events
        ), [event.get("event_name") for event in producer_events]
        for event in producer_events:
            await ingest(event)

        api = FastAPI()
        api.state.agent_observability_store = store

        @api.middleware("http")
        async def bind_test_source(request: Request, call_next):
            request.state.source_id = request.headers.get("X-Source-Id")
            request.state.effective_tenant_id = "integration-user"
            return await call_next(request)

        api.include_router(observability_router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api),
            base_url="http://agentobs.test",
        ) as client:
            response = await client.get(
                f"/agent-observability/runs/{run_id}/assets",
                params={"span_id": span_id},
                headers={
                    "X-User-Role": "manager",
                    "X-Source-Id": "agentobs-integration",
                },
            )
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 1
        assert items[0] == {
            "asset_ref": items[0]["asset_ref"],
            "span_id": span_id,
            "asset_type": "tool",
            "asset_id": version.name,
            "asset_version": str(version.version),
            "asset_digest": f"sha256:{version.content_digest}",
            "asset_source": "source_catalog_snapshot",
        }
    finally:
        await connection.close()


async def _emit_mcp_tool_snapshot_assets(monkeypatch):
    from types import SimpleNamespace

    from swe.agents.tool_guard_mixin import ToolGuardMixin
    from swe.observability import instrumentation

    class RegisteredMcpAgent(ToolGuardMixin):
        toolkit = SimpleNamespace(
            tools={"search": SimpleNamespace(mcp_name="runtime-mcp-name")},
        )
        _request_context = {
            "subagent_mcp_server_keys": {
                "runtime-mcp-name": "search_provider",
            },
            "mcp_config": {"headers": {"Authorization": "private-secret"}},
        }

        def _tool_has_specific_timeout(self, _tool_name):
            return True

        async def _run_tool_call_with_hard_timeout_raw(
            self,
            _tool_call,
            _tool_name,
            _tool_input,
        ):
            return {"content": "private MCP output"}

    emitter = None
    try:
        stream = StringIO()
        emitter = JsonlEmitter(stream=stream)
        observer = Observability(emitter)
        monkeypatch.setattr(
            instrumentation,
            "default_emitter",
            lambda: emitter,
        )
        context = observer.admit_run(
            scope=Scope(
                "swe",
                "agentobs-integration",
                "integration-user",
                "default",
            ),
            business_ref={
                "chat_id": "chat-mcp-asset",
                "root_question_msgid": "question-mcp-asset",
                "session_id": "session-mcp-asset",
            },
            enabled=True,
        )
        attempt = observer.start_attempt(context)
        agent = RegisteredMcpAgent()
        with bind_context(attempt):
            result = await agent._run_tool_call_with_hard_timeout_impl(
                {
                    "id": "mcp-call-1",
                    "name": "search",
                    "input": {"query": "private MCP input"},
                },
                "search",
                {"query": "private MCP input"},
            )
        observer.end_attempt(attempt, status="succeeded")
        observer.end_run(context, status="succeeded")
        assert result == {"content": "private MCP output"}
        assert emitter.close(timeout=1) == 0
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        tool_span_id = next(
            event["obs_span_id"]
            for event in events
            if event.get("event_name") == "span.started"
            and event.get("span_type") == "Tool"
        )
        serialized_events = json.dumps(events)
        for private_value in (
            "private MCP input",
            "private MCP output",
            "private-secret",
            "mcp_config",
            "Authorization",
        ):
            assert private_value not in serialized_events
        return context.obs_run_id, tool_span_id, events
    finally:
        if emitter is not None:
            emitter.close(timeout=1)


@pytest.mark.asyncio
async def test_mcp_tool_and_server_snapshot_assets_reach_authorized_mysql_api(
    monkeypatch,
):
    monkeypatch.setenv(
        "SWE_AGENTOBS_CURSOR_SECRET",
        "integration-cursor-secret-at-least-32",
    )
    connection = DatabaseConnection(_test_db_config())
    await connection.connect()
    try:
        store = ObservabilityStore(connection)
        assert (await store.readiness())["ready"]
        run_id, tool_span_id, events = await _emit_mcp_tool_snapshot_assets(
            monkeypatch,
        )
        projector = InboxProjector(store, poll_seconds=0.01)
        for event in events:
            async with connection.transaction() as session:
                ingest_id = await session.insert(
                    "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                    (json.dumps(event, separators=(",", ":")),),
                )
            for _ in range(10):
                await projector.project_batch()
                async with connection.acquire() as session:
                    state = await session.fetch_one(
                        "SELECT processing_state FROM swe_agentobs_inbox "
                        "WHERE ingest_id=%s",
                        (ingest_id,),
                    )
                if state and state["processing_state"] != 0:
                    break
            assert state and state["processing_state"] == 1

        api = FastAPI()
        api.state.agent_observability_store = store

        @api.middleware("http")
        async def bind_test_source(request: Request, call_next):
            request.state.source_id = request.headers.get("X-Source-Id")
            request.state.effective_tenant_id = "integration-user"
            return await call_next(request)

        api.include_router(observability_router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api),
            base_url="http://agentobs.test",
        ) as client:
            response = await client.get(
                f"/agent-observability/runs/{run_id}/assets",
                params={"span_id": tool_span_id},
                headers={
                    "X-User-Role": "manager",
                    "X-Source-Id": "agentobs-integration",
                },
            )
        assert response.status_code == 200
        items = response.json()["items"]
        assert {
            (
                item["span_id"],
                item["asset_type"],
                item["asset_id"],
                item["asset_version"],
                item["asset_source"],
            )
            for item in items
        } == {
            (
                tool_span_id,
                "tool",
                "search",
                "unknown",
                "runtime_registered",
            ),
            (
                tool_span_id,
                "mcp_server",
                "search_provider",
                "unknown",
                "runtime_snapshot",
            ),
        }
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_mysql_approval_continuation_keeps_one_run_and_links_attempts(
    monkeypatch,
):
    monkeypatch.setenv(
        "SWE_AGENTOBS_CURSOR_SECRET",
        "integration-cursor-secret-at-least-32",
    )
    connection = DatabaseConnection(_test_db_config())
    await connection.connect()
    try:
        store = ObservabilityStore(connection)
        assert (await store.readiness())["ready"]
        sink = StringIO()
        emitter = JsonlEmitter(stream=sink)
        observer = Observability(emitter)
        monkeypatch.setattr(runner_lifecycle, "_OBSERVABILITY", observer)
        context = observer.admit_run(
            scope=Scope("swe", "agentobs-integration", "user-a", "default"),
            business_ref={
                "chat_id": "approval-chat",
                "root_question_msgid": "question-root",
            },
            enabled=True,
        )
        previous_attempt = observer.start_attempt(context)
        approval_ids = [str(uuid4()), str(uuid4())]

        class Owner:
            agent_id = "default"

        request = SimpleNamespace(
            session_id="approval-session",
            user_id="user-a",
            source_id="agentobs-integration",
            channel_meta={"chat_id": "approval-chat"},
        )
        for approval_id in approval_ids:
            approval_started_at = datetime.now(timezone.utc).timestamp() - 2
            with bind_context(previous_attempt):
                carrier = runner_lifecycle.approval_context_carrier()
                runner_lifecycle.record_approval_requested(
                    previous_attempt,
                    approval_id,
                    started_at=approval_started_at,
                )
            runner_lifecycle.finish_query_run(
                context,
                previous_attempt,
                "completed",
                waiting_for_approval=True,
            )
            pending = SimpleNamespace(
                session_id="approval-session",
                user_id="user-a",
                extra={
                    "agent_id": "default",
                    "tenant_id": "user-a",
                    "source_id": "agentobs-integration",
                    "chat_id": "approval-chat",
                    "msgid": "question-root",
                    "agent_observability_context": carrier,
                },
            )
            resumed = runner_lifecycle.restore_approval_context(
                Owner(),
                request,
                pending,
            )
            assert resumed is not None
            assert resumed.obs_run_id == context.obs_run_id
            assert (
                resumed.business_ref["root_question_msgid"] == "question-root"
            )
            next_attempt = runner_lifecycle.start_query_attempt(resumed)
            assert next_attempt.predecessor_attempt_id == (
                previous_attempt.obs_attempt_id
            )
            with bind_context(next_attempt):
                runner_lifecycle.record_approval_resolved(
                    approval_id,
                    "approved",
                    started_at=approval_started_at,
                )
            previous_attempt = next_attempt
        runner_lifecycle.finish_query_run(
            context,
            previous_attempt,
            "completed",
        )
        assert emitter.close(timeout=1) == 0
        records = [json.loads(line) for line in sink.getvalue().splitlines()]
        async with connection.transaction() as session:
            for record in records:
                await session.insert(
                    "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                    (json.dumps(record, separators=(",", ":")),),
                )
        projector = InboxProjector(store, poll_seconds=0.01)
        while await projector.project_batch():
            pass

        await _assert_approval_run_api(store, context.obs_run_id, approval_ids)
        async with connection.acquire() as session:
            active = await session.fetch_one(
                "SELECT active_duration_us FROM swe_agentobs_runs "
                "WHERE run_id=%s",
                (context.obs_run_id,),
            )
        assert active["active_duration_us"] is not None
    finally:
        await connection.close()


def _emit_retry_events():
    sink = StringIO()
    emitter = JsonlEmitter(stream=sink)
    observability = Observability(emitter)
    context = observability.admit_run(
        scope=Scope(
            "swe",
            "agentobs-integration",
            "integration-user",
            "default",
        ),
        enabled=True,
    )
    attempt = observability.start_attempt(context)
    span_ids = []
    with bind_context(attempt):
        for span_index in range(3):
            span = observability.span(
                "LLM",
                "chat",
                name="model.call",
                kind="CLIENT",
                step_index=span_index + 1,
            )
            assert span is not None
            span_ids.append(span.span_id)
            with span:
                for retry_index in range(12):
                    span.event(
                        "model.retry_scheduled",
                        retries=retry_index + 1,
                        loop_count=12,
                    )
                    span.event(
                        "model.request_attempt_started",
                        request_attempt_index=retry_index + 1,
                        streaming=True,
                    )
                if span_index == 2:
                    span.event(
                        "link.added",
                        links=[
                            {
                                "relation_type": "retry_of",
                                "obs_span_id": span_ids[0],
                            },
                            {
                                "relation_type": "cause",
                                "obs_span_id": str(uuid4()),
                            },
                        ],
                    )
    assert emitter.close(timeout=1) == 0
    return context.obs_run_id, [
        json.loads(line) for line in sink.getvalue().splitlines()
    ]


async def _assert_events_projected(connection, run_id, records):
    async with connection.acquire() as session:
        projected_rows = await session.fetch_all(
            "SELECT event_id FROM swe_agentobs_events WHERE run_id=%s",
            (run_id,),
        )
        inbox_rows = await session.fetch_all(
            "SELECT event_id,processing_state,reason_code "
            "FROM swe_agentobs_inbox WHERE event_id IN ("
            + ",".join(["%s"] * len(records))
            + ")",
            tuple(record["event_id"] for record in records),
        )
    projected_ids = {row["event_id"] for row in projected_rows}
    inbox_by_id = {row["event_id"]: row for row in inbox_rows}
    missing_records = [
        record for record in records if record["event_id"] not in projected_ids
    ]
    assert not missing_records, [
        (
            record["event_name"],
            inbox_by_id.get(record["event_id"], {}).get("processing_state"),
            inbox_by_id.get(record["event_id"], {}).get("reason_code"),
        )
        for record in missing_records
    ]
    assert len(inbox_rows) == len(records)


async def _read_span_pages(client, run_id):
    spans = []
    cursor = None
    for _ in range(4):
        response = await client.get(
            f"/agent-observability/runs/{run_id}",
            headers={
                "X-User-Role": "manager",
                "X-Source-Id": "agentobs-integration",
            },
            params={
                "span_limit": 2,
                **({"span_cursor": cursor} if cursor else {}),
            },
        )
        assert response.status_code == 200, response.text
        page = response.json()
        spans.extend(page["spans"])
        cursor = page["spans_next_cursor"]
        if cursor is None:
            break
    return spans


async def _read_event_pages(client, run_id, *, view="events", limit=17):
    items = []
    cursor = None
    for _ in range(10):
        response = await client.get(
            f"/agent-observability/runs/{run_id}/events",
            headers={
                "X-User-Role": "manager",
                "X-Source-Id": "agentobs-integration",
            },
            params={
                "limit": limit,
                "view": view,
                **({"cursor": cursor} if cursor else {}),
            },
        )
        assert response.status_code == 200, response.text
        page = response.json()
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return items
    raise AssertionError("event page cursor did not terminate")


@pytest.mark.asyncio
async def test_mysql_event_api_pages_more_than_one_event_array(monkeypatch):
    monkeypatch.setenv(
        "SWE_AGENTOBS_CURSOR_SECRET",
        "integration-cursor-secret-at-least-32",
    )
    connection = DatabaseConnection(_test_db_config())
    await connection.connect()
    try:
        store = ObservabilityStore(connection)
        assert (await store.readiness())["ready"]
        run_id, records = _emit_retry_events()
        async with connection.transaction() as session:
            for record in records:
                await session.insert(
                    "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                    (json.dumps(record, separators=(",", ":")),),
                )
        projector = InboxProjector(store, poll_seconds=0.01)
        while await projector.project_batch():
            pass
        await _assert_events_projected(connection, run_id, records)

        api = FastAPI()
        api.state.agent_observability_store = store

        @api.middleware("http")
        async def bind_source(request: Request, call_next):
            request.state.source_id = request.headers.get("X-Source-Id")
            request.state.effective_tenant_id = "integration-user"
            return await call_next(request)

        api.include_router(observability_router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api),
            base_url="http://agentobs.test",
        ) as client:
            monkeypatch.delenv("SWE_AGENTOBS_CURSOR_SECRET")
            unpaged = await client.get(
                f"/agent-observability/runs/{run_id}",
                headers={
                    "X-User-Role": "manager",
                    "X-Source-Id": "agentobs-integration",
                },
                params={"span_limit": 10},
            )
            assert unpaged.status_code == 200, unpaged.text
            assert unpaged.json()["spans_next_cursor"] is None
            monkeypatch.setenv(
                "SWE_AGENTOBS_CURSOR_SECRET",
                "integration-cursor-secret-at-least-32",
            )
            pages = await _read_event_pages(client, run_id)
            links = await _read_event_pages(client, run_id, view="links")
            all_events = await _read_event_pages(client, run_id, limit=500)
            span_pages = await _read_span_pages(client, run_id)
            hidden = await client.get(
                f"/agent-observability/runs/{run_id}/events",
                headers={
                    "X-User-Role": "manager",
                    "X-Source-Id": "different-source",
                },
            )
            forbidden = await client.get(
                f"/agent-observability/runs/{run_id}/events",
                headers={"X-User-Role": "user"},
            )
        assert len({item["event_id"] for item in pages}) == len(pages)
        assert len(pages) == len(records)
        assert (
            sum(
                item["metadata"].get("event_type") == "model.retry_scheduled"
                for item in pages
            )
            == 36
        )
        assert (
            sum(
                item["metadata"].get("event_type")
                == "model.request_attempt_started"
                for item in pages
            )
            == 36
        )
        assert len(links) == 1
        assert links[0]["metadata"].get("event_type") == "link.added"
        assert len(all_events) == len(records)
        span_ids = {
            record["entity_id"]
            for record in records
            if record["event_name"] == "span.started"
        }
        first_span_id = next(
            record["entity_id"]
            for record in records
            if record["event_name"] == "span.started"
        )
        assert all(
            item["entity_id"] in span_ids
            for item in pages
            if item["event_name"] == "span.event"
        )
        link_event = next(
            item
            for item in pages
            if item["metadata"].get("event_type") == "link.added"
        )
        assert link_event["metadata"]["links"] == [
            {
                "relation_type": "retry_of",
                "obs_span_id": first_span_id,
            },
            {
                "relation_type": "cause",
                "target_unavailable": True,
            },
        ]
        assert len(span_pages) == 3
        assert len({span["span_id"] for span in span_pages}) == 3
        assert hidden.status_code == 404
        assert forbidden.status_code == 403
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_agent_loop_summary_requires_complete_step_indexes():
    connection = DatabaseConnection(_test_db_config())
    await connection.connect()
    try:
        store = ObservabilityStore(connection)
        sink = StringIO()
        emitter = JsonlEmitter(stream=sink)
        observer = Observability(emitter)
        run_ids = []
        scenarios = (
            [
                ("root-agent", 1),
                ("root-agent", 1),
                ("root-agent", 2),
                ("subagent", 1),
            ],
            [("root-agent", 1), ("root-agent", None)],
        )
        for scenario in scenarios:
            context = observer.admit_run(
                scope=Scope(
                    "swe",
                    "agentobs-integration",
                    "integration-user",
                    "default",
                ),
                enabled=True,
            )
            run_ids.append(context.obs_run_id)
            attempt = observer.start_attempt(context)
            parents = {
                "root-agent": str(uuid4()),
                "subagent": str(uuid4()),
            }
            for execution_group, step_index in scenario:
                step_context = attempt.with_updates(
                    parent_obs_span_id=parents[execution_group],
                )
                with bind_context(step_context):
                    span = observer.span(
                        "LLM",
                        "chat",
                        step_index=step_index,
                    )
                    assert span is not None
                    with span:
                        pass
            observer.end_attempt(attempt, status="succeeded")
            observer.end_run(context, status="succeeded")
        assert emitter.close(timeout=1) == 0

        records = [json.loads(line) for line in sink.getvalue().splitlines()]
        async with connection.transaction() as session:
            for record in records:
                await session.insert(
                    "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                    (json.dumps(record, separators=(",", ":")),),
                )
        projector = InboxProjector(store, poll_seconds=0.01)
        while await projector.project_batch():
            pass
        rows = []
        for run_id in run_ids:
            rows.append(
                await store.get_run(
                    service_unit_id="swe",
                    run_id=run_id,
                    source_ids=["agentobs-integration"],
                ),
            )
        assert rows[0]["agent_loops"] == 3
        assert rows[1]["agent_loops"] is None
    finally:
        await connection.close()


async def _seed_business_population_subagents(run_store_dir):
    from swe.app.subagents import (
        DelegationSpec,
        PerRunSubAgentRunStore,
        PermissionPolicy,
        SubAgentDefinition,
    )

    store = PerRunSubAgentRunStore(run_store_dir)
    definition = SubAgentDefinition(
        name="population-researcher",
        description="Population integration test",
        instruction="Inspect a test population.",
    )
    records = []
    cases = (
        ("visible-chat", "visible-session", False),
        ("", "visible-session", True),
        ("hidden-chat", "visible-session", False),
    )
    for chat_id, session_id, running in cases:
        record = await store.create(
            DelegationSpec(
                parent_chat_id=chat_id,
                parent_thread_id=session_id,
                name=definition.name,
                objective="Inspect population coverage.",
            ),
            definition,
            PermissionPolicy.readonly(),
        )
        if running:
            record = await store.mark_running(record.run_id, worker_pid=123)
        records.append(record)
    return records


async def _ingest_population_event(connection, event):
    async with connection.transaction() as session:
        await session.insert(
            "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
            (json.dumps(event, separators=(",", ":")),),
        )


@pytest.mark.asyncio
# pylint: disable-next=too-many-statements
async def test_mysql_business_population_excludes_unmatched_runs_from_success_rate(
    monkeypatch,
    tmp_path,
):
    from unittest.mock import AsyncMock

    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.config.config import AgentProfileConfig

    connection = DatabaseConnection(_test_db_config())
    await connection.connect()
    try:
        store = ObservabilityStore(connection)
        source_id = f"population-{uuid4().hex}"
        started = datetime.now(timezone.utc)
        window_start = started - timedelta(minutes=1)
        window_end = started + timedelta(minutes=1)
        terminal_at = started + timedelta(seconds=1)
        run_ids = [str(uuid4()) for _ in range(3)]

        for run_id, status in zip(
            run_ids,
            ("succeeded", "failed", "failed"),
            strict=True,
        ):
            event = _event(
                run_id=run_id,
                event_name="run.ended",
                phase="ended",
                status=status,
                occurred_at=terminal_at,
                started_at=started,
                ended_at=terminal_at,
                source_id=source_id,
            )
            if run_id == run_ids[2]:
                event["business_ref"] = {
                    "chat_id": "unmatched-chat",
                    "session_id": "unmatched-session",
                    "root_question_msgid": "unmatched-question",
                }
            await _ingest_population_event(connection, event)

        projector = InboxProjector(store, poll_seconds=0.01)
        while await projector.project_batch():
            pass
        runs = await store.business_run_population(
            service_unit_id="swe",
            tenant_id="integration-user",
            source_id=source_id,
            agent_id="default",
            start_at=window_start,
            end_at=window_end,
        )

        assert {row["run_id"] for row in runs} == set(run_ids)
        turns = [
            {
                "chat_id": f"chat-{run_ids[0]}",
                "session_id": f"session-{run_ids[0]}",
                "question_id": f"question-{run_ids[0]}",
                "state": "completed",
                "history_status": "completed",
            },
            {
                "chat_id": f"chat-{run_ids[1]}",
                "session_id": f"session-{run_ids[1]}",
                "question_id": f"question-{run_ids[1]}",
                "state": "completed",
                "history_status": "completed",
            },
            {
                "chat_id": "chat-without-run",
                "session_id": "session-without-run",
                "question_id": "question-without-run",
                "state": "completed",
                "history_status": "completed",
            },
        ]
        history = SimpleNamespace(
            scope=WorkspaceScope(
                "integration-user",
                source_id,
                "default",
                service_unit_id="swe",
            ),
            business_turn_population=AsyncMock(return_value=turns),
            business_visible_chat_sessions=AsyncMock(
                return_value=[
                    {
                        "chat_id": "visible-chat",
                        "session_id": "visible-session",
                    },
                ],
            ),
        )
        subagent_store_dir = tmp_path / "subagent_runs"
        await _seed_business_population_subagents(subagent_store_dir)
        workspace = SimpleNamespace(
            agent_id="default",
            tenant_id="integration-user",
            workspace_dir=tmp_path,
            config=AgentProfileConfig(
                id="default",
                name="Population Integration Agent",
                workspace_dir=str(tmp_path),
            ),
            subagent_run_store_dir=subagent_store_dir,
            conversation_storage=SimpleNamespace(history=history),
        )
        monkeypatch.setattr(
            "swe.app.runner.api.get_workspace",
            AsyncMock(return_value=workspace),
        )

        api = FastAPI()
        api.state.agent_observability_store = store

        @api.middleware("http")
        async def bind_test_source(request: Request, call_next):
            request.state.source_id = request.headers.get("X-Source-Id")
            request.state.effective_tenant_id = "integration-user"
            return await call_next(request)

        api.include_router(observability_router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api),
            base_url="http://agentobs.test",
        ) as client:
            response = await client.get(
                "/agent-observability/business-population",
                params={
                    "start": window_start.isoformat(),
                    "end": window_end.isoformat(),
                },
                headers={
                    "X-User-Role": "manager",
                    "X-Source-Id": source_id,
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["business"]["turn_count"] == 3
        assert body["business"]["not_collected_count"] == 1
        assert body["runs"]["settled_count"] == 2
        assert body["runs"]["success_count"] == 1
        assert body["runs"]["success_rate"] == 0.5
        assert body["runs"]["unresolved_count"] == 1
        assert body["unresolved_runs"][0]["obs_run_id"] == run_ids[2]
        assert body["subagents"]["count"] == 2
        assert body["subagents"]["by_status"] == {"pending": 1, "running": 1}
        assert body["subagents"]["unresolved_count"] == 1
        assert body["subagents"]["has_more"] is False
        assert body["subagents_as_of"]
        assert body["observability_as_of"]
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_mysql_span_end_replay_and_late_start_preserve_one_terminal_fact():
    connection = DatabaseConnection(_test_db_config())
    await connection.connect()
    try:
        store = ObservabilityStore(connection)
        projector = InboxProjector(store, poll_seconds=0.01)
        now = datetime.now(timezone.utc)
        started = now - timedelta(seconds=2)
        run_id, attempt_id, span_id = (str(uuid4()) for _ in range(3))
        tool = {
            "tool_key": "execute_shell_command",
            "tool_call_id": "call-idempotency",
            "tool_execution_id": str(uuid4()),
            "executor_kind": "agent_tool",
        }
        identity = {
            "entity_kind": "span",
            "entity_id": span_id,
            "obs_attempt_id": attempt_id,
            "attempt_index": 1,
            "obs_span_id": span_id,
            "span_type": "Tool",
            "operation": "execute",
            "kind": "INTERNAL",
            "name": "registered.tool",
            "tool": tool,
        }
        ended = _event(
            run_id=run_id,
            event_name="span.ended",
            phase="ended",
            status="failed",
            occurred_at=now,
            started_at=started,
            ended_at=now,
        )
        ended.update(
            {
                **identity,
                "quality": {"start_observed": True, "end_observed": True},
                "technical_status": "ERROR",
                "has_error": True,
                "error_type": "tool_timeout",
                "phase": "tool_execution",
            },
        )
        ended["status"] = "timed_out"
        duplicate_end = dict(ended)
        conflict_end = {
            **ended,
            "event_id": str(uuid4()),
            "producer_id": f"mysql-test:{uuid4()}",
            "status": "succeeded",
            "technical_status": "OK",
            "has_error": False,
            "error_type": None,
        }
        conflicting_parent = {
            **ended,
            "event_id": str(uuid4()),
            "producer_id": f"mysql-test:{uuid4()}",
            "parent_obs_span_id": str(uuid4()),
        }
        late_start = _event(
            run_id=run_id,
            event_name="span.started",
            phase="started",
            status="running",
            occurred_at=started,
            started_at=started,
            ended_at=None,
        )
        late_start.update(identity)
        orphan_span_id = str(uuid4())
        orphan_end = _event(
            run_id=run_id,
            event_name="span.ended",
            phase="ended",
            status="failed",
            occurred_at=now,
            started_at=started,
            ended_at=now,
        )
        orphan_end.update(
            {
                **identity,
                "entity_id": orphan_span_id,
                "obs_span_id": orphan_span_id,
                "quality": {"start_observed": False, "end_observed": True},
            },
        )
        run_end = _event(
            run_id=run_id,
            event_name="run.ended",
            phase="ended",
            status="succeeded",
            occurred_at=now,
            started_at=started,
            ended_at=now,
        )

        async with connection.transaction() as session:
            ingest_ids = [
                await session.insert(
                    "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                    (json.dumps(event, separators=(",", ":")),),
                )
                for event in (
                    ended,
                    duplicate_end,
                    conflict_end,
                    conflicting_parent,
                    run_end,
                    late_start,
                    orphan_end,
                )
            ]
        while await projector.project_batch():
            pass

        async with connection.acquire() as session:
            inbox = await session.fetch_all(
                "SELECT ingest_id,processing_state,reason_code "
                "FROM swe_agentobs_inbox WHERE ingest_id IN (%s,%s,%s,%s,%s,%s,%s) "
                "ORDER BY ingest_id",
                tuple(ingest_ids),
            )
            event_count = await session.fetch_one(
                "SELECT COUNT(*) AS count FROM swe_agentobs_events "
                "WHERE run_id=%s AND span_id=%s",
                (run_id, span_id),
            )

        spans = await store.list_spans(run_id=run_id)
        run = await store.get_run(
            service_unit_id="swe",
            run_id=run_id,
            source_ids=["agentobs-integration"],
        )
        assert [row["processing_state"] for row in inbox] == [
            1,
            1,
            3,
            3,
            1,
            1,
            1,
        ]
        assert inbox[2]["reason_code"] == "event_conflict"
        assert inbox[3]["reason_code"] == "event_conflict"
        assert event_count["count"] == 2
        original = next(span for span in spans if span["span_id"] == span_id)
        orphan = next(
            span for span in spans if span["span_id"] == orphan_span_id
        )
        assert len(spans) == 2
        assert original["status"] == "timed_out"
        assert original["technical_status"] == "ERROR"
        assert original["has_error"] == 1
        assert original["error_type"] == "tool_timeout"
        assert original["ended_at"] == now.replace(tzinfo=None)
        assert original["quality"] == "observed"
        assert orphan["status"] == "failed"
        assert orphan["ended_at"] == now.replace(tzinfo=None)
        assert orphan["quality"] == "partial"
        assert run["status"] == "succeeded"
        assert run["ended_at"] == now.replace(tzinfo=None)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_mysql_active_duration_is_unknown_if_any_attempt_is_open():
    connection = DatabaseConnection(_test_db_config())
    await connection.connect()
    try:
        store = ObservabilityStore(connection)
        projector = InboxProjector(store, poll_seconds=0.01)
        now = datetime.now(timezone.utc)
        started = now - timedelta(seconds=10)
        run_id, complete_attempt, open_attempt = (
            str(uuid4()) for _ in range(3)
        )

        def attempt_event(
            *,
            attempt_id: str,
            attempt_index: int,
            name: str,
            phase: str,
            occurred_at: datetime,
            ended_at: datetime | None,
        ) -> dict:
            event = _event(
                run_id=run_id,
                event_name=name,
                phase=phase,
                status="succeeded" if ended_at else "running",
                occurred_at=occurred_at,
                started_at=started,
                ended_at=ended_at,
            )
            event.update(
                {
                    "entity_kind": "attempt",
                    "entity_id": attempt_id,
                    "obs_attempt_id": attempt_id,
                    "attempt_index": attempt_index,
                },
            )
            return event

        events = [
            attempt_event(
                attempt_id=complete_attempt,
                attempt_index=1,
                name="attempt.started",
                phase="started",
                occurred_at=started,
                ended_at=None,
            ),
            attempt_event(
                attempt_id=complete_attempt,
                attempt_index=1,
                name="attempt.ended",
                phase="ended",
                occurred_at=started + timedelta(seconds=4),
                ended_at=started + timedelta(seconds=4),
            ),
            _event(
                run_id=run_id,
                event_name="run.ended",
                phase="ended",
                status="succeeded",
                occurred_at=now,
                started_at=started,
                ended_at=now,
            ),
            attempt_event(
                attempt_id=open_attempt,
                attempt_index=2,
                name="attempt.started",
                phase="started",
                occurred_at=started + timedelta(seconds=5),
                ended_at=None,
            ),
        ]
        async with connection.transaction() as session:
            for event in events:
                await session.insert(
                    "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                    (json.dumps(event, separators=(",", ":")),),
                )
        while await projector.project_batch():
            pass

        run = await store.get_run(
            service_unit_id="swe",
            run_id=run_id,
            source_ids=["agentobs-integration"],
        )
        assert run["status"] == "succeeded"
        assert run["active_duration_us"] is None
    finally:
        await connection.close()


async def _ingest_span_ancestor_chain(connection, store, source_id):
    started = datetime.now(timezone.utc)
    run_id = str(uuid4())
    span_ids = [str(uuid4()) for _ in range(5)]
    projector = InboxProjector(store, poll_seconds=0.01)

    async def ingest(event):
        async with connection.transaction() as session:
            ingest_id = await session.insert(
                "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                (json.dumps(event, separators=(",", ":")),),
            )
        for _ in range(10):
            await projector.project_batch()
            async with connection.acquire() as session:
                row = await session.fetch_one(
                    "SELECT processing_state,reason_code "
                    "FROM swe_agentobs_inbox "
                    "WHERE ingest_id=%s",
                    (ingest_id,),
                )
            if row and row["processing_state"] != 0:
                break
        assert row and row["processing_state"] == 1, row

    await ingest(
        _event(
            run_id=run_id,
            event_name="run.accepted",
            phase="accepted",
            status="accepted",
            occurred_at=started,
            started_at=started,
            ended_at=None,
            source_id=source_id,
        ),
    )
    for index, span_id in enumerate(span_ids):
        event = _event(
            run_id=run_id,
            event_name="span.started",
            phase="started",
            status="running",
            occurred_at=started,
            started_at=started,
            ended_at=None,
            source_id=source_id,
        )
        event.update(
            {
                "entity_kind": "span",
                "entity_id": span_id,
                "obs_span_id": span_id,
                "parent_obs_span_id": (
                    span_ids[index + 1] if index + 1 < len(span_ids) else None
                ),
                "span_type": "Agent",
                "operation": "execute",
                "kind": "SERVER",
                "name": "agent.execute",
            },
        )
        await ingest(event)
    return run_id, span_ids


@pytest.mark.asyncio
async def test_mysql_span_ancestor_pagination_and_source_authorization():
    connection = DatabaseConnection(_test_db_config())
    await connection.connect()
    try:
        store = ObservabilityStore(connection)
        assert (await store.readiness())["ready"]
        source_id = f"ancestor-{uuid4().hex}"
        run_id, span_ids = await _ingest_span_ancestor_chain(
            connection,
            store,
            source_id,
        )
        first_page = await store.list_span_ancestors(
            run_id=run_id,
            span_id=span_ids[0],
            limit=2,
        )
        second_page = await store.list_span_ancestors(
            run_id=run_id,
            span_id=span_ids[0],
            limit=2,
            cursor=first_page["next_cursor"],
        )
        assert [item["span_id"] for item in first_page["items"]] == span_ids[
            :2
        ]
        assert first_page["quality"] == "depth_limited"
        assert first_page["next_cursor"] == span_ids[2]
        assert [item["span_id"] for item in second_page["items"]] == span_ids[
            2:4
        ]
        assert second_page["quality"] == "depth_limited"
        assert second_page["next_cursor"] == span_ids[4]

        api = FastAPI()
        api.state.agent_observability_store = store

        @api.middleware("http")
        async def bind_test_source(request: Request, call_next):
            request.state.source_id = request.headers.get("X-Source-Id")
            request.state.effective_tenant_id = "integration-user"
            return await call_next(request)

        api.include_router(observability_router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api),
            base_url="http://agentobs.test",
        ) as client:
            response = await client.get(
                f"/agent-observability/runs/{run_id}/spans/{span_ids[0]}/ancestors",
                params={"limit": 2},
                headers={
                    "X-User-Role": "manager",
                    "X-Source-Id": source_id,
                },
            )
            forbidden = await client.get(
                f"/agent-observability/runs/{run_id}/spans/{span_ids[0]}/ancestors",
                headers={
                    "X-User-Role": "manager",
                    "X-Source-Id": "another-source",
                },
            )
        assert response.status_code == 200
        assert response.json()["quality"] == "depth_limited"
        assert response.json()["next_cursor"] == span_ids[2]
        assert forbidden.status_code == 404
    finally:
        await connection.close()


@pytest.mark.asyncio
async def _emit_successful_model_request_records(monkeypatch):
    from swe.observability import Scope, bind_context
    from swe.observability.instrumentation import (
        ObservabilityModelWrapper,
        record_model_retry,
        record_model_request_attempt,
    )
    from swe.providers.retry_chat_model import (
        RateLimitConfig,
        RetryChatModel,
        RetryConfig,
    )

    sink = StringIO()
    emitter = JsonlEmitter(stream=sink)
    observer = Observability(emitter)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )
    context = observer.admit_run(
        scope=Scope("swe", "agentobs-integration", "tenant-a", "agent-a"),
        enabled=True,
    )
    attempt = observer.start_attempt(context)

    class TransientProviderError(RuntimeError):
        status_code = 429

    class Model:
        model_name = "test-model"
        stream = False
        calls = 0

        async def __call__(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise TransientProviderError("transient provider failure")
            return SimpleNamespace(
                id="response-performance-contract",
                content=[{"type": "text", "text": "safe response"}],
                usage=SimpleNamespace(input_tokens=5, output_tokens=7),
                metadata={"model": "test-model", "finish_reason": "stop"},
            )

    async def on_attempt(index, streaming):
        record_model_request_attempt(index, streaming=streaming)

    async def on_retry(index, _max_attempts, error, delay):
        record_model_retry(index, type(error).__name__, delay)

    model = ObservabilityModelWrapper(
        RetryChatModel(
            Model(),
            retry_config=RetryConfig(
                enabled=True,
                max_retries=1,
                backoff_base=0,
                backoff_cap=0,
            ),
            rate_limit_config=RateLimitConfig(max_qpm=0),
            on_retry=on_retry,
            on_request_attempt=on_attempt,
        ),
        provider_key="test-provider",
        model_key="test-model",
    )
    with bind_context(attempt):
        result = await model([{"role": "system", "content": "private prompt"}])
        observer.end_attempt(attempt, status="succeeded")
        observer.end_run(context, status="succeeded")
    assert result.id == "response-performance-contract"
    assert emitter.close(timeout=1) == 0
    payload = sink.getvalue()
    assert "private prompt" not in payload
    assert "safe response" not in payload
    records = [json.loads(line) for line in payload.splitlines()]
    _assert_retry_attempt_records(records)
    return context.run_started_at, context.obs_run_id, records


def _assert_retry_attempt_records(records):
    retry_events = [
        event
        for event in records
        if event.get("event_type") == "model.retry_scheduled"
    ]
    attempt_starts = [
        event
        for event in records
        if event["event_name"] == "span.started"
        and event.get("operation") == "request_attempt"
    ]
    assert len(retry_events) == 1
    assert len(attempt_starts) == 2
    assert retry_events[0]["links"] == [
        {
            "relation_type": "cause",
            "obs_span_id": attempt_starts[0]["obs_span_id"],
        },
    ]
    assert attempt_starts[1]["links"] == [
        {
            "relation_type": "retry_of",
            "obs_span_id": attempt_starts[0]["obs_span_id"],
        },
    ]


def _assert_retry_recovery_detail(detail, link_pages):
    request_attempts = [
        span
        for span in detail["spans"]
        if span["operation"] == "request_attempt"
    ]
    assert len(request_attempts) == 2
    assert request_attempts[0]["status"] == "failed"
    assert request_attempts[0]["error_type"] == "TransientProviderError"
    assert request_attempts[1]["status"] == "succeeded"
    retry_link = next(
        item
        for item in link_pages
        if any(
            link.get("relation_type") == "retry_of"
            for link in item["metadata"].get("links", [])
        )
    )
    assert retry_link["entity_id"] == request_attempts[1]["span_id"]
    assert retry_link["metadata"]["links"] == [
        {
            "relation_type": "retry_of",
            "obs_span_id": request_attempts[0]["span_id"],
        },
    ]
    first, second = (span["span_id"] for span in request_attempts)
    assert {
        (link["relation_type"], link["obs_span_id"])
        for item in link_pages
        if item["entity_id"] == first
        for link in item["metadata"].get("links", [])
    } == {("retry_next", second), ("recovered_by", second)}


@pytest.mark.asyncio
async def test_mysql_performance_counts_logical_calls_separately_from_attempts(
    monkeypatch,
):
    monkeypatch.setenv(
        "SWE_AGENTOBS_CURSOR_SECRET",
        "integration-cursor-secret-at-least-32",
    )
    started_at, run_id, records = await _emit_successful_model_request_records(
        monkeypatch,
    )
    connection = DatabaseConnection(_test_db_config())
    await connection.connect()
    try:
        store = ObservabilityStore(connection)
        async with connection.transaction() as session:
            for record in records:
                await session.insert(
                    "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                    (json.dumps(record, separators=(",", ":")),),
                )
        projector = InboxProjector(store, poll_seconds=0.01)
        while await projector.project_batch():
            pass
        api = FastAPI()
        api.state.agent_observability_store = store

        @api.middleware("http")
        async def bind_test_source(request: Request, call_next):
            request.state.source_id = "agentobs-integration"
            request.state.effective_tenant_id = "tenant-a"
            return await call_next(request)

        api.include_router(observability_router)
        start = datetime.fromisoformat(
            started_at.replace("Z", "+00:00"),
        ) - timedelta(seconds=1)
        end = start + timedelta(seconds=20)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api),
            base_url="http://agentobs.test",
        ) as client:
            response = await client.get(
                "/agent-observability/performance",
                params={"start": start.isoformat(), "end": end.isoformat()},
                headers={
                    "X-User-Role": "manager",
                    "X-Source-Id": "agentobs-integration",
                },
            )
            assets_response = await client.get(
                f"/agent-observability/runs/{run_id}/assets",
                headers={
                    "X-User-Role": "manager",
                    "X-Source-Id": "agentobs-integration",
                },
            )
            detail_response = await client.get(
                f"/agent-observability/runs/{run_id}",
                headers={
                    "X-User-Role": "manager",
                    "X-Source-Id": "agentobs-integration",
                },
            )
            link_pages = await _read_event_pages(
                client,
                run_id,
                view="links",
            )
        assert response.status_code == 200, response.text
        assert assets_response.status_code == 200, assets_response.text
        assert detail_response.status_code == 200, detail_response.text
        performance = response.json()
        assert performance["sample_count"] == 3
        assert performance["llm"]["call_count"] == 1
        assert performance["llm"]["request_attempt_count"] == 2
        assert performance["llm"]["usage_known_count"] == 1
        assert performance["llm"]["usage_coverage"] == 0.5
        assert performance["llm"]["input_tokens_known_count"] == 1
        assert performance["llm"]["output_tokens_known_count"] == 1
        assert performance["run_count"] == 1
        assert run_id == records[0]["obs_run_id"]
        detail = detail_response.json()
        _assert_retry_recovery_detail(detail, link_pages)
        assert detail["run"]["status"] == "succeeded"
        assets = assets_response.json()["items"]
        agent_asset = next(
            item for item in assets if item["asset_type"] == "agent"
        )
        model_asset = next(
            item for item in assets if item["asset_type"] == "model"
        )
        prompt_asset = next(
            item for item in assets if item["asset_type"] == "prompt"
        )
        assert (agent_asset["asset_id"], agent_asset["asset_version"]) == (
            "agent-a",
            "unknown",
        )
        assert (model_asset["asset_id"], model_asset["asset_version"]) == (
            "test-provider/test-model",
            "unknown",
        )
        assert (prompt_asset["asset_id"], prompt_asset["asset_version"]) == (
            "system_prompt",
            "unknown",
        )
        assert prompt_asset["asset_digest"].startswith("sha256:")
        assert "asset_ref" in agent_asset and "asset_ref" in model_asset
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_mysql_late_subagent_terminal_does_not_reopen_root_run(
    monkeypatch,
):
    monkeypatch.setenv(
        "SWE_AGENTOBS_CURSOR_SECRET",
        "integration-cursor-secret-at-least-32",
    )
    connection = DatabaseConnection(_test_db_config())
    await connection.connect()
    try:
        store = ObservabilityStore(connection)
        assert (await store.readiness())["ready"]
        source_id = f"late-subagent-{uuid4().hex}"
        started = datetime.now(timezone.utc) - timedelta(seconds=5)
        root_ended = started + timedelta(seconds=1)
        child_ended_at = started + timedelta(seconds=4)
        run_id = str(uuid4())
        span_id = str(uuid4())
        projector = InboxProjector(store, poll_seconds=0.01)

        async def ingest(event):
            async with connection.transaction() as session:
                ingest_id = await session.insert(
                    "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                    (json.dumps(event, separators=(",", ":")),),
                )
            for _ in range(10):
                await projector.project_batch()
                async with connection.acquire() as session:
                    row = await session.fetch_one(
                        "SELECT processing_state,reason_code "
                        "FROM swe_agentobs_inbox WHERE ingest_id=%s",
                        (ingest_id,),
                    )
                if row and row["processing_state"] != 0:
                    break
            assert row and row["processing_state"] == 1, row

        await ingest(
            _event(
                run_id=run_id,
                event_name="run.accepted",
                phase="accepted",
                status="accepted",
                occurred_at=started,
                started_at=started,
                ended_at=None,
                source_id=source_id,
            ),
        )
        child_common = {
            "entity_kind": "span",
            "entity_id": span_id,
            "obs_span_id": span_id,
            "span_type": "SubAgent",
            "operation": "agent.execute",
            "kind": "SERVER",
            "name": "agent.execute",
            "subagent": {
                "mode": "background",
                "execution_agent_key": "late-worker",
                "business_subagent_key": "subagent-late-1",
                "result_status": "succeeded",
            },
        }
        child_started = _event(
            run_id=run_id,
            event_name="span.started",
            phase="started",
            status="running",
            occurred_at=started + timedelta(milliseconds=100),
            started_at=started + timedelta(milliseconds=100),
            ended_at=None,
            source_id=source_id,
        )
        child_started.update(child_common)
        await ingest(child_started)
        await ingest(
            _event(
                run_id=run_id,
                event_name="run.ended",
                phase="ended",
                status="succeeded",
                occurred_at=root_ended,
                started_at=started,
                ended_at=root_ended,
                source_id=source_id,
            ),
        )
        child_ended = _event(
            run_id=run_id,
            event_name="span.ended",
            phase="ended",
            status="succeeded",
            occurred_at=child_ended_at,
            started_at=started + timedelta(milliseconds=100),
            ended_at=child_ended_at,
            source_id=source_id,
        )
        child_ended.update(child_common)
        child_ended["quality"] = {
            "start_observed": True,
            "end_observed": True,
        }
        await ingest(child_ended)

        run = await store.get_run(
            service_unit_id="swe",
            run_id=run_id,
            source_ids=[source_id],
        )
        spans = await store.list_spans(run_id=run_id)
        assert run["status"] == "succeeded"
        assert run["ended_at"] == root_ended.replace(tzinfo=None)
        assert len(spans) == 1
        assert spans[0]["status"] == "succeeded"
        assert spans[0]["ended_at"] == child_ended_at.replace(tzinfo=None)
        assert spans[0]["subagent"]["business_subagent_key"] == (
            "subagent-late-1"
        )

        await _assert_late_subagent_detail_api(
            store,
            run_id,
            source_id,
            span_id,
            root_ended,
            child_ended_at,
        )
    finally:
        await connection.close()


async def _assert_late_subagent_detail_api(
    store,
    run_id,
    source_id,
    span_id,
    root_ended,
    child_ended_at,
):
    api = FastAPI()
    api.state.agent_observability_store = store

    @api.middleware("http")
    async def bind_test_source(request: Request, call_next):
        request.state.source_id = request.headers.get("X-Source-Id")
        request.state.effective_tenant_id = "integration-user"
        return await call_next(request)

    api.include_router(observability_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://agentobs.test",
    ) as client:
        response = await client.get(
            f"/agent-observability/runs/{run_id}",
            headers={
                "X-User-Role": "manager",
                "X-Source-Id": source_id,
            },
        )
    assert response.status_code == 200
    detail = response.json()
    assert detail["run"]["status"] == "succeeded"
    assert detail["run"]["ended_at"].startswith(
        root_ended.replace(tzinfo=None).isoformat(timespec="microseconds"),
    )
    api_subagent = next(
        item for item in detail["spans"] if item["span_id"] == span_id
    )
    assert api_subagent["status"] == "succeeded"
    assert api_subagent["ended_at"].startswith(
        child_ended_at.replace(tzinfo=None).isoformat(
            timespec="microseconds",
        ),
    )
    assert api_subagent["subagent"]["business_subagent_key"] == (
        "subagent-late-1"
    )

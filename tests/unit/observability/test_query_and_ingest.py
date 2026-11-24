# -*- coding: utf-8 -*-
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from swe.observability import Scope, ObservabilityContext
from swe.observability.db_config import get_observability_database_config
from swe.observability.projector import (
    InboxProjector,
    _attempt_interval_union_us,
    _validate_payload,
)
from swe.observability.query import (
    _authorized_sources,
    _business_population_projection,
    _decode_cursor,
    _encode_cursor,
    _is_mysql_query_timeout,
    _read_admitted_question,
    _read_pending_online,
    capabilities,
    business_population,
    health,
    performance_overview,
    run_assets,
    span_evidence,
    span_ancestors,
)
from swe.observability.store import _span_for_api


def test_scope_matches_logical_and_runtime_tenant_only_within_source():
    from swe.config.context import resolve_runtime_tenant_id
    from swe.observability.identity import same_observability_scope

    logical = {
        "service_unit_id": "swe",
        "tenant_id": "alice",
        "source_id": "source-a",
        "agent_id": "agent",
    }
    runtime = {
        **logical,
        "tenant_id": resolve_runtime_tenant_id("alice", "source-a"),
    }
    assert same_observability_scope(logical, runtime)
    assert same_observability_scope(
        {**logical, "tenant_id": "default"},
        {**logical, "tenant_id": "default_source-a"},
    )
    assert not same_observability_scope(
        logical,
        {**runtime, "source_id": "source-b"},
    )
    assert not same_observability_scope(
        logical,
        {**runtime, "tenant_id": resolve_runtime_tenant_id("bob", "source-a")},
    )
    assert not same_observability_scope(
        {**logical, "tenant_id": "default"},
        {**logical, "tenant_id": "default_source-b"},
    )


@pytest.mark.asyncio
async def test_admitted_question_requires_exact_run_and_rechecks_turn():
    from swe.app.conversation_storage.identity import WorkspaceScope

    scope = {
        "service_unit_id": "swe",
        "source_id": "source",
        "tenant_id": "tenant",
        "agent_id": "agent",
    }
    run = {
        "run_id": "run",
        "service_unit_key": b"swe",
        "source_key": b"source",
        "tenant_key": b"tenant",
        "agent_key": b"agent",
        "session_key": b"session",
        "root_question_key": b"question",
    }
    turn = {
        "chat_id": "chat",
        "message": {"id": "question", "role": "user", "content": "hello"},
        "agent_observability_context": {
            "obs_run_id": "run",
            "scope": scope,
            "business_ref": {
                "chat_id": "chat",
                "root_question_msgid": "question",
            },
        },
    }
    chat = SimpleNamespace(
        id="chat",
        session_id="session",
        user_id="user",
        meta={"source_id": "source", "agent_id": "agent"},
    )
    session = SimpleNamespace(
        get_persisted_session_state_dict=AsyncMock(return_value={}),
        get_turn_state=AsyncMock(return_value=turn),
    )
    workspace = SimpleNamespace(
        agent_id="agent",
        conversation_scope=WorkspaceScope(
            "tenant",
            "source",
            "agent",
            service_unit_id="swe",
        ),
        chat_manager=SimpleNamespace(get_chat=AsyncMock(return_value=chat)),
        runner=SimpleNamespace(session=session),
    )
    request = SimpleNamespace(state=SimpleNamespace(source_id="source"))
    result = await _read_admitted_question(
        workspace,
        request,
        run,
        "chat",
        "question",
    )
    assert result["status"] == "available"
    assert result["message"]["content"][0]["text"] == "hello"

    session.get_turn_state.side_effect = [turn, {**turn, "status": "changed"}]
    result = await _read_admitted_question(
        workspace,
        request,
        run,
        "chat",
        "question",
    )
    assert result["status"] == "unavailable"

    session.get_turn_state.side_effect = None
    run["run_id"] = "different"
    result = await _read_admitted_question(
        workspace,
        request,
        run,
        "chat",
        "question",
    )
    assert result["status"] == "unresolved"


@pytest.mark.asyncio
async def test_pending_output_requires_unique_committed_occurrence(
    monkeypatch,
):
    from agentscope.message import Msg
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.observability.outbox import sign_event

    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_KEY_ID", "test")
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_SECRET", "s" * 32)

    archive = SimpleNamespace(
        read_evidence_occurrence=AsyncMock(
            return_value={
                "status": "unavailable",
                "message": None,
                "snapshot": "stable",
            },
        ),
    )
    monkeypatch.setattr("swe.app.runner.api._archive_store", lambda _: archive)

    message = Msg(
        "system",
        [{"type": "tool_result", "id": "tool", "name": "t", "output": []}],
        "system",
        metadata={"agent_observability_evidence_ref": "token"},
    )
    message.metadata["agent_observability_outbox"] = sign_event(
        {
            "event_id": "event-id",
            "event_type": "evidence.linked",
            "saved_state": "available",
            "obs_run_id": "run",
            "obs_span_id": "span",
            "chat_id": "chat",
            "message_id": message.id,
            "reference_id": "token",
            "content_type": "tool_output",
            "scope": {
                "service_unit_id": "swe",
                "source_id": "source",
                "tenant_id": "tenant",
                "agent_id": "agent",
            },
        },
    )
    state = {"agent": {"memory": {"content": [[message.to_dict(), []]]}}}
    session = SimpleNamespace(
        get_persisted_session_state_dict=AsyncMock(return_value=state),
    )
    chat = SimpleNamespace(
        id="chat",
        session_id="session",
        user_id="user",
        meta={"source_id": "source", "agent_id": "agent"},
    )
    workspace = SimpleNamespace(
        agent_id="agent",
        conversation_scope=WorkspaceScope(
            "tenant",
            "source",
            "agent",
            service_unit_id="swe",
        ),
        chat_manager=SimpleNamespace(get_chat=AsyncMock(return_value=chat)),
        runner=SimpleNamespace(session=session),
    )
    request = SimpleNamespace(state=SimpleNamespace(source_id="source"))
    run = {
        "run_id": "run",
        "service_unit_key": b"swe",
        "source_key": b"source",
        "tenant_key": b"tenant",
        "agent_key": b"agent",
        "chat_key": b"chat",
        "session_key": b"session",
    }
    ref = {
        "evidence_ref": "event:event-id",
        "saved_state": "available",
        "chat_id": "chat",
        "message_id": message.id,
        "reference_id": "token",
        "content_type": "tool_output",
    }
    assert (await _read_pending_online(workspace, request, run, ref, "span"))[
        "status"
    ] == "available"
    archive.read_evidence_occurrence.return_value = {
        "status": "available",
        "message": message,
        "snapshot": "stable",
    }
    assert (await _read_pending_online(workspace, request, run, ref, "span"))[
        "status"
    ] == "unresolved"
    archive.read_evidence_occurrence.return_value = {
        "status": "unavailable",
        "message": None,
        "snapshot": "stable",
    }
    state["agent"]["memory"]["content"].append([message.to_dict(), []])
    assert (await _read_pending_online(workspace, request, run, ref, "span"))[
        "status"
    ] == "unresolved"
    state["agent"]["memory"]["content"].clear()
    assert (await _read_pending_online(workspace, request, run, ref, "span"))[
        "status"
    ] == "pending"
    archive.read_evidence_occurrence.return_value = {
        "status": "available",
        "message": message,
        "snapshot": "stable",
    }
    result = await _read_pending_online(workspace, request, run, ref, "span")
    assert result["status"] == "compacted"
    assert "agent_observability_evidence_ref" not in json.dumps(
        result["message"],
    )


def test_business_population_counts_only_exact_settled_run_links():
    turns = [
        {
            "chat_id": "chat-a",
            "session_id": "session-a",
            "question_id": "question-a",
            "state": "completed",
            "history_status": "completed",
        },
        {
            "chat_id": "chat-b",
            "session_id": "session-b",
            "question_id": "question-b",
            "state": "admitted",
            "history_status": None,
        },
        {
            "chat_id": "chat-c",
            "session_id": "session-c",
            "question_id": "question-c",
            "state": "completed",
            "history_status": "completed",
        },
    ]
    runs = [
        {
            "run_id": "run-success",
            "chat_key": b"chat-a",
            "session_key": b"session-a",
            "root_question_key": b"question-a",
            "status": "succeeded",
            "ended_at": object(),
            "terminal_fact_source": "business_settlement",
            "capture_enabled": True,
        },
        {
            "run_id": "run-pending",
            "chat_key": b"chat-b",
            "session_key": b"session-b",
            "root_question_key": b"question-b",
            "status": "waiting_approval",
            "ended_at": None,
            "terminal_fact_source": None,
            "capture_enabled": True,
        },
        {
            "run_id": "run-unresolved",
            "chat_key": b"chat-x",
            "session_key": b"session-x",
            "root_question_key": b"question-x",
            "status": "failed",
            "ended_at": object(),
            "terminal_fact_source": "business_settlement",
            "capture_enabled": True,
        },
    ]

    result = _business_population_projection(turns, runs)

    assert result["business"] == {
        "turn_count": 3,
        "by_turn_status": {"completed": 2, "admitted": 1},
        "linked_turn_count": 2,
        "not_collected_count": 1,
        "pending_count": 1,
        "unresolved_count": 0,
        "unknown_count": 0,
        "capture_disabled_count": 0,
        "run_identity_coverage": 2 / 3,
        "observation_coverage": 2 / 3,
    }
    assert result["runs"]["settled_count"] == 1
    assert result["runs"]["success_rate"] == 1.0
    assert result["runs"]["unresolved_count"] == 1
    assert result["items"][1]["observation_status"] == "pending"
    assert result["items"][2]["observation_status"] == "not_collected"
    assert result["unresolved_runs"][0]["obs_run_id"] == "run-unresolved"

    unavailable = _business_population_projection(
        turns[:1],
        [],
        observation_available=False,
    )
    assert unavailable["items"][0]["observation_status"] == "unknown"
    assert unavailable["business"]["not_collected_count"] == 0
    assert unavailable["runs"]["success_rate"] is None


def test_business_population_labels_capture_disabled_without_counting_success():
    result = _business_population_projection(
        [
            {
                "chat_id": "chat-a",
                "session_id": "session-a",
                "question_id": "question-a",
                "state": "completed",
                "history_status": "completed",
            },
        ],
        [
            {
                "run_id": "run-a",
                "chat_key": b"chat-a",
                "session_key": b"session-a",
                "root_question_key": b"question-a",
                "status": "succeeded",
                "ended_at": object(),
                "terminal_fact_source": "business_settlement",
                "capture_enabled": False,
            },
        ],
    )

    assert result["items"][0]["observation_status"] == "capture_disabled"
    assert result["business"]["capture_disabled_count"] == 1
    assert result["runs"]["settled_count"] == 0
    assert result["runs"]["success_rate"] is None


@pytest.mark.asyncio
async def test_business_population_api_returns_cross_store_coverage(
    monkeypatch,
):
    from datetime import datetime, timedelta, timezone
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.observability.store import ObservabilityStore

    now = datetime(2026, 11, 18, tzinfo=timezone.utc)
    history = SimpleNamespace(
        scope=WorkspaceScope(
            "tenant-a",
            "source-a",
            "agent-a",
            service_unit_id="swe",
        ),
        business_turn_population=AsyncMock(
            return_value=[
                {
                    "chat_id": b"chat-a",
                    "session_id": b"session-a",
                    "question_id": b"question-a",
                    "state": "completed",
                    "history_status": "completed",
                    "admitted_at": now,
                    "terminal_at": now,
                },
            ],
        ),
        business_visible_chat_sessions=AsyncMock(
            return_value=[
                {
                    "chat_id": b"chat-before-window",
                    "session_id": b"session-before-window",
                },
            ],
        ),
    )
    workspace = SimpleNamespace(
        conversation_storage=SimpleNamespace(history=history),
    )
    monkeypatch.setattr(
        "swe.app.runner.api.get_workspace",
        AsyncMock(return_value=workspace),
    )
    monitor = SimpleNamespace(
        population=AsyncMock(
            return_value={
                "count": 1,
                "by_status": {"succeeded": 1},
                "unresolved_count": 0,
                "has_more": False,
            },
        ),
    )
    monkeypatch.setattr(
        "swe.app.subagents.monitor.create_monitor_service",
        lambda _workspace: monitor,
    )
    store = ObservabilityStore(SimpleNamespace())
    store.business_run_population = AsyncMock(
        return_value=[
            {
                "run_id": "run-a",
                "chat_key": b"chat-a",
                "session_key": b"session-a",
                "root_question_key": b"question-a",
                "status": "succeeded",
                "ended_at": now,
                "terminal_fact_source": "business_settlement",
                "capture_enabled": 1,
            },
        ],
    )
    request = SimpleNamespace(
        headers={"X-User-Role": "manager"},
        state=SimpleNamespace(
            source_id="source-a",
            effective_tenant_id="tenant-a",
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(agent_observability_store=store),
        ),
    )

    result = await business_population(
        request,
        start=datetime(2026, 11, 17, tzinfo=timezone.utc),
        end=now,
    )

    assert result.business["turn_count"] == 1
    assert result.runs["settled_count"] == 1
    assert result.runs["success_rate"] == 1.0
    assert result.subagents["count"] == 1
    assert result.items[0]["obs_run_id"] == "run-a"
    assert result.items[0]["observation_status"] == "settled"
    assert result.business_as_of.tzinfo is not None
    assert result.observability_as_of.tzinfo is not None
    assert result.subagents_as_of.tzinfo is not None
    monitor.population.assert_awaited_once_with(
        chats_by_session={
            "session-before-window": {"chat-before-window"},
        },
        start_at=datetime(2026, 11, 17, tzinfo=timezone.utc),
        end_at=now,
        limit=50_000,
    )


@pytest.mark.asyncio
async def test_business_population_rejects_workspace_source_mismatch(
    monkeypatch,
):
    from datetime import datetime, timedelta, timezone
    from swe.app.conversation_storage.identity import WorkspaceScope

    history = SimpleNamespace(
        scope=WorkspaceScope(
            "tenant-a",
            "source-other",
            "agent-a",
            service_unit_id="swe",
        ),
        business_turn_population=AsyncMock(),
        business_visible_chat_sessions=AsyncMock(),
    )
    workspace = SimpleNamespace(
        conversation_storage=SimpleNamespace(history=history),
    )
    monkeypatch.setattr(
        "swe.app.runner.api.get_workspace",
        AsyncMock(return_value=workspace),
    )
    request = SimpleNamespace(
        headers={"X-User-Role": "manager"},
        state=SimpleNamespace(
            source_id="source-a",
            effective_tenant_id="tenant-a",
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(agent_observability_store=None),
        ),
    )

    with pytest.raises(HTTPException) as exc:
        end = datetime.now(timezone.utc)
        await business_population(
            request,
            start=end - timedelta(hours=1),
            end=end,
        )

    assert exc.value.status_code == 403
    history.business_turn_population.assert_not_awaited()
    history.business_visible_chat_sessions.assert_not_awaited()


@pytest.mark.asyncio
async def test_business_run_population_sql_uses_complete_workspace_scope():
    from datetime import datetime, timezone
    from swe.observability.store import ObservabilityStore

    class Session:
        query = ""
        params = ()

        async def fetch_all(self, query, params):
            self.query = query
            self.params = params
            return []

    session = Session()

    @asynccontextmanager
    async def acquire():
        yield session

    store = ObservabilityStore(SimpleNamespace(acquire=acquire))
    await store.business_run_population(
        service_unit_id="swe",
        tenant_id="tenant-a",
        source_id="source-a",
        agent_id="agent-a",
        start_at=datetime(2026, 11, 17, tzinfo=timezone.utc),
        end_at=datetime(2026, 11, 18, tzinfo=timezone.utc),
        limit=100,
    )

    assert all(
        f"{field}=%s" in session.query
        for field in (
            "service_unit_key",
            "tenant_key",
            "source_key",
            "agent_key",
        )
    )
    assert "MAX_EXECUTION_TIME(5000)" in session.query
    assert session.params[:4] == (
        b"swe",
        b"tenant-a",
        b"source-a",
        b"agent-a",
    )
    assert session.params[-1] == 101


def test_observability_database_does_not_fallback_to_application_database(
    monkeypatch,
):
    monkeypatch.setenv("SWE_DB_HOST", "business-db")
    monkeypatch.setenv("SWE_DB_NAME", "business")
    monkeypatch.delenv("SWE_AGENTOBS_DB_HOST", raising=False)
    assert get_observability_database_config() is None

    monkeypatch.setenv("SWE_AGENTOBS_DB_HOST", "obs-db")
    monkeypatch.setenv("SWE_AGENTOBS_DB_PASSWORD", "plain-secret")
    config = get_observability_database_config()
    assert config.host == "obs-db"
    assert config.database == "swe_agentobs"
    assert config.password == "plain-secret"


def test_manager_cannot_expand_source_scope_and_admin_can_be_explicit():
    manager = SimpleNamespace(
        headers={"X-User-Role": "manager"},
        state=SimpleNamespace(
            source_id="source-a",
            effective_tenant_id="tenant-a",
        ),
    )
    assert _authorized_sources(manager, None) == ["source-a"]
    with pytest.raises(HTTPException) as error:
        _authorized_sources(manager, ["source-b"])
    assert error.value.status_code == 403

    admin = SimpleNamespace(
        headers={"X-User-Role": "admin"},
        state=SimpleNamespace(
            source_id="source-a",
            effective_tenant_id="tenant-a",
        ),
    )
    assert _authorized_sources(admin, ["source-b", "source-a"]) == [
        "source-b",
        "source-a",
    ]


def test_cursor_is_signed_and_bound_to_authorized_filters(monkeypatch):
    monkeypatch.setenv("SWE_AGENTOBS_CURSOR_SECRET", "x" * 40)
    scope = {"role": "manager", "sources": ["source-a"], "window": "24h"}
    from datetime import datetime, timezone

    token = _encode_cursor(datetime.now(timezone.utc), str(uuid4()), scope)
    assert _decode_cursor(token, scope)[1]
    duration_scope = {**scope, "sort_by": "duration_us"}
    duration_token = _encode_cursor(1200, str(uuid4()), duration_scope)
    assert _decode_cursor(duration_token, scope)[0] == 1200
    with pytest.raises(HTTPException) as error:
        _decode_cursor(token, {**scope, "sources": ["source-b"]})
    assert error.value.status_code == 400
    with pytest.raises(HTTPException):
        _decode_cursor(token[:-2] + "aa", scope)


def test_query_timeout_detection_uses_mysql_execution_budget_error():
    cause = RuntimeError("query execution was interrupted")
    setattr(cause, "errno", 3024)
    timeout = RuntimeError("outer")
    timeout.__cause__ = cause
    assert _is_mysql_query_timeout(timeout)
    assert not _is_mysql_query_timeout(RuntimeError("connection lost"))


@pytest.mark.asyncio
async def test_capabilities_are_scoped_and_do_not_expose_database_secrets(
    monkeypatch,
):
    monkeypatch.delenv("SWE_AGENTOBS_ENABLED", raising=False)
    app = SimpleNamespace(
        state=SimpleNamespace(
            agent_observability_connection=None,
            agent_observability_projector=None,
            agent_observability_store=None,
        ),
    )
    request = SimpleNamespace(
        headers={"X-User-Role": "manager"},
        state=SimpleNamespace(
            source_id="source-a",
            effective_tenant_id="tenant-a",
        ),
        app=app,
    )
    result = await capabilities(request)
    assert result.capture_enabled is False
    assert result.storage_configured is False
    assert not hasattr(result, "password")

    request.headers = {"X-User-Role": "user"}
    with pytest.raises(HTTPException) as error:
        await capabilities(request)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_global_health_is_admin_only(monkeypatch):
    monkeypatch.delenv("SWE_AGENTOBS_ENABLED", raising=False)
    app = SimpleNamespace(
        state=SimpleNamespace(
            agent_observability_projector=None,
            agent_observability_store=None,
        ),
    )
    manager = SimpleNamespace(
        headers={"X-User-Role": "manager"},
        state=SimpleNamespace(
            source_id="source-a",
            effective_tenant_id="tenant-a",
        ),
        app=app,
    )
    with pytest.raises(HTTPException) as error:
        await health(manager)
    assert error.value.status_code == 403

    admin = SimpleNamespace(
        headers={"X-User-Role": "admin"},
        state=SimpleNamespace(),
        app=app,
    )
    result = await health(admin)
    assert result.storage_ready is False
    assert result.dropped_events is None


def test_ingest_validation_projects_body_and_rejects_bad_scope():
    context = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("svc", "source", "tenant", "agent"),
        business_ref={"chat_id": "chat"},
    )
    event = {
        "schema_version": 1,
        "event_id": str(uuid4()),
        "event_name": "run.accepted",
        "occurred_at": context.run_started_at,
        "producer_id": "host:123:boot",
        "entity_kind": "run",
        "entity_id": context.obs_run_id,
        "phase_key": "accepted",
        "local_seq": 0,
        "obs_run_id": context.obs_run_id,
        "run_started_at": context.run_started_at,
        "capture_enabled": True,
        "scope": context.scope.to_dict(),
        "business_ref": {"chat_id": "chat", "message": "private question"},
        "started_at": context.run_started_at,
        "prompt": "private prompt",
    }
    validated = _validate_payload(json.dumps(event))
    assert "prompt" not in validated
    assert "message" not in validated["business_ref"]
    event["scope"] = {"source_id": "source"}
    with pytest.raises(ValueError, match="invalid_scope"):
        _validate_payload(json.dumps(event))
    event["scope"] = context.scope.to_dict()
    event["event_name"] = "run.ended"
    event["phase_key"] = "ended"
    event["ended_at"] = context.run_started_at
    event["terminal_fact_source"] = "business_settlement"
    with pytest.raises(ValueError, match="event_missing_required_field"):
        _validate_payload(json.dumps(event))
    event["status"] = "partial"
    with pytest.raises(ValueError, match="invalid_run_terminal_status"):
        _validate_payload(json.dumps(event))


def test_approval_resolution_decision_is_a_controlled_enum():
    context = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("svc", "source", "tenant", "agent"),
        business_ref={"chat_id": "chat", "root_question_msgid": "question"},
    )
    approval_id = str(uuid4())
    event = {
        "schema_version": 1,
        "event_id": str(uuid4()),
        "event_name": "approval.resolved",
        "occurred_at": context.run_started_at,
        "producer_id": "host:123:boot",
        "entity_kind": "approval",
        "entity_id": approval_id,
        "phase_key": "resolved",
        "local_seq": 0,
        "obs_run_id": context.obs_run_id,
        "run_started_at": context.run_started_at,
        "capture_enabled": True,
        "scope": context.scope.to_dict(),
        "business_ref": context.business_ref,
        "approval_request_id": approval_id,
        "decision": "approved",
        "started_at": context.run_started_at,
        "ended_at": context.run_started_at,
    }

    for decision in ("approved", "denied", "timed_out", "superseded"):
        event["decision"] = decision
        assert _validate_payload(json.dumps(event))["decision"] == decision

    event["decision"] = "unexpected_reason"
    from swe.observability.projector import _validate_event_fields

    with pytest.raises(ValueError, match="invalid_approval_decision"):
        _validate_event_fields(event)


def test_ingest_validation_rejects_sql_width_overflow_and_bad_attempt_ids():
    context = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("svc", "source", "tenant", "agent"),
    )
    event = {
        "schema_version": 1,
        "event_id": str(uuid4()),
        "event_name": "span.started",
        "occurred_at": context.run_started_at,
        "producer_id": "p" * 129,
        "entity_kind": "span",
        "entity_id": str(uuid4()),
        "phase_key": "started",
        "local_seq": 0,
        "obs_run_id": context.obs_run_id,
        "obs_attempt_id": str(uuid4()),
        "obs_span_id": str(uuid4()),
        "run_started_at": context.run_started_at,
        "capture_enabled": True,
        "scope": context.scope.to_dict(),
        "span_type": "Tool",
        "operation": "execute",
        "kind": "INTERNAL",
        "name": "registered.tool",
        "started_at": context.run_started_at,
        "tool": {
            "tool_key": "read_file",
            "tool_call_id": "call-1",
            "tool_execution_id": str(uuid4()),
            "executor_kind": "agent_tool",
        },
    }
    event["entity_id"] = event["obs_span_id"]
    event["name"] = "registered.tool"
    event["producer_id"] = "host:123:boot"
    event["error_type"] = None
    assert _validate_payload(json.dumps(event))["error_type"] is None
    event.pop("error_type")
    event["producer_id"] = "p" * 129
    with pytest.raises(ValueError, match="field_too_long"):
        _validate_payload(json.dumps(event))
    event["obs_attempt_id"] = "bad-id"
    event["producer_id"] = "host:123:boot"
    with pytest.raises(ValueError, match="invalid_identity"):
        _validate_payload(json.dumps(event))
    event["obs_attempt_id"] = str(uuid4())
    event["has_error"] = "false"
    with pytest.raises(ValueError, match="invalid_error_flag"):
        _validate_payload(json.dumps(event))
    event["has_error"] = 1
    with pytest.raises(ValueError, match="invalid_error_flag"):
        _validate_payload(json.dumps(event))
    event["has_error"] = False
    event.update(
        event_name="span.ended",
        phase_key="ended",
        ended_at=context.run_started_at,
        status="failed",
        metrics={},
        quality={"start_observed": "false", "end_observed": True},
    )
    with pytest.raises(ValueError, match="invalid_span_quality"):
        _validate_payload(json.dumps(event))
    event["quality"] = {"start_observed": False, "end_observed": True}
    event["streaming"] = 1
    with pytest.raises(ValueError, match="invalid_streaming_flag"):
        _validate_payload(json.dumps(event))
    event["streaming"] = True
    event["model_key_source"] = "guessed"
    with pytest.raises(ValueError, match="invalid_model_key_source"):
        _validate_payload(json.dumps(event))
    event.pop("streaming")
    event.pop("model_key_source")
    event["terminal_fact_source"] = "x" * 33
    with pytest.raises(ValueError, match="field_too_long"):
        _validate_payload(json.dumps(event))


def test_ingest_validation_requires_bounded_tool_execution_identity():
    context = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("svc", "source", "tenant", "agent"),
    )
    event = {
        "schema_version": 1,
        "event_id": str(uuid4()),
        "event_name": "span.ended",
        "occurred_at": context.run_started_at,
        "producer_id": "host:123:boot",
        "entity_kind": "span",
        "entity_id": str(uuid4()),
        "phase_key": "ended",
        "local_seq": 0,
        "obs_run_id": context.obs_run_id,
        "obs_attempt_id": str(uuid4()),
        "obs_span_id": None,
        "run_started_at": context.run_started_at,
        "attempt_index": 1,
        "capture_enabled": True,
        "scope": context.scope.to_dict(),
        "span_type": "Tool",
        "operation": "execute",
        "kind": "INTERNAL",
        "name": "registered.tool",
        "started_at": context.run_started_at,
        "ended_at": context.run_started_at,
        "status": "succeeded",
        "metrics": {"duration_us": 1},
        "quality": {"start_observed": True, "end_observed": True},
        "tool": {
            "tool_key": "read_file",
            "tool_call_id": "call-1",
            "tool_execution_id": str(uuid4()),
            "executor_kind": "agent_tool",
            "timeout_budget_ms": 2500,
        },
    }
    event["entity_id"] = event["obs_span_id"] = str(uuid4())
    validated = _validate_payload(json.dumps(event))
    assert validated["tool"]["tool_execution_id"]
    assert validated["tool"]["timeout_budget_ms"] == 2500

    event["tool"]["retry_index"] = True
    with pytest.raises(ValueError, match="invalid_tool_metadata"):
        _validate_payload(json.dumps(event))

    event["tool"].pop("retry_index")
    event["links"] = [
        {
            "relation_type": "retry_of",
            "obs_span_id": str(uuid4()),
        },
    ]
    assert _validate_payload(json.dumps(event))["links"] == event["links"]
    event["links"][0]["relation_type"] = "nearest_in_time"
    with pytest.raises(ValueError, match="invalid_link_relation"):
        _validate_payload(json.dumps(event))
    event["links"][0]["relation_type"] = "retry_of"
    event["links"][0]["obs_run_id"] = str(uuid4())
    with pytest.raises(ValueError, match="cross_run_link_not_supported"):
        _validate_payload(json.dumps(event))


def test_span_api_exposes_only_whitelisted_tool_metadata():
    span = _span_for_api(
        {
            "span_type": "Tool",
            "tool_key": b"read_file",
            "metadata_text": json.dumps(
                {
                    "tool": {
                        "tool_key": "read_file",
                        "tool_call_id": "call-1",
                        "tool_execution_id": str(uuid4()),
                        "executor_kind": "agent_tool",
                        "timeout_budget_ms": 2500,
                        "tool_input": "private body",
                    },
                    "prompt": "private prompt",
                },
            ).encode("utf-8"),
        },
    )
    assert span["tool"]["tool_key"] == "read_file"
    assert span["tool"]["tool_call_id"] == "call-1"
    assert "tool_input" not in span["tool"]
    assert "metadata_text" not in span


def test_guard_api_exposes_policy_target_without_execution_identity():
    call_id = str(uuid4())
    span = _span_for_api(
        {
            "span_type": "Guard",
            "metadata_text": json.dumps(
                {
                    "tool": {
                        "tool_key": "execute_shell_command",
                        "tool_call_id": call_id,
                        "executor_kind": "agent_tool",
                        "tool_input": "private command",
                    },
                },
            ),
        },
    )

    assert span["tool"] == {
        "tool_key": "execute_shell_command",
        "tool_call_id": call_id,
        "executor_kind": "agent_tool",
    }
    assert "tool_execution_id" not in span["tool"]
    assert "metadata_text" not in span


@pytest.mark.asyncio
async def test_projector_sets_read_committed_on_its_transaction_connection():
    operations = []

    class Session:
        async def execute(self, query, params=None):
            operations.append(("execute", self, query))

        async def fetch_all(self, query, params=None):
            operations.append(("fetch", self, query))
            return []

    session = Session()

    @asynccontextmanager
    async def acquire():
        yield session

    @asynccontextmanager
    async def transaction():
        yield session

    connection = SimpleNamespace(acquire=acquire, transaction=transaction)
    projector = InboxProjector(SimpleNamespace(connection=connection))
    assert await projector.project_batch() == 0
    assert operations[0][0:2] == ("execute", session)
    assert operations[1][0:2] == ("fetch", session)
    assert "SKIP LOCKED" in operations[1][2]
    assert "READ COMMITTED" in operations[0][2]


@pytest.mark.asyncio
async def test_optional_database_configuration_errors_do_not_abort_app_startup(
    monkeypatch,
):
    from swe.app import _app
    from swe.observability import db_config

    def invalid_config():
        raise ValueError("invalid optional port")

    monkeypatch.setattr(
        db_config,
        "get_observability_database_config",
        invalid_config,
    )
    app = SimpleNamespace(state=SimpleNamespace())
    await _app._initialize_agent_observability_storage(app)
    assert app.state.agent_observability_connection is None
    assert app.state.agent_observability_store is None


@pytest.mark.asyncio
async def test_readiness_rejects_existing_tables_with_missing_schema_parts():
    from swe.observability.store import (
        REQUIRED_COLUMNS,
        REQUIRED_INDEXES,
        ObservabilityStore,
    )

    class Session:
        async def fetch_all(self, query, _params):
            if "information_schema.COLUMNS" in query:
                return [
                    {"TABLE_NAME": table, "COLUMN_NAME": column}
                    for table, columns in REQUIRED_COLUMNS.items()
                    for column in columns
                    if not (
                        table == "swe_agentobs_runs" and column == "status"
                    )
                ]
            return [
                {
                    "TABLE_NAME": table,
                    "INDEX_NAME": name,
                    "NON_UNIQUE": int(not unique),
                    "SEQ_IN_INDEX": position,
                    "COLUMN_NAME": (
                        "wrong_column"
                        if name == "uq_agentobs_event_slot"
                        else column
                    ),
                }
                for table, definitions in REQUIRED_INDEXES.items()
                for name, (unique, columns) in definitions.items()
                for position, column in enumerate(columns, start=1)
            ]

    session = Session()

    @asynccontextmanager
    async def acquire():
        yield session

    connection = SimpleNamespace(
        acquire=acquire,
        config=SimpleNamespace(database="obs"),
    )
    readiness = await ObservabilityStore(connection).readiness()
    assert not readiness["ready"]
    assert readiness["missing_columns"]["swe_agentobs_runs"] == ["status"]
    assert readiness["invalid_indexes"]["swe_agentobs_events"] == [
        "uq_agentobs_event_slot",
    ]


@pytest.mark.asyncio
async def test_span_evidence_reads_only_authorized_saved_occurrence(
    monkeypatch,
):
    from swe.observability.store import ObservabilityStore

    store = ObservabilityStore(SimpleNamespace())
    run_id, span_id = str(uuid4()), str(uuid4())
    store.get_run = AsyncMock(
        return_value={"chat_key": b"chat-1"},
    )
    store.list_span_evidence_refs = AsyncMock(
        return_value=[
            {
                "evidence_ref": "event:e1",
                "chat_id": "chat-1",
                "message_id": "message-1",
                "occurrence_id": 2,
                "content_type": "tool_output",
                "saved_state": "available",
            },
            {
                "evidence_ref": "event:e2",
                "chat_id": "chat-1",
                "message_id": "message-2",
                "occurrence_id": 0,
                "saved_state": "pending",
            },
        ],
    )
    history = SimpleNamespace(
        message_occurrence=AsyncMock(
            return_value={
                "status": "available",
                "message": {"id": "message-1", "content": []},
            },
        ),
    )
    workspace = SimpleNamespace(
        conversation_storage=SimpleNamespace(history=history),
    )
    monkeypatch.setattr(
        "swe.app.runner.api.get_workspace",
        AsyncMock(return_value=workspace),
    )
    request = SimpleNamespace(
        headers={"X-User-Role": "manager"},
        state=SimpleNamespace(
            source_id="source-a",
            effective_tenant_id="tenant-a",
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(agent_observability_store=store),
        ),
    )

    response = await span_evidence(run_id, span_id, request)

    assert response.items[0]["status"] == "available"
    assert response.items[0]["message"]["id"] == "message-1"
    assert response.items[1]["status"] == "pending"
    history.message_occurrence.assert_awaited_once()
    assert history.message_occurrence.await_args.args == (
        "chat-1",
        "message-1",
    )
    assert history.message_occurrence.await_args.kwargs["occurrence_no"] == 2
    assert (
        history.message_occurrence.await_args.kwargs["max_bytes"]
        == 8 * 1024 * 1024
    )
    assert callable(history.message_occurrence.await_args.kwargs["authorize"])


@pytest.mark.asyncio
async def test_span_evidence_keeps_nonavailable_states_distinct(monkeypatch):
    from swe.observability.store import ObservabilityStore

    states = ("pending", "not_saved", "compacted", "unavailable", "unknown")
    store = ObservabilityStore(SimpleNamespace())
    store.get_run = AsyncMock(return_value={"chat_key": b"chat-1"})
    store.list_span_evidence_refs = AsyncMock(
        return_value=[
            {
                "evidence_ref": f"event:{index}",
                "chat_id": "chat-1",
                "message_id": f"message-{index}",
                "saved_state": state,
            }
            for index, state in enumerate(states)
        ],
    )

    history = SimpleNamespace(
        message_occurrence=AsyncMock(
            return_value={
                "status": "available",
                "message": {"id": "message-2", "content": []},
            },
        ),
    )
    workspace = SimpleNamespace(
        conversation_storage=SimpleNamespace(history=history),
    )

    monkeypatch.setattr(
        "swe.app.runner.api.get_workspace",
        AsyncMock(return_value=workspace),
    )
    request = SimpleNamespace(
        headers={"X-User-Role": "manager"},
        state=SimpleNamespace(
            source_id="source-a",
            effective_tenant_id="tenant-a",
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(agent_observability_store=store),
        ),
    )

    response = await span_evidence(str(uuid4()), str(uuid4()), request)

    assert [item["status"] for item in response.items] == [
        "pending",
        "not_saved",
        "compacted",
        "unavailable",
        "unresolved",
    ]
    assert response.items[2]["message"]["id"] == "message-2"
    history.message_occurrence.assert_awaited_once()


@pytest.mark.asyncio
async def test_span_evidence_rejects_cross_chat_reference(monkeypatch):
    from swe.observability.store import ObservabilityStore

    store = ObservabilityStore(SimpleNamespace())
    store.get_run = AsyncMock(return_value={"chat_key": b"chat-1"})
    store.list_span_evidence_refs = AsyncMock(
        return_value=[
            {
                "evidence_ref": "event:e1",
                "chat_id": "chat-2",
                "message_id": "message-1",
                "saved_state": "available",
            },
        ],
    )
    monkeypatch.setattr(
        "swe.app.runner.api.get_workspace",
        AsyncMock(side_effect=AssertionError("must not read cross-chat body")),
    )
    request = SimpleNamespace(
        headers={"X-User-Role": "manager"},
        state=SimpleNamespace(
            source_id="source-a",
            effective_tenant_id="tenant-a",
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(agent_observability_store=store),
        ),
    )

    response = await span_evidence(str(uuid4()), str(uuid4()), request)

    assert response.items[0]["status"] == "unresolved"


@pytest.mark.asyncio
async def test_span_evidence_marks_oversize_saved_body_as_truncated(
    monkeypatch,
):
    from swe.observability.store import ObservabilityStore

    history = SimpleNamespace(
        message_occurrence=AsyncMock(
            return_value={"status": "truncated", "message": None},
        ),
    )
    workspace = SimpleNamespace(
        conversation_storage=SimpleNamespace(history=history),
        agent_id="agent-a",
    )
    monkeypatch.setattr(
        "swe.app.runner.api.get_workspace",
        AsyncMock(return_value=workspace),
    )
    store = ObservabilityStore(SimpleNamespace())
    store.get_run = AsyncMock(return_value={"chat_key": b"chat-a"})
    store.list_span_evidence_refs = AsyncMock(
        return_value=[
            {
                "evidence_ref": "event:e1",
                "chat_id": "chat-a",
                "message_id": "question-a",
                "content_type": "user_question",
                "saved_state": "available",
            },
        ],
    )
    request = SimpleNamespace(
        headers={"X-User-Role": "manager"},
        state=SimpleNamespace(
            source_id="source-a",
            effective_tenant_id="tenant-a",
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(agent_observability_store=store),
        ),
    )

    response = await span_evidence(str(uuid4()), str(uuid4()), request)

    assert response.items[0]["status"] == "truncated"
    assert response.items[0]["truncated"] is True
    assert "message" not in response.items[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_state", ["available", "compacted"])
async def test_span_evidence_continues_redacted_message_chunks(
    monkeypatch,
    saved_state,
):
    import base64

    from swe.observability.store import ObservabilityStore

    monkeypatch.setenv(
        "SWE_AGENTOBS_CURSOR_SECRET",
        "evidence-test-secret-at-least-32-bytes",
    )
    run_id, span_id = str(uuid4()), str(uuid4())
    safe_message = {
        "id": "message-1",
        "role": "assistant",
        "content": [{"type": "text", "text": "内容é" * 10_000}],
    }
    history = SimpleNamespace(
        message_occurrence=AsyncMock(
            return_value={"status": "available", "message": safe_message},
        ),
    )
    workspace = SimpleNamespace(
        conversation_storage=SimpleNamespace(history=history),
        agent_id="agent-a",
    )
    monkeypatch.setattr(
        "swe.app.runner.api.get_workspace",
        AsyncMock(return_value=workspace),
    )
    store = ObservabilityStore(SimpleNamespace())
    store.get_run = AsyncMock(return_value={"chat_key": b"chat-a"})
    store.list_span_evidence_refs = AsyncMock(
        return_value=[
            {
                "evidence_ref": "event:e1",
                "chat_id": "chat-a",
                "message_id": "message-1",
                "content_type": "assistant_answer",
                "saved_state": saved_state,
            },
        ],
    )
    request = SimpleNamespace(
        headers={"X-User-Role": "manager"},
        state=SimpleNamespace(
            source_id="source-a",
            effective_tenant_id="tenant-a",
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(agent_observability_store=store),
        ),
    )

    first = await span_evidence(run_id, span_id, request, max_bytes=32 * 1024)
    first_item = first.items[0]
    assert first_item["status"] == (
        "compacted" if saved_state == "compacted" else "truncated"
    )
    assert first_item["truncated"] is True
    assert first_item["chunk_offset"] == 0
    assert first_item["chunk_encoding"] == "base64url-json"
    assert first_item["has_more"] is True
    assert first_item["next_cursor"]
    first_chunk = base64.urlsafe_b64decode(
        first_item["message_chunk"]
        + "=" * (-len(first_item["message_chunk"]) % 4),
    )
    chunks = [first_chunk]
    cursor = first_item["next_cursor"]
    last_item = first_item
    while cursor:
        page = await span_evidence(
            run_id,
            span_id,
            request,
            max_bytes=32 * 1024,
            cursor=cursor,
        )
        last_item = page.items[0]
        chunks.append(
            base64.urlsafe_b64decode(
                last_item["message_chunk"]
                + "=" * (-len(last_item["message_chunk"]) % 4),
            ),
        )
        cursor = last_item["next_cursor"]
    assembled = b"".join(chunks)
    expected = json.dumps(
        safe_message,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert assembled == expected
    assert last_item["has_more"] is False
    assert last_item["next_cursor"] is None
    assert last_item["status"] == (
        "compacted" if saved_state == "compacted" else "available"
    )
    assert history.message_occurrence.await_count == len(chunks)

    changed_source = SimpleNamespace(
        headers=request.headers,
        state=SimpleNamespace(
            source_id="source-b",
            effective_tenant_id="tenant-a",
        ),
        app=request.app,
    )
    with pytest.raises(HTTPException) as changed_scope:
        await span_evidence(
            run_id,
            span_id,
            changed_source,
            max_bytes=32 * 1024,
            cursor=first_item["next_cursor"],
        )
    assert changed_scope.value.status_code == 400
    with pytest.raises(HTTPException) as changed_limit:
        await span_evidence(
            run_id,
            span_id,
            request,
            max_bytes=64 * 1024,
            cursor=first_item["next_cursor"],
        )
    assert changed_limit.value.status_code == 400
    with pytest.raises(HTTPException) as tampered:
        await span_evidence(
            run_id,
            span_id,
            request,
            max_bytes=32 * 1024,
            cursor=first_item["next_cursor"][:-2] + "aa",
        )
    assert tampered.value.status_code == 400
    history.message_occurrence.return_value = {
        "status": "available",
        "message": {**safe_message, "content": []},
    }
    with pytest.raises(HTTPException) as changed_content:
        await span_evidence(
            run_id,
            span_id,
            request,
            max_bytes=32 * 1024,
            cursor=first_item["next_cursor"],
        )
    assert changed_content.value.status_code == 409


@pytest.mark.asyncio
async def test_many_long_evidence_items_keep_continuation_cursors(monkeypatch):
    from swe.observability.store import ObservabilityStore

    monkeypatch.setenv(
        "SWE_AGENTOBS_CURSOR_SECRET",
        "evidence-test-secret-at-least-32-bytes",
    )
    store = ObservabilityStore(SimpleNamespace())
    store.get_run = AsyncMock(return_value={"chat_key": b"chat-a"})
    store.list_span_evidence_refs = AsyncMock(
        return_value=[
            {
                "evidence_ref": f"event:{uuid4()}",
                "chat_id": "chat-a",
                "message_id": f"message-{index}",
                "saved_state": "available",
            }
            for index in range(32)
        ],
    )
    history = SimpleNamespace(
        message_occurrence=AsyncMock(
            return_value={
                "status": "available",
                "message": {"text": "x" * 100_000},
            },
        ),
    )
    monkeypatch.setattr(
        "swe.app.runner.api.get_workspace",
        AsyncMock(
            return_value=SimpleNamespace(
                conversation_storage=SimpleNamespace(history=history),
            ),
        ),
    )
    request = SimpleNamespace(
        headers={"X-User-Role": "manager"},
        state=SimpleNamespace(
            source_id="source-a",
            effective_tenant_id="tenant-a",
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(agent_observability_store=store),
        ),
    )

    response = await span_evidence(
        str(uuid4()),
        str(uuid4()),
        request,
        max_bytes=32 * 1024,
    )

    assert len(response.items) == 32
    assert all(
        item["has_more"] and item["next_cursor"] for item in response.items
    )


@pytest.mark.asyncio
async def test_store_projects_only_bounded_evidence_locator_fields():
    from swe.observability.store import ObservabilityStore

    class Session:
        query = ""

        async def fetch_all(self, _query, _params):
            self.query = _query
            return [
                {
                    "event_id": "event-1",
                    "occurred_at": "2026-11-17T00:00:00",
                    "metadata_text": json.dumps(
                        {
                            "content_ref": {
                                "chat_id": "chat-1",
                                "message_id": "message-1",
                                "occurrence_id": 2,
                                "content_type": "tool_output",
                                "saved_state": "pending",
                                "relative_path": "/nas/private",
                            },
                            "event_type": "evidence.pending",
                            "result": "private body",
                        },
                    ),
                },
                {
                    "event_id": "event-2",
                    "occurred_at": "2026-11-17T00:00:01",
                    "metadata_text": json.dumps(
                        {
                            "chat_id": "chat-1",
                            "message_id": "message-1",
                            "occurrence_id": 2,
                            "content_type": "tool_output",
                            "saved_state": "available",
                            "event_type": "evidence.linked",
                        },
                    ),
                },
                {
                    "event_id": "event-3",
                    "legacy_pending_ambiguous": 1,
                    "metadata_text": json.dumps(
                        {
                            "chat_id": "chat-1",
                            "message_id": "message-1",
                            "content_type": "tool_output",
                            "saved_state": "pending",
                            "event_type": "evidence.pending",
                        },
                    ),
                },
            ]

    session = Session()

    @asynccontextmanager
    async def acquire_session():
        yield session

    refs = await ObservabilityStore(
        SimpleNamespace(acquire=acquire_session),
    ).list_span_evidence_refs(
        run_id=str(uuid4()),
        span_id=str(uuid4()),
        source_ids=["source-a"],
    )

    assert {
        (item.get("occurrence_id"), item["saved_state"]): item for item in refs
    } == {
        (2, "available"): {
            "evidence_ref": "event:event-2",
            "chat_id": "chat-1",
            "message_id": "message-1",
            "occurrence_id": 2,
            "content_type": "tool_output",
            "saved_state": "available",
        },
        (None, "unresolved"): {
            "evidence_ref": "event:event-3",
            "chat_id": "chat-1",
            "message_id": "message-1",
            "content_type": "tool_output",
            "saved_state": "unresolved",
        },
    }
    assert "event_name='span.event'" in session.query
    assert "$.event_type" in session.query
    assert "evidence.pending" in session.query
    assert "evidence.linked" in session.query
    assert "ROW_NUMBER() OVER (PARTITION BY" in session.query
    assert "legacy_pending_ambiguous" in session.query
    assert "WHERE locator_rank=1 ORDER BY occurred_at,event_id LIMIT %s" in (
        session.query
    )


@pytest.mark.asyncio
async def test_store_projects_asset_used_without_current_version_lookup():
    from swe.observability.store import ObservabilityStore

    class Session:
        query = ""

        async def fetch_all(self, _query, _params):
            self.query = _query
            return [
                {
                    "event_id": "asset-event",
                    "span_id": "span-1",
                    "metadata_text": json.dumps(
                        {
                            "parent_obs_span_id": "span-1",
                            "asset": {
                                "asset_type": "model",
                                "asset_id": "provider/model",
                                "asset_version": "unknown",
                                "asset_digest": "sha256:abc",
                                "asset_source": "runtime",
                                "secret": "must not escape",
                            },
                        },
                    ),
                },
            ]

    session = Session()

    @asynccontextmanager
    async def acquire():
        yield session

    assets = await ObservabilityStore(
        SimpleNamespace(acquire=acquire),
    ).list_run_assets(
        run_id=str(uuid4()),
        source_ids=["source-a"],
        span_id="span-1",
    )

    assert assets == {
        "items": [
            {
                "asset_ref": "event:asset-event",
                "span_id": "span-1",
                "asset_type": "model",
                "asset_id": "provider/model",
                "asset_version": "unknown",
                "asset_digest": "sha256:abc",
                "asset_source": "runtime",
            },
        ],
        "next_key": None,
        "has_more": False,
        "quality": "complete",
    }
    assert "parent_obs_span_id" in session.query


@pytest.mark.asyncio
async def test_asset_projection_marks_over_limit_set_partial():
    from datetime import datetime, timezone
    from swe.observability.store import ObservabilityStore

    occurred_at = datetime(2026, 11, 17, tzinfo=timezone.utc)

    class Session:
        async def fetch_all(self, _query, _params):
            return [
                {
                    "event_id": f"asset-event-{index}",
                    "span_id": None,
                    "occurred_at": occurred_at,
                    "metadata_text": json.dumps(
                        {
                            "parent_obs_span_id": "span-1",
                            "asset": {
                                "asset_type": "skill",
                                "asset_id": f"skill-{index}",
                            },
                        },
                    ),
                }
                for index in range(3)
            ]

    @asynccontextmanager
    async def acquire():
        yield Session()

    result = await ObservabilityStore(
        SimpleNamespace(acquire=acquire),
    ).list_run_assets(
        run_id=str(uuid4()),
        source_ids=["source-a"],
        span_id="span-1",
        limit=2,
    )
    assert len(result["items"]) == 2
    assert result["quality"] == "partial"
    assert result["has_more"] is True
    assert result["next_key"] == (
        occurred_at,
        "asset-event-1",
    )


@pytest.mark.asyncio
async def test_asset_store_uses_stable_time_and_event_cursor():
    from datetime import datetime, timezone
    from swe.observability.store import ObservabilityStore

    class Session:
        query = ""
        params = ()

        async def fetch_all(self, query, params):
            self.query = query
            self.params = params
            return []

    session = Session()

    @asynccontextmanager
    async def acquire():
        yield session

    before = datetime(2026, 11, 17, tzinfo=timezone.utc)
    await ObservabilityStore(
        SimpleNamespace(acquire=acquire),
    ).list_run_assets(
        run_id=str(uuid4()),
        source_ids=["source-a"],
        span_id="span-a",
        limit=12,
        before=(before, "event-a"),
    )

    assert (
        "(occurred_at>%s OR (occurred_at=%s AND event_id>%s))" in session.query
    )
    assert session.params[-4:] == (
        before,
        before,
        "event-a",
        13,
    )


@pytest.mark.asyncio
async def test_run_assets_cursor_is_signed_and_bound_to_scope(monkeypatch):
    from datetime import datetime, timezone
    from swe.observability.store import ObservabilityStore

    monkeypatch.setenv(
        "SWE_AGENTOBS_CURSOR_SECRET",
        "asset-cursor-secret-at-least-32-bytes",
    )
    store = ObservabilityStore(SimpleNamespace())
    store.get_run = AsyncMock(return_value={"run_id": str(uuid4())})
    occurred_at = datetime(2026, 11, 17, tzinfo=timezone.utc)
    cursor_event_id = str(uuid4())
    store.list_run_assets = AsyncMock(
        side_effect=[
            {
                "items": [{"asset_ref": "event:first"}],
                "next_key": (occurred_at, cursor_event_id),
                "has_more": True,
                "quality": "partial",
            },
            {
                "items": [{"asset_ref": "event:second"}],
                "next_key": None,
                "has_more": False,
                "quality": "complete",
            },
        ],
    )
    request = SimpleNamespace(
        headers={"X-User-Role": "manager"},
        state=SimpleNamespace(
            source_id="source-a",
            effective_tenant_id="tenant-a",
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(agent_observability_store=store),
        ),
    )
    run_id = str(uuid4())
    span_id = str(uuid4())

    first = await run_assets(
        run_id,
        request,
        span_id=span_id,
        limit=1,
        cursor=None,
    )
    second = await run_assets(
        run_id,
        request,
        span_id=span_id,
        limit=1,
        cursor=first.next_cursor,
    )

    assert first.next_cursor
    assert second.next_cursor is None
    assert store.list_run_assets.await_args_list[0].kwargs["before"] is None
    assert store.list_run_assets.await_args_list[1].kwargs["before"] == (
        occurred_at.replace(tzinfo=None),
        cursor_event_id,
    )
    with pytest.raises(HTTPException) as exc:
        await run_assets(
            run_id,
            request,
            span_id=str(uuid4()),
            limit=1,
            cursor=first.next_cursor,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_span_ancestors_continues_and_reports_broken_relationships():
    from swe.observability.store import ObservabilityStore

    rows = {
        "child": {"span_id": "child", "parent_span_id": "parent"},
        "parent": {"span_id": "parent", "parent_span_id": "root"},
        "root": {"span_id": "root", "parent_span_id": None},
    }
    requested = []

    class Session:
        async def fetch_one(self, _query, params):
            requested.append(params[1])
            return rows.get(params[1])

    @asynccontextmanager
    async def acquire():
        yield Session()

    store = ObservabilityStore(SimpleNamespace(acquire=acquire))
    first = await store.list_span_ancestors(
        run_id="run-a",
        span_id="child",
        limit=2,
    )
    second = await store.list_span_ancestors(
        run_id="run-a",
        span_id="child",
        cursor=first["next_cursor"],
        limit=2,
    )

    assert [item["span_id"] for item in first["items"]] == [
        "child",
        "parent",
    ]
    assert first["quality"] == "depth_limited"
    assert first["next_cursor"] == "root"
    assert [item["span_id"] for item in second["items"]] == ["root"]
    assert second["quality"] == "complete"
    assert second["next_cursor"] is None

    rows["root"]["parent_span_id"] = "missing"
    broken = await store.list_span_ancestors(
        run_id="run-a",
        span_id="root",
    )
    assert [item["span_id"] for item in broken["items"]] == ["root"]
    assert broken["quality"] == "parent_missing"
    assert broken["next_cursor"] is None

    rows["missing"] = {"span_id": "missing", "parent_span_id": "root"}
    cycle = await store.list_span_ancestors(
        run_id="run-a",
        span_id="root",
    )
    assert [item["span_id"] for item in cycle["items"]] == [
        "root",
        "missing",
    ]
    assert cycle["quality"] == "cycle"
    assert requested[-7:] == [
        "child",
        "parent",
        "root",
        "root",
        "missing",
        "root",
        "missing",
    ]


@pytest.mark.asyncio
async def test_span_ancestors_api_enforces_run_source_and_continues_cursor(
    monkeypatch,
):
    from swe.observability.store import ObservabilityStore

    store = ObservabilityStore(SimpleNamespace())
    run_id = str(uuid4())
    span_id = str(uuid4())
    next_span_id = str(uuid4())
    store.get_run = AsyncMock(return_value=None)
    store.list_span_ancestors = AsyncMock()
    request = SimpleNamespace(
        headers={"X-User-Role": "manager"},
        state=SimpleNamespace(
            source_id="source-a",
            effective_tenant_id="tenant-a",
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(agent_observability_store=store),
        ),
    )

    with pytest.raises(HTTPException) as denied:
        await span_ancestors(
            run_id,
            span_id,
            request,
            limit=1,
            cursor=None,
        )
    assert denied.value.status_code == 404
    store.list_span_ancestors.assert_not_awaited()

    store.get_run = AsyncMock(return_value={"run_id": run_id})
    store.list_span_ancestors = AsyncMock(
        return_value={
            "items": [{"span_id": span_id}],
            "next_cursor": next_span_id,
            "quality": "depth_limited",
        },
    )
    response = await span_ancestors(
        run_id,
        span_id,
        request,
        limit=1,
        cursor=next_span_id,
    )

    assert response.items == [{"span_id": span_id}]
    assert response.next_cursor == next_span_id
    assert response.quality == "depth_limited"
    store.list_span_ancestors.assert_awaited_once_with(
        run_id=run_id,
        span_id=span_id,
        limit=1,
        cursor=next_span_id,
    )


@pytest.mark.asyncio
async def test_performance_overview_uses_nearest_rank_and_explicit_coverage():
    from datetime import datetime, timedelta, timezone
    from swe.observability.store import ObservabilityStore

    start = datetime(2026, 11, 17, tzinfo=timezone.utc)
    end = datetime(2026, 11, 18, tzinfo=timezone.utc)
    store = ObservabilityStore(SimpleNamespace())
    store.count_performance_candidates = AsyncMock(return_value=5)
    store.performance_samples = AsyncMock(
        return_value=[
            {
                "span_type": "LLM",
                "operation": "chat",
                "duration_us": 10,
                "time_to_first_chunk_us": 4,
                "time_to_first_visible_text_us": 8,
                "input_tokens": 1,
                "output_tokens": 2,
                "has_error": 0,
                "status": "succeeded",
                "quality": "complete",
                "started_at": start,
                "ended_at": start + timedelta(seconds=1),
                "run_id": "run-a",
                "run_duration_us": 50,
                "active_duration_us": 30,
                "agent_loops": 2,
            },
            {
                "span_type": "Tool",
                "operation": "execute",
                "duration_us": 20,
                "has_error": 1,
                "status": "succeeded",
                "quality": "complete",
                "started_at": start + timedelta(seconds=1),
                "ended_at": start + timedelta(seconds=2),
                "run_id": "run-a",
                "run_duration_us": 50,
                "active_duration_us": 30,
                "agent_loops": 2,
            },
            {
                "span_type": "Tool",
                "operation": "execute",
                "duration_us": 30,
                "has_error": 0,
                "status": "succeeded",
                "quality": "partial",
                "started_at": start + timedelta(seconds=2),
                "ended_at": start + timedelta(seconds=3),
                "run_id": "run-a",
                "run_duration_us": 50,
                "active_duration_us": 30,
                "agent_loops": 2,
            },
            {
                "span_type": "Tool",
                "operation": "execute",
                "duration_us": 40,
                "has_error": 1,
                "status": "cancelled",
                "quality": "unknown",
                "started_at": start + timedelta(seconds=3),
                "ended_at": end,
                "run_id": "run-a",
                "run_duration_us": 50,
                "active_duration_us": 30,
                "agent_loops": 2,
            },
            {
                "span_type": "LLM",
                "operation": "request_attempt",
                "duration_us": 5,
                "time_to_first_chunk_us": 2,
                "input_tokens": None,
                "output_tokens": None,
                "has_error": 1,
                "status": "failed",
                "quality": "complete",
                "started_at": start,
                "ended_at": start + timedelta(milliseconds=500),
                "run_id": "run-a",
                "run_duration_us": 50,
                "active_duration_us": 30,
                "agent_loops": 2,
            },
        ],
    )
    request = SimpleNamespace(
        headers={"X-User-Role": "manager"},
        state=SimpleNamespace(
            source_id="source-a",
            effective_tenant_id="tenant-a",
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(agent_observability_store=store),
        ),
    )
    result = await performance_overview(request, start=start, end=end)

    assert result.sample_count == 5
    assert result.sample_start_at == start
    assert result.sample_end_at == end
    assert result.sample_quality == {
        "status": "partial",
        "query_complete": True,
        "span_quality_counts": {
            "complete": 3,
            "partial": 1,
            "unknown": 1,
        },
    }
    assert result.run_count == 1
    assert result.active_duration_us["p50"] == 30
    assert result.agent_loops["total"] == 2
    assert result.duration_us["p50"] == 20
    assert result.duration_us["p95"] == 40
    assert result.tool_failures == {
        "count": 1,
        "sample_count": 2,
        "cancelled_count": 1,
        "rate": 1 / 2,
    }
    assert result.run_status_filter is None
    assert result.llm["usage_known_count"] == 1
    assert result.llm["call_count"] == 1
    assert result.llm["request_attempt_count"] == 1
    assert result.llm["usage_coverage"] == 1
    store.count_performance_candidates.assert_awaited_once()

    store.count_performance_candidates.return_value = 1
    store.performance_samples.return_value = [
        store.performance_samples.return_value[-1],
    ]
    request_attempt = await performance_overview(
        request,
        start=start,
        end=end,
        operation="request_attempt",
    )
    assert request_attempt.llm["call_count"] == 0
    assert request_attempt.llm["request_attempt_count"] == 1
    assert request_attempt.llm["usage_coverage"] is None


@pytest.mark.asyncio
async def test_performance_overview_rejects_over_limit_before_fetching_samples():
    from datetime import datetime, timedelta, timezone
    from swe.observability.store import ObservabilityStore

    store = ObservabilityStore(SimpleNamespace())
    store.count_performance_candidates = AsyncMock(return_value=50_001)
    store.performance_samples = AsyncMock()
    request = SimpleNamespace(
        headers={"X-User-Role": "manager"},
        state=SimpleNamespace(
            source_id="source-a",
            effective_tenant_id="tenant-a",
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(agent_observability_store=store),
        ),
    )
    end = datetime.now(timezone.utc)

    with pytest.raises(HTTPException) as exc:
        await performance_overview(
            request,
            start=end - timedelta(hours=1),
            end=end,
        )

    assert exc.value.status_code == 422
    assert exc.value.detail == "range_too_large"
    store.performance_samples.assert_not_awaited()


@pytest.mark.asyncio
async def test_performance_samples_can_filter_business_run_status():
    from datetime import datetime, timezone
    from swe.observability.store import ObservabilityStore

    class Session:
        query = ""
        params = ()

        async def fetch_all(self, query, params):
            self.query = query
            self.params = params
            return []

    session = Session()

    @asynccontextmanager
    async def acquire():
        yield session

    await ObservabilityStore(
        SimpleNamespace(acquire=acquire),
    ).performance_samples(
        service_unit_id="swe",
        source_ids=["source-a"],
        start_at=datetime(2026, 11, 17, tzinfo=timezone.utc),
        end_at=datetime(2026, 11, 18, tzinfo=timezone.utc),
        status="failed",
        run_status="succeeded",
    )
    assert "s.status=%s" in session.query
    assert "r.status=%s" in session.query
    assert session.params[-3:-1] == ("failed", "succeeded")


@pytest.mark.asyncio
async def test_performance_candidate_count_is_bounded_and_uses_same_filters():
    from datetime import datetime, timezone
    from swe.observability.store import ObservabilityStore

    class Session:
        query = ""
        params = ()

        async def fetch_one(self, query, params):
            self.query = query
            self.params = params
            return {"candidate_count": 5}

    session = Session()

    @asynccontextmanager
    async def acquire():
        yield session

    count = await ObservabilityStore(
        SimpleNamespace(acquire=acquire),
    ).count_performance_candidates(
        service_unit_id="swe",
        source_ids=["source-a"],
        start_at=datetime(2026, 11, 17, tzinfo=timezone.utc),
        end_at=datetime(2026, 11, 18, tzinfo=timezone.utc),
        status="failed",
        run_status="succeeded",
        span_type="Tool",
        operation="execute",
        max_candidates=50_000,
    )
    assert count == 5
    assert "MAX_EXECUTION_TIME(5000)" in session.query
    assert "LIMIT %s) AS bounded_performance_candidates" in session.query
    assert "s.status=%s" in session.query
    assert "r.status=%s" in session.query
    assert "s.span_type=%s" in session.query
    assert "s.operation=%s" in session.query
    assert session.params[-5:] == (
        "failed",
        "succeeded",
        "Tool",
        "execute",
        50_001,
    )


def test_attempt_active_time_uses_interval_union_not_sum():
    from datetime import datetime, timedelta

    start = datetime(2026, 11, 17)
    rows = [
        {"started_at": start, "ended_at": start + timedelta(seconds=4)},
        {
            "started_at": start + timedelta(seconds=2),
            "ended_at": start + timedelta(seconds=6),
        },
        {
            "started_at": start + timedelta(seconds=8),
            "ended_at": start + timedelta(seconds=10),
        },
    ]
    assert _attempt_interval_union_us(rows) == 8_000_000


def test_attempt_active_time_is_unknown_with_incomplete_or_invalid_interval():
    from datetime import datetime, timedelta

    start = datetime(2026, 11, 17)
    complete = {
        "started_at": start,
        "ended_at": start + timedelta(seconds=4),
    }
    incomplete = {
        "started_at": start + timedelta(seconds=5),
        "ended_at": None,
    }
    reversed_interval = {
        "started_at": start + timedelta(seconds=8),
        "ended_at": start + timedelta(seconds=7),
    }

    assert _attempt_interval_union_us([]) is None
    assert _attempt_interval_union_us([complete, incomplete]) is None
    assert _attempt_interval_union_us([complete, reversed_interval]) is None


def _run_accepted_payload() -> str:
    context = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("svc", "source", "tenant", "agent"),
        business_ref={"chat_id": "chat"},
    )
    event = {
        "schema_version": 1,
        "event_id": str(uuid4()),
        "event_name": "run.accepted",
        "occurred_at": context.run_started_at,
        "producer_id": "host:123:boot",
        "entity_kind": "run",
        "entity_id": context.obs_run_id,
        "phase_key": "accepted",
        "local_seq": 0,
        "obs_run_id": context.obs_run_id,
        "run_started_at": context.run_started_at,
        "capture_enabled": True,
        "scope": context.scope.to_dict(),
        "business_ref": context.business_ref,
        "started_at": context.run_started_at,
    }
    return json.dumps(event)


class _RecordingSession:
    def __init__(self):
        self.calls: list[tuple[str, tuple | None]] = []

    async def execute(self, sql: str, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return 1

    async def fetch_one(self, *_args, **_kwargs):
        return None

    async def fetch_all(self, *_args, **_kwargs):
        return []


def _inbox_updates(session: _RecordingSession) -> list[tuple]:
    return [
        params
        for sql, params in session.calls
        if params is not None
        and sql.startswith("UPDATE swe_agentobs_inbox SET processing_state")
    ]


def test_validation_rejects_invalid_optional_timestamps():
    """坏的可选时间字段必须在校验期被拒绝，而不是投影中途抛错。"""
    payload = json.loads(_run_accepted_payload())
    payload["started_at"] = "not-a-date"
    with pytest.raises(ValueError, match="invalid_started_at"):
        _validate_payload(json.dumps(payload))

    payload = json.loads(_run_accepted_payload())
    payload["ended_at"] = 123
    with pytest.raises(ValueError, match="invalid_ended_at"):
        _validate_payload(json.dumps(payload))

    payload = json.loads(_run_accepted_payload())
    payload["attempt_started_at"] = "2026-13-40T00:00:00Z"
    with pytest.raises(ValueError, match="invalid_attempt_started_at"):
        _validate_payload(json.dumps(payload))


@pytest.mark.asyncio
async def test_project_one_isolates_row_error_and_marks_rejected():
    """单行投影异常只回滚该行并置 rejected，不向批次外抛出。"""
    projector = InboxProjector(SimpleNamespace(connection=SimpleNamespace()))
    session = _RecordingSession()

    async def boom(_session, _event, _ingest_id):
        raise ValueError("bad projection")

    projector._store_event = boom

    await projector._project_one(
        session,
        {"ingest_id": 7, "payload_text": _run_accepted_payload()},
    )

    sqls = [sql for sql, _ in session.calls]
    assert "SAVEPOINT agentobs_row" in sqls
    assert "ROLLBACK TO SAVEPOINT agentobs_row" in sqls
    updates = _inbox_updates(session)
    assert len(updates) == 1
    state, reason, _event_id, ingest_id = updates[0]
    assert state == 2
    assert reason == "projection_error:ValueError"
    assert ingest_id == 7


@pytest.mark.asyncio
async def test_project_one_reraises_infrastructure_error():
    """连接类错误必须向上抛出，交由批次重试。"""
    errors = pytest.importorskip("mysql.connector.errors")
    projector = InboxProjector(SimpleNamespace(connection=SimpleNamespace()))
    session = _RecordingSession()

    async def down(_session, _event, _ingest_id):
        raise errors.OperationalError("connection lost")

    projector._store_event = down

    with pytest.raises(errors.OperationalError):
        await projector._project_one(
            session,
            {"ingest_id": 8, "payload_text": _run_accepted_payload()},
        )

    assert _inbox_updates(session) == []


def test_performance_quality_treats_observed_as_complete():
    """投影写入的 observed 必须按完整观测计入统计。"""
    from swe.observability.query import _performance_sample_quality

    mixed = _performance_sample_quality(
        [
            {"quality": "observed"},
            {"quality": "partial"},
            {"quality": "unexpected"},
        ],
    )
    assert mixed["span_quality_counts"] == {
        "complete": 1,
        "partial": 1,
        "unknown": 1,
    }
    assert mixed["status"] == "partial"

    complete = _performance_sample_quality([{"quality": "observed"}])
    assert complete["span_quality_counts"] == {
        "complete": 1,
        "partial": 0,
        "unknown": 0,
    }
    assert complete["status"] == "complete"


class _ScriptedSession(_RecordingSession):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)

    async def fetch_one(self, *_args, **_kwargs):
        return self.responses.pop(0)


def _span_event(context) -> dict:
    return {
        "schema_version": 1,
        "event_name": "span.ended",
        "occurred_at": context.run_started_at,
        "run_started_at": context.run_started_at,
        "obs_run_id": context.obs_run_id,
        "obs_attempt_id": context.obs_attempt_id,
        "obs_span_id": str(uuid4()),
        "span_type": "Tool",
        "operation": "execute",
        "name": "registered.tool",
        "started_at": context.run_started_at,
        "ended_at": context.run_started_at,
        "status": "succeeded",
        "metrics": {},
        "quality": {"start_observed": True, "end_observed": True},
        "scope": context.scope.to_dict(),
        "capture_enabled": True,
    }


@pytest.mark.asyncio
async def test_span_end_updates_run_aggregate_columns():
    """span 结束后回填 span_count / error_span_count / tool_calls。"""
    projector = InboxProjector(SimpleNamespace(connection=SimpleNamespace()))
    session = _RecordingSession()
    context = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("svc", "source", "tenant", "agent"),
    ).new_attempt()

    await projector._project_entity(session, _span_event(context), "{}")

    aggregate_sql = [
        sql
        for sql, _ in session.calls
        if "span_count=" in sql and "tool_calls=" in sql
    ]
    assert len(aggregate_sql) == 1
    assert "error_span_count=" in aggregate_sql[0]


@pytest.mark.asyncio
async def test_store_event_rejects_reused_attempt_index():
    """新 Attempt 复用 (run_id, attempt_index) 时必须判冲突而非静默覆盖。"""
    projector = InboxProjector(SimpleNamespace(connection=SimpleNamespace()))
    context = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("svc", "source", "tenant", "agent"),
    ).new_attempt()
    event = {
        "schema_version": 1,
        "event_id": str(uuid4()),
        "event_name": "attempt.started",
        "occurred_at": context.run_started_at,
        "run_started_at": context.run_started_at,
        "obs_run_id": context.obs_run_id,
        "obs_attempt_id": context.obs_attempt_id,
        "attempt_index": 2,
        "started_at": context.run_started_at,
        "scope": context.scope.to_dict(),
        "capture_enabled": True,
        "entity_kind": "attempt",
        "entity_id": context.obs_attempt_id,
        "phase_key": "started",
        "local_seq": 0,
    }
    stored = {
        "service_unit_key": b"svc",
        "source_key": b"source",
        "tenant_key": b"tenant",
        "agent_key": b"agent",
        "chat_key": b"",
        "root_question_key": b"",
        "session_key": None,
        "capture_enabled": 1,
        "ended_at": None,
        "status": None,
    }
    session = _ScriptedSession(
        [
            None,
            None,
            stored,
            {"run_id": context.obs_run_id},
            {"attempt_id": "different-attempt"},
        ],
    )

    assert await projector._store_event(session, event, 1) == 3


def test_run_where_clause_binds_tenant_when_provided():
    """提供 tenant 时必须在 SQL 层绑定，不能只靠 source 隔离。"""
    from swe.observability.store import _run_where_clause

    where, params = _run_where_clause(
        "svc",
        ["source-a"],
        None,
        None,
        {},
        tenant_id="tenant-a",
    )
    assert "tenant_key=%s" in where
    assert params[-1] == b"tenant-a"

    unfiltered, unfiltered_params = _run_where_clause(
        "svc",
        ["source-a"],
        None,
        None,
        {},
    )
    assert "tenant_key" not in unfiltered
    assert unfiltered_params == [b"svc", b"source-a"]

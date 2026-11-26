# -*- coding: utf-8 -*-
"""Runner admission to authorized MySQL projection integration contract."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from io import StringIO
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from agentscope.message import Msg
from fastapi import FastAPI, Request

from swe.database import DatabaseConfig, DatabaseConnection
from swe.observability import Scope
from swe.observability.emitter import JsonlEmitter
from swe.observability.instrumentation import Observability
from swe.observability.projector import InboxProjector, _validate_payload
from swe.observability.query import router as observability_router
from swe.observability.store import ObservabilityStore

_DB_HOST = os.getenv("SWE_AGENTOBS_TEST_DB_HOST")
pytestmark = pytest.mark.skipif(
    not _DB_HOST,
    reason="set SWE_AGENTOBS_TEST_DB_HOST to a disposable MySQL *_test database",
)


class _RetryableAgentExecutionError(RuntimeError):
    status_code = 429


def _config() -> DatabaseConfig:
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


def _create_agent_workspace(tmp_path, monkeypatch):
    from swe.config.config import (
        AgentProfileConfig,
        AgentProfileRef,
        AgentsConfig,
        AgentsRunningConfig,
        Config,
        save_agent_config,
    )
    from swe.config.utils import save_config

    monkeypatch.setattr("swe.config.utils.WORKING_DIR", tmp_path)
    monkeypatch.setattr("swe.config.config.WORKING_DIR", tmp_path)
    workspace_dir = tmp_path / "workspaces" / "critical-agent"
    workspace_dir.mkdir(parents=True)
    save_config(
        Config(
            agents=AgentsConfig(
                active_agent="critical-agent",
                profiles={
                    "critical-agent": AgentProfileRef(
                        id="critical-agent",
                        workspace_dir=str(workspace_dir),
                    ),
                },
            ),
        ),
    )
    save_agent_config(
        "critical-agent",
        AgentProfileConfig(
            id="critical-agent",
            name="Critical Agent",
            workspace_dir=str(workspace_dir),
            running=AgentsRunningConfig(
                max_iters=3,
                llm_retry_enabled=True,
                llm_max_retries=1,
                llm_backoff_base=0.1,
                llm_backoff_cap=0.5,
            ),
            system_prompt_files=[],
        ),
    )
    return workspace_dir


async def _assert_projected_run_available(
    connection,
    records,
    *,
    expected_status="succeeded",
) -> None:
    store = ObservabilityStore(connection)
    projector = InboxProjector(store, poll_seconds=0.01)
    async with connection.transaction() as session:
        projection_records = [
            record
            for record in records
            if record["event_name"] == "span.ended"
        ] + [
            record
            for record in records
            if record["event_name"] != "span.ended"
        ]
        for record in projection_records:
            await session.insert(
                "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                (json.dumps(record, separators=(",", ":")),),
            )
    while await projector.project_batch():
        pass
    run_id = records[0]["obs_run_id"]
    run_end_event = next(
        (record for record in records if record["event_name"] == "run.ended"),
        None,
    )
    window_end = (
        run_end_event["ended_at"]
        if run_end_event
        else records[-1]["occurred_at"]
    )
    rows = await store.list_runs(
        service_unit_id="swe",
        source_ids=["runner-integration"],
        limit=20,
        start_at=datetime.fromisoformat(
            records[0]["run_started_at"].replace("Z", "+00:00"),
        ),
        end_at=datetime.fromisoformat(window_end.replace("Z", "+00:00")),
    )
    row = next(item for item in rows if item["run_id"] == run_id)
    assert row["status"] == expected_status
    if run_end_event:
        assert row["quality"] == "observed"
        assert row["ended_at"] is not None
        assert row["duration_us"] > 0
        expected_run_end = datetime.fromisoformat(
            run_end_event["ended_at"].replace("Z", "+00:00"),
        ).replace(tzinfo=None)
        assert row["ended_at"] == expected_run_end
    else:
        assert row["quality"] == "partial"
        assert row["ended_at"] is None
        assert row["duration_us"] is None

    attempt_ids = await _assert_attempt_projection(
        connection,
        run_id,
        records,
        expected_status,
        run_end_event,
        row["ended_at"],
    )

    llm_span = await _assert_llm_span_projection(store, records, run_id)

    api = FastAPI()
    api.state.agent_observability_store = store

    @api.middleware("http")
    async def bind_source(http_request: Request, call_next):
        http_request.state.source_id = "runner-integration"
        http_request.state.effective_tenant_id = records[0]["scope"][
            "tenant_id"
        ]
        return await call_next(http_request)

    api.include_router(observability_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://runner.test",
    ) as client:
        response = await client.get(
            "/agent-observability/runs",
            headers={"X-User-Role": "manager"},
            params={
                "start": records[0]["run_started_at"],
                "end": window_end,
            },
        )
        detail_response = await client.get(
            f"/agent-observability/runs/{run_id}",
            headers={"X-User-Role": "manager"},
        )
        attempt_detail_response = await client.get(
            f"/agent-observability/runs/{run_id}",
            headers={"X-User-Role": "manager"},
            params={"attempt_id": attempt_ids[-1]},
        )
    assert response.status_code == 200, response.text
    api_row = next(
        item for item in response.json()["items"] if item["run_id"] == run_id
    )
    assert response.status_code == 200 and api_row["status"] == expected_status
    _assert_run_model_summary(row, api_row, records)
    if llm_span is not None:
        assert (
            detail_response.status_code == 200
            and detail_response.json()["generated_at"]
        )
        api_llm_span = next(
            span
            for span in detail_response.json()["spans"]
            if span["span_id"] == llm_span["span_id"]
        )
        assert api_llm_span["input_tokens"] == llm_span["input_tokens"]
        assert api_llm_span["output_tokens"] == llm_span["output_tokens"]
        assert api_llm_span["quality"] == "observed"
    assert attempt_detail_response.status_code == 200 and all(
        span["attempt_id"] == attempt_ids[-1]
        for span in attempt_detail_response.json()["spans"]
    )


async def _assert_attempt_projection(
    connection,
    run_id,
    records,
    expected_status,
    run_end_event,
    run_ended_at,
):
    attempt_ends = [
        record for record in records if record["event_name"] == "attempt.ended"
    ]
    attempt_ids = [record["obs_attempt_id"] for record in attempt_ends]
    async with connection.acquire() as session:
        attempts = await session.fetch_all(
            "SELECT attempt_id,attempt_index,predecessor_attempt_id,ended_at,status "
            "FROM swe_agentobs_attempts WHERE run_id = %s ORDER BY attempt_index",
            (run_id,),
        )
    assert len(attempts) == len(attempt_ends)
    assert [
        _identity_text(row["attempt_id"]) for row in attempts
    ] == attempt_ids
    assert [row["attempt_index"] for row in attempts] == list(
        range(1, len(attempts) + 1),
    )
    for index, (row, attempt_end) in enumerate(zip(attempts, attempt_ends)):
        assert row["status"] == attempt_end["status"]
        predecessor = row["predecessor_attempt_id"]
        predecessor_id = _identity_text(predecessor) if predecessor else None
        assert predecessor_id == (attempt_ids[index - 1] if index else None)
    expected_attempt_end = datetime.fromisoformat(
        attempt_ends[-1]["ended_at"].replace("Z", "+00:00"),
    ).replace(tzinfo=None)
    assert attempts[-1]["ended_at"] == expected_attempt_end
    assert attempts[-1]["status"] == expected_status
    if run_end_event:
        assert attempts[-1]["ended_at"] < run_ended_at
    return attempt_ids


def _identity_text(value):
    return value.decode() if isinstance(value, bytes) else value


def _assert_run_model_summary(row, api_row, records):
    logical_model_ends = [
        record
        for record in records
        if record.get("event_name") == "span.ended"
        and record.get("span_type") == "LLM"
        and record.get("operation") == "chat"
    ]
    if not logical_model_ends:
        assert row["llm_calls"] is None
        assert api_row["llm_calls"] is None
        assert row["agent_loops"] is None
        assert api_row["agent_loops"] is None
        assert row["input_tokens"] is None
        assert row["output_tokens"] is None
        assert api_row["input_tokens"] is None
        assert api_row["output_tokens"] is None
        return
    assert row["llm_calls"] == len(logical_model_ends)
    assert api_row["llm_calls"] == len(logical_model_ends)
    step_indexes = [record.get("step_index") for record in logical_model_ends]
    expected_agent_loops = (
        len(
            {
                (
                    record.get("parent_obs_span_id")
                    or record.get("obs_attempt_id"),
                    step_index,
                )
                for record, step_index in zip(logical_model_ends, step_indexes)
            },
        )
        if all(step_index is not None for step_index in step_indexes)
        else None
    )
    assert row["agent_loops"] == expected_agent_loops
    assert api_row["agent_loops"] == expected_agent_loops
    request_ends = [
        record
        for record in records
        if record.get("event_name") == "span.ended"
        and record.get("span_type") == "LLM"
        and record.get("operation") == "request_attempt"
    ]
    assert api_row["request_attempt_count"] == len(request_ends)
    for field in ("input_tokens", "output_tokens"):
        values = [
            (record.get("metrics") or {}).get(field) for record in request_ends
        ]
        known = [value for value in values if value is not None]
        expected = sum(known) if known else None
        assert row[field] == expected
        assert api_row[field] == row[field]
        assert api_row[f"{field}_known_requests"] == len(known)


def _assert_agent_retry_lifecycle(records):
    starts = [
        record
        for record in records
        if record["event_name"] == "attempt.started"
    ]
    ends = [
        record for record in records if record["event_name"] == "attempt.ended"
    ]
    assert len(starts) == len(ends) == 2
    assert starts[1]["attempt_index"] == 2
    assert starts[1]["predecessor_attempt_id"] == starts[0]["obs_attempt_id"]
    assert ends[0]["obs_attempt_id"] == starts[0]["obs_attempt_id"]
    assert ends[0]["status"] == "failed"
    assert ends[1]["obs_attempt_id"] == starts[1]["obs_attempt_id"]
    assert ends[1]["status"] == "succeeded"


def _inject_agent_retry(runner):
    original_stream = runner._stream_single_query_attempt
    retry_state = {"calls": 0}

    async def fail_first_agent_execution(**kwargs):
        retry_state["calls"] += 1
        if retry_state["calls"] == 1:
            raise _RetryableAgentExecutionError("transient status only")
        async for item in original_stream(**kwargs):
            yield item

    runner._stream_single_query_attempt = fail_first_agent_execution
    runner._load_query_retry_settings = lambda _config=None: (2, 1, 0, 0)
    return retry_state


def _assert_runner_lifecycle_records(records, *, agent_retry):
    event_names = [record["event_name"] for record in records]
    assert event_names[:4] == [
        "run.accepted",
        "asset.used",
        "run.state",
        "attempt.started",
    ]
    if agent_retry:
        _assert_agent_retry_lifecycle(records)
    assert event_names[-2:] == ["attempt.ended", "run.ended"]
    request_attempts = [
        record
        for record in records
        if record.get("span_type") == "LLM"
        and record.get("operation") == "request_attempt"
    ]
    request_starts = [
        record
        for record in request_attempts
        if record["event_name"] == "span.started"
    ]
    assert request_starts
    assert all(record["step_index"] == 1 for record in request_starts)
    request_ends = [
        record
        for record in request_attempts
        if record["event_name"] == "span.ended"
    ]
    assert request_starts and len(request_starts) == len(request_ends)
    logical_ends = [
        record
        for record in records
        if record.get("span_type") == "LLM"
        and record.get("operation") == "chat"
        and record["event_name"] == "span.ended"
    ]
    assert len(logical_ends) == 1
    logical_starts = [
        record
        for record in records
        if record.get("span_type") == "LLM"
        and record.get("operation") == "chat"
        and record["event_name"] == "span.started"
    ]
    assert len(logical_starts) == 1
    agent_starts = [
        record
        for record in records
        if record.get("span_type") == "Agent"
        and record["event_name"] == "span.started"
    ]
    agent_ends = [
        record
        for record in records
        if record.get("span_type") == "Agent"
        and record["event_name"] == "span.ended"
    ]
    assert len(agent_starts) == len(agent_ends) == 1
    agent_span_id = agent_starts[0]["obs_span_id"]
    assert all(
        record.get("parent_obs_span_id") == agent_span_id
        for record in logical_starts
    )
    assert event_names.count("span.started") == (
        len(request_starts) + len(logical_starts) + len(agent_starts)
    )
    assert event_names.count("span.ended") == (
        len(request_ends) + len(logical_ends) + len(agent_ends)
    )


async def _assert_llm_span_projection(store, records, run_id):
    model_end = next(
        (
            record
            for record in records
            if record["event_name"] == "span.ended"
            and record["span_type"] == "LLM"
            and record.get("operation") == "request_attempt"
            and record["status"] == "succeeded"
        ),
        None,
    )
    if model_end is None:
        return None
    projected_spans = await store.list_spans(run_id=run_id, limit=50)
    llm_span = next(
        span
        for span in projected_spans
        if span["span_id"] == model_end["obs_span_id"]
    )
    assert llm_span["input_tokens"] == model_end["metrics"]["input_tokens"]
    assert llm_span["output_tokens"] == model_end["metrics"]["output_tokens"]
    decode = lambda value: (
        value.decode() if isinstance(value, bytes) else value
    )
    assert (
        decode(llm_span["request_model_key"]) == model_end["request_model_key"]
    )
    assert decode(llm_span["response_model_key"]) == "response-model"
    assert decode(llm_span["model_key"]) == "response-model"
    assert llm_span["model_key_source"] == "response"
    assert llm_span["streaming"] == model_end["streaming"]
    assert llm_span["step_index"] == model_end["step_index"] == 1
    assert decode(llm_span["finish_reason"]) == "stop"
    if model_end["streaming"]:
        assert llm_span["time_to_first_chunk_us"] is not None
        assert llm_span["time_to_first_visible_text_us"] is not None
    else:
        assert llm_span["time_to_first_chunk_us"] is None
        assert llm_span["time_to_first_visible_text_us"] is None
    assert llm_span["quality"] == "observed"
    return llm_span


def _make_runner_case(
    coordinator_enabled: bool,
    expert_pending: bool | None,
):
    from swe.app.answer_turn.models import TurnIdentity, TurnOutcome
    from swe.app.runner.runner import AgentRunner
    from swe.app.runner.query_execution.admission import stream_admission
    from swe.app.runner.expert_main_runtime import (
        EXPERT_MAIN_TOKEN_KEY,
        ExpertMainRegistry,
    )

    class SessionExecution:
        def __init__(self):
            self.state = {"turn_states": {}}
            self.is_active = True

        async def hydrate_turn(self, _turn_id):
            return None

        def mark_state_dirty(self):
            return None

        async def commit_state(self, _state):
            return None

    execution = SessionExecution()

    class Session:
        @asynccontextmanager
        async def execution(self, _session_id, *, user_id, timeout_seconds):
            assert user_id == "user-a"
            assert timeout_seconds == 5.0
            yield execution

    question_id, chat_id = str(uuid4()), str(uuid4())
    identity = TurnIdentity.create(chat_id=chat_id, msgid=question_id)
    request = SimpleNamespace(
        channel="console",
        session_id="session-a",
        user_id="user-a",
        source_id="runner-integration",
        channel_meta={
            "answer_turn_identity": identity,
            "chat_id": chat_id,
            "source_id": "runner-integration",
        },
        execution_origin="interactive",
    )
    expert_registry = None
    if expert_pending is not None:
        expert_registry = ExpertMainRegistry()
        token = expert_registry.register(
            SimpleNamespace(),
            chat_id=chat_id,
            user_id="user-a",
            source_id="runner-integration",
            turn_id=question_id,
        )
        request.channel_meta[EXPERT_MAIN_TOKEN_KEY] = token
        lease = expert_registry.get(request)
        lease.agent = SimpleNamespace(
            _tool_guard_pending_info=(
                {"request_id": "approval-1"} if expert_pending else None
            ),
        )
    question = Msg("user", "private question body", "user")
    question.id = question_id

    async def preflight(**_kwargs):
        return SimpleNamespace(
            response=None,
            cleanup_denied_memory=False,
            approval_consumed=False,
        )

    async def successful_execution(_msgs, **_kwargs):
        execution.state["turn_states"][question_id] = {"status": "completed"}
        yield Msg("assistant", "private answer body", "assistant"), True

    owner = AgentRunner.__new__(AgentRunner)
    owner.agent_id = "default"
    owner.tenant_id = "tenant-a"
    owner._workspace = SimpleNamespace(conversation_storage=None)
    owner.session = Session()
    owner._answer_turn_coordinator = object() if coordinator_enabled else None
    owner._answer_turn_runtimes = {identity: (None, execution)}
    owner._answer_turn_locations = {}
    owner._agent_observability_contexts = {}
    if expert_registry is not None:
        owner._expert_main_executions = expert_registry
        owner._get_expert_main_execution = (
            AgentRunner._get_expert_main_execution.__get__(owner, AgentRunner)
        )
    else:
        owner._get_expert_main_execution = lambda _request: None
    owner._prepare_query_preflight = preflight
    owner._stream_query_after_preflight = successful_execution
    owner._answer_turn_identity = AgentRunner._answer_turn_identity
    return (
        AgentRunner,
        TurnOutcome,
        stream_admission,
        owner,
        request,
        question,
        identity,
    )


def _install_deterministic_provider(monkeypatch, *, streaming=False):
    from agentscope.model._model_response import ChatResponse
    from agentscope.model._model_usage import ChatUsage
    from swe.agents import model_factory
    from swe.observability.instrumentation import observe_openai_response
    from swe.tracing import agent_trace_model_wrapper
    from tests.integrated.critical_paths.conftest import (
        DeterministicChatModel,
        FakeProvider,
        FakeProviderManager,
    )

    class CopyableFakeProvider(FakeProvider):
        def __deepcopy__(self, _memo):
            return self

    responses = [
        ChatResponse(
            content=[{"type": "text", "text": "hello from the agent"}],
            usage=ChatUsage(input_tokens=3, output_tokens=4, time=0.0),
            metadata={"model": "response-model", "finish_reason": "stop"},
        ),
    ] * 4

    class ObservedDeterministicChatModel(DeterministicChatModel):
        async def __call__(self, *args, **kwargs):
            def observe():
                observe_openai_response(
                    SimpleNamespace(
                        model="response-model",
                        choices=[SimpleNamespace(finish_reason="stop")],
                        usage=SimpleNamespace(
                            prompt_tokens=3,
                            completion_tokens=4,
                        ),
                    ),
                )

            if not self.stream:
                observe()
                return await super().__call__(*args, **kwargs)
            self.calls.append({"args": args, "kwargs": kwargs})
            item = self.responses.pop(0)

            async def chunks():
                observe()
                yield item

            return chunks()

    model = ObservedDeterministicChatModel(responses, stream=streaming)
    provider_manager = FakeProviderManager(CopyableFakeProvider(model))

    async def get_provider_manager(_tenant_id=None):
        return provider_manager

    async def refresh_provider_manager():
        return None

    provider_manager.refresh_if_due = refresh_provider_manager
    monkeypatch.setattr(
        model_factory.ProviderManager,
        "ensure_tenant_provider_storage",
        lambda _tenant_id: None,
    )
    monkeypatch.setattr(
        model_factory.ProviderManager,
        "get_instance",
        lambda _tenant_id=None: provider_manager,
    )
    monkeypatch.setattr(
        model_factory.ProviderManager,
        "get_or_create_instance",
        get_provider_manager,
    )
    monkeypatch.setattr(
        agent_trace_model_wrapper,
        "current_capture",
        lambda: None,
    )
    return model


def _register_expert_main_for_request(
    runner,
    request,
    workspace_dir,
    chat_id,
    user_id,
    source_id,
    turn_id,
):
    from swe.agents.skill_runtime_snapshot import (
        ManifestStat,
        WorkspaceSkillSnapshot,
    )
    from swe.app.subagents.models import BudgetConfig, PermissionPolicy

    prepared = SimpleNamespace(
        expert_id="integration-expert",
        expert_name="Integration Expert",
        binding_revision=1,
        instruction="Answer as the integration expert.",
        permission=PermissionPolicy.readonly(),
        budget=BudgetConfig(max_turns=2, max_tool_calls=1, timeout_ms=10_000),
        skill_snapshot_dirs=(),
        mcp_configs={},
        model_slot=None,
        cleanup=lambda: None,
        workspace_skill_snapshot=WorkspaceSkillSnapshot(
            workspace_dir=workspace_dir,
            generation=0,
            manifest_stat=ManifestStat(0, 0, 0),
            skills={},
        ),
    )
    request.channel_meta["_expert_main_execution_token"] = (
        runner.register_expert_main_execution(
            prepared,
            chat_id=chat_id,
            user_id=user_id,
            source_id=source_id,
            turn_id=turn_id,
        )
    )


async def _emit_model_failure_records(monkeypatch, failure_mode):
    from swe.observability import Scope, bind_context
    from swe.observability.instrumentation import (
        ObservabilityModelWrapper,
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
        scope=Scope("swe", "runner-integration", "tenant-a", "agent-a"),
        business_ref={
            "chat_id": "model-failure-chat",
            "root_question_msgid": "question-a",
        },
        enabled=True,
    )
    attempt = observer.start_attempt(context)
    source_closed = False

    class FailingModel:
        model_name = "test-model"
        stream = failure_mode != "request"

        async def __call__(self, *_args, **_kwargs):
            nonlocal source_closed
            if failure_mode == "request":
                raise RuntimeError("private provider failure detail")

            async def chunks():
                nonlocal source_closed
                try:
                    yield SimpleNamespace(
                        content=[
                            {"type": "text", "text": "private partial output"},
                        ],
                        usage=(
                            SimpleNamespace(input_tokens=10, output_tokens=0)
                            if failure_mode == "cancelled"
                            else None
                        ),
                    )
                    if failure_mode == "cancelled":
                        raise asyncio.CancelledError
                    raise RuntimeError("private stream failure detail")
                finally:
                    source_closed = True

            return chunks()

    async def on_attempt(index, streaming):
        record_model_request_attempt(index, streaming=streaming)

    wrapped = ObservabilityModelWrapper(
        RetryChatModel(
            FailingModel(),
            retry_config=RetryConfig(enabled=False, max_retries=1),
            rate_limit_config=RateLimitConfig(max_qpm=0),
            on_request_attempt=on_attempt,
        ),
        provider_key="test-provider",
        model_key="test-model",
    )
    with bind_context(attempt):
        try:
            result = await wrapped([{"text": "private prompt body"}])
            async for _chunk in result:
                pass
        except (RuntimeError, asyncio.CancelledError) as exc:
            assert isinstance(exc, asyncio.CancelledError) == (
                failure_mode == "cancelled"
            )
        else:
            raise AssertionError("model failure did not propagate")
        status = "cancelled" if failure_mode == "cancelled" else "failed"
        observer.end_attempt(attempt, status=status)
        observer.end_run(context, status=status)
    assert emitter.close(timeout=1) == 0
    records = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert all(
        _validate_payload(json.dumps(record, ensure_ascii=False))
        for record in records
    )
    assert all(
        private not in sink.getvalue()
        for private in (
            "private provider failure detail",
            "private stream failure detail",
            "private partial output",
            "private prompt body",
        )
    )
    if failure_mode != "request":
        assert source_closed

    return records, context.obs_run_id


async def _project_model_failure(connection, records, run_id, failure_mode):
    store = ObservabilityStore(connection)
    projector = InboxProjector(store, poll_seconds=0.01)
    async with connection.transaction() as session:
        for record in records:
            await session.insert(
                "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                (json.dumps(record, separators=(",", ":")),),
            )
    while await projector.project_batch():
        pass
    async with connection.acquire() as session:
        run = await session.fetch_one(
            "SELECT status,ended_at,llm_calls,input_tokens,output_tokens "
            "FROM swe_agentobs_runs WHERE run_id=%s",
            (run_id,),
        )
        llm_span = await session.fetch_one(
            "SELECT status,technical_status,error_type,streaming "
            "FROM swe_agentobs_spans WHERE run_id=%s AND span_type='LLM' "
            "AND operation='chat'",
            (run_id,),
        )
        request_attempt = await session.fetch_one(
            "SELECT status,error_type FROM swe_agentobs_spans "
            "WHERE run_id=%s AND operation='request_attempt'",
            (run_id,),
        )
    cancelled = failure_mode == "cancelled"
    status = "cancelled" if cancelled else "failed"
    assert run["status"] == status
    assert run["ended_at"] is not None
    assert run["llm_calls"] == 1
    assert run["input_tokens"] == (10 if cancelled else None)
    assert run["output_tokens"] == (0 if cancelled else None)
    assert llm_span["status"] == status
    assert llm_span["streaming"] == int(failure_mode != "request")
    assert request_attempt["status"] == status
    if not cancelled:
        assert llm_span["technical_status"] == "ERROR"
        assert llm_span["error_type"] == "RuntimeError"
        assert request_attempt["error_type"] == "RuntimeError"
    return store


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["request", "stream", "cancelled"])
async def test_model_failure_projects_through_mysql_and_authorized_api(
    monkeypatch,
    failure_mode,
):
    monkeypatch.setenv(
        "SWE_AGENTOBS_CURSOR_SECRET",
        "runner-integration-secret-at-least-32",
    )
    records, run_id = await _emit_model_failure_records(
        monkeypatch,
        failure_mode,
    )
    connection = DatabaseConnection(_config())
    await connection.connect()
    try:
        store = await _project_model_failure(
            connection,
            records,
            run_id,
            failure_mode,
        )
        api = FastAPI()
        api.state.agent_observability_store = store

        @api.middleware("http")
        async def bind_source(request: Request, call_next):
            request.state.source_id = "runner-integration"
            request.state.effective_tenant_id = records[0]["scope"][
                "tenant_id"
            ]
            return await call_next(request)

        api.include_router(observability_router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api),
            base_url="http://runner.test",
        ) as client:
            response = await client.get(
                f"/agent-observability/runs/{run_id}",
                headers={"X-User-Role": "manager"},
            )
        assert response.status_code == 200
        run = response.json()["run"]
        cancelled = failure_mode == "cancelled"
        status = "cancelled" if cancelled else "failed"
        assert run["status"] == status
        assert run["ended_at"]
        assert run["llm_calls"] == 1
        assert run["request_attempt_count"] == 1
        assert run["input_tokens"] == (10 if cancelled else None)
        assert run["output_tokens"] == (0 if cancelled else None)
        api_span = next(
            span
            for span in response.json()["spans"]
            if span["span_type"] == "LLM" and span["operation"] == "chat"
        )
        api_request_attempt = next(
            span
            for span in response.json()["spans"]
            if span["span_type"] == "LLM"
            and span["operation"] == "request_attempt"
        )
        assert api_span["status"] == status
        assert api_request_attempt["status"] == status
        request_end = next(
            record
            for record in records
            if record["event_name"] == "span.ended"
            and record.get("operation") == "request_attempt"
        )
        assert (
            api_request_attempt["request_attempt_index"]
            == request_end["request_attempt_index"]
        )
        assert api_request_attempt["total_tokens"] == (
            10 if cancelled else None
        )
        if not cancelled:
            assert api_span["technical_status"] == "ERROR"
            assert api_span["error_type"] == "RuntimeError"
            assert api_request_attempt["error_type"] == "RuntimeError"
        assert "metadata_text" not in api_span
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_run_without_model_span_keeps_usage_summary_unknown(
    monkeypatch,
):
    monkeypatch.setenv(
        "SWE_AGENTOBS_CURSOR_SECRET",
        "runner-integration-secret-at-least-32",
    )
    sink = StringIO()
    emitter = JsonlEmitter(stream=sink)
    observer = Observability(emitter)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )
    context = observer.admit_run(
        scope=Scope(
            "swe",
            "runner-integration",
            "tenant-a",
            "agent-a",
        ),
        enabled=True,
    )
    attempt = observer.start_attempt(context)
    observer.end_attempt(attempt, status="succeeded")
    observer.end_run(context, status="succeeded")
    assert emitter.close(timeout=1) == 0
    records = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert not any(
        record.get("event_name") == "span.ended"
        and record.get("span_type") == "LLM"
        and record.get("operation") == "chat"
        for record in records
    )
    connection = DatabaseConnection(_config())
    await connection.connect()
    try:
        await _assert_projected_run_available(connection, records)
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "coordinator_enabled,expert_pending,expected_status",
    [
        pytest.param(True, None, "succeeded", id="coordinator-settlement"),
        pytest.param(False, None, "succeeded", id="legacy-settlement"),
        pytest.param(True, False, "succeeded", id="expert-main-success"),
        pytest.param(
            True,
            True,
            "waiting_approval",
            id="expert-main-waiting-approval",
        ),
    ],
)
async def test_runner_admission_settlement_emits_and_projects_one_run(
    monkeypatch,
    coordinator_enabled,
    expert_pending,
    expected_status,
):
    monkeypatch.setenv("SWE_ALLOW_MISSING_TRACE_SDK", "true")
    monkeypatch.setenv("SWE_AGENTOBS_ENABLED", "true")
    monkeypatch.setenv(
        "SWE_AGENTOBS_CURSOR_SECRET",
        "runner-integration-secret-at-least-32",
    )

    from swe.observability import runner_lifecycle

    sink = StringIO()
    emitter = JsonlEmitter(stream=sink)
    monkeypatch.setattr(
        runner_lifecycle,
        "_OBSERVABILITY",
        Observability(),
    )
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )
    connection = DatabaseConnection(_config())
    await connection.connect()

    (
        AgentRunner,
        TurnOutcome,
        stream_admission,
        owner,
        request,
        question,
        identity,
    ) = _make_runner_case(coordinator_enabled, expert_pending)

    try:
        frames = [
            frame
            async for frame in stream_admission(
                owner,
                [question],
                request=request,
                query="private question body",
                session_id="session-a",
                user_id="user-a",
            )
        ]
        assert len(frames) == 1 and frames[0][1] is True
        if coordinator_enabled:
            await AgentRunner.persist_outcome(
                owner,
                TurnOutcome.completed(identity),
            )
            assert not owner._agent_observability_contexts

        assert emitter.close(timeout=1) == 0
        records = [json.loads(line) for line in sink.getvalue().splitlines()]
        event_names = [record["event_name"] for record in records]
        assert event_names[:4] == [
            "run.accepted",
            "asset.used",
            "run.state",
            "attempt.started",
        ]
        if expected_status == "waiting_approval":
            assert event_names[-1] == "attempt.ended"
            assert "run.ended" not in event_names
            assert any(
                record.get("status") == "waiting_approval"
                for record in records
            )
        else:
            assert event_names[-2:] == ["attempt.ended", "run.ended"]
        assert all(
            _validate_payload(json.dumps(record, ensure_ascii=False))
            for record in records
        )
        assert all(
            "private question body" not in line
            and "private answer body" not in line
            for line in sink.getvalue().splitlines()
        )

        await _assert_projected_run_available(
            connection,
            records,
            expected_status=expected_status,
        )
    finally:
        emitter.close(timeout=1)
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expert_main,agent_retry,streaming",
    [
        pytest.param(False, False, False, id="standard"),
        pytest.param(True, False, False, id="expert-main"),
        pytest.param(False, True, False, id="agent-retry"),
        pytest.param(False, False, True, id="streaming"),
    ],
)
async def test_real_agent_runner_question_emits_and_projects_one_run(
    monkeypatch,
    tmp_path,
    expert_main,
    agent_retry,
    streaming,
):
    monkeypatch.setenv("SWE_ALLOW_MISSING_TRACE_SDK", "true")
    monkeypatch.setenv("SWE_AGENTOBS_ENABLED", "true")
    monkeypatch.setenv(
        "SWE_AGENTOBS_CURSOR_SECRET",
        "runner-integration-secret-at-least-32",
    )

    from swe.app.answer_turn.models import TurnIdentity
    from swe.app.runner.runner import AgentRunner
    from swe.observability import runner_lifecycle

    workspace_dir = _create_agent_workspace(tmp_path, monkeypatch)
    model = _install_deterministic_provider(monkeypatch, streaming=streaming)

    sink = StringIO()
    emitter = JsonlEmitter(stream=sink)
    monkeypatch.setattr(
        runner_lifecycle,
        "_OBSERVABILITY",
        Observability(),
    )
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )
    connection = DatabaseConnection(_config())
    await connection.connect()

    class PersistingCoordinator:
        runner = None

        async def status(self, _identity):
            from swe.app.answer_turn.models import TurnStatus

            return TurnStatus.RUNNING

        async def settle(self, outcome):
            await self.runner.persist_outcome(outcome)

    coordinator = PersistingCoordinator()
    runner = AgentRunner(
        agent_id="critical-agent",
        workspace_dir=workspace_dir,
        answer_turn_coordinator=coordinator,
    )
    coordinator.runner = runner
    if agent_retry:
        retry_state = _inject_agent_retry(runner)
    question_id = str(uuid4())
    chat_id = str(uuid4())
    identity = TurnIdentity.create(chat_id=chat_id, msgid=question_id)
    question = Msg("user", "Return a short greeting.", "user")
    question.id = question_id
    request = SimpleNamespace(
        channel="console",
        session_id="real-runner-session",
        user_id="user-a",
        source_id="runner-integration",
        execution_origin="interactive",
        channel_meta={
            "answer_turn_identity": identity,
            "chat_id": chat_id,
            "source_id": "runner-integration",
        },
    )
    if expert_main:
        _register_expert_main_for_request(
            runner,
            request,
            workspace_dir,
            chat_id,
            "user-a",
            "runner-integration",
            question_id,
        )

    try:
        await runner.start()
        outputs = [
            item
            async for item in runner.query_handler(
                [question],
                request=request,
            )
        ]
        assert (
            outputs
            and model.calls
            and (retry_state["calls"] == 2 if agent_retry else True)
            and not runner._agent_observability_contexts
        )

        assert emitter.close(timeout=1) == 0
        records = [json.loads(line) for line in sink.getvalue().splitlines()]
        _assert_runner_lifecycle_records(records, agent_retry=agent_retry)
        if expert_main:
            assert any(
                getattr(message, "metadata", {})
                .get(
                    "execution_identity",
                    {},
                )
                .get("kind")
                == "expert_main"
                for message, _last in outputs
            )
        assert all(
            "Return a short greeting." not in line
            and "hello from the agent" not in line
            for line in sink.getvalue().splitlines()
        )
        await _assert_projected_run_available(connection, records)
    finally:
        emitter.close(timeout=1)
        await runner.stop()
        await connection.close()

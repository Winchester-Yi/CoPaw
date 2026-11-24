# -*- coding: utf-8 -*-
"""Real Runner to MySQL evidence API with a deterministic model and tool."""

import json
import os
import asyncio
from pathlib import Path
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from agentscope.message import Msg
from agentscope.model._model_response import ChatResponse
from agentscope.model._model_usage import ChatUsage
from fastapi import FastAPI, Request

from swe.app.answer_turn.models import TurnIdentity
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.runner.manager import ChatManager
from swe.app.runner.models import ChatSpec
from swe.app.runner.repo.json_repo import JsonChatRepository
from swe.app.runner.runner import AgentRunner
from swe.app.workspace.workspace import Workspace
from swe.database import DatabaseConnection
from swe.observability import runner_lifecycle
from swe.observability.emitter import JsonlEmitter
from swe.observability.instrumentation import Observability
from swe.observability.outbox_recovery import (
    OutboxRecoveryStore,
    OutboxRecoveryScheduler,
    clear_registered_scopes,
    enable_registered_scopes,
    scope_digest,
)
from swe.observability.projector import InboxProjector
from swe.observability.query import router
from swe.observability.store import ObservabilityStore
from tests.integrated.observability.test_mysql_pipeline import _test_db_config
from tests.integrated.observability.test_runner_lifecycle_mysql import (
    _create_agent_workspace,
    _install_deterministic_provider,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("SWE_AGENTOBS_TEST_DB_HOST"),
    reason="requires disposable MySQL *_test schema",
)


async def run_full_recovery_cycle(worker, expected_refs):
    """Finish a cycle after verifying the specific committed occurrences."""
    seen = set()
    original = worker.scanner.verified_event

    async def track(record):
        event = await original(record)
        if event is not None:
            seen.add(event.get("reference_id"))
        return event

    worker.scanner.verified_event = track
    try:
        for _ in range(32):
            await worker.run_once()
            row = await worker.store.connection.fetch_one(
                "SELECT scan_cursor FROM swe_agentobs_outbox_recovery WHERE scope_key=%s",
                (worker.store.scope_key,),
            )
            if (
                row is not None
                and row["scan_cursor"] is None
                and expected_refs <= seen
            ):
                return
        raise AssertionError(
            "recovery did not revisit all expected references",
        )
    finally:
        worker.scanner.verified_event = original


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "memory_add", "session_commit"])
async def test_runner_tool_answer_and_question_evidence_mysql(
    tmp_path,
    monkeypatch,
    failure,
):
    # Preserve the end-to-end Runner, recovery, and authorization scenario.
    # pylint: disable=too-many-statements
    monkeypatch.setenv("SWE_ALLOW_MISSING_TRACE_SDK", "true")
    monkeypatch.setenv("SWE_AGENTOBS_ENABLED", "true")
    monkeypatch.setenv("CMB_CAAS_SERVICEUNITID", "swe")
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_KEY_ID", "test")
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_SECRET", "s" * 32)
    monkeypatch.setenv(
        "SWE_AGENTOBS_CURSOR_SECRET",
        "runner-evidence-integration-secret-32",
    )
    workspace_dir = _create_agent_workspace(tmp_path, monkeypatch)
    model = _install_deterministic_provider(monkeypatch)
    model.responses = [
        ChatResponse(
            content=[
                {
                    "type": "tool_use",
                    "id": "call-test",
                    "name": "get_current_time",
                    "input": {},
                },
            ],
            usage=ChatUsage(input_tokens=3, output_tokens=4, time=0.0),
        ),
        ChatResponse(
            content=[{"type": "text", "text": "private assistant answer"}],
            usage=ChatUsage(input_tokens=3, output_tokens=4, time=0.0),
        ),
    ]
    sink = StringIO()
    emitter = JsonlEmitter(stream=sink)
    monkeypatch.setattr(runner_lifecycle, "_OBSERVABILITY", Observability())
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )

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
    manager = ChatManager(
        repo=JsonChatRepository(workspace_dir / "chats.json"),
    )
    runner.set_chat_manager(manager)
    workspace = Workspace("critical-agent", str(workspace_dir))
    workspace.source_id = "runner-integration"
    workspace._service_manager.services.update(
        runner=runner,
        chat_manager=manager,
    )
    runner._workspace = workspace
    monkeypatch.setattr(
        "swe.app.runner.api.get_workspace",
        AsyncMock(return_value=workspace),
    )
    chat_id, question_id = str(uuid4()), str(uuid4())
    await manager.create_chat(
        ChatSpec(
            id=chat_id,
            session_id="session",
            user_id="user",
            meta={
                "source_id": "runner-integration",
                "agent_id": "critical-agent",
            },
        ),
    )
    identity = TurnIdentity.create(chat_id=chat_id, msgid=question_id)
    question = Msg("user", "private user question", "user")
    question.id = question_id
    request = SimpleNamespace(
        channel="console",
        session_id="session",
        user_id="user",
        source_id="runner-integration",
        execution_origin="interactive",
        channel_meta={
            "answer_turn_identity": identity,
            "chat_id": chat_id,
            "source_id": "runner-integration",
        },
    )
    db = DatabaseConnection(_test_db_config())
    await db.connect()
    clear_registered_scopes()
    enable_registered_scopes()
    try:
        registered_scope = WorkspaceScope(
            "default",
            "runner-integration",
            "critical-agent",
            service_unit_id="swe",
        )
        recovery_store = OutboxRecoveryStore(
            db,
            scope_digest(registered_scope),
        )
        await recovery_store.register_scope(
            registered_scope,
            str(workspace_dir),
            "legacy",
        )
        injected_refs = set()
        injection_hits = []
        if failure == "memory_add":
            from agentscope.memory import InMemoryMemory

            original_add = InMemoryMemory.add

            async def fail_tool_add(memory, messages, *args, **kwargs):
                values = messages if isinstance(messages, list) else [messages]
                if any(
                    getattr(message, "role", None) == "system"
                    for message in values
                ):
                    for message in values:
                        metadata = getattr(message, "metadata", {}) or {}
                        if metadata.get("agent_observability_evidence_ref"):
                            injected_refs.add(
                                metadata["agent_observability_evidence_ref"],
                            )
                    injection_hits.append("memory_add")
                    raise RuntimeError("injected memory add failure")
                return await original_add(memory, messages, *args, **kwargs)

            monkeypatch.setattr(InMemoryMemory, "add", fail_tool_add)
        if failure == "session_commit":
            from swe.app.runner.session import SessionExecution

            original_commit = SessionExecution.commit_state

            async def reject_agent_state(execution, state):
                if state.get("agent", {}).get("memory", {}).get("content"):
                    for entry in state["agent"]["memory"]["content"]:
                        metadata = entry[0].get("metadata", {})
                        if metadata.get("agent_observability_evidence_ref"):
                            injected_refs.add(
                                metadata["agent_observability_evidence_ref"],
                            )
                    injection_hits.append("session_commit")
                    raise OSError("injected session commit failure")
                return await original_commit(execution, state)

            monkeypatch.setattr(
                SessionExecution,
                "commit_state",
                reject_agent_state,
            )
        crash_marker = os.getenv("SWE_TEST_CRASH_MARKER")
        if crash_marker and failure is None:
            from swe.app.runner.session import SessionExecution

            original_commit = SessionExecution.commit_state

            async def pause_at_commit(execution, state):
                entries = (
                    state.get("agent", {}).get("memory", {}).get("content", [])
                )
                refs = [
                    entry[0]
                    .get("metadata", {})
                    .get("agent_observability_evidence_ref")
                    for entry in entries
                ]
                refs = [ref for ref in refs if ref]
                if not refs:
                    return await original_commit(execution, state)
                if os.environ["SWE_TEST_CRASH_PHASE"] == "after":
                    await original_commit(execution, state)
                marker_path = Path(crash_marker)
                pending_marker = marker_path.with_suffix(".tmp")
                pending_marker.write_text(
                    json.dumps(
                        {
                            "phase": os.environ["SWE_TEST_CRASH_PHASE"],
                            "refs": refs,
                            "chat_id": chat_id,
                            "message_ids": {
                                entry[0]
                                .get("metadata", {})
                                .get(
                                    "agent_observability_evidence_ref",
                                ): entry[0]
                                .get("id")
                                for entry in entries
                                if entry[0]
                                .get("metadata", {})
                                .get("agent_observability_evidence_ref")
                            },
                            "workspace": str(workspace_dir),
                            "config": str(tmp_path / "config.json"),
                        },
                    ),
                )
                os.replace(pending_marker, marker_path)
                await asyncio.Event().wait()

            monkeypatch.setattr(
                SessionExecution,
                "commit_state",
                pause_at_commit,
            )
        await runner.start()
        outputs = []
        try:
            outputs = [
                item
                async for item in runner.query_handler(
                    [question],
                    request=request,
                )
            ]
        except (RuntimeError, OSError) as exc:
            expected = {
                "memory_add": "injected memory add failure",
                "session_commit": "injected session commit failure",
            }
            if failure is None or expected[failure] not in str(exc):
                raise
        if failure:
            assert injection_hits and set(injection_hits) == {failure}
            assert injected_refs
            assert emitter.close(timeout=2) == 0
            failed_events = [
                json.loads(line) for line in sink.getvalue().splitlines()
            ]
            pending = [
                event
                for event in failed_events
                if event.get("event_type") == "evidence.pending"
            ]
            targets = [
                event
                for event in pending
                if event.get("reference_id") in injected_refs
            ]
            assert {
                event["reference_id"] for event in targets
            } == injected_refs
            assert not any(
                event.get("event_type") == "evidence.linked"
                and event.get("reference_id") in injected_refs
                for event in failed_events
            )
            async with db.transaction() as sql:
                for event in failed_events:
                    await sql.insert(
                        "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                        (json.dumps(event),),
                    )
            failed_store = ObservabilityStore(db)
            failed_projector = InboxProjector(failed_store, poll_seconds=0.01)
            while await failed_projector.project_batch():
                pass
            for target in targets:
                refs = await failed_store.list_span_evidence_refs(
                    run_id=target["obs_run_id"],
                    span_id=target["obs_span_id"],
                    source_ids=["runner-integration"],
                )
                assert any(
                    ref.get("reference_id") == target["reference_id"]
                    and ref["saved_state"] == "pending"
                    for ref in refs
                )
            from swe.observability import outbox_runtime

            monkeypatch.setattr(
                outbox_runtime,
                "get_tenant_storage_config_path",
                lambda _: tmp_path / "config.json",
            )
            recovery_sink = StringIO()
            recovery_emitter = JsonlEmitter(stream=recovery_sink)
            worker = await outbox_runtime.recovery_worker_for_scope(
                registered_scope,
                metadata_db=None,
                observability_db=db,
                emitter=recovery_emitter,
                expected_root=str(workspace_dir),
                expected_authority="legacy",
            )
            await worker.run_once()
            assert recovery_emitter.close(timeout=2) == 0
            assert recovery_sink.getvalue() == ""
            return
        assert outputs and len(model.calls) >= 2
        assert emitter.close(timeout=2) == 0
        records = [json.loads(line) for line in sink.getvalue().splitlines()]
        assert all(
            secret not in sink.getvalue()
            for secret in (
                "private user question",
                "private assistant answer",
            )
        )
        refs = {
            e["content_type"]: e
            for e in records
            if e.get("content_type")
            in {"user_question", "tool_output", "assistant_answer"}
        }
        assert set(refs) == {
            "user_question",
            "tool_output",
            "assistant_answer",
        }
        scope = records[0]["scope"]
        workspace.tenant_id = scope["tenant_id"]
        store = ObservabilityStore(db)
        async with db.transaction() as sql:
            for event in records:
                await sql.insert(
                    "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                    (json.dumps(event),),
                )
        projector = InboxProjector(store, poll_seconds=0.01)
        while await projector.project_batch():
            pass
        from swe.observability import outbox_runtime

        monkeypatch.setattr(
            outbox_runtime,
            "get_tenant_storage_config_path",
            lambda _: tmp_path / "config.json",
        )
        recovery_sink = StringIO()
        recovery_emitter = JsonlEmitter(stream=recovery_sink)

        async def resolve_worker(scope, root, authority):
            return await outbox_runtime.recovery_worker_for_scope(
                scope,
                metadata_db=None,
                observability_db=db,
                emitter=recovery_emitter,
                expected_root=root,
                expected_authority=authority,
            )

        await resolve_worker(registered_scope, str(workspace_dir), "legacy")
        scheduler = OutboxRecoveryScheduler(
            recovery_store,
            "swe",
            resolve_worker,
        )
        # Simulate a dead worker holding the lease after the Runner committed.
        crashed_owner = str(uuid4())
        assert await recovery_store.claim(crashed_owner, 30) is None
        assert await scheduler.run_once() == 1
        await db.execute(
            "UPDATE swe_agentobs_outbox_recovery SET lease_until=TIMESTAMPADD(SECOND,-1,UTC_TIMESTAMP(6)) WHERE scope_key=%s",
            (recovery_store.scope_key,),
        )
        assert await scheduler.run_once() == 1
        assert recovery_emitter.close(timeout=2) == 0
        recovered = [
            json.loads(line) for line in recovery_sink.getvalue().splitlines()
        ]
        assert {event.get("content_type") for event in recovered} == {
            "tool_output",
            "assistant_answer",
        }
        expected_refs = {event["reference_id"] for event in recovered}
        unacked_sink = StringIO()
        unacked_emitter = JsonlEmitter(stream=unacked_sink)
        recovery_emitter = unacked_emitter
        worker = await resolve_worker(
            registered_scope,
            str(workspace_dir),
            "legacy",
        )
        await run_full_recovery_cycle(worker, expected_refs)
        assert unacked_emitter.close(timeout=2) == 0
        assert {
            event["reference_id"]
            for event in map(json.loads, unacked_sink.getvalue().splitlines())
        } == expected_refs
        async with db.transaction() as sql:
            for event in recovered:
                await sql.insert(
                    "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                    (json.dumps(event),),
                )
        while await projector.project_batch():
            pass
        for event in recovered:
            assert await recovery_store.acknowledged(event["event_id"])
        replay_sink = StringIO()
        replay_emitter = JsonlEmitter(stream=replay_sink)
        recovery_emitter = replay_emitter
        worker = await resolve_worker(
            registered_scope,
            str(workspace_dir),
            "legacy",
        )
        await run_full_recovery_cycle(worker, expected_refs)
        assert replay_emitter.close(timeout=2) == 0
        assert replay_sink.getvalue() == ""
        app = FastAPI()
        app.state.agent_observability_store = store

        @app.middleware("http")
        async def bind_scope(req: Request, call_next):
            req.state.source_id = req.headers.get(
                "X-Test-Source",
                "runner-integration",
            )
            req.state.effective_tenant_id = req.headers.get(
                "X-Test-Tenant",
                scope["tenant_id"],
            )
            return await call_next(req)

        app.include_router(router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            for kind, expected in (
                ("user_question", "private user question"),
                ("tool_output", "get_current_time"),
                ("assistant_answer", "private assistant answer"),
            ):
                response = await client.get(
                    f"/agent-observability/runs/{run_id(records)}/spans/{refs[kind]['obs_span_id']}/evidence",
                    headers={"X-User-Role": "manager"},
                )
                assert response.status_code == 200
                item = next(
                    x
                    for x in response.json()["items"]
                    if x["content_type"] == kind
                )
                assert item["status"] == "available", (kind, item["status"])
                assert expected in json.dumps(item["message"])
                for header, value in (
                    ("X-Test-Source", "other-source"),
                    ("X-Test-Tenant", "other-tenant"),
                ):
                    denied = await client.get(
                        f"/agent-observability/runs/{run_id(records)}/spans/{refs[kind]['obs_span_id']}/evidence",
                        headers={"X-User-Role": "manager", header: value},
                    )
                    assert denied.status_code == 404
    finally:
        clear_registered_scopes()
        emitter.close(timeout=2)
        await runner.stop()
        await db.execute(
            "DELETE FROM swe_agentobs_outbox_recovery WHERE scope_key=%s",
            (recovery_store.scope_key,),
        )
        await db.execute(
            "DELETE FROM swe_agentobs_outbox_scopes WHERE scope_key=%s",
            (recovery_store.scope_key,),
        )
        await db.close()


def run_id(records):
    return records[0]["obs_run_id"]

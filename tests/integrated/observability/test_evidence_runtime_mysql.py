# -*- coding: utf-8 -*-
"""Persisted user-turn evidence through real MySQL projection and HTTP API."""

import asyncio
import json
import os
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from agentscope.message import Msg
from fastapi import FastAPI, Request

from swe.app.runner.manager import ChatManager
from swe.app.runner.models import ChatSpec
from swe.app.runner.repo.json_repo import JsonChatRepository
from swe.app.runner.session import SafeJSONSession
from swe.app.runner.session_lifecycle import admit_user_turn_with_creation
from swe.database import DatabaseConnection
from swe.observability import Scope, bind_context
from swe.observability.emitter import JsonlEmitter
from swe.observability.instrumentation import (
    Observability,
    ObservabilityModelWrapper,
)
from swe.observability.projector import InboxProjector
from swe.observability.query import router
from swe.observability.store import ObservabilityStore
from tests.integrated.observability.test_mysql_pipeline import _test_db_config

pytestmark = pytest.mark.skipif(
    not os.getenv("SWE_AGENTOBS_TEST_DB_HOST"),
    reason="set SWE_AGENTOBS_TEST_DB_HOST to a disposable MySQL *_test database",
)


@pytest.mark.asyncio
async def test_admitted_question_evidence_reads_committed_session_with_source_authorization(
    tmp_path,
    monkeypatch,
):
    # Keep the authorization checks in the same committed-session scenario.
    # pylint: disable=too-many-statements
    monkeypatch.setenv(
        "SWE_AGENTOBS_CURSOR_SECRET",
        "integration-evidence-secret-at-least-32",
    )
    chat_id, question_id = str(uuid4()), str(uuid4())
    session = SafeJSONSession(str(tmp_path / "sessions"))
    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    await manager.create_chat(
        ChatSpec(
            id=chat_id,
            session_id="session-a",
            user_id="user-a",
            meta={"source_id": "source-a", "agent_id": "agent-a"},
        ),
    )
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="agent-a",
        source_id="source-a",
        tenant_id="tenant-a",
        chat_manager=manager,
        runner=SimpleNamespace(session=session),
    )
    monkeypatch.setattr(
        "swe.app.runner.api.get_workspace",
        AsyncMock(return_value=workspace),
    )
    sink = StringIO()
    emitter = JsonlEmitter(stream=sink)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )
    obs = Observability(emitter)
    run = obs.admit_run(
        scope=Scope("swe", "source-a", "tenant-a", "agent-a"),
        business_ref={
            "chat_id": chat_id,
            "session_id": "session-a",
            "root_question_msgid": question_id,
        },
        enabled=True,
    )
    attempt = obs.start_attempt(run)
    question = Msg("user", "private question evidence", "user")
    question.id = question_id
    request = SimpleNamespace(channel_meta={"chat_id": chat_id})
    async with session.execution("session-a", "user-a") as execution:
        admitted, created, _ = await admit_user_turn_with_creation(
            execution,
            msgs=[question],
            request=request,
            observability_context=attempt,
        )
    assert (admitted, created) == (question_id, True)
    async with session.execution("session-a", "user-a") as execution:
        execution.state["turn_states"][question_id]["status"] = "completed"
        for index in range(70):
            execution.state["turn_states"][f"later-{index}"] = {
                "status": "completed",
                "message": {
                    "id": f"later-{index}",
                    "role": "user",
                    "content": "other",
                },
                "chat_id": chat_id,
            }
        await execution.commit_state(execution.state)
    await session.migrate_terminal_turns("session-a", "user-a", dry_run=False)
    state = await session.get_persisted_session_state_dict(
        "session-a",
        "user-a",
    )
    assert state["schema_version"] == 3
    assert question_id not in state["turn_states"]

    class Model:
        stream = False

        async def __call__(self, *_args, **_kwargs):
            return SimpleNamespace(
                id="response",
                content=[],
                usage=None,
                metadata={},
            )

    with bind_context(attempt):
        await ObservabilityModelWrapper(
            Model(),
            provider_key="test",
            model_key="test",
        )(
            [],
        )
    obs.end_attempt(attempt, status="succeeded")
    obs.end_run(attempt, status="succeeded")
    assert emitter.close(timeout=2) == 0
    events = [json.loads(line) for line in sink.getvalue().splitlines()]
    evidence = next(
        event
        for event in events
        if event.get("content_type") == "user_question"
    )
    assert "private question evidence" not in sink.getvalue()

    # Four concurrent API reads must have enough pool capacity and time for
    # the remote integration database; this test checks authorization/content.
    config = _test_db_config().model_copy(
        update={
            "max_connections": 5,
            "acquire_timeout": 20,
            "read_timeout": 20,
        },
    )
    db = DatabaseConnection(config)
    await db.connect()
    try:
        store = ObservabilityStore(db)
        async with db.transaction() as sql:
            for event in events:
                await sql.insert(
                    "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                    (json.dumps(event),),
                )
        projector = InboxProjector(store, poll_seconds=0.01)
        while await projector.project_batch():
            pass
        app = FastAPI()
        app.state.agent_observability_store = store

        @app.middleware("http")
        async def bind_scope(req: Request, call_next):
            req.state.source_id = req.headers.get("X-Source-Id")
            req.state.effective_tenant_id = req.headers.get(
                "X-Test-Tenant",
                "tenant-a",
            )
            return await call_next(req)

        app.include_router(router)
        path = f"/agent-observability/runs/{run.obs_run_id}/spans/{evidence['obs_span_id']}/evidence"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                path,
                headers={"X-User-Role": "manager", "X-Source-Id": "source-a"},
            )
            forbidden = await client.get(
                path,
                headers={"X-User-Role": "manager", "X-Source-Id": "source-b"},
            )
            admin = await client.get(
                path,
                headers={"X-User-Role": "admin", "X-Source-Id": "source-a"},
            )
            wrong_tenant = await client.get(
                path,
                headers={
                    "X-User-Role": "manager",
                    "X-Source-Id": "source-a",
                    "X-Test-Tenant": "tenant-b",
                },
            )
            concurrent = await asyncio.gather(
                *(
                    client.get(
                        path,
                        headers={
                            "X-User-Role": "manager",
                            "X-Source-Id": "source-a",
                        },
                    )
                    for _ in range(4)
                ),
            )
            workspace.agent_id = "other-agent"
            wrong_agent = await client.get(
                path,
                headers={"X-User-Role": "manager", "X-Source-Id": "source-a"},
            )
            workspace.agent_id = "agent-a"
            workspace.tenant_id = "tenant-b"
            wrong_workspace_tenant = await client.get(
                path,
                headers={"X-User-Role": "manager", "X-Source-Id": "source-a"},
            )
            workspace.tenant_id = "tenant-a"
            workspace.source_id = "source-b"
            wrong_workspace_source = await client.get(
                path,
                headers={"X-User-Role": "manager", "X-Source-Id": "source-a"},
            )
            workspace.source_id = "source-a"
            other_manager = ChatManager(
                repo=JsonChatRepository(tmp_path / "other-chats.json"),
            )
            await other_manager.create_chat(
                ChatSpec(
                    id=chat_id,
                    session_id="other-session",
                    user_id="user-a",
                    meta={"source_id": "source-a", "agent_id": "agent-a"},
                ),
            )
            workspace.chat_manager = other_manager
            wrong_session = await client.get(
                path,
                headers={"X-User-Role": "manager", "X-Source-Id": "source-a"},
            )
            workspace.chat_manager = manager
            async with session.execution("session-a", "user-a") as execution:
                await execution.hydrate_turn(question_id)
                execution.state["turn_states"][question_id]["chat_id"] = str(
                    uuid4(),
                )
                await execution.commit_state(execution.state)
            wrong_chat = await client.get(
                path,
                headers={"X-User-Role": "manager", "X-Source-Id": "source-a"},
            )
            async with session.execution("session-a", "user-a") as execution:
                await execution.hydrate_turn(question_id)
                execution.state["turn_states"][question_id][
                    "chat_id"
                ] = chat_id
                execution.state["turn_states"][question_id][
                    "agent_observability_context"
                ]["obs_run_id"] = str(uuid4())
                await execution.commit_state(execution.state)
            replaced = await client.get(
                path,
                headers={"X-User-Role": "manager", "X-Source-Id": "source-a"},
            )
        assert response.status_code == 200
        item = next(
            item
            for item in response.json()["items"]
            if item["content_type"] == "user_question"
        )
        assert item["status"] == "available"
        assert item["message"]["id"] == question_id
        assert "private question evidence" in json.dumps(item["message"])
        assert forbidden.status_code == 404
        assert wrong_tenant.status_code == 404
        assert all(
            r.status_code == 200 and "private question evidence" in r.text
            for r in concurrent
        )
        assert "private question evidence" not in wrong_agent.text
        for denied in (
            wrong_workspace_tenant,
            wrong_workspace_source,
            wrong_session,
            wrong_chat,
        ):
            assert denied.status_code == 200
            assert "private question evidence" not in denied.text
        assert admin.status_code == 200
        assert (
            next(
                item
                for item in admin.json()["items"]
                if item["content_type"] == "user_question"
            )["status"]
            == "available"
        )
        assert (
            next(
                item
                for item in replaced.json()["items"]
                if item["content_type"] == "user_question"
            )["status"]
            == "unresolved"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_tool_and_assistant_evidence_survive_archive_and_restart(
    tmp_path,
    monkeypatch,
):
    # The scenario exercises one evidence identity across archive and restart.
    # pylint: disable=too-many-statements
    import base64

    from swe.agents.memory.conversation_archive import (
        ConversationArchiveStore,
        attach_conversation_archive,
    )
    from swe.observability.instrumentation import bind_tool_span
    from tests.unit.agents.test_checkpoint_memory_attachment import _memory

    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_KEY_ID", "test")
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_SECRET", "s" * 32)
    monkeypatch.setattr(
        "swe.observability.outbox_recovery.scope_registered",
        lambda *_: True,
    )

    monkeypatch.setenv(
        "SWE_AGENTOBS_CURSOR_SECRET",
        "integration-evidence-secret-at-least-32",
    )
    chat_id = str(uuid4())
    session = SafeJSONSession(str(tmp_path / "sessions"))
    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    await manager.create_chat(
        ChatSpec(
            id=chat_id,
            session_id="session-a",
            user_id="user-a",
            meta={"source_id": "source-a", "agent_id": "agent-a"},
        ),
    )
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="agent-a",
        source_id="source-a",
        tenant_id="tenant-a",
        chat_manager=manager,
        runner=SimpleNamespace(session=session),
    )
    monkeypatch.setattr(
        "swe.app.runner.api.get_workspace",
        AsyncMock(return_value=workspace),
    )
    sink = StringIO()
    emitter = JsonlEmitter(stream=sink)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )
    obs = Observability(emitter)
    run = obs.admit_run(
        scope=Scope("swe", "source-a", "tenant-a", "agent-a"),
        business_ref={"chat_id": chat_id, "session_id": "session-a"},
        enabled=True,
    )
    attempt = obs.start_attempt(run)
    archive = ConversationArchiveStore(tmp_path / "dialog")
    memory = attach_conversation_archive(
        _memory(),
        tmp_path / "dialog",
        chat_id,
    )

    class Model:
        stream = False

        async def __call__(self, *_args, **_kwargs):
            return SimpleNamespace(
                id="invocation",
                content=[
                    {
                        "type": "text",
                        "text": "private assistant " + "x" * 50000,
                    },
                ],
                usage=None,
                metadata={},
            )

    with bind_context(attempt):
        tool_span = obs.span("Tool", "execute", name="tool")
        tool_message = Msg(
            "system",
            [
                {
                    "type": "tool_result",
                    "id": "call",
                    "name": "tool",
                    "output": [{"type": "text", "text": "private tool"}],
                },
            ],
            "system",
        )
        with bind_tool_span(tool_span):
            await memory.add(tool_message)
        tool_span.end(status="succeeded")
        response = await ObservabilityModelWrapper(
            Model(),
            provider_key="test",
            model_key="test",
        )([])
        answer = Msg(
            "assistant",
            response.content,
            "assistant",
            invocation_id=response.id,
        )
        await memory.add(answer)
    obs.end_attempt(attempt, status="succeeded")
    obs.end_run(attempt, status="succeeded")
    assert emitter.close(timeout=2) == 0
    events = [json.loads(line) for line in sink.getvalue().splitlines()]
    evidence = {
        event["content_type"]: event
        for event in events
        if event.get("event_type") == "evidence.pending"
    }
    assert set(evidence) == {"tool_output", "assistant_answer"}
    assert (
        "private tool" not in sink.getvalue()
        and "private assistant" not in sink.getvalue()
    )

    async def save_memory(messages):
        await session.save_merged_state(
            "session-a",
            user_id="user-a",
            state={
                "agent": {
                    "memory": {
                        "content": [[m.to_dict(), []] for m in messages],
                        "_compressed_summary": "",
                    },
                },
            },
        )

    await save_memory([])
    db = DatabaseConnection(_test_db_config())
    await db.connect()
    try:
        store = ObservabilityStore(db)
        async with db.transaction() as sql:
            for event in events:
                await sql.insert(
                    "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                    (json.dumps(event),),
                )
        projector = InboxProjector(store, poll_seconds=0.01)
        while await projector.project_batch():
            pass
        app = FastAPI()
        app.state.agent_observability_store = store

        @app.middleware("http")
        async def bind_scope(req: Request, call_next):
            req.state.source_id = req.headers.get("X-Source-Id")
            req.state.effective_tenant_id = req.headers.get(
                "X-Test-Tenant",
                "tenant-a",
            )
            return await call_next(req)

        app.include_router(router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            headers = {"X-User-Role": "manager", "X-Source-Id": "source-a"}

            def path(kind):
                return f"/agent-observability/runs/{run.obs_run_id}/spans/{evidence[kind]['obs_span_id']}/evidence"

            async def item(kind, **kwargs):
                result = await client.get(
                    path(kind),
                    headers=headers,
                    **kwargs,
                )
                assert result.status_code == 200
                return next(
                    x
                    for x in result.json()["items"]
                    if x["content_type"] == kind
                )

            assert (await item("tool_output"))["status"] == "pending"
            await save_memory([tool_message, answer])
            online_tool = await item("tool_output")
            assert online_tool["status"] == "available"
            assert "private tool" in json.dumps(online_tool["message"])
            assert "agent_observability_evidence_ref" not in json.dumps(
                online_tool["message"],
            )
            first = await item("assistant_answer")
            assert first["status"] == "truncated" and first["next_cursor"]
            second = await item(
                "assistant_answer",
                params={"cursor": first["next_cursor"]},
            )

            def decode(s):
                return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

            body = decode(first["message_chunk"]) + decode(
                second["message_chunk"],
            )
            assert "private assistant" in body.decode()
            assert b"agent_observability_evidence_ref" not in body
            assert (
                await client.get(
                    path("tool_output"),
                    headers={
                        "X-User-Role": "manager",
                        "X-Source-Id": "source-b",
                    },
                )
            ).status_code == 404
            assert (
                await client.get(
                    path("tool_output"),
                    headers={"X-User-Role": "user", "X-Source-Id": "source-a"},
                )
            ).status_code == 403
            await archive.commit(chat_id, [tool_message, answer])
            assert (await item("tool_output"))["status"] == "unresolved"
            await save_memory([])
            workspace.runner.session = SafeJSONSession(
                str(tmp_path / "sessions"),
            )
            workspace.chat_manager = ChatManager(
                repo=JsonChatRepository(tmp_path / "chats.json"),
            )
            archived_tool = await item("tool_output")
            assert archived_tool["status"] == "compacted"
            assert "private tool" in json.dumps(archived_tool["message"])
            assert "agent_observability_evidence_ref" not in json.dumps(
                archived_tool["message"],
            )
            archived_answer = await item("assistant_answer")
            assert (
                archived_answer["status"] == "compacted"
                and archived_answer["next_cursor"]
            )
            archived_tail = await item(
                "assistant_answer",
                params={"cursor": archived_answer["next_cursor"]},
            )
            assert (
                decode(archived_answer["message_chunk"])
                + decode(archived_tail["message_chunk"])
                == body
            )
            duplicate = Msg.from_dict(tool_message.to_dict())
            await archive.commit(chat_id, [duplicate])
            assert (await item("tool_output"))["status"] == "unresolved"
            await archive.delete_chat(chat_id)
            assert (await item("tool_output"))["status"] == "unavailable"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unregistered_scope_projects_not_saved_evidence(
    tmp_path,
    monkeypatch,
):
    from swe.observability.instrumentation import (
        bind_tool_span,
        mark_pending_message_evidence,
    )
    from swe.observability.outbox_recovery import clear_registered_scopes

    clear_registered_scopes()
    sink = StringIO()
    emitter = JsonlEmitter(stream=sink)
    obs = Observability(emitter)
    chat_id = str(uuid4())
    run = obs.admit_run(
        scope=Scope("swe", "source-a", "tenant-a", "agent-a"),
        business_ref={"chat_id": chat_id, "session_id": "session-a"},
        enabled=True,
    )
    attempt = obs.start_attempt(run)
    message = Msg(
        "system",
        [
            {
                "type": "tool_result",
                "id": "call",
                "name": "tool",
                "output": [{"type": "text", "text": "private"}],
            },
        ],
        "system",
    )
    with bind_context(attempt):
        span = obs.span("Tool", "execute", name="tool")
        with bind_tool_span(span):
            mark_pending_message_evidence(message)
        span.end(status="succeeded")
    obs.end_attempt(attempt, status="succeeded")
    obs.end_run(attempt, status="succeeded")
    assert emitter.close(timeout=2) == 0
    events = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert "private" not in sink.getvalue()
    assert not message.metadata.get("agent_observability_outbox")
    assert any(
        event.get("event_type") == "evidence.linked"
        and event.get("saved_state") == "not_saved"
        for event in events
    )
    db = DatabaseConnection(_test_db_config())
    await db.connect()
    try:
        store = ObservabilityStore(db)
        async with db.transaction() as sql:
            for event in events:
                await sql.insert(
                    "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                    (json.dumps(event),),
                )
        projector = InboxProjector(store, poll_seconds=0.01)
        while await projector.project_batch():
            pass
        app = FastAPI()
        app.state.agent_observability_store = store

        @app.middleware("http")
        async def bind_scope(req: Request, call_next):
            req.state.source_id = "source-a"
            req.state.effective_tenant_id = "tenant-a"
            return await call_next(req)

        app.include_router(router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                f"/agent-observability/runs/{run.obs_run_id}/spans/{span.span_id}/evidence",
                headers={"X-User-Role": "manager"},
            )
        assert response.status_code == 200
        item = next(
            item
            for item in response.json()["items"]
            if item["content_type"] == "tool_output"
        )
        assert item["status"] == "not_saved" and "message" not in item
    finally:
        await db.close()
        clear_registered_scopes()

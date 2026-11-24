# -*- coding: utf-8 -*-
import io
from uuid import uuid4

import pytest
from agentscope.memory import InMemoryMemory
from agentscope.message import Msg

from swe.agents.memory.conversation_archive import (
    ConversationArchiveStore,
    attach_conversation_archive,
)
from swe.app.runner.session import SafeJSONSession
from swe.observability import Observability, Scope, bind_context
from swe.observability.config import EmitterConfig
from swe.observability.emitter import JsonlEmitter
from swe.observability.instrumentation import (
    attach_evidence_capture,
    bind_tool_span,
)
from swe.observability.outbox import verify_event


@pytest.mark.asyncio
async def test_default_memory_persists_evidence_without_archive(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_KEY_ID", "test")
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_SECRET", "s" * 32)
    monkeypatch.setattr(
        "swe.observability.outbox_recovery.scope_registered",
        lambda *_: True,
    )
    memory = InMemoryMemory()
    attach_evidence_capture(memory)
    attach_evidence_capture(memory)
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    context = Observability().prepare_run(
        scope=Scope("swe", "source", "tenant", "agent"),
        business_ref={"chat_id": str(uuid4())},
        enabled=True,
    )
    message = Msg(
        "system",
        [{"type": "tool_result", "id": "call", "name": "tool", "output": []}],
        "system",
    )
    with bind_context(context):
        span = Observability(emitter).span("TOOL", "tool.execute")
        with bind_tool_span(span):
            await memory.add(message)
    session = SafeJSONSession(str(tmp_path / "sessions"))
    async with session.execution("session", "user") as execution:
        await execution.commit_state(
            {"agent": {"memory": memory.state_dict()}},
        )
    root = await session.get_persisted_session_state_dict("session", "user")
    raw = root["agent"]["memory"]["content"][0][0]
    event = verify_event(raw["metadata"]["agent_observability_outbox"])
    assert event["message_id"] == message.id
    emitter.close()
    assert stream.getvalue().count('"event_type":"evidence.pending"') == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_version", [2, 3])
async def test_signed_tool_occurrence_survives_session_and_compaction(
    tmp_path,
    monkeypatch,
    schema_version,
):
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_KEY_ID", "test")
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_SECRET", "s" * 32)
    monkeypatch.setattr(
        "swe.observability.outbox_recovery.scope_registered",
        lambda *_: True,
    )
    if schema_version == 3:
        monkeypatch.setenv("SWE_SESSION_V3_TENANTS", "tenant")
        monkeypatch.setattr(
            "swe.config.context.get_current_tenant_id",
            lambda: "tenant",
        )
    chat_id = str(uuid4())
    memory = attach_conversation_archive(
        InMemoryMemory(),
        tmp_path / "dialog",
        chat_id,
    )
    session = SafeJSONSession(str(tmp_path / "sessions"))
    emitter = JsonlEmitter(EmitterConfig(), stream=io.StringIO())
    context = Observability().prepare_run(
        scope=Scope("swe", "source", "tenant", "agent"),
        business_ref={"chat_id": chat_id},
        enabled=True,
    )
    message = Msg(
        "system",
        [{"type": "tool_result", "id": "call", "name": "tool", "output": []}],
        "system",
    )
    with bind_context(context):
        span = Observability(emitter).span("tool", "call")
        with bind_tool_span(span):
            await memory.add(message)
    event = verify_event(message.metadata["agent_observability_outbox"])
    async with session.execution("session", "user") as execution:
        await execution.commit_state(
            {"agent": {"memory": memory.state_dict()}},
        )
    persisted = await session.get_persisted_session_state_dict(
        "session",
        "user",
    )
    assert persisted["schema_version"] == schema_version
    saved = persisted["agent"]["memory"]["content"][0][0]
    assert (
        verify_event(saved["metadata"]["agent_observability_outbox"]) == event
    )
    await memory.archive_compacted_messages([message])
    async with session.execution("session", "user") as execution:
        await execution.commit_state(
            {"agent": {"memory": memory.state_dict()}},
        )
    archived = await ConversationArchiveStore(
        tmp_path / "dialog",
    ).read_evidence_occurrence(
        chat_id,
        event["reference_id"],
        message.id,
        max_bytes=1024 * 1024,
    )
    assert archived["status"] == "available"
    assert (
        verify_event(
            archived["message"].metadata["agent_observability_outbox"],
        )
        == event
    )
    emitter.close()

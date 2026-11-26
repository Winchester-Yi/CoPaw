# -*- coding: utf-8 -*-
"""Exercise an external approval through the Guard and signed tool output."""

import json
from io import StringIO
from types import SimpleNamespace

import httpx
import pytest
from agentscope.memory import InMemoryMemory
from agentscope.message import Msg
from fastapi import FastAPI

from swe.agents.tool_guard_mixin import ToolGuardMixin
from swe.app.answer_turn.coordinator import AnswerTurnCoordinator
from swe.app.answer_turn.in_memory import (
    InMemoryApproval,
    InMemoryExecution,
    InMemoryGoal,
    InMemorySession,
    InMemorySubagent,
)
from swe.app.approvals.service import ApprovalService
from swe.app.routers import approvals as approvals_router
from swe.app.runner.manager import ChatManager
from swe.app.runner.query_execution.admission import _approval_continuation
from swe.app.runner.repo.json_repo import JsonChatRepository
from swe.app.runner.runner import AgentRunner
from swe.app.runner.session import SafeJSONSession
from swe.app.runner.task_tracker import TaskTracker
from swe.config.context import get_current_effective_tenant_id, tenant_context
from swe.observability import (
    Observability,
    Scope,
    bind_context,
    runner_lifecycle,
)
from swe.observability.emitter import JsonlEmitter
from swe.observability.instrumentation import attach_evidence_capture
from swe.observability.outbox import verify_event
from swe.observability.projector import _validate_payload
from swe.security.tool_guard.engine import ToolGuardEngine
from swe.security.tool_guard.guardians import BaseToolGuardian
from swe.security.tool_guard.models import (
    GuardFinding,
    GuardSeverity,
    GuardThreatCategory,
)

_SOURCE = "approval-integration"
_USER = "approval-owner"
_SESSION = "approval-session"
_QUESTION = "approval-root-question"
_TOOL_INPUT = "PRIVATE_INPUT.txt"
_TOOL_OUTPUT = "PRIVATE_OUTPUT"


class _RequireApproval(BaseToolGuardian):
    def guard(self, tool_name, params):
        return [
            GuardFinding(
                id="test-finding",
                rule_id="approval-required",
                tool_name=tool_name,
                category=GuardThreatCategory.SENSITIVE_FILE_ACCESS,
                severity=GuardSeverity.MEDIUM,
                title="Requires approval",
                description="Test guardian requires approval for this file.",
                guardian=self.name,
            ),
        ]


class _FileTool:
    async def _acting(self, tool_call):
        content = (
            self._workspace_dir / tool_call["input"]["file_path"]
        ).read_text()
        self.executed.append(tool_call)
        await self.memory.add(
            Msg(
                "system",
                [
                    {
                        "type": "tool_result",
                        "id": tool_call["id"],
                        "name": tool_call["name"],
                        "output": [{"type": "text", "text": content}],
                    },
                ],
                "system",
            ),
        )


class _GuardedAgent(ToolGuardMixin, _FileTool):
    def __init__(self, workspace, chat, tenant):
        self._workspace_dir = workspace
        self._agent_config = SimpleNamespace()
        self._request_context = {
            "session_id": _SESSION,
            "user_id": _USER,
            "tenant_id": tenant,
            "source_id": _SOURCE,
            "agent_id": "default",
            "chat_id": chat.id,
            "msgid": _QUESTION,
            "channel": "console",
        }
        self.memory = InMemoryMemory()
        attach_evidence_capture(self.memory)
        self.executed = []

    async def print(self, *_args, **_kwargs):
        pass


class _ApprovalChannel:
    def __init__(self, runner, agent):
        self.runner = runner
        self.agent = agent

    async def stream_one(self, payload):
        meta = payload["meta"]
        assert meta["approval_source_channel"] == "zhaohu"
        request = SimpleNamespace(
            session_id=meta["session_id"],
            user_id=payload["sender_id"],
            source_id=meta["source_id"],
            channel_meta=meta,
        )
        command = payload["content_parts"][0].text
        context, decision = await _approval_continuation(
            self.runner,
            request=request,
            session_id=request.session_id,
            user_id=request.user_id,
            query=command,
        )
        assert context is not None
        assert decision == "approved"
        response, consumed, restored_call = (
            await self.runner._resolve_pending_approval(
                request.session_id,
                command,
                request=request,
            )
        )
        assert response is None and consumed and restored_call is not None
        attempt = runner_lifecycle.start_query_attempt(context)
        with bind_context(attempt):
            await self.agent._acting(restored_call)
        async with self.runner.session.execution(_SESSION, _USER) as execution:
            state = execution.state
            state["agent"]["memory"]["content"].extend(
                self.agent.memory.state_dict()["content"],
            )
            await execution.commit_state(state)
        runner_lifecycle.finish_query_run(context, attempt, "completed")
        yield {"event": "complete"}


@pytest.mark.asyncio
async def test_external_approval_restores_guarded_tool_and_persists_evidence(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("SWE_TOOL_GUARD_TOOLS", "read_file")
    monkeypatch.setenv("SWE_TOOL_GUARD_DENIED_TOOLS", "unrelated_tool")
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_KEY_ID", "test")
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_SECRET", "s" * 32)
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_VERIFY_KEYS", "{}")
    engine = ToolGuardEngine([_RequireApproval("test-guardian")], enabled=True)
    monkeypatch.setattr(
        "swe.security.tool_guard.engine._engine_instance",
        engine,
    )
    service = ApprovalService()
    monkeypatch.setattr("swe.app.approvals.service._approval_service", service)
    (tmp_path / _TOOL_INPUT).write_text(_TOOL_OUTPUT)
    sink = StringIO()
    emitter = JsonlEmitter(stream=sink)
    observer = Observability(emitter)
    monkeypatch.setattr(runner_lifecycle, "_OBSERVABILITY", observer)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )

    try:
        with tenant_context(tenant_id="default", source_id=_SOURCE):
            await _exercise_external_approval(
                tmp_path,
                observer,
                service,
                monkeypatch,
            )
    finally:
        assert emitter.close() == 0

    assert _TOOL_INPUT not in sink.getvalue()
    assert _TOOL_OUTPUT not in sink.getvalue()
    records = [
        _validate_payload(line) for line in sink.getvalue().splitlines()
    ]
    _assert_approval_trace(records)


async def _exercise_external_approval(
    workspace,
    observer,
    service,
    monkeypatch,
):
    tenant = get_current_effective_tenant_id()
    tracker = TaskTracker()
    runner = AgentRunner(workspace_dir=workspace, task_tracker=tracker)
    runner.session = SafeJSONSession(str(workspace / "sessions"))
    chats = ChatManager(repo=JsonChatRepository(workspace / "chats.json"))
    chat = await chats.get_or_create_chat(_SESSION, _USER, "console")
    agent = _GuardedAgent(workspace, chat, tenant)
    channel = _ApprovalChannel(runner, agent)
    binding = runner_lifecycle._trusted_workspace_binding(runner)
    assert binding is not None
    monkeypatch.setattr(
        "swe.observability.outbox_recovery.scope_registered",
        lambda scope, actual: scope["source_id"] == _SOURCE
        and actual == binding,
    )
    coordinator = AnswerTurnCoordinator(
        stream=tracker,
        execution=InMemoryExecution(),
        session=InMemorySession(),
        goal=InMemoryGoal(),
        subagent=InMemorySubagent(),
        approval=InMemoryApproval(),
    )

    def get_channel(name):
        return channel if name == "console" else None

    async def get_workspace(_request):
        return SimpleNamespace(
            agent_id="default",
            channel_manager=SimpleNamespace(get_channel=get_channel),
            chat_manager=chats,
            task_tracker=tracker,
            runner=runner,
            answer_turn_coordinator=coordinator,
        )

    monkeypatch.setattr(
        approvals_router,
        "get_agent_for_request",
        get_workspace,
    )
    context = observer.admit_run(
        scope=Scope("swe", _SOURCE, tenant, "default"),
        enabled=True,
        business_ref={
            "chat_id": chat.id,
            "root_question_msgid": _QUESTION,
            "session_id": _SESSION,
        },
    )
    attempt = runner_lifecycle.start_query_attempt(context)
    tool_call = {
        "id": "call-approval",
        "name": "read_file",
        "input": {"file_path": _TOOL_INPUT},
    }
    with bind_context(attempt):
        await agent._acting(tool_call)
    assert not agent.executed
    pending = await service.get_request(
        agent._tool_guard_pending_info["request_id"],
    )
    assert pending.status == "pending"
    runner_lifecycle.finish_query_run(
        context,
        attempt,
        "completed",
        waiting_for_approval=True,
    )

    api = FastAPI()
    api.include_router(approvals_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://approval.test",
    ) as client:
        endpoint = f"/approvals/{pending.request_id}/approve"
        submit = {
            "source_channel": "zhaohu",
            "source_message_id": "external-message",
        }
        foreign = await client.post(
            endpoint,
            json=submit,
            headers={"X-User-Id": "other-user", "X-Source-Id": _SOURCE},
        )
        assert foreign.status_code == 403
        assert (
            await service.get_request(pending.request_id)
        ).status == "pending"
        headers = {"X-User-Id": _USER, "X-Source-Id": _SOURCE}
        result = await client.post(endpoint, json=submit, headers=headers)
        assert result.status_code == 200
        assert result.json()["status"] == "submitted"
        assert await tracker.wait_all_done(timeout=3)
        replay = await client.post(endpoint, json=submit, headers=headers)
        assert replay.status_code == 200
        assert replay.json()["submitted"] is False

    assert (await service.get_request(pending.request_id)).status == "approved"
    assert agent.executed == [tool_call]
    await _assert_saved_evidence(runner, context.obs_run_id)


async def _assert_saved_evidence(runner, run_id):
    state = await runner.session.get_persisted_session_state_dict(
        _SESSION,
        _USER,
    )
    messages = [item[0] for item in state["agent"]["memory"]["content"]]
    assert messages[0]["metadata"]["external_approval_message"] is True
    saved = next(
        message
        for message in messages
        if "agent_observability_outbox" in (message.get("metadata") or {})
    )
    evidence = verify_event(saved["metadata"]["agent_observability_outbox"])
    assert evidence["saved_state"] == "available"
    assert evidence["event_type"] == "evidence.linked"
    assert evidence["obs_run_id"] == run_id
    assert evidence["message_id"] == saved["id"]
    assert evidence["content_type"] == "tool_output"
    assert saved["content"][0]["output"][0]["text"] == _TOOL_OUTPUT


def _assert_approval_trace(records):
    assert len({record["obs_run_id"] for record in records}) == 1
    requested = [
        record
        for record in records
        if record["event_name"] == "approval.requested"
    ]
    resolved = [
        record
        for record in records
        if record["event_name"] == "approval.resolved"
    ]
    assert len(requested) == len(resolved) == 1
    assert (
        requested[0]["approval_request_id"]
        == resolved[0]["approval_request_id"]
    )
    assert resolved[0]["decision"] == "approved"
    attempts = [
        record for record in records if record["event_name"] == "attempt.ended"
    ]
    assert [record["status"] for record in attempts] == [
        "waiting_approval",
        "succeeded",
    ]
    guard, tool = [
        record for record in records if record["event_name"] == "span.ended"
    ]
    assert (guard["span_type"], guard["status"]) == (
        "Guard",
        "waiting_approval",
    )
    assert (tool["span_type"], tool["status"]) == ("Tool", "succeeded")
    assert guard["tool"]["tool_call_id"] == tool["tool"]["tool_call_id"]
    assert guard["obs_attempt_id"] != tool["obs_attempt_id"]
    pending = [
        record
        for record in records
        if record.get("event_type") == "evidence.pending"
    ]
    assert len(pending) == 1
    assert pending[0]["obs_span_id"] == tool["obs_span_id"]
    assert [
        record["status"]
        for record in records
        if record["event_name"] == "run.ended"
    ] == ["succeeded"]

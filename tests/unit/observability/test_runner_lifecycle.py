# -*- coding: utf-8 -*-
from types import SimpleNamespace
import json
import time

import pytest

from swe.observability import runner_lifecycle
from swe.observability.context import bind_context, current_context
from swe.observability import instrumentation
from swe.observability.instrumentation import Observability
from swe.observability.projector import _validate_payload
from swe.observability.models import ObservabilityContext, Scope
from swe.app.runner.session_lifecycle import admit_user_turn_with_creation
from swe.app.answer_turn.models import TurnIdentity


class RecordingEmitter:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event.to_dict())
        return True


def _setup(monkeypatch, tenant_id="tenant-a"):
    emitter = RecordingEmitter()
    monkeypatch.setattr(runner_lifecycle, "capture_enabled", lambda: True)
    monkeypatch.setattr(instrumentation, "capture_enabled", lambda: True)
    monkeypatch.setattr(
        runner_lifecycle,
        "get_current_source_id",
        lambda: "source-a",
    )
    monkeypatch.setattr(
        runner_lifecycle,
        "get_current_effective_tenant_id",
        lambda: tenant_id,
    )
    monkeypatch.setattr(
        runner_lifecycle,
        "_OBSERVABILITY",
        Observability(emitter=emitter),
    )
    return emitter


def _request():
    return SimpleNamespace(channel_meta={"chat_id": "chat-a"})


def _message():
    class Message:
        role = "user"
        id = "question-a"
        content = "private"

        def to_dict(self):
            return {"id": self.id, "role": self.role, "content": self.content}

    return Message()


def test_query_run_uses_safe_scope_and_emits_accepted_running_and_terminal(
    monkeypatch,
):
    emitter = _setup(monkeypatch)
    context = runner_lifecycle.admit_query_run(
        SimpleNamespace(agent_id="agent-a"),
        _request(),
        [_message()],
    )

    attempt = runner_lifecycle.start_query_attempt(context)
    with runner_lifecycle.bind_query_context(attempt):
        assert current_context() == attempt
    assert current_context() is None

    runner_lifecycle.finish_query_run(context, attempt, "completed")

    names = [event["event_name"] for event in emitter.events]
    assert names == [
        "run.accepted",
        "asset.used",
        "run.state",
        "attempt.started",
        "attempt.ended",
        "run.ended",
    ]
    accepted = emitter.events[0]
    assert accepted["scope"] == {
        "service_unit_id": "swe",
        "source_id": "source-a",
        "tenant_id": "tenant-a",
        "agent_id": "agent-a",
    }
    assert accepted["business_ref"] == {
        "chat_id": "chat-a",
        "root_question_msgid": "question-a",
    }
    assert all(
        "private" not in json.dumps(event, ensure_ascii=False)
        for event in emitter.events
    )
    assert emitter.events[-1]["status"] == "succeeded"
    assert all(
        _validate_payload(json.dumps(event, ensure_ascii=False))
        for event in emitter.events
    )


@pytest.mark.parametrize(
    ("admission_enabled", "later_enabled", "expected_events"),
    [
        (True, False, True),
        (False, True, False),
    ],
)
def test_query_run_keeps_admission_capture_snapshot(
    monkeypatch,
    admission_enabled,
    later_enabled,
    expected_events,
):
    emitter = _setup(monkeypatch)
    monkeypatch.setattr(
        runner_lifecycle,
        "capture_enabled",
        lambda: admission_enabled,
    )
    context = runner_lifecycle.admit_query_run(
        SimpleNamespace(agent_id="agent-a"),
        _request(),
        [_message()],
    )
    monkeypatch.setattr(
        runner_lifecycle,
        "capture_enabled",
        lambda: later_enabled,
    )
    attempt = runner_lifecycle.start_query_attempt(context)
    runner_lifecycle.finish_query_run(context, attempt, "completed")

    assert bool(emitter.events) is expected_events
    if context is not None:
        assert context.capture_enabled is admission_enabled


def test_query_waiting_for_approval_does_not_end_run(monkeypatch):
    emitter = _setup(monkeypatch)
    context = runner_lifecycle.admit_query_run(
        SimpleNamespace(agent_id="agent-a"),
        _request(),
        [_message()],
    )
    attempt = runner_lifecycle.start_query_attempt(context)

    runner_lifecycle.finish_query_run(
        context,
        attempt,
        "completed",
        waiting_for_approval=True,
    )

    assert [event["event_name"] for event in emitter.events] == [
        "run.accepted",
        "asset.used",
        "run.state",
        "attempt.started",
        "run.state",
        "attempt.ended",
    ]
    assert emitter.events[-2]["status"] == "waiting_approval"
    assert all(
        _validate_payload(json.dumps(event, ensure_ascii=False))
        for event in emitter.events
    )


def test_approval_facts_share_run_and_restore_only_matching_scope(monkeypatch):
    emitter = _setup(monkeypatch)
    owner = SimpleNamespace(agent_id="agent-a")
    approval_id = "550e8400-e29b-41d4-a716-446655440000"
    request = SimpleNamespace(
        session_id="session-a",
        user_id="user-a",
        source_id="source-a",
        channel_meta={"chat_id": "chat-a", "source_id": "source-a"},
    )
    context = runner_lifecycle.admit_query_run(
        owner,
        request,
        [_message()],
    )
    attempt = runner_lifecycle.start_query_attempt(context)
    with bind_context(attempt):
        carrier = runner_lifecycle.approval_context_carrier()
        runner_lifecycle.record_approval_requested(
            context,
            approval_id,
            started_at=1_794_700_800,
        )
        resolved_context = runner_lifecycle.record_approval_resolved(
            approval_id,
            "approved",
            started_at=1_794_700_800,
        )
    pending = SimpleNamespace(
        session_id="session-a",
        user_id="user-a",
        extra={
            "agent_id": "agent-a",
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "chat_id": "chat-a",
            "msgid": "question-a",
            "agent_observability_context": carrier,
        },
    )

    restored = runner_lifecycle.restore_approval_context(
        owner,
        request,
        pending,
    )
    wrong_pending = SimpleNamespace(
        session_id=pending.session_id,
        user_id=pending.user_id,
        extra={**pending.extra, "source_id": "other-source"},
    )
    denied = runner_lifecycle.restore_approval_context(
        owner,
        request,
        wrong_pending,
    )

    assert carrier["obs_run_id"] == context.obs_run_id
    assert resolved_context is not None
    assert restored is not None and restored.obs_run_id == context.obs_run_id
    assert denied is None
    requested, resolved = emitter.events[-2:]
    assert (requested["event_name"], requested["phase_key"]) == (
        "approval.requested",
        "requested",
    )
    assert (resolved["event_name"], resolved["phase_key"]) == (
        "approval.resolved",
        "resolved",
    )
    assert (
        requested["obs_run_id"] == resolved["obs_run_id"] == context.obs_run_id
    )
    assert resolved["decision"] == "approved"
    assert requested["occurred_at"] == resolved["started_at"]
    for event in emitter.events:
        try:
            _validate_payload(json.dumps(event, ensure_ascii=False))
        except ValueError as exc:
            pytest.fail(f"{event['event_name']}: {exc}; event={event}")

    pending = SimpleNamespace(
        request_id=approval_id,
        session_id="session-a",
        created_at=1_794_700_800,
        extra=pending.extra,
    )
    runner_lifecycle.record_approval_superseded(pending)
    superseded = emitter.events[-1]
    assert superseded["event_name"] == "approval.resolved"
    assert superseded["decision"] == "superseded"
    assert superseded["obs_attempt_id"] == attempt.obs_attempt_id

    runner_lifecycle.record_approval_superseded(
        pending,
        decision="timed_out",
    )
    timed_out = emitter.events[-1]
    assert timed_out["event_name"] == "approval.resolved"
    assert timed_out["decision"] == "timed_out"


@pytest.mark.asyncio
async def test_approval_command_resumes_same_run_with_predecessor_attempt(
    monkeypatch,
):
    from swe.app.runner.query_execution import admission

    emitter = _setup(monkeypatch)
    owner = SimpleNamespace(agent_id="agent-a", _APPROVAL_TIMEOUT_SECONDS=60)
    request = SimpleNamespace(
        session_id="session-a",
        user_id="user-a",
        source_id="source-a",
        channel_meta={"chat_id": "chat-a", "source_id": "source-a"},
    )
    context = runner_lifecycle.admit_query_run(owner, request, [_message()])
    previous_attempt = runner_lifecycle.start_query_attempt(context)
    with bind_context(previous_attempt):
        carrier = runner_lifecycle.approval_context_carrier()
    approval_id = "550e8400-e29b-41d4-a716-446655440000"
    pending = SimpleNamespace(
        request_id=approval_id,
        status="pending",
        session_id="session-a",
        user_id="user-a",
        created_at=time.time(),
        extra={
            "approval_kind": "tool_guard",
            "agent_id": "agent-a",
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "chat_id": "chat-a",
            "msgid": "question-a",
            "agent_observability_context": carrier,
        },
    )

    class ApprovalService:
        async def get_request(self, _request_id, **_kwargs):
            return pending

    monkeypatch.setattr(
        "swe.app.approvals.get_approval_service",
        ApprovalService,
    )
    restored, decision = await admission._approval_continuation(
        owner,
        request=request,
        session_id="session-a",
        user_id="user-a",
        query=f"/approve {approval_id}",
    )
    resumed_attempt = runner_lifecycle.start_query_attempt(restored)

    assert decision == "approved"
    assert restored is not None
    assert restored.obs_run_id == context.obs_run_id
    assert resumed_attempt is not None
    assert resumed_attempt.attempt_index == previous_attempt.attempt_index + 1
    assert (
        resumed_attempt.predecessor_attempt_id
        == previous_attempt.obs_attempt_id
    )
    assert [
        event["event_name"]
        for event in emitter.events
        if event["event_name"] == "run.accepted"
    ] == ["run.accepted"]


@pytest.mark.asyncio
async def test_consecutive_approvals_keep_root_run_and_attempt_chain(
    monkeypatch,
):
    from swe.app.runner.query_execution import admission

    emitter = _setup(monkeypatch)
    owner = SimpleNamespace(agent_id="agent-a", _APPROVAL_TIMEOUT_SECONDS=60)
    request = SimpleNamespace(
        session_id="session-a",
        user_id="user-a",
        source_id="source-a",
        channel_meta={"chat_id": "chat-a", "source_id": "source-a"},
    )
    root = runner_lifecycle.admit_query_run(owner, request, [_message()])
    previous_attempt = runner_lifecycle.start_query_attempt(root)

    class ApprovalService:
        pending = None

        async def get_request(self, _request_id, **_kwargs):
            return self.pending

    service = ApprovalService()
    monkeypatch.setattr(
        "swe.app.approvals.get_approval_service",
        lambda: service,
    )
    predecessor = previous_attempt
    approval_ids = (
        "550e8400-e29b-41d4-a716-446655440001",
        "550e8400-e29b-41d4-a716-446655440002",
    )
    for approval_id in approval_ids:
        runner_lifecycle.finish_query_run(
            root,
            predecessor,
            "completed",
            waiting_for_approval=True,
        )
        approval_started_at = time.time()
        with bind_context(predecessor):
            carrier = runner_lifecycle.approval_context_carrier()
            runner_lifecycle.record_approval_requested(
                root,
                approval_id,
                started_at=approval_started_at,
            )
        service.pending = SimpleNamespace(
            request_id=approval_id,
            status="pending",
            session_id="session-a",
            user_id="user-a",
            created_at=time.time(),
            extra={
                "approval_kind": "tool_guard",
                "agent_id": "agent-a",
                "tenant_id": "tenant-a",
                "source_id": "source-a",
                "chat_id": "chat-a",
                "msgid": "question-a",
                "agent_observability_context": carrier,
            },
        )
        restored, decision = await admission._approval_continuation(
            owner,
            request=request,
            session_id="session-a",
            user_id="user-a",
            query=f"/approve {approval_id}",
        )
        assert decision == "approved"
        assert restored is not None
        assert restored.obs_run_id == root.obs_run_id
        assert restored.business_ref["root_question_msgid"] == "question-a"
        current_attempt = runner_lifecycle.start_query_attempt(restored)
        assert current_attempt.attempt_index == predecessor.attempt_index + 1
        assert (
            current_attempt.predecessor_attempt_id
            == predecessor.obs_attempt_id
        )
        with bind_context(current_attempt):
            resolved = runner_lifecycle.record_approval_resolved(
                approval_id,
                "approved",
                started_at=approval_started_at,
            )
        assert resolved is not None
        service.pending.status = "approved"
        duplicate, duplicate_decision = await admission._approval_continuation(
            owner,
            request=request,
            session_id="session-a",
            user_id="user-a",
            query=f"/approve {approval_id}",
        )
        assert duplicate is None
        assert duplicate_decision == "unresolved"
        service.pending.status = "pending"
        predecessor = current_attempt

    assert not any(
        event["event_name"] == "run.ended" for event in emitter.events
    )
    runner_lifecycle.finish_query_run(root, predecessor, "completed")
    assert (
        len(
            [
                event
                for event in emitter.events
                if event["event_name"] == "run.accepted"
            ],
        )
        == 1
    )
    attempt_ends = [
        event
        for event in emitter.events
        if event["event_name"] == "attempt.ended"
    ]
    assert [event["status"] for event in attempt_ends] == [
        "waiting_approval",
        "waiting_approval",
        "succeeded",
    ]
    approval_events = [
        event
        for event in emitter.events
        if event["event_name"].startswith("approval.")
    ]
    assert len(approval_events) == 4
    run_ends = [
        event for event in emitter.events if event["event_name"] == "run.ended"
    ]
    assert len(run_ends) == 1
    assert run_ends[0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_approval_with_missing_observability_carrier_is_unresolved(
    monkeypatch,
):
    from swe.app.runner.query_execution import admission

    _setup(monkeypatch)
    owner = SimpleNamespace(agent_id="agent-a", _APPROVAL_TIMEOUT_SECONDS=60)
    request = SimpleNamespace(
        session_id="session-a",
        user_id="user-a",
        source_id="source-a",
        channel_meta={"chat_id": "chat-a", "source_id": "source-a"},
    )
    approval_id = "550e8400-e29b-41d4-a716-446655440000"
    pending = SimpleNamespace(
        request_id=approval_id,
        status="pending",
        session_id="session-a",
        user_id="user-a",
        created_at=time.time(),
        extra={
            "approval_kind": "tool_guard",
            "agent_id": "agent-a",
            "tenant_id": "tenant-a",
            "source_id": "source-a",
            "chat_id": "chat-a",
            "msgid": "question-a",
        },
    )

    class ApprovalService:
        async def get_request(self, _request_id, **_kwargs):
            return pending

    monkeypatch.setattr(
        "swe.app.approvals.get_approval_service",
        ApprovalService,
    )
    restored, decision = await admission._approval_continuation(
        owner,
        request=request,
        session_id="session-a",
        user_id="user-a",
        query=f"/approve {approval_id}",
    )

    assert restored is None
    assert decision == "unresolved"


def test_unknown_business_status_leaves_run_unresolved(monkeypatch):
    emitter = _setup(monkeypatch)
    context = runner_lifecycle.admit_query_run(
        SimpleNamespace(agent_id="agent-a"),
        _request(),
        [_message()],
    )
    attempt = runner_lifecycle.start_query_attempt(context)

    runner_lifecycle.finish_query_run(context, attempt, "mystery")

    assert not any(
        event["event_name"] == "run.ended" for event in emitter.events
    )


def test_interrupted_business_status_is_terminal(monkeypatch):
    """取消路径可能上报 interrupted，必须映射为可终结的 Run 状态。"""
    emitter = _setup(monkeypatch)
    context = runner_lifecycle.admit_query_run(
        SimpleNamespace(agent_id="agent-a"),
        _request(),
        [_message()],
    )
    attempt = runner_lifecycle.start_query_attempt(context)

    runner_lifecycle.finish_query_run(context, attempt, "interrupted")

    assert emitter.events[-1]["event_name"] == "run.ended"
    assert emitter.events[-1]["status"] == "cancelled"


def test_default_tenant_is_emittable_without_tenant_context(monkeypatch):
    emitter = _setup(monkeypatch, tenant_id=None)
    context = runner_lifecycle.admit_query_run(
        SimpleNamespace(agent_id="agent-a", tenant_id=None),
        _request(),
        [_message()],
    )

    assert context is not None
    assert emitter.events[0]["scope"]["tenant_id"] == "default_source-a"
    assert _validate_payload(json.dumps(emitter.events[0], ensure_ascii=False))


@pytest.mark.asyncio
@pytest.mark.parametrize("capture_enabled", [False, True])
async def test_terminal_turn_reexecution_is_new_but_active_reconnect_is_not(
    capture_enabled,
):
    message = _message()
    original = TurnIdentity.create(chat_id="chat-a", msgid=message.id)
    retry = TurnIdentity.create(chat_id="chat-a", msgid=message.id)

    class Execution:
        state = {
            "turn_states": {
                message.id: {
                    "status": "completed",
                    "message": message.to_dict(),
                    "execution_id": original.turn_id,
                },
            },
        }

        async def hydrate_turn(self, _turn_id):
            return None

        def mark_state_dirty(self):
            return None

        async def commit_state(self, _state):
            self.commits += 1

        commits = 0

    execution = Execution()
    run_context = (
        ObservabilityContext.new_run(
            capture_enabled=True,
            scope=Scope("swe", "source-a", "tenant-a", "agent-a"),
            business_ref={"root_question_msgid": message.id},
        )
        if capture_enabled
        else None
    )
    for channel_meta in ({}, {"answer_turn_identity": original}):
        duplicate = await admit_user_turn_with_creation(
            execution,
            msgs=[message],
            request=SimpleNamespace(channel_meta=channel_meta),
            observability_context=run_context,
        )
        assert duplicate == (message.id, False, None)
    assert execution.state["turn_states"][message.id]["status"] == "completed"
    assert execution.commits == 0
    request = SimpleNamespace(channel_meta={"answer_turn_identity": retry})
    first_admission = await admit_user_turn_with_creation(
        execution,
        msgs=[message],
        request=request,
        observability_context=run_context,
    )
    assert first_admission == (message.id, True, run_context)
    assert execution.state["turn_states"][message.id]["status"] == "admitted"
    second_admission = await admit_user_turn_with_creation(
        execution,
        msgs=[message],
        request=request,
        observability_context=run_context,
    )
    assert second_admission == (message.id, False, None)
    assert execution.commits == 1


def test_deferred_runner_settlement_emits_terminal_after_business_commit(
    monkeypatch,
):
    emitter = _setup(monkeypatch)
    owner = SimpleNamespace(_agent_observability_contexts={})
    identity = object()
    context = runner_lifecycle.admit_query_run(
        SimpleNamespace(agent_id="agent-a"),
        _request(),
        [_message()],
    )
    attempt = runner_lifecycle.start_query_attempt(context)
    runner_lifecycle.defer_query_run_settlement(
        owner,
        identity,
        context,
        attempt,
        waiting_for_approval=False,
    )

    assert not any(
        event["event_name"] == "run.ended" for event in emitter.events
    )
    runner_lifecycle.finish_deferred_query_run(owner, identity, "completed")

    assert emitter.events[-1]["event_name"] == "run.ended"
    assert emitter.events[-1]["status"] == "succeeded"


def test_early_stop_settlement_is_not_reopened_by_stream_finally(monkeypatch):
    emitter = _setup(monkeypatch)
    owner = SimpleNamespace(_agent_observability_contexts={})
    identity = object()
    context = runner_lifecycle.admit_query_run(
        SimpleNamespace(agent_id="agent-a"),
        _request(),
        [_message()],
    )
    attempt = runner_lifecycle.start_query_attempt(context)
    runner_lifecycle.defer_query_run_settlement(
        owner,
        identity,
        context,
        attempt,
        waiting_for_approval=False,
    )

    runner_lifecycle.finish_deferred_query_run(owner, identity, "cancelled")
    runner_lifecycle.update_deferred_query_run_approval(
        owner,
        identity,
        waiting_for_approval=False,
    )

    assert owner._agent_observability_contexts == {}
    assert emitter.events[-1]["status"] == "cancelled"


@pytest.mark.parametrize("during_retry_backoff", [False, True])
def test_early_stop_settles_only_live_attempt(
    monkeypatch,
    during_retry_backoff,
):
    emitter = _setup(monkeypatch)
    owner = SimpleNamespace(_agent_observability_contexts={})
    identity = object()
    context = runner_lifecycle.admit_query_run(
        SimpleNamespace(agent_id="agent-a"),
        _request(),
        [_message()],
    )
    first = runner_lifecycle.start_query_attempt(context)
    attempt_ref = {"value": first}
    runner_lifecycle.defer_query_run_settlement(
        owner,
        identity,
        context,
        first,
        waiting_for_approval=False,
        attempt_ref=attempt_ref,
    )
    runner_lifecycle.end_query_attempt(first, status="failed")
    second = (
        None
        if during_retry_backoff
        else runner_lifecycle.start_query_retry_attempt(first)
    )
    attempt_ref["value"] = second

    runner_lifecycle.finish_deferred_query_run(owner, identity, "cancelled")

    ended = [
        (event["obs_attempt_id"], event["status"])
        for event in emitter.events
        if event["event_name"] == "attempt.ended"
    ]
    expected = [(first.obs_attempt_id, "failed")]
    if second is not None:
        expected.append((second.obs_attempt_id, "cancelled"))
    assert ended == expected
    assert emitter.events[-1]["status"] == "cancelled"


def test_stopped_approval_wait_settles_run_as_cancelled(monkeypatch):
    emitter = _setup(monkeypatch)
    owner = SimpleNamespace(_agent_observability_contexts={})
    identity = object()
    context = runner_lifecycle.admit_query_run(
        SimpleNamespace(agent_id="agent-a"),
        _request(),
        [_message()],
    )
    attempt = runner_lifecycle.start_query_attempt(context)
    runner_lifecycle.finish_query_run(
        context,
        attempt,
        "completed",
        waiting_for_approval=True,
    )
    runner_lifecycle.defer_query_run_settlement(
        owner,
        identity,
        context,
        attempt,
        waiting_for_approval=True,
    )

    runner_lifecycle.update_deferred_query_run_approval(
        owner,
        identity,
        waiting_for_approval=False,
        clear_attempt=True,
    )
    runner_lifecycle.finish_deferred_query_run(owner, identity, "cancelled")

    assert not any(
        event["event_name"] == "attempt.ended"
        and event["status"] == "cancelled"
        for event in emitter.events
    )
    assert emitter.events[-1]["event_name"] == "run.ended"
    assert emitter.events[-1]["status"] == "cancelled"


def test_approval_identity_matches_user_and_session(monkeypatch):
    """用户或会话不一致必须判定为身份冲突。"""
    monkeypatch.setattr(
        runner_lifecycle,
        "get_current_source_id",
        lambda: "source-a",
    )
    monkeypatch.setattr(
        runner_lifecycle,
        "get_current_effective_tenant_id",
        lambda: "tenant-a",
    )
    owner = SimpleNamespace(agent_id="agent-a")
    pending = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        extra={"source_id": "source-a", "tenant_id": "tenant-a"},
    )
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        source_id="source-a",
        channel_meta={},
    )
    assert runner_lifecycle.approval_identity_matches(owner, request, pending)
    assert not runner_lifecycle.approval_identity_matches(
        owner,
        SimpleNamespace(
            session_id="session-1",
            user_id="user-2",
            source_id="source-a",
            channel_meta={},
        ),
        pending,
    )
    assert not runner_lifecycle.approval_identity_matches(
        owner,
        SimpleNamespace(
            session_id="session-2",
            user_id="user-1",
            source_id="source-a",
            channel_meta={},
        ),
        pending,
    )


def test_approval_identity_ignores_missing_observability_carrier(monkeypatch):
    """观测载体缺失不应阻止业务审批。"""
    monkeypatch.setattr(
        runner_lifecycle,
        "get_current_source_id",
        lambda: "source-a",
    )
    monkeypatch.setattr(
        runner_lifecycle,
        "get_current_effective_tenant_id",
        lambda: "tenant-a",
    )
    owner = SimpleNamespace(agent_id="agent-a")
    pending = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        extra={},
    )
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        source_id="source-a",
        channel_meta={},
    )
    assert runner_lifecycle.approval_identity_matches(owner, request, pending)


def test_cancelled_business_status_ends_waiting_run(monkeypatch):
    """非 completed 的业务状态不能被 waiting 分支拦下。"""
    emitter = _setup(monkeypatch)
    context = runner_lifecycle.admit_query_run(
        SimpleNamespace(agent_id="agent-a"),
        _request(),
        [_message()],
    )
    attempt = runner_lifecycle.start_query_attempt(context)

    runner_lifecycle.finish_query_run(
        context,
        attempt,
        "cancelled",
        waiting_for_approval=True,
    )

    assert emitter.events[-1]["event_name"] == "run.ended"
    assert emitter.events[-1]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_query_waiting_for_approval_uses_service_status(monkeypatch):
    """陈旧 pending 标记不能覆盖审批服务的真实终态。"""
    monkeypatch.setattr(
        runner_lifecycle,
        "get_current_scope_id",
        lambda: "scope-a",
    )
    agent = SimpleNamespace(
        _tool_guard_pending_info={"request_id": "approval-1"},
    )
    execution = SimpleNamespace(
        runtime=SimpleNamespace(agent=agent),
    )

    class _Service:
        def __init__(self, status):
            self.status = status

        async def get_request(self, _request_id):
            return SimpleNamespace(status=self.status)

    monkeypatch.setattr(
        "swe.app.approvals.get_approval_service",
        lambda: _Service("pending"),
    )
    assert (
        await runner_lifecycle.query_waiting_for_approval(
            None,
            None,
            execution,
        )
        is True
    )

    monkeypatch.setattr(
        "swe.app.approvals.get_approval_service",
        lambda: _Service("superseded"),
    )
    assert (
        await runner_lifecycle.query_waiting_for_approval(
            None,
            None,
            execution,
        )
        is False
    )

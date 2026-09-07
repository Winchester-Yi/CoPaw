# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio

import pytest

from swe.app.answer_turn.coordinator import AnswerTurnCoordinator
from swe.app.answer_turn.in_memory import (
    InMemoryApproval,
    InMemoryExecution,
    InMemoryGoal,
    InMemorySession,
    InMemoryStream,
    InMemorySubagent,
)
from swe.app.answer_turn.models import (
    TERMINAL_STATUSES,
    TurnIdentity,
    TurnOutcome,
    TurnStatus,
)
from swe.app.runner.runner import AgentRunner


def _coordinator(*, hard_cancel_delay: float = 5.0):
    adapters = {
        "stream": InMemoryStream(),
        "execution": InMemoryExecution(),
        "session": InMemorySession(),
        "goal": InMemoryGoal(),
        "subagent": InMemorySubagent(),
        "approval": InMemoryApproval(),
    }
    return (
        AnswerTurnCoordinator(**adapters, hard_cancel_delay=hard_cancel_delay),
        adapters,
    )


def test_turn_value_objects_are_immutable_and_terminal_statuses_are_explicit():
    identity = TurnIdentity.create(chat_id="chat-1", msgid="msg-1")
    outcome = TurnOutcome.completed(identity, assistant_text="ok")
    assert identity.chat_id == "chat-1"
    assert outcome.status in TERMINAL_STATUSES
    with pytest.raises((AttributeError, TypeError)):
        outcome.status = TurnStatus.CANCELLED  # type: ignore[misc]


@pytest.mark.asyncio
async def test_start_or_attach_creates_one_identity_and_one_producer_per_chat():
    coordinator, adapters = _coordinator()

    async def producer(_identity, _payload):
        return None

    first = await coordinator.start_or_attach(
        "chat-1",
        {"q": 1},
        producer,
        msgid="msg-1",
    )
    second = await coordinator.start_or_attach("chat-1", {"q": 1}, producer)
    assert first.is_new_run is True
    assert second.is_new_run is False
    assert second.identity == first.identity
    assert len(adapters["stream"].start_calls) == 1
    await coordinator.settle(
        TurnOutcome.completed(first.identity, assistant_text="ok"),
    )


@pytest.mark.asyncio
async def test_statuses_reads_multiple_chats_in_one_coordinator_operation():
    coordinator, _adapters = _coordinator()

    async def producer(_identity, _payload):
        return None

    first = await coordinator.start_or_attach(
        "chat-1",
        {},
        producer,
        msgid="msg-1",
    )
    second = await coordinator.start_or_attach(
        "chat-2",
        {},
        producer,
        msgid="msg-2",
    )

    statuses = await coordinator.statuses(["chat-1", "chat-2", "missing"])

    assert statuses == {
        first.identity.chat_id: TurnStatus.RUNNING,
        second.identity.chat_id: TurnStatus.RUNNING,
        "missing": None,
    }


@pytest.mark.asyncio
async def test_attach_and_before_start_are_bound_to_active_turn_identity():
    coordinator, _adapters = _coordinator()
    before_start_calls = 0

    def before_start():
        nonlocal before_start_calls
        before_start_calls += 1

    async def producer(_identity, _payload):
        return None

    first = await coordinator.start_or_attach(
        "chat-1",
        {"q": 1},
        producer,
        msgid="msg-1",
        before_start=before_start,
    )
    second = await coordinator.start_or_attach(
        "chat-1",
        {"q": 2},
        producer,
        msgid="msg-2",
        before_start=before_start,
    )
    attached = await coordinator.attach("chat-1", msgid="msg-1")
    stale = await coordinator.attach("chat-1", msgid="msg-2")

    assert before_start_calls == 1
    assert first.is_new_run is True
    assert second.is_new_run is False
    assert second.identity == first.identity
    assert attached is not None
    assert attached.identity == first.identity
    assert stale is None
    assert await coordinator.current_identity("chat-1") == first.identity
    await coordinator.settle(TurnOutcome.completed(first.identity))
    assert await coordinator.attach("chat-1", msgid="msg-1") is None


@pytest.mark.asyncio
async def test_claim_stop_orders_effects_and_is_idempotent():
    coordinator, adapters = _coordinator(hard_cancel_delay=0.01)

    async def producer(_identity, _payload):
        await asyncio.Event().wait()

    lease = await coordinator.start_or_attach(
        "chat-1",
        {},
        producer,
        msgid="turn-1",
    )
    claim = await coordinator.claim_stop(lease.identity, msgid="turn-1")
    assert claim.accepted is True
    assert await coordinator.status(lease.identity) == TurnStatus.STOPPING
    assert adapters["approval"].calls == [lease.identity]
    assert adapters["goal"].calls == [lease.identity]
    assert adapters["subagent"].calls == [lease.identity]

    duplicate = await coordinator.claim_stop(lease.identity, msgid="turn-1")
    assert duplicate.accepted is True
    assert adapters["approval"].calls == [lease.identity]
    await coordinator.settle(
        TurnOutcome.cancelled(lease.identity, result="stopped"),
    )


@pytest.mark.asyncio
async def test_claim_stop_rejects_stale_msgid_and_hard_cancels_only_live_turn():
    coordinator, adapters = _coordinator(hard_cancel_delay=0.01)

    async def producer(_identity, _payload):
        await asyncio.Event().wait()

    lease = await coordinator.start_or_attach(
        "chat-1",
        {},
        producer,
        msgid="turn-1",
    )
    rejected = await coordinator.claim_stop(lease.identity, msgid="stale")
    assert rejected.accepted is False
    assert await coordinator.status(lease.identity) == TurnStatus.RUNNING
    await coordinator.claim_stop(lease.identity, msgid="turn-1")
    await asyncio.sleep(0.03)
    assert adapters["execution"].hard_cancel_calls == [lease.identity]
    await coordinator.settle(TurnOutcome.cancelled(lease.identity))


@pytest.mark.asyncio
async def test_settle_persists_once_closes_stream_and_removes_active_turn():
    coordinator, adapters = _coordinator()

    async def producer(_identity, _payload):
        return None

    lease = await coordinator.start_or_attach(
        "chat-1",
        {},
        producer,
        msgid="turn-1",
    )
    outcome = TurnOutcome.completed(lease.identity, result={"answer": 1})
    assert await coordinator.settle(outcome) is True
    assert await coordinator.settle(outcome) is False
    assert adapters["session"].calls == [(lease.identity, outcome)]
    assert adapters["stream"].close_calls == [lease.identity]
    assert await coordinator.status(lease.identity) is None


@pytest.mark.asyncio
async def test_recover_current_linearizes_terminal_snapshot_before_next_admission():
    coordinator, _adapters = _coordinator()
    snapshot_started = asyncio.Event()
    release_snapshot = asyncio.Event()

    async def terminal_snapshot():
        snapshot_started.set()
        await release_snapshot.wait()
        return "snapshot"

    recovery = asyncio.create_task(
        coordinator.recover_current("chat-1", terminal_snapshot),
    )
    await snapshot_started.wait()

    async def producer(_identity, _payload):
        return None

    admission = asyncio.create_task(
        coordinator.start_or_attach("chat-1", {}, producer),
    )
    await asyncio.sleep(0)
    assert admission.done() is False

    release_snapshot.set()
    assert await recovery == "snapshot"
    lease = await admission
    assert lease.is_new_run is True
    await coordinator.settle(TurnOutcome.completed(lease.identity))


@pytest.mark.asyncio
async def test_recover_current_rejects_pending_settlement_before_snapshot():
    coordinator, _adapters = _coordinator()

    class _FailingSession(InMemorySession):
        async def persist_outcome(self, _outcome):
            raise OSError("disk unavailable")

    coordinator.session = _FailingSession()
    snapshot_called = False

    async def producer(_identity, _payload):
        return None

    async def terminal_snapshot():
        nonlocal snapshot_called
        snapshot_called = True
        return "unexpected"

    lease = await coordinator.start_or_attach("chat-1", {}, producer)
    await coordinator.settle(TurnOutcome.completed(lease.identity))
    with pytest.raises(Exception, match="settlement"):
        await coordinator.recover_current("chat-1", terminal_snapshot)
    assert snapshot_called is False


@pytest.mark.asyncio
async def test_runner_persists_terminal_outcome_from_admission_location():
    class _Session:
        def __init__(self):
            self.state = {"turn_states": {}}

        async def mutate_session_state(
            self,
            _session_id,
            callback,
            user_id="",
        ):
            self.state = callback(self.state)

    runner = AgentRunner(agent_id="agent-1")
    runner.session = _Session()
    identity = TurnIdentity.create(chat_id="chat-1", msgid="msg-1")
    runner._answer_turn_locations[identity] = ("session-1", "user-1")

    await runner.persist_outcome(TurnOutcome.completed(identity))

    assert runner.session.state["turn_states"]["msg-1"]["status"] == (
        "completed"
    )

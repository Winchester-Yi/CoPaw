# -*- coding: utf-8 -*-
"""SQL admission and settlement fence all model work and final frames."""

from contextlib import asynccontextmanager, nullcontext
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import Msg

from swe.app.conversation_storage.errors import IntegrityError, UnknownCommit
from swe.app.conversation_storage.identity import (
    SessionIdentity,
    WorkspaceScope,
)
from swe.app.conversation_storage.execution import ExecutionAuthority
from swe.app.conversation_storage.session_adapter import (
    DatabaseSessionExecution,
)


def setup_runtime():
    scope = WorkspaceScope(
        "t",
        "s",
        "a",
        service_unit_id="schema-test-service",
    )
    identity = SessionIdentity(scope, "", "console", "s")
    authority = ExecutionAuthority(identity, 1, 2, 3, 1, "owner")
    events = []

    @asynccontextmanager
    async def bind(_authority):
        assert _authority == authority
        yield

    adapter = SimpleNamespace(
        identity=lambda *args: identity,
        bind_authority=bind,
        get_session_state_dict=AsyncMock(return_value={"revision": 0}),
    )
    execution = DatabaseSessionExecution(adapter, identity)
    execution._active = True
    execution._state = {"revision": 0, "unknown": {"keep": True}}
    admitted = SimpleNamespace(
        created=True,
        authority=authority,
        turn={"id": 3, "question_msg_key": b"question"},
    )
    service = SimpleNamespace(
        renewal_capacity=nullcontext,
        reserve_admission=AsyncMock(return_value=admitted),
        publish_admission=AsyncMock(
            side_effect=lambda *args: events.append("admitted"),
        ),
        start=AsyncMock(side_effect=lambda *args: events.append("started")),
        renew=AsyncMock(),
        lease_seconds=30,
        finish=AsyncMock(
            side_effect=lambda *args, **kwargs: events.append("terminal"),
        ),
    )
    storage = SimpleNamespace(
        db=SimpleNamespace(control_priority=nullcontext),
        session_for=lambda channel: adapter,
        executions=service,
        history=SimpleNamespace(answer_turn=AsyncMock()),
    )
    owner = SimpleNamespace(
        _get_or_create_chat=AsyncMock(return_value=SimpleNamespace(id="chat")),
    )
    question = Msg("user", "hello", "user")
    question.id = "question"
    request = SimpleNamespace(
        session_id="s",
        user_id="",
        channel="console",
        channel_meta={},
    )
    return storage, owner, execution, request, question, events


async def test_admission_commits_before_work_and_final_frame_follows_terminal():
    from swe.app.conversation_storage.runner_integration import IndexedQuery

    storage, owner, execution, request, question, events = setup_runtime()
    query = IndexedQuery(storage, owner, request, execution)
    async with query.admit([question]):
        assert events == ["admitted", "started"]
        assert execution.state["unknown"] == {"keep": True}

        async def stream():
            events.append("model")
            yield Msg("assistant", "answer", "assistant"), True
            events.append("cleanup")

        received = []
        async for frame in query.stream(stream()):
            events.append("delivered")
            received.append(frame)
        assert len(received) == 1 and received[0][1] is True
        assert events == [
            "admitted",
            "started",
            "model",
            "cleanup",
            "terminal",
            "delivered",
        ]


async def test_unknown_terminal_never_emits_success_or_replays_work():
    from swe.app.conversation_storage.runner_integration import IndexedQuery

    storage, owner, execution, request, question, events = setup_runtime()
    storage.executions.finish.side_effect = UnknownCommit("a" * 64)
    query = IndexedQuery(storage, owner, request, execution)
    async with query.admit([question]):

        async def stream():
            events.append("model")
            yield Msg("assistant", "answer", "assistant"), True

        with pytest.raises(UnknownCommit):
            _ = [frame async for frame in query.stream(stream())]
    assert events.count("model") == 1
    assert query.pending_operation == "a" * 64
    storage.executions.finish.assert_awaited_once()


@pytest.mark.parametrize(
    "turn_state,history_status",
    [("succeeded", "completed"), ("running", None)],
)
async def test_unknown_terminal_confirmed_committed_releases_closed_execution(
    turn_state,
    history_status,
):
    from swe.app.conversation_storage.runner_integration import IndexedQuery

    storage, owner, execution, request, question, _ = setup_runtime()
    indexed = IndexedQuery(storage, owner, request, execution)
    indexed.authority = ExecutionAuthority(
        execution.identity,
        1,
        2,
        3,
        1,
        "owner",
    )
    indexed.pending_operation = "a" * 64
    execution.pending_operation = indexed.pending_operation
    execution._active = False
    execution._state = {}
    storage.publications = SimpleNamespace(
        get=AsyncMock(
            return_value={
                "id": 8,
                "state": "committed",
                "service_unit_key": b"schema-test-service",
                "tenant_key": b"t",
                "source_key": b"s",
                "agent_key": b"a",
                "session_id": 1,
                "chat_id": 2,
                "turn_id": 3,
            },
        ),
    )
    storage.db = SimpleNamespace(
        fetch_one=AsyncMock(
            return_value={
                "session_id": 1,
                "chat_id": 2,
                "generation": 1,
                "state": turn_state,
                "history_status": history_status,
                "publication_state": "committed",
                "publication_kind": "terminal",
                "publication_session_id": 1,
                "publication_chat_id": 2,
                "publication_turn_id": 3,
            },
        ),
    )
    if history_status is None:
        with pytest.raises(IntegrityError, match="history status"):
            await indexed.finish("failed")
        assert indexed.pending_operation is not None
        return
    await indexed.finish("failed")
    assert indexed.terminal_status == "completed"
    assert indexed.pending_operation is execution.pending_operation is None
    storage.executions.finish.assert_not_awaited()


async def test_intermediate_last_flag_cannot_escape_before_terminal_commit():
    from swe.app.conversation_storage.runner_integration import IndexedQuery

    storage, owner, execution, request, question, events = setup_runtime()
    indexed = IndexedQuery(storage, owner, request, execution)
    async with indexed.admit([question]):

        async def source():
            yield Msg("assistant", "first", "assistant"), True
            yield Msg("assistant", "second", "assistant"), True

        frames = []
        async for message, last in indexed.stream(source()):
            if last:
                assert "terminal" in events
            frames.append((message.content, last))
    assert frames == [("first", False), ("second", True)]


async def test_preflight_failure_commits_failed_turn(monkeypatch):
    from swe.app.conversation_storage.runner_integration import (
        stream_indexed_query,
    )

    storage, owner, execution, request, question, events = setup_runtime()

    @asynccontextmanager
    async def execute(*args, **kwargs):
        yield execution

    storage.session_for("console").execution = execute
    owner._prepare_query_preflight = AsyncMock(
        side_effect=ValueError("bad preflight"),
    )
    with pytest.raises(ValueError, match="bad preflight"):
        _ = [
            frame
            async for frame in stream_indexed_query(
                owner,
                storage,
                [question],
                request=request,
                query="hello",
            )
        ]
    assert events == ["admitted", "started", "terminal"]
    assert storage.executions.finish.await_args.args[1] == "failed"


async def test_existing_question_is_observed_without_running_again():
    from swe.app.conversation_storage.runner_integration import IndexedQuery

    storage, owner, execution, request, question, events = setup_runtime()
    storage.executions.reserve_admission.return_value = SimpleNamespace(
        created=False,
        authority=None,
        turn={"state": "succeeded"},
    )
    query = IndexedQuery(storage, owner, request, execution)
    async with query.admit([question]):
        assert query.observation is not None
    assert not events
    storage.executions.start.assert_not_awaited()
    storage.executions.publish_admission.assert_not_awaited()


async def test_settling_execution_rejects_new_cleanup_publication():
    _, _, execution, _, _, _ = setup_runtime()
    execution.pending_operation = "a" * 64
    with pytest.raises(UnknownCommit):
        await execution.commit_state({"revision": 0})
    execution.defer_close = True
    await execution.close()
    assert execution.is_active


async def test_runner_admission_routes_trusted_workspace_storage(monkeypatch):
    from swe.app.runner.query_execution import admission

    storage, owner, execution, request, question, _ = setup_runtime()
    owner._workspace = SimpleNamespace(conversation_storage=storage)
    called = []

    async def indexed(*args, **kwargs):
        called.append(args[0])
        yield question, True

    monkeypatch.setattr(
        "swe.app.conversation_storage.runner_integration.stream_indexed_query",
        indexed,
        raising=False,
    )
    frames = [
        frame
        async for frame in admission.stream_admission(
            owner,
            [question],
            request=request,
            query="hello",
            session_id="s",
            user_id="",
        )
    ]
    assert called == [owner]
    assert frames == [(question, True)]


async def test_stop_claim_is_durable_before_local_agent_interruption(tmp_path):
    from swe.app.runner.runner import AgentRunner
    from swe.app.answer_turn.models import TurnIdentity

    runner = AgentRunner(workspace_dir=tmp_path)
    storage, _, execution, _, _, events = setup_runtime()
    query = SimpleNamespace(
        storage=storage,
        authority=SimpleNamespace(identity=execution.identity),
    )
    storage.executions.request_stop = AsyncMock(
        side_effect=lambda *args: events.append("stop_committed"),
    )
    identity = TurnIdentity("chat", "question", "turn-runtime")
    agent = SimpleNamespace(
        interrupt=AsyncMock(side_effect=lambda: events.append("interrupted")),
    )
    runner._answer_turn_runtimes[identity] = (
        SimpleNamespace(agent=agent),
        SimpleNamespace(indexed_query=query),
    )
    await runner.request_cooperative_stop(identity)
    assert events == ["stop_committed", "interrupted"]


async def test_indexed_terminal_outcome_uses_atomic_terminal_publication(
    tmp_path,
):
    from swe.app.runner.runner import AgentRunner
    from swe.app.answer_turn.models import TurnIdentity, TurnOutcome

    runner = AgentRunner(workspace_dir=tmp_path)
    _, _, execution, _, _, _ = setup_runtime()
    execution.commit_state = AsyncMock()
    indexed = SimpleNamespace(finish=AsyncMock())
    execution.indexed_query = indexed
    identity = TurnIdentity("chat", "question", "turn-runtime")
    assert await runner._persist_active_outcome(
        execution,
        TurnOutcome.cancelled(identity),
        "stopped",
    )
    indexed.finish.assert_awaited_once_with("stopped")
    execution.commit_state.assert_not_awaited()


@pytest.mark.parametrize(
    "terminal,transport,expected",
    [
        ("failed", "completed", "failed"),
        ("interrupted", "cancelled", "cancelled"),
    ],
)
async def test_outer_settlement_preserves_committed_indexed_outcome(
    tmp_path,
    terminal,
    transport,
    expected,
):
    from swe.app.runner.runner import AgentRunner
    from swe.app.answer_turn.models import TurnIdentity

    runner = AgentRunner(workspace_dir=tmp_path)
    identity = TurnIdentity("chat", "question", "turn-runtime")
    indexed = SimpleNamespace(
        terminal_status=terminal,
        finish=AsyncMock(side_effect=AssertionError("already committed")),
    )
    execution = SimpleNamespace(indexed_query=indexed)
    runner._answer_turn_runtimes[identity] = (None, execution)
    runner._report_answer_turn_outcome = AsyncMock()
    await runner._settle_query_handler_outcome(identity, transport)
    outcome = runner._report_answer_turn_outcome.await_args.args[1]
    assert outcome.status.value == expected
    await runner.persist_outcome(outcome)
    indexed.finish.assert_not_awaited()


async def test_indexed_denial_cleanup_needs_no_file_path_and_joins_terminal(
    tmp_path,
):
    from swe.app.runner.runner import AgentRunner

    runner = AgentRunner(workspace_dir=tmp_path)
    _, _, execution, _, _, _ = setup_runtime()
    runner.session = execution.adapter
    execution._state["agent"] = {
        "memory": {
            "content": [[{"role": "assistant", "content": "tool call"}, []]],
        },
    }
    execution.indexed_query = SimpleNamespace()
    execution.commit_state = AsyncMock()
    await runner._cleanup_denied_session_memory(
        "s",
        "",
        Msg("assistant", "denied", "assistant"),
        execution,
    )
    assert (
        execution.state["agent"]["memory"]["content"][-1][0]["content"]
        == "denied"
    )
    assert execution.has_uncommitted_state
    execution.commit_state.assert_not_awaited()


async def test_stopped_stream_keeps_terminal_and_never_completes_again():
    from swe.app.conversation_storage.runner_integration import IndexedQuery

    storage, owner, execution, request, question, _ = setup_runtime()
    indexed = IndexedQuery(storage, owner, request, execution)
    async with indexed.admit([question]):

        async def source():
            yield Msg("assistant", "stopped", "assistant"), True
            await indexed.finish("stopped")

        assert len([frame async for frame in indexed.stream(source())]) == 1
    assert indexed.terminal_status == "stopped"
    storage.executions.finish.assert_awaited_once()


@pytest.mark.parametrize(
    "error,status",
    [
        (RuntimeError("model failed"), "failed"),
        (asyncio.CancelledError(), "interrupted"),
    ],
)
async def test_source_failure_publishes_terminal_before_propagating(
    error,
    status,
):
    from swe.app.conversation_storage.runner_integration import IndexedQuery

    storage, owner, execution, request, question, _ = setup_runtime()
    indexed = IndexedQuery(storage, owner, request, execution)
    async with indexed.admit([question]):

        async def source():
            if error is not None:
                raise error
            yield question, True

        with pytest.raises(type(error)):
            _ = [frame async for frame in indexed.stream(source())]
    assert indexed.terminal_status == status


async def test_command_binds_complete_root_and_commits_empty_user_before_reply(
    monkeypatch,
):
    from swe.app.runner.command_dispatch import (
        _CommandRequestContext,
        _run_conversation_command,
    )
    from unittest.mock import Mock

    _, _, execution, _, _, events = setup_runtime()
    execution.commit_state = AsyncMock(
        side_effect=lambda state: events.append("committed"),
    )
    memory = SimpleNamespace(
        load_state_dict=Mock(),
        state_dict=lambda: {"content": []},
    )
    archive = SimpleNamespace(bind_memory=Mock())
    runner = SimpleNamespace(
        memory_manager=SimpleNamespace(
            get_in_memory_memory=lambda **kw: memory,
        ),
        _workspace=SimpleNamespace(
            conversation_storage=SimpleNamespace(archive=archive),
        ),
    )
    handler = SimpleNamespace(
        handle_conversation_command=AsyncMock(
            return_value=Msg("assistant", "done", "assistant"),
        ),
    )
    monkeypatch.setattr(
        "swe.app.runner.command_dispatch.CommandHandler",
        lambda **kw: handler,
    )
    source = _run_conversation_command(
        "/new",
        SimpleNamespace(channel_meta={}),
        _CommandRequestContext("s", "", "chat", "console"),
        runner,
        execution,
    )
    reply = await anext(source)
    assert reply[1] is True and events == ["committed"]
    binding = archive.bind_memory.call_args.args
    assert binding[0:3] == (memory, "chat", execution)
    assert binding[3]()["unknown"] == {"keep": True}
    assert binding[3]()["agent"]["memory"] == {"content": []}
    assert binding[4] is None
    await source.aclose()


async def test_unknown_settlement_requires_ended_local_request():
    from swe.app.conversation_storage.runner_integration import IndexedQuery

    storage, owner, execution, request, _, _ = setup_runtime()
    indexed = IndexedQuery(storage, owner, request, execution)
    indexed.authority = ExecutionAuthority(
        execution.identity,
        1,
        2,
        3,
        1,
        "owner",
    )
    indexed.pending_operation = "a" * 64
    storage.publications = SimpleNamespace(
        get=AsyncMock(return_value={"state": "prepared"}),
        reconcile=AsyncMock(),
    )
    with pytest.raises(UnknownCommit):
        await indexed.finish("failed")
    storage.publications.reconcile.assert_not_awaited()
    assert not indexed.request_ended


async def test_request_ended_proof_follows_source_cleanup(monkeypatch):
    from swe.app.conversation_storage import runner_integration as integration

    storage, owner, execution, request, question, _ = setup_runtime()
    states = []

    @asynccontextmanager
    async def execute(*args, **kwargs):
        yield execution

    async def source(*args):
        try:
            yield question, False
            raise UnknownCommit("a" * 64)
        finally:
            states.append(execution.indexed_query.request_ended)
            await asyncio.sleep(0)
            states.append(execution.indexed_query.request_ended)

    storage.session_for("console").execution = execute
    monkeypatch.setattr(integration, "_stream_admitted", source)
    with pytest.raises(UnknownCommit):
        _ = [
            frame
            async for frame in integration.stream_indexed_query(
                owner,
                storage,
                [question],
                request=request,
                query="hello",
            )
        ]
    assert states == [False, False]
    assert execution.indexed_query.request_ended


@pytest.mark.parametrize(
    "changed,ignore_renewal_loss",
    [
        ({}, True),
        ({"state": "prepared"}, False),
        ({"kind": "state"}, False),
        ({"owner_key": b"another"}, False),
        ({"generation": 2}, False),
        ({"turn_id": 4}, False),
    ],
)
async def test_renewal_loss_after_own_terminal_commit_does_not_cancel_cleanup(
    monkeypatch,
    changed,
    ignore_renewal_loss,
):
    from unittest.mock import Mock
    from swe.app.conversation_storage import runner_integration as integration
    from swe.app.conversation_storage.errors import ConflictError
    from swe.app.conversation_storage.publications import PublicationRequest

    storage, owner, execution, request, _, _ = setup_runtime()
    query = integration.IndexedQuery(storage, owner, request, execution)
    query.authority = ExecutionAuthority(
        execution.identity,
        1,
        2,
        3,
        1,
        "owner",
    )
    query.terminal_operation = "a" * 64
    row = PublicationRequest(
        query.terminal_operation,
        execution.identity.scope,
        "terminal",
        "owner",
        1,
        session_id=1,
        chat_id=2,
        turn_id=3,
    ).fields()
    row.update({"state": "committed", **changed})
    storage.publications = SimpleNamespace(get=AsyncMock(return_value=row))
    loss = ConflictError("execution authority changed")
    storage.executions.renew.side_effect = loss
    parent = SimpleNamespace(cancel=Mock())
    monkeypatch.setattr(integration.asyncio, "sleep", AsyncMock())

    await query._renew(parent)

    assert parent.cancel.called is not ignore_renewal_loss
    assert query.renew_error is (None if ignore_renewal_loss else loss)


async def test_renewal_starts_before_slow_admission_publication():
    from swe.app.conversation_storage.runner_integration import IndexedQuery

    storage, owner, execution, request, question, _ = setup_runtime()
    storage.executions.lease_seconds = 0.06
    renewed = asyncio.Event()
    storage.executions.renew.side_effect = lambda *args: renewed.set()

    async def publish(*args):
        await asyncio.wait_for(renewed.wait(), 0.15)

    storage.executions.publish_admission.side_effect = publish
    query = IndexedQuery(storage, owner, request, execution)
    async with query.admit([question]):
        assert renewed.is_set()


async def test_terminal_reconciliation_is_bounded_and_preserves_confirmed_conflict():
    from unittest.mock import Mock
    from swe.app.conversation_storage.errors import ConflictError
    from swe.app.conversation_storage.runner_integration import IndexedQuery

    storage, owner, execution, request, _, _ = setup_runtime()
    storage.executions.lease_seconds = 0.06
    storage.executions.renew.side_effect = ConflictError("expired")
    query = IndexedQuery(storage, owner, request, execution)
    query.terminal_operation = "a" * 64
    storage.publications = SimpleNamespace(
        get=AsyncMock(side_effect=lambda *args: None),
    )

    async def stalled(*args):
        await asyncio.sleep(5)

    storage.publications.get.side_effect = stalled
    parent = SimpleNamespace(cancel=Mock())
    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(query._renew(parent), 0.2)
    assert asyncio.get_running_loop().time() - started < 0.15
    assert parent.cancel.called
    assert isinstance(query.renew_error, ConflictError)


async def test_delayed_heartbeat_still_checks_current_authority(monkeypatch):
    from unittest.mock import Mock
    from swe.app.conversation_storage import runner_integration as integration
    from swe.app.conversation_storage.errors import ConflictError

    storage, owner, execution, request, _, _ = setup_runtime()
    storage.executions.lease_seconds = 0.03
    query = integration.IndexedQuery(storage, owner, request, execution)
    loss = ConflictError("generation changed")
    storage.executions.renew.side_effect = loss
    sleep = asyncio.sleep

    async def delayed(delay):
        await sleep(0.04)

    monkeypatch.setattr(integration.asyncio, "sleep", delayed)
    parent = SimpleNamespace(cancel=Mock())
    await query._renew(parent)
    storage.executions.renew.assert_awaited_once()
    assert parent.cancel.called
    assert query.renew_error is loss


async def test_renewal_schedule_subtracts_prior_operation_duration(
    monkeypatch,
):
    from unittest.mock import Mock
    from swe.app.conversation_storage import runner_integration as integration
    from swe.app.conversation_storage.errors import ConflictError

    storage, owner, execution, request, _, _ = setup_runtime()
    storage.executions.lease_seconds = 0.12
    query = integration.IndexedQuery(storage, owner, request, execution)
    parent = SimpleNamespace(cancel=Mock())
    sleep = asyncio.sleep
    waits = []

    async def track_sleep(delay):
        waits.append(delay)
        await sleep(delay)

    async def renew(*args):
        if len(waits) == 1:
            await sleep(0.025)
        else:
            raise ConflictError("stop after second cycle")

    storage.executions.renew.side_effect = renew
    monkeypatch.setattr(integration.asyncio, "sleep", track_sleep)
    await query._renew(parent)
    assert len(waits) == 2
    assert waits[1] < 0.025


async def test_external_cancellation_during_reconciliation_is_preserved():
    from unittest.mock import Mock
    from swe.app.conversation_storage.errors import ConflictError
    from swe.app.conversation_storage.runner_integration import IndexedQuery

    storage, owner, execution, request, _, _ = setup_runtime()
    storage.executions.lease_seconds = 0.06
    storage.executions.renew.side_effect = ConflictError("expired")
    query = IndexedQuery(storage, owner, request, execution)
    query.terminal_operation = "a" * 64
    entered = asyncio.Event()

    async def stalled(*args):
        entered.set()
        await asyncio.Event().wait()

    storage.publications = SimpleNamespace(get=stalled)
    parent = SimpleNamespace(cancel=Mock())
    task = asyncio.create_task(query._renew(parent))
    await asyncio.wait_for(entered.wait(), 0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert query.renew_error is None
    assert not parent.cancel.called


async def test_delayed_heartbeat_success_does_not_expire_execution(
    monkeypatch,
):
    import time
    from unittest.mock import Mock
    from swe.app.conversation_storage.runner_integration import IndexedQuery

    storage, owner, execution, request, _, _ = setup_runtime()
    storage.executions.lease_seconds = 0.06

    async def late(*args):
        time.sleep(0.06)
        # Stop a missing-budget implementation after two renewals.
        if storage.executions.renew.await_count > 1:
            raise RuntimeError("unexpected second renewal")

    storage.executions.renew.side_effect = late
    from swe.app.conversation_storage import runner_integration as integration

    sleep = asyncio.sleep
    waits = []

    async def track_sleep(delay):
        waits.append(delay)
        await sleep(delay)

    monkeypatch.setattr(integration.asyncio, "sleep", track_sleep)
    query = IndexedQuery(storage, owner, request, execution)
    parent = SimpleNamespace(cancel=Mock())
    await query._renew(parent)
    assert isinstance(query.renew_error, RuntimeError)
    assert storage.executions.renew.await_count == 2

    assert waits[1] >= 0.019


async def test_admission_renewal_failure_prevents_start_and_keeps_unknown_operation():
    from swe.app.conversation_storage.errors import ConflictError
    from swe.app.conversation_storage.runner_integration import IndexedQuery

    storage, owner, execution, request, question, _ = setup_runtime()
    storage.executions.lease_seconds = 0.06
    loss = ConflictError("admission lease lost")
    storage.executions.renew.side_effect = loss

    async def publish(*args):
        await asyncio.Event().wait()

    storage.executions.publish_admission.side_effect = publish
    query = IndexedQuery(storage, owner, request, execution)
    with pytest.raises(ConflictError, match="admission lease lost"):
        async with query.admit([question]):
            pytest.fail("model work started after renewal failure")
    storage.executions.start.assert_not_called()
    assert query.pending_operation == query._admission_args["operation_key"]
    assert query.request_ended


async def test_slow_reservation_uses_sql_authority_before_model_work():
    from swe.app.conversation_storage.runner_integration import IndexedQuery

    storage, owner, execution, request, question, _ = setup_runtime()
    storage.executions.lease_seconds = 0.03
    query = IndexedQuery(storage, owner, request, execution)
    admission = storage.executions.reserve_admission.return_value

    async def reserve(*args, **kwargs):
        await asyncio.sleep(0.04)
        return admission

    storage.executions.reserve_admission.side_effect = reserve
    async with query.admit([question]):
        storage.executions.publish_admission.assert_awaited_once()
        storage.executions.start.assert_awaited_once_with(admission.authority)
    assert query.pending_operation is None


async def test_heartbeat_unavailability_retries_without_cancelling_model_work(
    monkeypatch,
):
    from unittest.mock import Mock
    from swe.app.conversation_storage.errors import StorageUnavailable
    from swe.app.conversation_storage import runner_integration as integration
    from swe.app.conversation_storage.runner_integration import IndexedQuery

    logger = Mock()
    monkeypatch.setattr(integration, "logger", logger)
    storage, owner, execution, request, question, _ = setup_runtime()
    storage.executions.lease_seconds = 0.06
    calls = 0
    recovered = asyncio.Event()

    async def renew(*args):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise StorageUnavailable("heartbeat database unavailable")
        recovered.set()

    storage.executions.renew.side_effect = renew
    query = IndexedQuery(storage, owner, request, execution)
    async with query.admit([question]):
        await asyncio.wait_for(recovered.wait(), 0.3)
        assert query.renew_error is None
        await query.finish()
    assert calls >= 3
    logger.warning.assert_called_once()
    logger.info.assert_called_once_with("Conversation heartbeat recovered")
    assert query.terminal_status == "completed"


async def test_heartbeat_unavailability_waits_between_retries_and_external_cancel(
    monkeypatch,
):
    from unittest.mock import Mock
    from swe.app.conversation_storage import runner_integration as integration
    from swe.app.conversation_storage.errors import StorageUnavailable

    storage, owner, execution, request, _, _ = setup_runtime()
    storage.executions.lease_seconds = 0.03
    query = integration.IndexedQuery(storage, owner, request, execution)
    storage.executions.renew.side_effect = StorageUnavailable("offline")
    parent = SimpleNamespace(cancel=Mock())
    waits = []

    async def sleep(delay):
        waits.append(delay)
        if len(waits) == 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(integration.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await query._renew(parent)
    assert all(delay >= 0.009 for delay in waits)
    assert storage.executions.renew.await_count == 3
    assert not parent.cancel.called
    assert query.renew_error is None


async def test_heartbeat_never_retries_an_unknown_business_commit(monkeypatch):
    from unittest.mock import Mock
    from swe.app.conversation_storage import runner_integration as integration

    storage, owner, execution, request, _, _ = setup_runtime()
    failure = UnknownCommit("e" * 64)
    storage.executions.renew.side_effect = failure
    query = integration.IndexedQuery(storage, owner, request, execution)
    parent = SimpleNamespace(cancel=Mock())
    waits = 0

    async def sleep(delay):
        nonlocal waits
        waits += 1
        if waits > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(integration.asyncio, "sleep", sleep)
    await query._renew(parent)
    assert query.renew_error is failure
    assert parent.cancel.called
    storage.executions.renew.assert_awaited_once()


async def test_heartbeat_timeout_is_transient_not_authority_rejection(
    monkeypatch,
):
    from unittest.mock import Mock
    from swe.app.conversation_storage import runner_integration as integration
    from swe.app.conversation_storage.errors import StorageUnavailable

    storage, owner, execution, request, _, _ = setup_runtime()
    storage.executions.lease_seconds = 0.03
    storage.executions.renew.side_effect = StorageUnavailable("deadline")
    query = integration.IndexedQuery(storage, owner, request, execution)
    parent = SimpleNamespace(cancel=Mock())
    sleeps = 0

    async def sleep(delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(integration.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await query._renew(parent)
    assert storage.executions.renew.await_count == 1
    assert query.renew_error is None
    assert not parent.cancel.called

# -*- coding: utf-8 -*-
"""SQL admission and settlement fence all model work and final frames."""

from contextlib import asynccontextmanager
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
    scope = WorkspaceScope("t", "s", "a")
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

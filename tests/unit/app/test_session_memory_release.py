# -*- coding: utf-8 -*-
"""Session transaction exits must drop their state ownership."""

import asyncio
import json
from contextlib import nullcontext

import pytest

from swe.app.runner import session as session_module
from swe.app.runner.session import SessionExecution


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_session_exit_drops_state_without_mutating_or_losing_commit(
    tmp_path,
    failed,
):
    path = tmp_path / "session.json"
    execution = SessionExecution(str(path))
    with pytest.raises(ValueError) if failed else nullcontext():
        async with execution:
            state = execution.state
            state["payload"] = "large-message"
            await execution.commit_state(state)
            if failed:
                raise ValueError("query failed")
    assert execution._state == {}
    assert state["payload"] == "large-message"
    assert json.loads(path.read_text())["payload"] == "large-message"
    with pytest.raises(RuntimeError):
        await execution.read_state()
    await execution.close()
    async with SessionExecution(str(path)) as again:
        assert again.state["payload"] == "large-message"


@pytest.mark.asyncio
async def test_enter_failure_drops_loaded_state_and_releases_lock(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "session.json"
    path.write_text('{"payload": "loaded"}')
    execution = SessionExecution(str(path))

    def fail_schema(_state):
        raise ValueError("bad schema")

    monkeypatch.setattr(execution, "_get_schema_version", fail_schema)
    with pytest.raises(ValueError, match="bad schema"):
        await execution.__aenter__()
    assert execution._state == {}
    assert not execution.is_active
    async with SessionExecution(str(path)) as again:
        assert again.state["payload"] == "loaded"


@pytest.mark.asyncio
async def test_cancelled_execution_restores_parent_context_and_drops_state(
    tmp_path,
):
    entered = asyncio.Event()
    execution = SessionExecution(str(tmp_path / "cancelled.json"))

    async def query():
        async with execution:
            execution.state["payload"] = "request-data"
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(query())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert execution._state == {}
    assert not execution.is_active
    assert session_module._ACTIVE_SESSION_EXECUTIONS.get() == {}


@pytest.mark.asyncio
async def test_nested_execution_keeps_parent_active_and_detaches_child(
    tmp_path,
):
    async with SessionExecution(str(tmp_path / "parent.json")) as parent:
        parent.state["parent"] = True
        async with SessionExecution(str(tmp_path / "child.json")) as child:
            child.state["child"] = True
            assert len(session_module._ACTIVE_SESSION_EXECUTIONS.get()) == 2
        assert child._state == {}
        assert list(
            session_module._ACTIVE_SESSION_EXECUTIONS.get().values(),
        ) == [parent]
        assert parent.state["parent"] is True
    assert parent._state == {}

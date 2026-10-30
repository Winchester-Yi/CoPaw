# -*- coding: utf-8 -*-
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.identity import (
    SessionIdentity,
    WorkspaceScope,
)
from swe.app.conversation_storage.session_adapter import (
    MySQLSessionAdapter,
    SemanticBatch,
)
from swe.app.conversation_storage.errors import ConflictError


class EmptyDatabase:
    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetch_one(self, query, params=None):
        return None


def adapter():
    return MySQLSessionAdapter(
        EmptyDatabase(),
        WorkspaceScope("tenant", "source", "agent"),
        None,
        channel="console",
    )


async def test_adapter_preserves_full_identity_and_never_sanitizes_session():
    session = adapter()
    assert session.identity("same__session", "").sql_values == (
        b"tenant",
        b"source",
        b"agent",
        b"",
        b"console",
        b"same__session",
    )
    assert await session.get_session_state_dict("missing") == {}
    with pytest.raises(ValueError, match="does not exist"):
        await session.get_session_state_dict("missing", allow_not_exist=False)


async def test_state_module_save_preserves_unknown_top_level_modules():
    session = adapter()
    session.get_session_state_dict = AsyncMock(
        return_value={"future": {"keep": True}, "agent": {"old": 1}},
    )
    session._save_state = AsyncMock(
        side_effect=lambda identity, state: {**state, "revision": 1},
    )

    class Module:
        def state_dict(self):
            return {"new": 2}

    await session.save_session_state("s", agent=Module())
    assert session._save_state.call_args.args[1] == {
        "future": {"keep": True},
        "agent": {"new": 2},
    }


async def test_nested_modify_and_cron_merge_use_one_compare_and_set_snapshot():
    session = adapter()
    session.get_session_state_dict = AsyncMock(
        return_value={"future": {"keep": True}},
    )
    session._save_state = AsyncMock(
        side_effect=lambda identity, state: {**state, "revision": 1},
    )
    result = await session.update_session_state(
        "s",
        ["agent", "memory"],
        {"messages": []},
    )
    assert result["future"] == {"keep": True}
    assert result["agent"]["memory"] == {"messages": []}
    assert session._save_state.await_count == 1
    await session.save_merged_state(
        "s",
        state={"cron": {"receipt": True}, "future": {"keep": True}},
    )
    assert session._save_state.call_args.args[1]["cron"] == {"receipt": True}


def test_semantic_boundaries_force_batch_and_streaming_has_bounded_thresholds():
    batch = SemanticBatch(
        max_bytes=4,
        max_events=3,
        max_seconds=2,
        clock=lambda: 0,
    )
    assert not batch.add("a")
    assert batch.add("abc")
    batch.reset()
    assert batch.add("", semantic=True)
    batch.reset()
    assert not batch.add("")
    assert not batch.add("")
    assert batch.add("")


async def test_execution_context_keeps_snapshot_and_commits_without_database_long_transaction():
    session = adapter()
    session.get_session_state_dict = AsyncMock(
        return_value={"revision": 4, "future": {"keep": True}},
    )
    session._save_state = AsyncMock(
        side_effect=lambda identity, state: {**state, "revision": 5},
    )
    async with session.execution("s") as execution:
        assert execution.is_active and execution.revision == 4
        execution.state["future"]["added"] = True
        execution.mark_state_dirty()
        assert execution.has_uncommitted_state
        result = await execution.commit_state(execution.state)
        assert result["revision"] == 5
        assert execution.revision == 5 and not execution.has_uncommitted_state
    assert not execution.is_active
    with pytest.raises(RuntimeError, match="active"):
        _ = execution.state


async def test_execution_accepts_confirmed_archive_root_without_second_publication():
    session = adapter()
    session.get_session_state_dict = AsyncMock(
        return_value={"revision": 4, "future": {"keep": True}},
    )
    session._save_state = AsyncMock()
    async with session.execution("s") as execution:
        execution.mark_state_dirty()
        root = {"revision": 5, "future": {"keep": True}}
        execution.accept_committed_state(root)
        root["future"]["keep"] = False
        assert execution.state["future"]["keep"] is True
        assert execution.revision == 5
        assert not execution.has_uncommitted_state
        with pytest.raises(ValueError, match="revision"):
            execution.accept_committed_state({"revision": 3})
        session._save_state.assert_not_awaited()
    with pytest.raises(RuntimeError, match="active"):
        execution.accept_committed_state({"revision": 6})


async def test_state_save_checks_backup_fence_before_session_creation():
    session = adapter()
    session.retentions.assert_writes_allowed = AsyncMock(
        side_effect=ConflictError("backup fence"),
    )
    session._ensure_session = AsyncMock()
    with pytest.raises(ConflictError, match="backup fence"):
        await session._save_state(session.identity("s"), {"revision": 0})
    session._ensure_session.assert_not_awaited()

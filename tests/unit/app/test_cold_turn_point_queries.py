# -*- coding: utf-8 -*-
"""Point history consumers must not hydrate every cold terminal turn."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.chat_sharing import router as sharing
from swe.app.chat_sharing.models import ChatShareRecord
from swe.app.runner import api
from swe.app.runner.models import ChatSpec
from swe.app.runner.session import SafeJSONSession


async def cold_session(tmp_path):
    session = SafeJSONSession(str(tmp_path))
    turns = {
        f"t{i}": {
            "status": "completed",
            "message": {"id": f"t{i}", "role": "user", "content": f"q{i}"},
            "created_at": str(i),
            "chat_id": "chat-a",
        }
        for i in range(80)
    }
    await session.save_merged_state("s", state={"turn_states": turns})
    await session.migrate_terminal_turns("s", dry_run=False)
    return session


def chat():
    return ChatSpec(
        id="chat-a",
        name="demo",
        session_id="s",
        user_id="",
        channel="console",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("by_chat", [True, False])
async def test_answer_turn_reads_only_requested_cold_turn(
    tmp_path,
    monkeypatch,
    by_chat,
):
    session = await cold_session(tmp_path)
    state = await session.get_session_state_dict("s")
    assert "t0" not in state["turn_states"]
    monkeypatch.setattr(
        session,
        "materialize_turn_states",
        AsyncMock(side_effect=AssertionError("full cold read")),
    )
    point_read = AsyncMock(wraps=session.get_turn_state)
    monkeypatch.setattr(session, "get_turn_state", point_read)
    monkeypatch.setattr(api, "_authorize_chat", lambda *a: None)
    result = await api.get_answer_turn(
        SimpleNamespace(state=SimpleNamespace(user_id="alice")),
        sessionid=None if by_chat else "s",
        msgid="t0",
        chat_id="chat-a" if by_chat else None,
        mgr=SimpleNamespace(
            get_chat=AsyncMock(return_value=chat()),
            list_chats=AsyncMock(return_value=[chat()]),
        ),
        session=session,
        workspace=SimpleNamespace(workspace_dir=None),
    )
    assert [
        api._message_original_id(message) for message in result.messages
    ] == ["t0"]
    assert result.turn_status == "completed"
    assert point_read.await_count == 1


@pytest.mark.asyncio
async def test_share_creation_reads_selected_cold_turns_only(
    tmp_path,
    monkeypatch,
):
    session = await cold_session(tmp_path)
    monkeypatch.setattr(
        session,
        "materialize_turn_states",
        AsyncMock(side_effect=AssertionError("full cold read")),
    )
    point_read = AsyncMock(wraps=session.get_turn_state)
    monkeypatch.setattr(session, "get_turn_state", point_read)
    create = AsyncMock(
        return_value=ChatShareRecord(
            token="token",
            chat_id="chat-a",
            creator_id="alice",
            snapshot_key="token.json",
            created_at="2026-09-02T00:00:00Z",
        ),
    )
    monkeypatch.setattr(
        sharing,
        "_service",
        SimpleNamespace(create_snapshot=create),
    )
    monkeypatch.setattr(sharing, "_authorize_chat", lambda *a: None)
    workspace = SimpleNamespace(
        workspace_dir=None,
        runner=SimpleNamespace(session=session),
        chat_manager=SimpleNamespace(get_chat=AsyncMock(return_value=chat())),
    )
    result = await sharing.create_chat_share(
        "chat-a",
        sharing.ChatShareCreateRequest(turn_ids=["t0", "t3"]),
        SimpleNamespace(state=SimpleNamespace(user_id="alice")),
        workspace,
    )
    assert result.token == "token"
    assert [call.args[-1] for call in point_read.await_args_list] == [
        "t0",
        "t3",
    ]
    assert create.await_args.kwargs["turn_statuses"]["t0"] == "completed"
    ids = {
        api._message_original_id(m)
        for m in create.await_args.kwargs["messages"]
    }
    assert {"t0", "t3"} <= ids
    assert "t1" not in ids


@pytest.mark.asyncio
async def test_share_options_still_materializes_complete_cold_history(
    tmp_path,
    monkeypatch,
):
    session = await cold_session(tmp_path)
    materialize = AsyncMock(wraps=session.materialize_turn_states)
    monkeypatch.setattr(session, "materialize_turn_states", materialize)
    monkeypatch.setattr(sharing, "_authorize_chat", lambda *a: None)
    result = await sharing.get_chat_share_options(
        "chat-a",
        SimpleNamespace(state=SimpleNamespace(user_id="alice")),
        SimpleNamespace(
            workspace_dir=None,
            runner=SimpleNamespace(session=session),
            chat_manager=SimpleNamespace(
                get_chat=AsyncMock(return_value=chat()),
            ),
        ),
    )
    assert len(result.messages) == 80
    assert len(result.turn_statuses) == 80
    assert materialize.await_count == 1

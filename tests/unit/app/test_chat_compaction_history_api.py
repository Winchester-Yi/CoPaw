# -*- coding: utf-8 -*-
from types import SimpleNamespace
import uuid

import pytest
from agentscope.message import Msg

from swe.app.runner.api import (
    _archive_metadata,
    _archive_page,
    _archive_store,
    _filter_archived_turn_messages,
    get_chat_history_page,
)
from swe.app.runner.models import ChatMessage, ChatSpec


def _message(index: int) -> Msg:
    message = Msg(
        name="user",
        role="user",
        content=f"old-{index}",
        timestamp=f"2026-08-01T12:00:{index:02d}+00:00",
    )
    message.id = f"old-{index}"
    return message


@pytest.mark.asyncio
async def test_archive_metadata_keeps_only_the_latest_adjacent_boundary(
    tmp_path,
) -> None:
    workspace = SimpleNamespace(workspace_dir=tmp_path)
    chat_id = str(uuid.uuid4())
    store = _archive_store(workspace)
    assert store is not None
    first = await store.commit(chat_id, [_message(1)])
    second = await store.commit(chat_id, [_message(2)])

    metadata = await _archive_metadata(workspace, chat_id)

    assert metadata.has_more is True
    assert [boundary.id for boundary in metadata.boundaries] == [second.id]
    assert metadata.boundaries[0].last_message_id == "old-2"
    assert first.id != second.id


@pytest.mark.asyncio
async def test_archive_page_returns_display_messages_and_respects_limit(
    tmp_path,
) -> None:
    workspace = SimpleNamespace(workspace_dir=tmp_path)
    chat_id = str(uuid.uuid4())
    store = _archive_store(workspace)
    assert store is not None
    await store.commit(chat_id, [_message(index) for index in range(55)])

    page = await _archive_page(workspace, chat_id, None, 50)

    assert [message.metadata["original_id"] for message in page.messages] == [
        f"old-{index}" for index in range(5, 55)
    ]
    assert page.has_more is True
    assert page.next_cursor is not None


@pytest.mark.asyncio
async def test_history_api_keeps_each_archived_message_source_identity(
    tmp_path,
) -> None:
    class _ChatManager:
        async def get_chat(self, chat_id: str) -> ChatSpec:
            return ChatSpec(
                id=chat_id,
                session_id="console:user-1",
                user_id="user-1",
                channel="console",
            )

    workspace = SimpleNamespace(workspace_dir=tmp_path)
    chat_id = str(uuid.uuid4())
    store = _archive_store(workspace)
    assert store is not None
    await store.commit(chat_id, [_message(1), _message(2)])

    page = await get_chat_history_page(
        SimpleNamespace(state=SimpleNamespace(user_id="user-1")),
        chat_id,
        before=None,
        limit=50,
        mgr=_ChatManager(),
        workspace=workspace,
    )

    assert [message.metadata["original_id"] for message in page.messages] == [
        "old-1",
        "old-2",
    ]


@pytest.mark.asyncio
async def test_workspace_without_archive_root_preserves_empty_legacy_metadata() -> (
    None
):
    workspace = SimpleNamespace()

    assert (
        await _archive_metadata(workspace, str(uuid.uuid4()))
    ).model_dump() == {
        "has_more": False,
        "boundaries": [],
    }


@pytest.mark.asyncio
async def test_archived_turn_anchor_is_not_readded_to_current_history(
    tmp_path,
) -> None:
    workspace = SimpleNamespace(workspace_dir=tmp_path)
    chat_id = str(uuid.uuid4())
    store = _archive_store(workspace)
    assert store is not None
    archived = _message(1)
    await store.commit(chat_id, [archived])
    archived_anchor = ChatMessage.model_validate(
        {
            "id": archived.id,
            "role": "user",
            "type": "message",
            "content": [{"type": "text", "text": archived.content}],
        },
    )
    current = ChatMessage.model_validate(
        {
            "id": "current-user",
            "role": "user",
            "type": "message",
            "content": [{"type": "text", "text": "current"}],
        },
    )

    result = await _filter_archived_turn_messages(
        workspace,
        chat_id,
        [archived_anchor, current],
    )

    assert [message.id for message in result] == ["current-user"]

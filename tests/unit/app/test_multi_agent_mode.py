# -*- coding: utf-8 -*-
"""Atomic Chat mode and expert binding transitions."""

import asyncio

import pytest

from swe.app.runner.expert_binding import (
    ExpertBindingConflict,
    read_expert_binding,
)
from swe.app.runner.models import ChatSpec
from swe.app.runner.repo.json_repo import JsonChatRepository


@pytest.mark.asyncio
async def test_mode_transition_releases_expert_and_rejects_stale_revision(
    tmp_path,
):
    repo = JsonChatRepository(tmp_path / "chats.json")
    await repo.upsert_chat(
        ChatSpec(id="chat", session_id="s", user_id="u", meta={"other": 1}),
    )
    await repo.compare_and_set_expert_binding("chat", 0, "expert")

    updated = await repo.set_multi_agent_mode("chat", True, 1)
    assert updated.meta["multi_agent_mode_enabled"] is True
    assert updated.meta["other"] == 1
    assert read_expert_binding(updated).model_dump() == {
        "revision": 2,
        "expert_id": None,
    }
    with pytest.raises(ExpertBindingConflict):
        await repo.set_multi_agent_mode("chat", False, 1)
    assert (await repo.get_chat("chat")).meta[
        "multi_agent_mode_enabled"
    ] is True


@pytest.mark.asyncio
async def test_expert_and_mode_transitions_are_serialized(tmp_path):
    path = tmp_path / "chats.json"
    left, right = JsonChatRepository(path), JsonChatRepository(path)
    await left.upsert_chat(ChatSpec(id="chat", session_id="s", user_id="u"))
    results = await asyncio.gather(
        left.set_multi_agent_mode("chat", True, 0),
        right.compare_and_set_expert_binding("chat", 0, "expert"),
        return_exceptions=True,
    )
    assert (
        sum(isinstance(result, ExpertBindingConflict) for result in results)
        == 1
    )
    chat = await JsonChatRepository(path).get_chat("chat")
    assert not (
        chat.meta.get("multi_agent_mode_enabled")
        and read_expert_binding(chat).expert_id
    )


@pytest.mark.asyncio
async def test_stale_chat_update_cannot_restore_mode_after_expert_selection(
    tmp_path,
):
    repo = JsonChatRepository(tmp_path / "chats.json")
    await repo.upsert_chat(ChatSpec(id="chat", session_id="s", user_id="u"))
    await repo.set_multi_agent_mode("chat", True, 0)
    stale = await repo.get_chat("chat")
    await repo.compare_and_set_expert_binding("chat", 1, "expert")
    stale.name = "updated title"
    await repo.upsert_chat(stale)
    current = await repo.get_chat("chat")
    assert current.meta["multi_agent_mode_enabled"] is False
    assert read_expert_binding(current).expert_id == "expert"
    assert current.name == "updated title"

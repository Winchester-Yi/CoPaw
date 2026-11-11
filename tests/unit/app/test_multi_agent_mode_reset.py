# -*- coding: utf-8 -*-
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from swe.app.runner.command_dispatch import _reset_multi_agent_mode
from swe.app.runner.models import ChatSpec
from swe.app.runner.repo.json_repo import JsonChatRepository
from swe.app.runner.manager import ChatManager
from swe.app.runner.command_dispatch import (
    _CommandRequestContext,
    _run_conversation_command,
)
from agentscope.message import Msg


@pytest.mark.asyncio
async def test_reset_clears_only_the_mode_revision_seen_by_the_command(
    tmp_path,
):
    repo = JsonChatRepository(tmp_path / "chats.json")
    await repo.upsert_chat(ChatSpec(id="chat", session_id="s", user_id="u"))
    await repo.set_multi_agent_mode("chat", True, 0)
    runner = SimpleNamespace(_chat_manager=ChatManager(repo=repo))
    await _reset_multi_agent_mode(runner, "chat", 0)
    assert (await repo.get_chat("chat")).meta[
        "multi_agent_mode_enabled"
    ] is True
    await _reset_multi_agent_mode(runner, "chat", 1)
    assert (await repo.get_chat("chat")).meta[
        "multi_agent_mode_enabled"
    ] is False


@pytest.mark.asyncio
async def test_successful_clear_command_resets_mode(tmp_path):
    repo = JsonChatRepository(tmp_path / "chats.json")
    await repo.upsert_chat(ChatSpec(id="chat", session_id="s", user_id="u"))
    await repo.set_multi_agent_mode("chat", True, 0)
    memory = SimpleNamespace(
        load_state_dict=Mock(),
        get_memory=AsyncMock(return_value=[Msg("u", "hello", "user")]),
        clear_content=Mock(),
        clear_compressed_summary=Mock(),
        state_dict=Mock(return_value={}),
    )
    runner = SimpleNamespace(
        _chat_manager=ChatManager(repo=repo),
        memory_manager=SimpleNamespace(
            get_in_memory_memory=Mock(return_value=memory),
            reset_context_epoch=AsyncMock(),
        ),
        session=SimpleNamespace(
            get_session_state_dict=AsyncMock(return_value={}),
            update_session_state=AsyncMock(),
        ),
    )
    request = SimpleNamespace(channel_meta={"expert_binding_revision": 1})
    context = _CommandRequestContext("s", "u", "chat", "console")
    result = [
        item
        async for item in _run_conversation_command(
            "/clear",
            request,
            context,
            runner,
        )
    ]
    assert "History Cleared" in str(result)
    assert (await repo.get_chat("chat")).meta[
        "multi_agent_mode_enabled"
    ] is False

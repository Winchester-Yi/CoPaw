# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone
import uuid

import pytest
from agentscope.message import Msg

from swe.agents.memory.conversation_archive import ConversationArchiveStore
from swe.app.runner.models import ChatSpec
from swe.app.runner.session import SafeJSONSession

IDENTITY_CASES = [
    ("tenant", "source", "agent", "", "console", "session"),
    ("tenant", "source", "agent", "alice", "console", "session"),
    ("tenant", "other", "agent", "alice", "console", "session"),
    ("tenant", "source", "other", "alice", "console", "session"),
    ("tenant", "source", "agent", "alice", "cron", "session"),
]


@pytest.fixture
def conversation_contracts():
    timestamp = datetime(2026, 10, 30, tzinfo=timezone.utc)
    shared_session_chats = [
        ChatSpec(
            id="chat-first",
            name="first",
            session_id="logical-session",
            user_id="alice",
            channel="console",
            created_at=timestamp,
            updated_at=timestamp,
            meta={"custom": {"enabled": True}},
        ),
        ChatSpec(
            id="chat-second",
            name="second",
            session_id="logical-session",
            user_id="alice",
            channel="console",
            created_at=timestamp,
            updated_at=timestamp,
        ),
    ]
    return {
        "identity_cases": tuple(IDENTITY_CASES),
        "chats": shared_session_chats,
        "legacy_session_state": {
            "messages": [
                {
                    "id": "duplicate-source-id",
                    "role": "user",
                    "content": "one",
                },
                {
                    "id": "duplicate-source-id",
                    "role": "assistant",
                    "content": "two",
                },
            ],
            "history": {"messages": [{"id": "archived", "role": "assistant"}]},
            "custom_recovery_module": {"must_survive": True},
        },
        "legacy_session_without_turn_states": {"messages": []},
    }


@pytest.fixture
async def legacy_reader_contract(tmp_path, conversation_contracts):
    session = SafeJSONSession(str(tmp_path))
    source_state = conversation_contracts["legacy_session_state"]
    await session.save_merged_state("legacy-session", state=source_state)
    loaded_state = await session.get_session_state_dict("legacy-session")
    return session, loaded_state


@pytest.fixture
async def legacy_archive_contract(tmp_path):
    chat_id = str(uuid.uuid4())
    messages = []
    for index in range(2):
        message = Msg(
            name="assistant",
            role="assistant",
            content=f"archived-{index}",
            timestamp=f"2026-10-30T00:00:0{index}Z",
        )
        message.id = f"archive-{index}"
        messages.append(message)
    archive = ConversationArchiveStore(tmp_path / "dialog")
    boundary = await archive.commit(chat_id, messages)
    page = await archive.read_page(chat_id)
    return boundary, page

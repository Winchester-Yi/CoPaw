# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from swe.app.chat_sharing.service import ChatSharingService


class FakeShareStore:
    def __init__(self) -> None:
        self.created = []
        self.records = {}
        self.accesses = []

    async def create(self, record):
        self.created.append(record)
        self.records[record.token] = record

    async def get(self, token):
        return self.records.get(token)

    async def record_access(self, token):
        self.accesses.append(token)


def _message(message_id: str, role: str, text: str, **metadata):
    return {
        "id": message_id,
        "role": role,
        "type": "message",
        "content": [{"type": "text", "text": text}],
        "metadata": metadata,
        "timestamp": "2026-09-02T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_create_snapshot_requires_at_least_one_completed_turn(
    tmp_path: Path,
):
    service = ChatSharingService(FakeShareStore(), tmp_path)
    messages = [_message("q1", "user", "hello")]

    with pytest.raises(ValueError, match="at least one"):
        await service.create_snapshot(
            chat_id="chat-1",
            chat_name="Demo",
            messages=messages,
            selected_turn_ids=[],
            turn_statuses={"q1": "completed"},
            creator_id="alice",
        )

    with pytest.raises(ValueError, match="completed"):
        await service.create_snapshot(
            chat_id="chat-1",
            chat_name="Demo",
            messages=messages,
            selected_turn_ids=["q1"],
            turn_statuses={"q1": "running"},
            creator_id="alice",
        )


@pytest.mark.asyncio
async def test_create_snapshot_writes_separate_redacted_file_and_new_token(
    tmp_path: Path,
):
    store = FakeShareStore()
    service = ChatSharingService(store, tmp_path)
    messages = [
        _message(
            "q1",
            "user",
            "visible",
            hidden_context={"visible_text": "visible", "suffix": ""},
        ),
        _message("a1", "assistant", "answer"),
        _message("q2", "user", "second"),
        _message("a2", "assistant", "later"),
    ]

    first = await service.create_snapshot(
        chat_id="chat-1",
        chat_name="Demo",
        messages=messages,
        selected_turn_ids=["q1"],
        turn_statuses={"q1": "completed"},
        creator_id="alice",
    )
    second = await service.create_snapshot(
        chat_id="chat-1",
        chat_name="Demo",
        messages=messages,
        selected_turn_ids=["q1"],
        turn_statuses={"q1": "completed"},
        creator_id="alice",
    )

    assert first.token != second.token
    assert len(store.created) == 2
    assert first.snapshot_key != second.snapshot_key
    snapshot_path = tmp_path / first.snapshot_key
    assert snapshot_path.is_file()
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert payload["chat_name"] == "Demo"
    assert [item["id"] for item in payload["messages"]] == ["q1", "a1"]
    assert payload["messages"][0]["content"][0]["text"] == "visible"


@pytest.mark.asyncio
async def test_get_snapshot_records_anonymous_access(tmp_path: Path):
    store = FakeShareStore()
    service = ChatSharingService(store, tmp_path)
    created = await service.create_snapshot(
        chat_id="chat-1",
        chat_name="Demo",
        messages=[
            _message("q1", "user", "hello"),
            _message("a1", "assistant", "world"),
        ],
        selected_turn_ids=["q1"],
        turn_statuses={"q1": "completed"},
        creator_id="alice",
    )

    result = await service.get_snapshot(created.token)

    assert result["chat_name"] == "Demo"
    assert store.accesses == [created.token]


@pytest.mark.asyncio
async def test_snapshot_file_is_scoped_to_tenant(tmp_path: Path):
    store = FakeShareStore()
    service = ChatSharingService(store, tmp_path)

    created = await service.create_snapshot(
        chat_id="chat-1",
        chat_name="Demo",
        messages=[
            _message("q1", "user", "hello"),
            _message("a1", "assistant", "world"),
        ],
        selected_turn_ids=["q1"],
        turn_statuses={"q1": "completed"},
        creator_id="alice",
        tenant_id="tenant-a",
    )

    assert created.tenant_id == "tenant-a"
    assert created.snapshot_key.startswith("tenant-a/chat_shares/")
    assert (tmp_path / created.snapshot_key).is_file()


@pytest.mark.asyncio
async def test_snapshot_lookup_rejects_tampered_cross_tenant_key(
    tmp_path: Path,
):
    store = FakeShareStore()
    service = ChatSharingService(store, tmp_path)
    created = await service.create_snapshot(
        chat_id="chat-1",
        chat_name="Demo",
        messages=[
            _message("q1", "user", "hello"),
            _message("a1", "assistant", "world"),
        ],
        selected_turn_ids=["q1"],
        turn_statuses={"q1": "completed"},
        creator_id="alice",
        tenant_id="tenant-a",
    )
    store.records[created.token].snapshot_key = (
        "tenant-a/chat_shares/../../tenant-b/secret.json"
    )

    with pytest.raises(OSError, match="scope"):
        await service.get_snapshot(created.token)


@pytest.mark.asyncio
async def test_snapshot_keeps_tool_system_and_approval_messages(
    tmp_path: Path,
):
    service = ChatSharingService(FakeShareStore(), tmp_path)
    messages = [
        _message("q1", "user", "question"),
        _message("system-1", "system", "system detail"),
        _message(
            "tool-1",
            "tool",
            "tool output",
            approval_action={"requestId": "approval-1", "status": "approved"},
        ),
        _message("a1", "assistant", "answer"),
    ]

    record = await service.create_snapshot(
        chat_id="chat-1",
        chat_name="Demo",
        messages=messages,
        selected_turn_ids=["q1"],
        turn_statuses={"q1": "completed"},
        creator_id="alice",
    )
    payload = json.loads((tmp_path / record.snapshot_key).read_text("utf-8"))

    assert [item["role"] for item in payload["messages"]] == [
        "user",
        "system",
        "tool",
        "assistant",
    ]
    assert (
        payload["messages"][2]["metadata"]["approval_action"]["requestId"]
        == "approval-1"
    )

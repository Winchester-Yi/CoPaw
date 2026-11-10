# -*- coding: utf-8 -*-
import json

import pytest

from swe.app.runner.session import SafeJSONSession
from swe.app.conversation_storage.migration.legacy import (
    read_session,
    read_archive,
)


def tree_bytes(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("schema", [1, 2])
def test_legacy_root_preserves_all_unknown_modules_without_copy(
    tmp_path,
    schema,
    monkeypatch,
):
    from swe.app.conversation_storage.migration import legacy

    path = tmp_path / "session.json"
    state = {
        "schema_version": schema,
        "agent": {"memory": {"content": []}},
        "future_module": {"private": "body"},
        "turn_states": {
            "question": {
                "status": "stopped",
                "message": {"id": "question", "role": "user", "content": "q"},
            },
        },
    }
    path.write_text(json.dumps(state))
    before = tree_bytes(tmp_path)
    monkeypatch.setattr(
        legacy.shutil,
        "copyfile",
        lambda *_args: pytest.fail("legacy Session should not be copied"),
    )
    result = read_session(path)
    assert result["root"] == state and result["state"] == state
    assert tree_bytes(tmp_path) == before


async def test_ledger_reader_hydrates_cold_turns_and_preserves_full_root(
    tmp_path,
):
    session = SafeJSONSession(str(tmp_path))
    turns = {
        f"q{i}": {
            "status": "completed",
            "chat_id": "chat",
            "message": {"id": f"q{i}", "role": "user", "content": "q"},
        }
        for i in range(80)
    }
    await session.save_merged_state(
        "s",
        state={"turn_states": turns, "future_module": {"x": True}},
    )
    await session.migrate_terminal_turns("s", dry_run=False)
    before = tree_bytes(tmp_path)
    result = read_session(tmp_path / "s.json")
    assert result["format"] == "session-v3"
    assert len(result["state"]["turn_states"]) == 80
    assert result["root"]["session_generation"]
    assert result["state"]["future_module"] == {"x": True}
    assert tree_bytes(tmp_path) == before
    cold = list((tmp_path / ".turn-ledger").rglob("turns/*/*/*.json"))
    cold[0].unlink()
    with pytest.raises((ValueError, OSError)):
        read_session(tmp_path / "s.json")


def test_unknown_root_schema_is_blocked(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"schema_version": 99}))
    with pytest.raises(ValueError, match="schema"):
        read_session(path)


async def test_archive_keeps_raw_messages_current_and_previous_epochs(
    tmp_path,
):
    from agentscope.message import Msg
    from swe.agents.memory.conversation_archive import ConversationArchiveStore
    from swe.agents.memory.chat_checkpoint import CheckpointEvent

    chat = "16374f34-c02e-4bb9-87bf-eb5e64348cef"
    store = ConversationArchiveStore(tmp_path, cursor_secret=b"test")
    store.migrate_checkpoint_v2(chat)
    await store.append_checkpoint_event(
        chat,
        CheckpointEvent.new(
            sequence=1,
            epoch=1,
            type="message_added",
            facts={"message_id": "m", "role": "user"},
            source_refs=("message:m",),
        ),
    )
    message = Msg("user", "private body", "user")
    message.id = "m"
    await store.commit(chat, [message])
    await store.reset_checkpoint_epoch(chat, reason="clear")
    before = tree_bytes(tmp_path)
    result = read_archive(tmp_path, chat)
    assert result["format"] == "checkpoint-v2"
    assert [epoch["epoch"] for epoch in result["epochs"]] == [2, 1]
    assert result["batches"][0]["messages"][0]["content"] == "private body"
    assert tree_bytes(tmp_path) == before
    reference = result["epochs"][1]["head"]["checkpoint"]["path"]
    (tmp_path / chat / reference).unlink()
    with pytest.raises(ValueError):
        read_archive(tmp_path, chat)


async def test_archive_v1_missing_batch_and_deleted_marker(tmp_path):
    from agentscope.message import Msg
    from swe.agents.memory.conversation_archive import ConversationArchiveStore

    chat = "16374f34-c02e-4bb9-87bf-eb5e64348cef"
    store = ConversationArchiveStore(tmp_path, cursor_secret=b"test")
    boundary = await store.commit(chat, [Msg("user", "body", "user")])
    assert read_archive(tmp_path, chat)["format"] == "checkpoint-v1"
    (tmp_path / chat / (boundary.id + ".jsonl")).unlink()
    with pytest.raises(ValueError, match="missing"):
        read_archive(tmp_path, chat)
    await store.delete_chat(chat)
    assert read_archive(tmp_path, chat)["deleted"] is True


async def test_pending_recovery_occurs_only_in_working_copy(tmp_path):
    from swe.app.runner.turn_ledger import TurnLedger

    session = SafeJSONSession(str(tmp_path))
    await session.save_merged_state(
        "s",
        state={
            "turn_states": {
                "q": {"status": "completed", "message": {"id": "q"}},
            },
        },
    )
    await session.migrate_terminal_turns("s", dry_run=False)
    root = json.loads((tmp_path / "s.json").read_text())
    pending = TurnLedger(tmp_path / "s.json").directory(root) / "pending.json"
    intent = {
        "generation": root["session_generation"],
        "base_revision": root["revision"],
        "target_revision": root["revision"] + 1,
        "nonce": "16374f34-c02e-4bb9-87bf-eb5e64348cef",
        "files": [],
    }
    pending.write_text(json.dumps(intent))
    before = tree_bytes(tmp_path)
    result = read_session(tmp_path / "s.json")
    assert result["recovery_actions"][0]["kind"] == "ledger_pending_reconciled"
    assert pending.exists() and tree_bytes(tmp_path) == before
    intent["target_revision"] += 1
    pending.write_text(json.dumps(intent))
    with pytest.raises(ValueError, match="intent"):
        read_session(tmp_path / "s.json")


async def test_archive_quarantine_and_uncommitted_tail_are_explicit(tmp_path):
    from swe.agents.memory.chat_checkpoint import CheckpointEvent
    from swe.agents.memory.conversation_archive import ConversationArchiveStore
    from swe.agents.memory.checkpoint_storage_v2 import CheckpointStorageV2

    chat = "16374f34-c02e-4bb9-87bf-eb5e64348cef"
    store = ConversationArchiveStore(tmp_path, cursor_secret=b"test")
    store.migrate_checkpoint_v2(chat)
    await store.append_checkpoint_event(
        chat,
        CheckpointEvent.new(
            sequence=1,
            epoch=1,
            type="message_added",
            facts={"message_id": "m", "role": "user"},
            source_refs=("message:m",),
        ),
    )
    storage = CheckpointStorageV2(tmp_path / chat)
    with storage.segment_path(storage.head()).open("ab") as handle:
        handle.write(b"uncommitted tail")
    before = tree_bytes(tmp_path)
    result = read_archive(tmp_path, chat)
    assert len(result["epochs"][0]["events"]) == 1
    assert (
        result["recovery_actions"][0]["kind"]
        == "journal_uncommitted_tail_excluded"
    )
    assert tree_bytes(tmp_path) == before
    storage.quarantine("manual-review")
    with pytest.raises(ValueError, match="quarantined"):
        read_archive(tmp_path, chat)


def test_session_reader_does_not_copy_legacy_session(tmp_path, monkeypatch):
    from swe.app.conversation_storage.migration import legacy

    path = tmp_path / "selected.json"
    path.write_text("{}")
    (tmp_path / "unrelated.json").write_text("{}")
    copied = []

    def copy(*_args):
        copied.append(True)

    monkeypatch.setattr(legacy.shutil, "copyfile", copy)
    read_session(path)
    assert copied == []


def test_v1_checkpoint_watermark_requires_events_even_when_file_missing(
    tmp_path,
):
    from dataclasses import replace
    from swe.agents.memory.chat_checkpoint import CheckpointRecord

    chat = "16374f34-c02e-4bb9-87bf-eb5e64348cef"
    root = tmp_path / chat
    root.mkdir()
    record = replace(
        CheckpointRecord.new(chat_id=chat, epoch=1),
        applied_event_sequence=5,
    )
    (root / "checkpoint.json").write_text(record.to_json())
    with pytest.raises(ValueError, match="watermark"):
        read_archive(tmp_path, chat)


@pytest.mark.parametrize(
    "epochs",
    [
        {"current_epoch": 2, "history": [{"epoch": 3, "reason": "clear"}]},
        {
            "current_epoch": 2,
            "history": [
                {"epoch": 2, "reason": "clear"},
                {"epoch": 1, "reason": "clear"},
            ],
        },
    ],
)
def test_v1_epoch_history_cannot_disagree_with_current_root(tmp_path, epochs):
    from swe.agents.memory.chat_checkpoint import CheckpointRecord

    chat = "16374f34-c02e-4bb9-87bf-eb5e64348cef"
    root = tmp_path / chat
    root.mkdir()
    (root / "checkpoint.json").write_text(
        CheckpointRecord.new(chat_id=chat, epoch=2).to_json(),
    )
    (root / "epochs.json").write_text(json.dumps(epochs))
    with pytest.raises(ValueError, match="epoch"):
        read_archive(tmp_path, chat)

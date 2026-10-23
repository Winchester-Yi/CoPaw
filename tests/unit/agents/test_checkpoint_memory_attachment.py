# -*- coding: utf-8 -*-
"""Chat checkpoint attachment coverage for ReMe-compatible memory."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from agentscope.message import Msg

from swe.agents.memory.chat_checkpoint import CheckpointRecord
from swe.agents.memory.conversation_archive import (
    ConversationArchiveStore,
    attach_conversation_archive,
)
from swe.agents.memory.reme_light_memory_manager import ReMeLightMemoryManager


def _chat_id() -> str:
    return str(uuid4())


def _message(index: int) -> Msg:
    message = Msg(
        name="user",
        role="user",
        content=f"message-{index}",
        timestamp=f"2026-08-01 12:00:{index:02d}.000",
    )
    message.id = f"message-{index}"
    return message


def _memory() -> SimpleNamespace:
    memory = SimpleNamespace(content=[], _compressed_summary="")

    async def add(message: Msg) -> None:
        memory.content.append((message, []))

    async def get_memory(*, prepend_summary: bool = True) -> list[Msg]:
        del prepend_summary
        return [message for message, _marks in memory.content]

    memory.add = add
    memory.get_memory = get_memory

    def clear_content() -> None:
        memory.content.clear()

    memory.clear_content = clear_content

    def clear_compressed_summary() -> None:
        memory._compressed_summary = ""

    memory.clear_compressed_summary = clear_compressed_summary
    return memory


@pytest.mark.asyncio
async def test_attached_memory_appends_event_after_message_storage(
    tmp_path: Path,
) -> None:
    chat_id = _chat_id()
    memory = _memory()
    attach_conversation_archive(memory, tmp_path / "dialog", chat_id)
    message = _message(1)

    await memory.add(message)

    state = await memory.chat_checkpoint_store.read_checkpoint_state(chat_id)
    assert state.events[-1].source_refs == (f"message:{message.id}",)
    assert state.events[-1].facts == {
        "message_id": message.id,
        "role": "user",
    }


@pytest.mark.asyncio
async def test_projection_exposes_markdown_not_checkpoint_json(
    tmp_path: Path,
) -> None:
    chat_id = _chat_id()
    memory = _memory()
    attach_conversation_archive(memory, tmp_path / "dialog", chat_id)
    record = CheckpointRecord.new(chat_id=chat_id, epoch=1).with_current_task(
        "keep API stable",
        (),
        evidence_refs=("message:1",),
    )

    await memory.install_checkpoint_projection(record)

    assert "## 目标" in memory._compressed_summary
    assert '"schema_version"' not in memory._compressed_summary


@pytest.mark.asyncio
async def test_clear_wrappers_preserve_original_memory_operations(
    tmp_path: Path,
) -> None:
    chat_id = _chat_id()
    memory = _memory()
    attach_conversation_archive(memory, tmp_path / "dialog", chat_id)
    await memory.add(_message(1))

    memory.clear_content()
    memory.clear_compressed_summary()

    assert memory.content == []
    assert memory._compressed_summary == ""
    assert callable(memory._checkpoint_original_add)
    assert callable(memory._checkpoint_original_get_memory)


@pytest.mark.asyncio
async def test_candidate_install_archives_only_its_prefix_and_keeps_delta_recoverable(
    tmp_path: Path,
) -> None:
    chat_id = _chat_id()
    memory = _memory()
    attach_conversation_archive(memory, tmp_path / "dialog", chat_id)
    first, second = _message(1), _message(2)
    await memory.add(first)
    await memory.add(second)

    manager = object.__new__(ReMeLightMemoryManager)
    manager.working_dir = str(tmp_path)
    manager.get_in_memory_memory = lambda **_kwargs: memory
    assert await manager.schedule_precompaction(
        chat_id=chat_id,
        watermark=0,
        messages=[first],
        memory=memory,
    )

    later = _message(3)
    await memory.add(later)
    assert await manager.install_ready_precompaction(
        chat_id=chat_id,
        messages=[first],
        memory=memory,
    )

    state = await memory.chat_checkpoint_store.read_checkpoint_state(chat_id)
    assert state.record.revision == 1
    assert state.record.applied_event_sequence == 1
    assert [event.sequence for event in state.events] == [2, 3]
    assert [message.id for message, _marks in memory.content] == [
        "message-2",
        "message-3",
    ]
    recovered = await manager.recover_evidence(
        chat_id=chat_id,
        epoch=1,
        refs=["message:message-1"],
        limit=1,
    )
    assert [message.id for message in recovered] == ["message-1"]
    assert (
        await memory.chat_checkpoint_store.recover_evidence(
            _chat_id(),
            epoch=1,
            refs=["message:message-1"],
            limit=1,
        )
        == []
    )


@pytest.mark.asyncio
async def test_switching_chat_on_reused_memory_does_not_cross_journal_events(
    tmp_path: Path,
) -> None:
    first_chat, second_chat = _chat_id(), _chat_id()
    memory = _memory()
    attach_conversation_archive(memory, tmp_path / "dialog", first_chat)
    await memory.add(_message(1))

    attach_conversation_archive(memory, tmp_path / "dialog", second_chat)
    await memory.add(_message(2))

    store = memory.chat_checkpoint_store
    first_state = await store.read_checkpoint_state(first_chat)
    second_state = await store.read_checkpoint_state(second_chat)
    assert [event.source_refs for event in first_state.events] == [
        ("message:message-1",),
    ]
    assert [event.source_refs for event in second_state.events] == [
        ("message:message-2",),
    ]


@pytest.mark.asyncio
async def test_attach_restores_existing_epoch_after_memory_recreation(
    tmp_path: Path,
) -> None:
    chat_id = _chat_id()
    first_memory = _memory()
    attach_conversation_archive(first_memory, tmp_path / "dialog", chat_id)
    await first_memory.reset_context_epoch(reason="clear")

    restarted_memory = _memory()
    attach_conversation_archive(restarted_memory, tmp_path / "dialog", chat_id)

    assert restarted_memory._chat_checkpoint_epoch == 2


def test_attachment_reads_epoch_without_checkpoint_or_events(
    tmp_path: Path,
    monkeypatch,
) -> None:
    chat_id = _chat_id()
    dialog_root = tmp_path / "dialog"
    chat_dir = dialog_root / chat_id
    chat_dir.mkdir(parents=True)
    (chat_dir / "epochs.json").write_text(
        json.dumps({"current_epoch": 3, "history": []}),
        encoding="utf-8",
    )
    (chat_dir / "checkpoint.json").write_text("{}", encoding="utf-8")
    (chat_dir / "events.jsonl").write_text("x" * 1024 * 1024)

    def reject_full_read(*_args):
        pytest.fail("request attachment read full checkpoint state")

    monkeypatch.setattr(
        ConversationArchiveStore,
        "_read_checkpoint_locked",
        reject_full_read,
    )
    monkeypatch.setattr(
        ConversationArchiveStore,
        "_read_checkpoint_events",
        reject_full_read,
    )
    memory = attach_conversation_archive(_memory(), dialog_root, chat_id)

    assert memory._chat_checkpoint_epoch == 3


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "{}",
        "[]",
        '{"current_epoch":true}',
        '{"current_epoch":0}',
        '{"current_epoch":"2"}',
    ],
)
def test_epoch_only_read_preserves_corruption_error(
    tmp_path: Path,
    payload: str,
) -> None:
    store = ConversationArchiveStore(tmp_path / "dialog")
    chat_id = _chat_id()
    chat_dir = store.path_for(chat_id)
    chat_dir.mkdir(parents=True)
    (chat_dir / "epochs.json").write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match="Checkpoint epoch state is invalid"):
        store._read_checkpoint_epoch(chat_id)


def test_epoch_only_read_waits_for_chat_lock(tmp_path: Path) -> None:
    store = ConversationArchiveStore(tmp_path / "dialog")
    chat_id = _chat_id()
    started = threading.Event()

    def read_epoch() -> int:
        started.set()
        return store._read_checkpoint_epoch(chat_id)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with store._chat_lock(chat_id):
            future = executor.submit(read_epoch)
            assert started.wait(timeout=2)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.05)
            chat_dir = store.path_for(chat_id)
            chat_dir.mkdir(parents=True)
            store._write_epochs(chat_dir, 2, "new")
        assert future.result(timeout=2) == 2


@pytest.mark.asyncio
async def test_full_state_read_still_rejects_corrupt_record(
    tmp_path: Path,
) -> None:
    chat_id = _chat_id()
    dialog_root = tmp_path / "dialog"
    chat_dir = dialog_root / chat_id
    chat_dir.mkdir(parents=True)
    (chat_dir / "checkpoint.json").write_text("{}", encoding="utf-8")
    memory = attach_conversation_archive(_memory(), dialog_root, chat_id)

    with pytest.raises(ValueError, match="Checkpoint record is invalid"):
        await memory.chat_checkpoint_store.read_checkpoint_state(chat_id)

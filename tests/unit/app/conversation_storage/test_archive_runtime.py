# -*- coding: utf-8 -*-
"""Request-local indexed archive injection and atomic memory updates."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from agentscope.message import Msg

from swe.agents.memory.conversation_archive import attach_conversation_archive
from swe.agents.memory.reme_light_memory_manager import ReMeLightMemoryManager


async def test_explicit_store_attachment_never_constructs_file_store(
    monkeypatch,
    tmp_path,
):
    from swe.agents.memory import conversation_archive

    monkeypatch.setattr(
        conversation_archive,
        "ConversationArchiveStore",
        Mock(side_effect=AssertionError("file fallback")),
    )
    store = SimpleNamespace(attach_memory=Mock())
    memory = SimpleNamespace(content=[])
    attach_conversation_archive(
        memory,
        tmp_path / "existing",
        "chat",
        archive_store=store,
    )
    store.attach_memory.assert_called_once_with(memory, "chat")


async def test_prepare_request_memory_uses_injected_async_store(monkeypatch):
    manager = object.__new__(ReMeLightMemoryManager)
    memory = SimpleNamespace(content=[])
    manager._new_request_memory = Mock(return_value=memory)
    store = SimpleNamespace(prepare_memory=AsyncMock(return_value=memory))
    manager.bind_conversation_archive(store)
    result = await manager.prepare_request_memory("chat")
    assert result is memory
    store.prepare_memory.assert_awaited_once_with(memory, "chat")


async def test_manager_evidence_uses_injected_store():
    manager = object.__new__(ReMeLightMemoryManager)
    store = SimpleNamespace(recover_evidence=AsyncMock(return_value=["exact"]))
    manager.bind_conversation_archive(store)
    assert await manager.recover_evidence(
        chat_id="chat",
        epoch=1,
        refs=["message:q"],
    ) == ["exact"]
    store.recover_evidence.assert_awaited_once_with(
        chat_id="chat",
        epoch=1,
        refs=["message:q"],
    )


async def test_request_binding_prunes_memory_only_after_confirmed_publication(
    tmp_path,
):
    from swe.app.conversation_storage.archive_runtime import MySQLArchiveStore
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.app.conversation_storage.errors import UnknownCommit

    first, second = Msg("user", "one", "user"), Msg(
        "assistant",
        "two",
        "assistant",
    )
    memory = SimpleNamespace(
        content=[(first, []), (second, [])],
        _compressed_summary="old",
    )
    tx = SimpleNamespace(
        accept_committed_state=Mock(),
        mutation_lock=asyncio.Lock(),
        pending_operation=None,
    )
    facade = MySQLArchiveStore(
        SimpleNamespace(),
        WorkspaceScope("t", "s", "a"),
        BundleStore(tmp_path),
        SimpleNamespace(),
    )
    provider = AsyncMock(
        return_value={"revision": 4, "unknown_module": {"keep": True}},
    )
    facade.bind_memory(memory, "chat", tx, provider, SimpleNamespace())
    binding = memory._conversation_archive_binding
    facade.compact = AsyncMock(side_effect=UnknownCommit("op"))
    with pytest.raises(UnknownCommit):
        await memory.archive_compacted_messages([first], summary="new")
    assert memory.content == [(first, []), (second, [])]
    assert memory._compressed_summary == "old"
    tx.accept_committed_state.assert_not_called()
    assert tx.pending_operation == "op"
    tx.pending_operation = None
    facade.compact = AsyncMock(
        return_value=SimpleNamespace(
            boundary="boundary",
            recovery_state={"revision": 5},
        ),
    )
    assert (
        await memory.archive_compacted_messages([first], summary="new")
        == "boundary"
    )
    assert memory.content == [(second, [])]
    assert memory._compressed_summary == "new"
    tx.accept_committed_state.assert_called_once_with({"revision": 5})
    assert facade.compact.await_args.kwargs["recovery_state"][
        "unknown_module"
    ] == {"keep": True}


async def test_reset_binding_publishes_clear_root_before_memory_changes(
    tmp_path,
):
    from swe.app.conversation_storage.archive_runtime import MySQLArchiveStore
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.identity import WorkspaceScope

    memory = SimpleNamespace(
        content=[(Msg("user", "one", "user"), [])],
        _compressed_summary="old",
    )
    facade = MySQLArchiveStore(
        SimpleNamespace(),
        WorkspaceScope("t", "s", "a"),
        BundleStore(tmp_path),
        SimpleNamespace(),
    )
    tx = SimpleNamespace(
        accept_committed_state=Mock(),
        mutation_lock=asyncio.Lock(),
        pending_operation=None,
    )
    facade.bind_memory(
        memory,
        "chat",
        tx,
        AsyncMock(return_value={"revision": 2}),
        SimpleNamespace(),
    )
    facade.reset = AsyncMock(
        return_value=SimpleNamespace(
            state=SimpleNamespace(current_epoch=3),
            recovery_state={"revision": 3},
        ),
    )
    state = await memory.reset_context_epoch(reason="clear")
    assert state.current_epoch == 3
    assert memory.content == [] and memory._compressed_summary == ""
    tx.accept_committed_state.assert_called_once_with({"revision": 3})


async def test_compaction_hook_passes_new_summary_to_atomic_indexed_archive():
    from swe.agents.hooks.memory_compaction import MemoryCompactionHook

    memory = SimpleNamespace(
        _indexed_archive_store=object(),
        archive_compacted_messages=AsyncMock(return_value="boundary"),
        update_compressed_summary=AsyncMock(),
    )
    hook = object.__new__(MemoryCompactionHook)
    messages = [Msg("user", "one", "user")]
    assert (
        await hook._persist_compaction_result(memory, messages, "new summary")
        == "boundary"
    )
    memory.archive_compacted_messages.assert_awaited_once_with(
        messages,
        summary="new summary",
    )


def test_compaction_cursor_only_advances_exact_message_event_prefix():
    from swe.app.conversation_storage.archive_runtime import (
        _compacted_watermark,
    )
    from swe.agents.memory.chat_checkpoint import (
        CheckpointEvent,
        CheckpointRecord,
    )
    from swe.agents.memory.conversation_archive import CheckpointArchiveState

    message = Msg("user", "one", "user")
    event = CheckpointEvent.new(
        sequence=1,
        epoch=1,
        type="message_added",
        facts={"message_id": message.id, "role": message.role},
        source_refs=(f"message:{message.id}",),
    )
    record = CheckpointRecord.new(chat_id="chat", epoch=1)
    assert (
        _compacted_watermark(
            CheckpointArchiveState(record, (event,), 1),
            [message],
        )
        == 1
    )
    wrong = Msg("user", "other", "user")
    assert (
        _compacted_watermark(
            CheckpointArchiveState(record, (event,), 1),
            [wrong],
        )
        == 0
    )


def test_cloned_memory_discards_request_binding_and_candidate():
    from swe.agents.memory.reme_light_memory_manager import (
        _clone_unbound_chat_memory,
    )

    memory = SimpleNamespace(
        content=[],
        _compressed_summary="",
        _indexed_archive_store=object(),
        _conversation_archive_binding=object(),
        _checkpoint_candidate_id="old",
    )
    clone = _clone_unbound_chat_memory(memory)
    assert not hasattr(clone, "_conversation_archive_binding")
    assert not hasattr(clone, "_indexed_archive_store")
    assert not hasattr(clone, "_checkpoint_candidate_id")


def test_indexed_evidence_reuses_exact_reference_precedence_without_file_store():
    from swe.app.conversation_storage.archive_reader import (
        EvidenceSelectors,
        _decode,
    )
    from swe.app.conversation_storage.errors import IntegrityError

    selector = EvidenceSelectors()
    query = selector.prepare(["message:q"], "does not match", ["tool"], None)
    message = Msg("user", "other content", "user")
    message.id = "q"
    assert selector.matches(
        message,
        requested=query.requested,
        semantic_query=query.semantic_query,
        kind_filter=query.kind_filter,
        time_bounds=query.time_bounds,
    )
    with pytest.raises(IntegrityError, match="schema"):
        _decode(Msg, {"malformed": True})


async def test_candidate_compact_root_contains_new_checkpoint_projection(
    tmp_path,
):
    from swe.app.conversation_storage.archive_runtime import MySQLArchiveStore
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.agents.memory.chat_checkpoint import CheckpointRecord

    memory = SimpleNamespace(content=[], _compressed_summary="old")
    message = Msg("user", "one", "user")
    memory.content = [(message, [])]
    store = MySQLArchiveStore(
        SimpleNamespace(),
        WorkspaceScope("t", "s", "a"),
        BundleStore(tmp_path),
        SimpleNamespace(),
    )
    candidate = CheckpointRecord.new(
        chat_id="chat",
        epoch=1,
    ).with_current_task(
        "new checkpoint goal",
        (),
        evidence_refs=("message:q",),
    )
    store._candidate = AsyncMock(
        return_value=SimpleNamespace(record=candidate),
    )
    store.compact = AsyncMock(
        return_value=SimpleNamespace(
            boundary="b",
            recovery_state={"revision": 2},
        ),
    )

    async def root():
        return {"revision": 1, "summary": memory._compressed_summary}

    store.bind_memory(
        memory,
        "chat",
        SimpleNamespace(
            accept_committed_state=Mock(),
            mutation_lock=asyncio.Lock(),
            pending_operation=None,
        ),
        root,
        None,
    )
    await memory.archive_checkpoint_messages([message], "candidate")
    assert (
        "new checkpoint goal"
        in store.compact.await_args.kwargs["recovery_state"]["summary"]
    )


async def test_archive_root_provider_result_is_detached_before_publication():
    from swe.app.conversation_storage.archive_binding import ArchiveBinding

    state = {"revision": 1, "unknown": {"keep": True}}
    binding = ArchiveBinding(
        SimpleNamespace(),
        SimpleNamespace(content=[], _compressed_summary="old"),
        "chat",
        None,
        lambda: state,
        None,
    )
    root = await binding._root([], "new")
    state["unknown"]["keep"] = False
    assert root["unknown"]["keep"] is True


@pytest.mark.parametrize(
    "operation",
    ["add", "compact", "reset", "projection"],
)
async def test_memory_mutation_waits_for_request_publication_lock(
    tmp_path,
    operation,
):
    from agentscope.memory import InMemoryMemory
    from swe.app.conversation_storage.archive_runtime import MySQLArchiveStore
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.identity import WorkspaceScope
    from swe.agents.memory.chat_checkpoint import CheckpointRecord

    memory = InMemoryMemory()
    message = Msg("user", "one", "user")
    lock = asyncio.Lock()
    tx = SimpleNamespace(
        mutation_lock=lock,
        pending_operation=None,
        accept_committed_state=Mock(),
    )
    store = MySQLArchiveStore(
        SimpleNamespace(),
        WorkspaceScope("t", "s", "a"),
        BundleStore(tmp_path),
        SimpleNamespace(),
    )
    store.bind_memory(memory, "chat", tx, lambda: {"revision": 1}, None)
    committed = SimpleNamespace(
        recovery_state={"revision": 2},
        boundary="boundary",
        state=SimpleNamespace(current_epoch=2),
    )
    store.append_message = AsyncMock(return_value=committed)
    store.compact = AsyncMock(return_value=committed)
    store.reset = AsyncMock(return_value=committed)
    store.write_active_checkpoint = AsyncMock(return_value=committed)
    record = CheckpointRecord.new(chat_id="chat", epoch=1)
    store.read_checkpoint_state = AsyncMock(
        return_value=SimpleNamespace(record=record, events=[]),
    )
    operations = {
        "add": lambda: memory.add(message),
        "compact": lambda: memory.archive_compacted_messages([]),
        "reset": lambda: memory.reset_context_epoch(reason="clear"),
        "projection": lambda: memory.install_checkpoint_projection(record),
    }
    async with lock:
        task = asyncio.create_task(operations[operation]())
        await asyncio.sleep(0)
        waiting = not task.done()
        unmodified = not memory.content
    await task
    assert waiting, "archive mutation bypassed request publication lock"
    assert unmodified, "memory changed before publication lock was acquired"


async def test_unknown_publication_blocks_memory_and_archive_side_effects(
    tmp_path,
):
    from agentscope.memory import InMemoryMemory
    from swe.app.conversation_storage.archive_runtime import MySQLArchiveStore
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.errors import UnknownCommit
    from swe.app.conversation_storage.identity import WorkspaceScope

    memory = InMemoryMemory()
    tx = SimpleNamespace(
        mutation_lock=asyncio.Lock(),
        pending_operation="original-key",
        accept_committed_state=Mock(),
    )
    store = MySQLArchiveStore(
        SimpleNamespace(),
        WorkspaceScope("t", "s", "a"),
        BundleStore(tmp_path),
        SimpleNamespace(),
    )
    store.bind_memory(memory, "chat", tx, lambda: {"revision": 1}, None)
    store.append_message = AsyncMock()
    with pytest.raises(UnknownCommit, match="original-key"):
        await memory.add(Msg("user", "later", "user"))
    assert not memory.content
    store.append_message.assert_not_called()

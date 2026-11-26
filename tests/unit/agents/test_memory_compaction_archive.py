# -*- coding: utf-8 -*-
"""Regression coverage for chat-scoped compaction archive integration."""

import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from agentscope.message import Msg

from swe.agents.command_handler import CommandHandler
from swe.agents.hooks.memory_compaction import MemoryCompactionHook
from swe.agents.memory.conversation_archive import (
    attach_conversation_archive,
)
from swe.agents.memory.chat_checkpoint import CheckpointEvent
from swe.agents.memory.reme_light_memory_manager import ReMeLightMemoryManager
from swe.config.config import ToolResultCompactConfig
from swe.observability import Observability, Scope, bind_context
from swe.observability.emitter import JsonlEmitter
from swe.observability.projector import _validate_payload


def _chat_id() -> str:
    return str(uuid.uuid4())


def _message(index: int) -> Msg:
    message = Msg(
        name="user",
        role="user",
        content=f"message-{index}",
        timestamp=f"2026-08-01 12:00:{index:02d}.000",
    )
    message.id = f"message-{index}"
    return message


@pytest.mark.asyncio
async def test_chat_memory_commits_archive_before_removing_online_messages(
    tmp_path,
) -> None:
    first, second = _message(1), _message(2)
    memory = SimpleNamespace(content=[(first, []), (second, [])])
    attach_conversation_archive(memory, tmp_path / "dialog", _chat_id())

    boundary = await memory.archive_compacted_messages([first])

    assert [message.id for message, _marks in memory.content] == ["message-2"]
    page = await memory.conversation_archive_store.read_page(boundary.chat_id)
    assert [message.id for message in page.messages] == ["message-1"]


@pytest.mark.asyncio
async def test_chat_memory_removes_only_selected_duplicate_id_occurrence(
    tmp_path,
) -> None:
    archived = _message(1)
    retained = _message(2)
    archived.id = retained.id = "duplicate-id"
    memory = SimpleNamespace(content=[(archived, []), (retained, [])])
    chat_id = _chat_id()
    attach_conversation_archive(memory, tmp_path / "dialog", chat_id)

    await memory.archive_compacted_messages([archived])

    assert [message.content for message, _marks in memory.content] == [
        "message-2",
    ]
    page = await memory.conversation_archive_store.read_page(chat_id)
    assert [message.content for message in page.messages] == ["message-1"]


@pytest.mark.asyncio
async def test_legacy_archive_advances_event_cursor_for_next_candidate(
    tmp_path,
) -> None:
    old, retained = _message(1), _message(2)
    memory = SimpleNamespace(content=[], _compressed_summary="")

    async def add(message: Msg) -> None:
        memory.content.append((message, []))

    memory.add = add
    chat_id = _chat_id()
    attach_conversation_archive(memory, tmp_path / "dialog", chat_id)
    await memory.add(old)
    await memory.add(retained)

    await memory.archive_compacted_messages([old])

    state = await memory.chat_checkpoint_store.read_checkpoint_state(chat_id)
    assert state.record.applied_event_sequence == 1
    assert [event.source_refs for event in state.events] == [
        ("message:message-2",),
    ]

    manager = object.__new__(ReMeLightMemoryManager)
    manager.get_in_memory_memory = lambda **_kwargs: memory

    assert await manager.schedule_precompaction(
        chat_id=chat_id,
        watermark=0,
        messages=[retained],
        memory=memory,
    )


@pytest.mark.asyncio
async def test_legacy_archive_skips_nonprefix_duplicate_cursor_advance(
    tmp_path,
) -> None:
    first, selected_later = _message(1), _message(2)
    first.id = selected_later.id = "duplicate-id"
    memory = SimpleNamespace(content=[], _compressed_summary="")

    async def add(message: Msg) -> None:
        memory.content.append((message, []))

    memory.add = add
    chat_id = _chat_id()
    attach_conversation_archive(memory, tmp_path / "dialog", chat_id)
    await memory.add(first)
    await memory.add(selected_later)

    await memory.archive_compacted_messages([selected_later])

    state = await memory.chat_checkpoint_store.read_checkpoint_state(chat_id)
    assert state.record.applied_event_sequence == 0
    assert [event.source_refs for event in state.events] == [
        ("message:duplicate-id",),
        ("message:duplicate-id",),
    ]


@pytest.mark.asyncio
async def test_legacy_archive_advances_copied_unique_prefix_cursor(
    tmp_path,
) -> None:
    original = _message(1)
    selected_copy = Msg.from_dict(original.to_dict())
    memory = SimpleNamespace(content=[], _compressed_summary="")

    async def add(message: Msg) -> None:
        memory.content.append((message, []))

    memory.add = add
    chat_id = _chat_id()
    attach_conversation_archive(memory, tmp_path / "dialog", chat_id)
    await memory.add(original)

    await memory.archive_compacted_messages([selected_copy])

    state = await memory.chat_checkpoint_store.read_checkpoint_state(chat_id)
    assert state.record.applied_event_sequence == 1
    assert memory.content == []


@pytest.mark.asyncio
async def test_legacy_archive_does_not_partially_advance_event_cursor(
    tmp_path,
) -> None:
    tracked, untracked = _message(1), _message(2)
    memory = SimpleNamespace(content=[], _compressed_summary="")

    async def add(message: Msg) -> None:
        memory.content.append((message, []))

    memory.add = add
    chat_id = _chat_id()
    attach_conversation_archive(memory, tmp_path / "dialog", chat_id)
    await memory.add(tracked)
    memory.content.append((untracked, []))

    await memory.archive_compacted_messages([tracked, untracked])

    state = await memory.chat_checkpoint_store.read_checkpoint_state(chat_id)
    assert state.record.applied_event_sequence == 0
    assert [event.sequence for event in state.events] == [1]


@pytest.mark.asyncio
async def test_legacy_archive_rejects_mismatched_event_metadata_for_cursor(
    tmp_path,
) -> None:
    message = _message(1)
    memory = SimpleNamespace(content=[(message, [])], _compressed_summary="")
    chat_id = _chat_id()
    attach_conversation_archive(memory, tmp_path / "dialog", chat_id)
    await memory.chat_checkpoint_store.append_checkpoint_event(
        chat_id,
        CheckpointEvent.new(
            sequence=1,
            epoch=1,
            type="unexpected",
            facts={"message_id": message.id, "role": message.role},
            source_refs=(f"message:{message.id}",),
        ),
    )

    await memory.archive_compacted_messages([message])

    state = await memory.chat_checkpoint_store.read_checkpoint_state(chat_id)
    assert state.record.applied_event_sequence == 0


@pytest.mark.asyncio
async def test_empty_legacy_event_cursor_advance_is_a_noop(tmp_path) -> None:
    message = _message(1)
    memory = SimpleNamespace(content=[], _compressed_summary="")

    async def add(item: Msg) -> None:
        memory.content.append((item, []))

    memory.add = add
    chat_id = _chat_id()
    attach_conversation_archive(memory, tmp_path / "dialog", chat_id)
    await memory.add(message)

    await memory.chat_checkpoint_store.advance_archived_message_events(
        chat_id,
        [],
        str(uuid.uuid4()),
    )

    state = await memory.chat_checkpoint_store.read_checkpoint_state(chat_id)
    assert state.record.applied_event_sequence == 0


@pytest.mark.asyncio
async def test_legacy_archive_preserves_online_duplicates_when_copy_is_ambiguous(
    tmp_path,
) -> None:
    first, retained = _message(1), _message(2)
    first.id = retained.id = "duplicate-id"
    selected_copy = Msg.from_dict(retained.to_dict())
    memory = SimpleNamespace(content=[(first, []), (retained, [])])
    attach_conversation_archive(memory, tmp_path / "dialog", _chat_id())

    await memory.archive_compacted_messages([selected_copy])

    assert [message.content for message, _marks in memory.content] == [
        "message-1",
        "message-2",
    ]


@pytest.mark.asyncio
async def test_chat_memory_clear_does_not_create_a_recoverable_boundary(
    tmp_path,
) -> None:
    memory = SimpleNamespace(content=[(_message(1), [])])
    chat_id = _chat_id()
    attach_conversation_archive(memory, tmp_path / "dialog", chat_id)

    memory.clear_content()

    assert memory.content == []
    assert (
        await memory.conversation_archive_store.read_page(chat_id)
    ).messages == []


@pytest.mark.asyncio
async def test_reme_memory_factory_attaches_archive_only_for_a_chat_id(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_memory = SimpleNamespace(content=[], _compressed_summary="")

    async def add(message: Msg) -> None:
        raw_memory.content.append((message, []))

    raw_memory.add = add
    factory_calls = 0

    def get_memory(**_kwargs):
        nonlocal factory_calls
        factory_calls += 1
        return raw_memory

    manager = object.__new__(ReMeLightMemoryManager)
    manager.agent_id = "default"
    manager.tenant_id = None
    manager.working_dir = str(tmp_path)
    manager._warn_if_version_mismatch = lambda: None
    manager._reme = SimpleNamespace(
        get_in_memory_memory=get_memory,
    )
    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.load_agent_config",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.get_swe_token_counter",
        lambda _config: object(),
    )

    unbound_memory = manager.get_in_memory_memory()
    assert unbound_memory is not raw_memory
    assert not hasattr(unbound_memory, "chat_checkpoint_store")
    chat_id = _chat_id()
    first_memory = manager.get_in_memory_memory(chat_id=chat_id)
    second_memory = manager.get_in_memory_memory(chat_id=chat_id)
    assert first_memory is not raw_memory
    assert second_memory is not first_memory
    assert factory_calls == 3
    assert hasattr(first_memory, "archive_compacted_messages")

    other_chat = _chat_id()
    other_memory = manager.get_in_memory_memory(chat_id=other_chat)
    assert other_memory is not raw_memory
    assert manager.get_in_memory_memory(chat_id=chat_id) is not first_memory
    await first_memory.add(_message(1))
    await other_memory.add(_message(2))
    await second_memory.add(_message(3))
    first_state = (
        await first_memory.chat_checkpoint_store.read_checkpoint_state(
            chat_id,
        )
    )
    second_state = (
        await other_memory.chat_checkpoint_store.read_checkpoint_state(
            other_chat,
        )
    )
    assert [event.source_refs for event in first_state.events] == [
        ("message:message-1",),
        ("message:message-3",),
    ]
    assert [event.source_refs for event in second_state.events] == [
        ("message:message-2",),
    ]


@pytest.mark.asyncio
async def test_automatic_compaction_persists_before_summary_and_returns_boundary() -> (
    None
):
    boundary = SimpleNamespace(id="boundary-1")
    memory = SimpleNamespace(
        archive_compacted_messages=AsyncMock(return_value=boundary),
        update_compressed_summary=AsyncMock(),
    )
    hook = MemoryCompactionHook(SimpleNamespace())

    result = await hook._persist_compaction_result(
        memory,
        [_message(1)],
        "summary",
    )

    assert result is boundary
    memory.archive_compacted_messages.assert_awaited_once()
    memory.update_compressed_summary.assert_awaited_once_with("summary")


@pytest.mark.asyncio
async def test_automatic_compaction_emits_only_one_boundary_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = _message(1)
    boundary = SimpleNamespace(
        to_dict=lambda: {"id": "boundary-1", "archived_message_count": 1},
    )
    memory = SimpleNamespace(
        get_compressed_summary=lambda: "",
        get_memory=AsyncMock(side_effect=[[message], [message], []]),
        archive_compacted_messages=AsyncMock(return_value=boundary),
        update_compressed_summary=AsyncMock(),
    )
    manager = SimpleNamespace(
        agent_id="default",
        tenant_id=None,
        compact_tool_result=AsyncMock(),
        check_context=AsyncMock(return_value=([message], [], True)),
        compact_memory=AsyncMock(return_value="summary"),
    )
    running = SimpleNamespace(
        memory_compact_threshold=10,
        memory_compact_reserve=0,
        tool_result_compact=ToolResultCompactConfig(enabled=False),
        memory_summary=SimpleNamespace(memory_summary_enabled=False),
        context_compact=SimpleNamespace(context_compact_enabled=True),
    )
    monkeypatch.setattr(
        "swe.agents.hooks.memory_compaction.load_agent_config",
        lambda *_args, **_kwargs: SimpleNamespace(running=running),
    )

    async def count(messages, **kwargs):
        return 3 if messages or "text" in kwargs else 0

    monkeypatch.setattr(
        "swe.agents.hooks.memory_compaction.get_swe_token_counter",
        lambda _config: SimpleNamespace(count=count),
    )
    agent = SimpleNamespace(
        name="agent",
        sys_prompt="",
        memory=memory,
        model=object(),
        formatter=object(),
        print=AsyncMock(),
        _request_context={"chat_id": _chat_id()},
    )

    sink = StringIO()
    emitter = JsonlEmitter(stream=sink)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )
    context = (
        Observability()
        .prepare_run(
            scope=Scope("swe", "source", "tenant", "agent"),
            enabled=True,
        )
        .new_attempt()
    )
    with bind_context(context):
        await MemoryCompactionHook(manager)(agent, {})

    printed = agent.print.await_args.args[0]
    assert printed.content == [{"type": "text", "text": ""}]
    assert (
        printed.metadata["conversation_compaction_boundary"]["id"]
        == "boundary-1"
    )
    assert agent.print.await_count == 1
    assert emitter.close() == 0
    started, ended = [
        _validate_payload(line) for line in sink.getvalue().splitlines()
    ]
    assert started["span_type"] == ended["span_type"] == "Context"
    assert started["event_name"] == "span.started"
    assert ended["event_name"] == "span.ended"
    assert ended["status"] == "succeeded"
    assert ended["compaction_strategy"] == "summary"
    assert ended["metrics"]["context_before_tokens"] == 6
    assert "context_after_tokens" not in ended["metrics"]
    assert message.get_text_content() not in sink.getvalue()


@pytest.mark.asyncio
async def test_manual_compact_emits_only_boundary_metadata_for_chat_memory() -> (
    None
):
    boundary = SimpleNamespace(
        to_dict=lambda: {
            "id": "boundary-1",
            "archived_message_count": 1,
        },
    )
    memory = SimpleNamespace(
        get_compressed_summary=lambda: "",
        archive_compacted_messages=AsyncMock(return_value=boundary),
        update_compressed_summary=AsyncMock(),
        clear_content=AsyncMock(),
    )
    manager = SimpleNamespace(
        add_async_summary_task=lambda **_kwargs: None,
        compact_memory=AsyncMock(return_value="summary"),
    )
    handler = CommandHandler(
        agent_name="agent",
        memory=memory,
        memory_manager=manager,
        request_context={"chat_id": _chat_id()},
    )

    result = await handler._process_compact([_message(1)])

    assert result.content == [
        {"type": "text", "text": ""},
    ]
    assert (
        result.metadata["conversation_compaction_boundary"]["id"]
        == "boundary-1"
    )
    memory.archive_compacted_messages.assert_awaited_once()
    memory.update_compressed_summary.assert_awaited_once_with("summary")
    memory.clear_content.assert_not_called()


@pytest.mark.asyncio
async def test_load_history_resets_checkpoint_epoch_before_replacing_memory(
    tmp_path,
) -> None:
    loaded = _message(1)
    (tmp_path / "debug_history.jsonl").write_text(
        json.dumps(loaded.to_dict()) + "\n",
        encoding="utf-8",
    )
    memory = SimpleNamespace(
        content=[],
        clear_compressed_summary=lambda: None,
    )

    async def add(message: Msg) -> None:
        memory.content.append((message, []))

    memory.add = add
    manager = SimpleNamespace(reset_context_epoch=AsyncMock())
    chat_id = _chat_id()
    handler = CommandHandler(
        agent_name="agent",
        memory=memory,
        memory_manager=manager,
        request_context={"chat_id": chat_id},
    )
    handler._get_agent_config = lambda: SimpleNamespace(workspace_dir=tmp_path)

    await handler._process_load_history([])

    manager.reset_context_epoch.assert_awaited_once_with(
        chat_id=chat_id,
        reason="load_history",
        memory=memory,
    )
    assert [message.id for message, _marks in memory.content] == [
        "message-1",
    ]


@pytest.mark.asyncio
async def test_load_history_parse_failure_does_not_reset_checkpoint_epoch(
    tmp_path,
) -> None:
    (tmp_path / "debug_history.jsonl").write_text(
        "not-json\n",
        encoding="utf-8",
    )
    memory = SimpleNamespace(
        content=[],
        clear_compressed_summary=lambda: None,
    )
    manager = SimpleNamespace(reset_context_epoch=AsyncMock())
    handler = CommandHandler(
        agent_name="agent",
        memory=memory,
        memory_manager=manager,
        request_context={"chat_id": _chat_id()},
    )
    handler._get_agent_config = lambda: SimpleNamespace(workspace_dir=tmp_path)

    await handler._process_load_history([])

    manager.reset_context_epoch.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_and_clear_do_not_call_the_archive_method() -> None:
    memory = SimpleNamespace(
        clear_compressed_summary=lambda: None,
        clear_content=lambda: None,
        archive_compacted_messages=AsyncMock(),
    )
    handler = CommandHandler(
        agent_name="agent",
        memory=memory,
        memory_manager=SimpleNamespace(),
    )

    await handler._process_new([])
    await handler._process_clear([])

    memory.archive_compacted_messages.assert_not_awaited()

# -*- coding: utf-8 -*-
"""Request memory preparation stays outside the query's event loop."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from swe.agents.memory.conversation_archive import ConversationArchiveStore
from swe.agents.memory.reme_light_memory_manager import ReMeLightMemoryManager
from swe.agents.react_agent import SWEAgent


def _manager(tmp_path, monkeypatch):
    manager = object.__new__(ReMeLightMemoryManager)
    manager.agent_id = "default"
    manager.tenant_id = None
    manager.working_dir = str(tmp_path)
    manager._warn_if_version_mismatch = lambda: None
    manager._reme = SimpleNamespace(
        get_in_memory_memory=lambda **_kwargs: SimpleNamespace(content=[]),
    )
    monkeypatch.setattr(
        manager,
        "_load_agent_config",
        SimpleNamespace,
    )
    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.get_swe_token_counter",
        lambda _config: object(),
    )
    return manager


@pytest.mark.asyncio
async def test_request_preparation_reads_epoch_once_in_worker(
    tmp_path,
    monkeypatch,
):
    manager = _manager(tmp_path, monkeypatch)
    event_loop_thread = threading.get_ident()
    reads = []
    original = ConversationArchiveStore._read_checkpoint_epoch

    def read(store, chat_id):
        reads.append(threading.get_ident())
        assert threading.get_ident() != event_loop_thread
        return original(store, chat_id)

    monkeypatch.setattr(
        ConversationArchiveStore,
        "_read_checkpoint_epoch",
        read,
    )

    memory = await manager.prepare_request_memory(str(uuid4()))

    assert memory._chat_checkpoint_epoch == 1
    assert len(reads) == 1


@pytest.mark.asyncio
async def test_preparation_captures_new_epoch_after_context_reset(
    tmp_path,
    monkeypatch,
):
    manager = _manager(tmp_path, monkeypatch)
    chat_id = str(uuid4())
    first = await manager.prepare_request_memory(chat_id)
    await first.reset_context_epoch(reason="new")

    prepared = await manager.prepare_request_memory(chat_id)

    assert prepared is not first
    assert prepared._chat_checkpoint_epoch == 2
    assert prepared.content == []


@pytest.mark.asyncio
async def test_preparation_preserves_invalid_chat_id_error(
    tmp_path,
    monkeypatch,
):
    manager = _manager(tmp_path, monkeypatch)

    with pytest.raises(ValueError):
        await manager.prepare_request_memory("../other-chat")


@pytest.mark.asyncio
async def test_blocked_checkpoint_preparation_keeps_loop_responsive(
    tmp_path,
    monkeypatch,
):
    manager = _manager(tmp_path, monkeypatch)
    started = threading.Event()
    release = threading.Event()
    original = ConversationArchiveStore._read_checkpoint_epoch

    def read(store, chat_id):
        started.set()
        assert release.wait(timeout=2)
        return original(store, chat_id)

    monkeypatch.setattr(
        ConversationArchiveStore,
        "_read_checkpoint_epoch",
        read,
    )
    task = asyncio.create_task(manager.prepare_request_memory(str(uuid4())))
    try:
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()
        assert not task.done()
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_agent_uses_prepared_memory_epoch_without_archive_reread(
    monkeypatch,
):
    monkeypatch.delenv("ENABLE_MEMORY_MANAGER", raising=False)
    chat_id = str(uuid4())
    memory = SimpleNamespace(
        _chat_checkpoint_epoch=7,
        chat_checkpoint_store=SimpleNamespace(
            _read_checkpoint_state=lambda *_args: pytest.fail(
                "checkpoint reread",
            ),
        ),
    )
    manager = SimpleNamespace(
        create_request_memory=lambda *_args: pytest.fail("memory recreated"),
        recover_evidence=AsyncMock(return_value=[]),
    )
    tools = []
    agent = object.__new__(SWEAgent)
    agent._request_context = {"chat_id": chat_id}
    agent.toolkit = SimpleNamespace(
        register_tool_function=lambda tool, **_kwargs: tools.append(tool),
    )

    agent._setup_memory_manager(
        True,
        manager,
        "skip",
        prepared_request_memory=memory,
    )

    assert agent.memory is memory
    recover = next(
        tool for tool in tools if tool.__name__ == "recover_evidence"
    )
    await recover()
    assert manager.recover_evidence.await_args.kwargs["epoch"] == 7


def test_prepared_empty_memory_does_not_call_sync_factory(monkeypatch):
    monkeypatch.delenv("ENABLE_MEMORY_MANAGER", raising=False)
    agent = object.__new__(SWEAgent)
    agent._request_context = {"chat_id": str(uuid4())}
    agent.toolkit = SimpleNamespace(
        register_tool_function=lambda *_args, **_kw: None,
    )
    manager = SimpleNamespace(
        create_request_memory=lambda *_args: pytest.fail(
            "sync factory called",
        ),
    )

    agent._setup_memory_manager(
        True,
        manager,
        "skip",
        prepared_request_memory=None,
    )

    assert agent.memory is None

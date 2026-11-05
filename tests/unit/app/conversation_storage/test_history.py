# -*- coding: utf-8 -*-
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentscope.message import Msg

from swe.app.conversation_storage.bundle import BundleStore
from swe.app.conversation_storage.history import HistoryStore
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.errors import (
    IntegrityError,
    StorageUnavailable,
)
from swe.app.runner import api
from swe.app.runner.models import ChatHistory, ChatSpec, ChatArchivePage


def history(tmp_path):
    return HistoryStore(
        None,
        WorkspaceScope(
            "tenant",
            "source",
            "agent",
            service_unit_id="schema-test-service",
        ),
        BundleStore(tmp_path),
        cursor_secret=b"test-secret",
    )


def test_archive_cursor_binds_scope_chat_epoch_and_high(tmp_path):
    store = history(tmp_path)
    token = store._encode_cursor("chat", epoch=3, high=10, before=7)
    assert store._decode_cursor(token, "chat") == {
        "v": 2,
        "chat": "chat",
        "epoch": 3,
        "high": 10,
        "before": 7,
    }
    with pytest.raises(ValueError, match="cursor"):
        store._decode_cursor(token, "other")
    with pytest.raises(ValueError, match="cursor"):
        store._decode_cursor(token + "x", "chat")
    other = HistoryStore(
        None,
        WorkspaceScope(
            "tenant",
            "other",
            "agent",
            service_unit_id="schema-test-service",
        ),
        BundleStore(tmp_path),
        cursor_secret=b"test-secret",
    )
    with pytest.raises(ValueError, match="cursor"):
        other._decode_cursor(token, "chat")


async def test_memory_payload_preserves_duplicate_source_and_fragments(
    tmp_path,
):
    store = history(tmp_path)
    msg = Msg(
        "assistant",
        [{"type": "thinking", "thinking": "a"}, {"type": "text", "text": "b"}],
        "assistant",
    )
    msg.id = "duplicate"
    rows = [
        {"layer": "memory", "source_occurrence_no": i, "task_run_key": None}
        for i in (1, 2)
    ]
    messages = await store._convert(
        rows,
        [msg.to_dict(), msg.to_dict()],
        "session",
    )
    assert len(messages) == 4
    assert {api._message_original_id(m) for m in messages} == {"duplicate"}
    assert messages[0].id == messages[2].id


async def test_current_route_uses_injected_history_without_file_session(
    tmp_path,
):
    spec = ChatSpec(id="chat", session_id="s", user_id="alice")
    injected = SimpleNamespace(
        current=AsyncMock(return_value=ChatHistory(chat=spec)),
    )
    workspace = SimpleNamespace(
        conversation_storage=SimpleNamespace(history=injected),
    )
    result = await api._build_chat_history(
        spec,
        session=object(),
        workspace=workspace,
    )
    assert result.chat.id == "chat"
    injected.current.assert_awaited_once()


async def test_archive_route_uses_injected_indexed_reader(tmp_path):
    expected = ChatArchivePage(has_more=True, next_cursor="cursor")
    injected = SimpleNamespace(archive_page=AsyncMock(return_value=expected))
    workspace = SimpleNamespace(
        conversation_storage=SimpleNamespace(history=injected),
    )
    assert await api._archive_page(workspace, "chat", None, 5) is expected
    injected.archive_page.assert_awaited_once_with(
        "chat",
        before=None,
        limit=5,
    )


async def test_task_projection_envelope_preserves_exact_run_index(tmp_path):
    store = history(tmp_path)
    msg = Msg("assistant", "answer", "assistant")
    msg.id = "raw"
    row = {
        "layer": "memory",
        "source_occurrence_no": 0,
        "task_run_key": b"run",
    }
    payload = {
        "version": 1,
        "message": msg.to_dict(),
        "history_projection": {
            "task_run_id": "run",
            "task_run_index": 7,
            "task_run_section": "final",
        },
    }
    messages = await store._convert([row], [payload], "session")
    assert messages[0].metadata["task_run_index"] == 7
    assert messages[0].metadata["task_run_section"] == "final"
    with pytest.raises(IntegrityError, match="projection"):
        await store._convert([row], [msg.to_dict()], "session")


async def test_tracing_overlay_binds_target_scope_and_restores_operator(
    tmp_path,
    monkeypatch,
):
    from swe.config.context import (
        tenant_context,
        get_current_scope_id,
        resolve_scope_id,
    )
    from swe.app.conversation_storage.history import HistorySnapshot

    store = history(tmp_path)
    spec = ChatSpec(id="chat", session_id="s", user_id="bob")
    store.chats._hydrate = AsyncMock(return_value=[spec])
    store._validate_chat_row = AsyncMock()
    store._payloads = AsyncMock(return_value=[])
    store._finish = AsyncMock()
    seen = []

    async def overlay(messages):
        seen.append(get_current_scope_id())
        return messages

    monkeypatch.setattr(api, "_annotate_approval_action_statuses", overlay)
    with tenant_context(tenant_id="operator", source_id="operator-source"):
        operator = get_current_scope_id()
        await store._assemble(HistorySnapshot({}, [], [], []), tracing=True)
        assert get_current_scope_id() == operator
    assert seen == [resolve_scope_id("tenant", "source")]


@pytest.mark.parametrize("kind", ["storage", "mysql", "nas"])
async def test_indexed_detail_storage_failure_maps_to_503(tmp_path, kind):
    from fastapi import HTTPException
    from mysql.connector.errors import InterfaceError

    spec = ChatSpec(id="chat", session_id="s", user_id="alice")
    error = {
        "storage": StorageUnavailable("db unavailable"),
        "mysql": InterfaceError("connection lost"),
        "nas": OSError("NAS missing"),
    }[kind]
    injected = SimpleNamespace(current=AsyncMock(side_effect=error))
    workspace = SimpleNamespace(
        conversation_storage=SimpleNamespace(history=injected),
    )
    manager = SimpleNamespace(get_chat=AsyncMock(return_value=spec))
    with pytest.raises(HTTPException) as failure:
        await api.get_chat(
            SimpleNamespace(state=SimpleNamespace(user_id="alice")),
            "chat",
            mgr=manager,
            session=object(),
            workspace=workspace,
        )
    assert failure.value.status_code == 503


async def test_target_approval_overlay_uses_actual_scope_filter(
    tmp_path,
    monkeypatch,
):
    from swe.app.approvals.service import ApprovalService
    from swe.config.context import tenant_context, resolve_scope_id
    from swe.app.conversation_storage.history import HistorySnapshot
    from swe.app.runner.models import ChatMessage

    service = ApprovalService()
    service._completed["same-id"] = SimpleNamespace(
        scope_id=resolve_scope_id("tenant", "source"),
        status="approved",
    )
    monkeypatch.setattr(api, "get_approval_service", lambda: service)
    store = history(tmp_path)
    spec = ChatSpec(id="chat", session_id="s", user_id="bob")
    store.chats._hydrate = AsyncMock(return_value=[spec])
    store._validate_chat_row = AsyncMock()
    store._payloads = AsyncMock(return_value=[])
    store._finish = AsyncMock()
    message = ChatMessage(
        id="approval",
        role="assistant",
        content=[],
        metadata={
            "metadata": {
                "approval_action": {
                    "requestId": "same-id",
                    "status": "pending",
                },
            },
        },
    )
    store._convert = AsyncMock(return_value=[message])
    with tenant_context(tenant_id="operator", source_id="operator-source"):
        result = await store._assemble(
            HistorySnapshot({}, [], [], []),
            tracing=True,
        )
    assert (
        result.messages[0].metadata["metadata"]["approval_action"]["status"]
        == "approved"
    )

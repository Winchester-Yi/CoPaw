# -*- coding: utf-8 -*-
"""Analytics reads the same durable history and archives as Chat."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from agentscope.message import Msg
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from mysql.connector.errors import Error as MySQLError

from swe.agents.memory.conversation_archive import ConversationArchiveStore
from swe.app.conversation_storage.bundle import BundleStore
from swe.app.conversation_storage.errors import (
    ConflictError,
    StorageUnavailable,
)
from swe.app.conversation_storage.history import HistoryStore
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.runner import api
from swe.app.routers import tracing
from swe.app.runner.models import ChatArchivePage, ChatHistory, ChatSpec
from swe.app.runner.session import SafeJSONSession


@pytest.fixture
async def history_client(tmp_path, monkeypatch):
    chat = ChatSpec(
        id=str(uuid4()),
        session_id="s",
        user_id="target",
        channel="console",
        name="test",
    )
    session = SafeJSONSession(str(tmp_path / "sessions"))
    manager = SimpleNamespace(get_chat=AsyncMock(return_value=chat))
    workspace = SimpleNamespace(
        chat_manager=manager,
        runner=SimpleNamespace(session=session),
        workspace_dir=tmp_path,
    )
    monkeypatch.setattr(
        tracing,
        "_get_target_workspace",
        AsyncMock(return_value=workspace),
    )
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(tracing.router)
    app.dependency_overrides[api.get_workspace] = lambda: workspace
    app.dependency_overrides[api.get_chat_manager] = lambda: manager
    app.dependency_overrides[api.get_session] = lambda: session
    return (
        TestClient(app, raise_server_exceptions=False),
        session,
        chat,
        workspace,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["task", "failed", "memory", "cold-turns"])
async def test_detail_parity(history_client, kind):
    client, session, chat, workspace = history_client
    if kind == "cold-turns":
        state = {
            "turn_states": {
                f"t{i}": {
                    "status": "completed",
                    "message": {
                        "id": f"t{i}",
                        "role": "user",
                        "content": "question",
                    },
                    "created_at": str(i),
                    "chat_id": chat.id,
                }
                for i in range(80)
            },
        }
    elif kind == "memory":
        raw = Msg(
            "user",
            "visible + secret",
            "user",
            metadata={
                "console_hidden_context_v1": {
                    "visible_text": "visible",
                    "suffix": " + secret",
                },
            },
        )
        state = {"agent": {"memory": {"content": [[raw.to_dict(), []]]}}}
    else:
        key = (
            "task_messages" if kind == "task" else "model_call_failed_messages"
        )
        state = {
            key: [
                {
                    "id": "m",
                    "role": "assistant",
                    "type": "message" if kind == "task" else "error",
                    "content": [{"type": "text", "text": "result"}],
                    "code": "model_call_failed",
                    "message": "provider failed",
                    "status": "completed" if kind == "task" else "failed",
                },
            ],
        }
    await session.save_merged_state("s", user_id="target", state=state)
    if kind == "cold-turns":
        await session.migrate_terminal_turns("s", "target", dry_run=False)
    normal = client.get(f"/chats/{chat.id}")
    analytics = client.get(
        f"/tracing/chats/{chat.id}",
        params={"user_id": "target"},
    )
    assert normal.status_code == analytics.status_code == 200
    assert analytics.json() == normal.json()
    assert analytics.json()["messages"]
    assert "secret" not in str(analytics.json())


@pytest.mark.asyncio
async def test_archive_pages_and_validation(history_client):
    client, _, chat, workspace = history_client
    store = ConversationArchiveStore(workspace.workspace_dir / "dialog")
    await store.commit(
        chat.id,
        [Msg("user", f"archive-{i}", "user") for i in range(5)],
    )
    before = None
    seen = []
    while True:
        params = {"limit": 2}
        if before:
            params["before"] = before
        normal = client.get(f"/chats/{chat.id}/history", params=params)
        analytics = client.get(
            f"/tracing/chats/{chat.id}/history",
            params={**params, "user_id": "target"},
        )
        assert analytics.status_code == normal.status_code == 200
        assert analytics.json() == normal.json()
        page = analytics.json()
        seen.extend(m["id"] for m in page["messages"])
        if not page["has_more"]:
            break
        before = page["next_cursor"]
    assert len(seen) == len(set(seen)) == 5
    for params in ({"limit": 51}, {"limit": 0}, {"before": "invalid"}):
        assert (
            client.get(
                f"/tracing/chats/{chat.id}/history",
                params={**params, "user_id": "target"},
            ).status_code
            == 422
        )
    for suffix in ("", "/history"):
        assert (
            client.get(
                f"/tracing/chats/{chat.id}{suffix}",
                params={"user_id": "other"},
            ).status_code
            == 404
        )


@pytest.mark.asyncio
async def test_running_history_does_not_take_execution_lock(history_client):
    client, session, chat, workspace = history_client
    await session.save_merged_state(
        "s",
        user_id="target",
        state={"task_messages": [{"id": "saved", "content": "committed"}]},
    )
    workspace.answer_turn_coordinator = SimpleNamespace(
        status=AsyncMock(return_value=SimpleNamespace(value="running")),
    )
    async with session.execution("s", "target"):
        response = client.get(
            f"/tracing/chats/{chat.id}",
            params={"user_id": "target"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert response.json()["messages"][0]["id"] == "saved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,header",
    [("source_id", "X-Source-Id"), ("agent_id", "X-Agent-Id")],
)
async def test_target_metadata_isolation(history_client, key, header):
    client, _, chat, _ = history_client
    chat.meta[key] = "original"
    for suffix in ("", "/history"):
        response = client.get(
            f"/tracing/chats/{chat.id}{suffix}",
            params={"user_id": "target"},
            headers={header: "other"},
        )
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_empty_archive_and_missing_target(history_client):
    client, _, chat, workspace = history_client
    response = client.get(
        f"/tracing/chats/{chat.id}/history",
        params={"user_id": "target"},
    )
    assert response.status_code == 200
    assert response.json()["messages"] == []
    assert response.json()["has_more"] is False
    assert client.get(f"/tracing/chats/{chat.id}/history").status_code == 422
    workspace.chat_manager.get_chat.return_value = None
    assert (
        client.get(
            f"/tracing/chats/{chat.id}/history",
            params={"user_id": "target"},
        ).status_code
        == 404
    )


@pytest.mark.asyncio
async def test_indexed_tracing_uses_target_current_and_archive(history_client):
    client, _, chat, workspace = history_client

    async def current(chat_id, *, authorize):
        assert chat_id == chat.id
        authorize(chat)
        return ChatHistory(chat=chat, messages=[], status="running")

    async def archive_page(chat_id, *, before, limit, authorize):
        assert (chat_id, before, limit) == (chat.id, None, 2)
        authorize(chat)
        return ChatArchivePage()

    indexed = SimpleNamespace(
        tracing_view=AsyncMock(side_effect=AssertionError("obsolete view")),
        current=AsyncMock(side_effect=current),
        archive_page=AsyncMock(side_effect=archive_page),
    )
    workspace.conversation_storage = SimpleNamespace(history=indexed)
    response = client.get(
        f"/tracing/chats/{chat.id}",
        params={"user_id": "target"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert response.json()["chat"]["id"] == chat.id
    indexed.current.assert_awaited_once()
    indexed.tracing_view.assert_not_awaited()
    response = client.get(
        f"/tracing/chats/{chat.id}/history",
        params={"user_id": "target", "limit": 2},
    )
    assert response.status_code == 200
    indexed.archive_page.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "/history"])
@pytest.mark.parametrize("field", ["id", "user_id", "source_id", "agent_id"])
@pytest.mark.parametrize("phase", ["lookup", "reader"])
async def test_indexed_target_checks_at_each_read(
    history_client,
    suffix,
    field,
    phase,
):
    client, _, chat, workspace = history_client
    changed = chat.model_copy(deep=True)
    if field in ("id", "user_id"):
        setattr(changed, field, "foreign")
    else:
        changed.meta[field] = "foreign"
    if phase == "lookup":
        workspace.chat_manager.get_chat.return_value = changed

    async def read(*args, authorize, **kwargs):
        authorize(changed if phase == "reader" else chat)
        return ChatArchivePage() if suffix else ChatHistory(chat=chat)

    indexed = SimpleNamespace(
        current=AsyncMock(side_effect=read),
        tracing_view=AsyncMock(side_effect=read),
        archive_page=AsyncMock(side_effect=read),
    )
    workspace.conversation_storage = SimpleNamespace(history=indexed)
    response = client.get(
        f"/tracing/chats/{chat.id}{suffix}",
        params={"user_id": "target"},
        headers={"X-Source-Id": "source", "X-Agent-Id": "agent"},
    )
    assert response.status_code == 404
    if phase == "lookup":
        indexed.current.assert_not_awaited()
        indexed.archive_page.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "/history"])
@pytest.mark.parametrize("phase", ["lookup", "reader"])
@pytest.mark.parametrize(
    "error",
    [
        StorageUnavailable("NAS unavailable"),
        ConflictError("revision changed"),
        OSError("read failed"),
        RuntimeError("unavailable"),
        MySQLError("database unavailable"),
    ],
)
async def test_indexed_tracing_storage_failure_is_unavailable(
    history_client,
    suffix,
    phase,
    error,
):
    client, _, chat, workspace = history_client
    indexed = SimpleNamespace(
        current=AsyncMock(side_effect=error),
        tracing_view=AsyncMock(side_effect=error),
        archive_page=AsyncMock(side_effect=error),
    )
    workspace.conversation_storage = SimpleNamespace(history=indexed)
    if phase == "lookup":
        workspace.chat_manager.get_chat.side_effect = error
    response = client.get(
        f"/tracing/chats/{chat.id}{suffix}",
        params={"user_id": "target"},
    )
    assert response.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [None, "owner", "nas", "revision", "cancel"],
)
async def test_indexed_tracing_real_reader_keeps_current_history(
    history_client,
    tmp_path,
    monkeypatch,
    failure,
):
    client, _, chat, workspace = history_client
    question = Msg(
        "user",
        "question + secret",
        "user",
        metadata={
            "console_hidden_context_v1": {
                "visible_text": "question",
                "suffix": " + secret",
            },
        },
    )
    question.id = "question"
    answer = Msg("assistant", "answer", "assistant")
    answer.id = "answer"
    rows = [
        {
            "layer": layer,
            "source_msg_key": key.encode(),
            "source_occurrence_no": i,
            "task_run_key": None,
        }
        for i, (layer, key) in enumerate(
            [
                ("anchor", "question"),
                ("memory", "answer"),
                ("task", "task"),
                ("error", "failure"),
            ],
        )
    ]

    class Database:
        @asynccontextmanager
        async def transaction(self):
            yield self

        async def fetch_one(self, sql, params):
            return {"history_high": 4}

        async def fetch_all(self, sql, params):
            if sql.startswith("SELECT source_msg_key"):
                return []
            assert sql.startswith("SELECT m.*")
            layers = params[3:]
            return [r for r in rows if not layers or r["layer"] in layers]

    store = HistoryStore(
        Database(),
        WorkspaceScope("tenant", "source", "agent", service_unit_id="test"),
        BundleStore(tmp_path / "bundles"),
        cursor_secret=b"test",
    )
    store._chat = AsyncMock(return_value={"id": 1, "session_id": 2})
    store._pins = AsyncMock(return_value=[])
    store._boundaries = AsyncMock(
        return_value=[
            {
                "boundary_key": b"compacted",
                "archive_display_count": 2,
                "archive_first_source_msg_key": b"old-q",
                "archive_last_source_msg_key": b"old-a",
                "committed_at": datetime.now(timezone.utc),
                "archive_first_timestamp": None,
                "archive_last_timestamp": None,
            },
        ],
    )
    store._revalidate_rows = AsyncMock()
    store._validate_chat_row = AsyncMock()
    store._finish = AsyncMock()
    store.chats._hydrate = AsyncMock(
        return_value=[
            (
                chat.model_copy(update={"user_id": "foreign"})
                if failure == "owner"
                else chat
            ),
        ],
    )
    if failure == "revision":
        store._revalidate_rows.side_effect = [None, ConflictError("changed")]
    payloads = {
        "anchor": question.to_dict(),
        "memory": answer.to_dict(),
        "task": {"id": "task", "role": "assistant", "content": "task"},
        "error": {
            "id": "failure",
            "role": "assistant",
            "code": "model_call_failed",
            "message": "failed",
            "content": [],
        },
    }

    async def load(snapshot):
        if failure == "nas":
            raise OSError("NAS unavailable")
        if failure == "cancel":
            raise asyncio.CancelledError()
        return [payloads[row["layer"]] for row in snapshot.rows]

    monkeypatch.setattr(store, "_payloads", load)
    monkeypatch.setattr(
        api,
        "_annotate_approval_action_statuses",
        AsyncMock(side_effect=lambda messages: messages),
    )
    workspace.conversation_storage = SimpleNamespace(history=store)
    if failure == "cancel":
        request = Request({"type": "http", "headers": []})
        with pytest.raises(asyncio.CancelledError):
            await tracing.get_user_chat(request, chat.id, user_id="target")
        store._finish.assert_awaited_once()
        return
    response = client.get(
        f"/tracing/chats/{chat.id}",
        params={"user_id": "target"},
    )
    if failure:
        assert response.status_code == (404 if failure == "owner" else 503)
        store._finish.assert_awaited_once()
        return
    assert response.status_code == 200
    body = response.json()
    assert {
        api._message_original_id(api.ChatMessage.model_validate(m))
        for m in body["messages"]
    } == {
        "question",
        "answer",
        "task",
        "failure",
    }
    assert body["chat"]["id"] == chat.id
    assert body["archive"]["has_more"] is True
    assert len(body["archive"]["boundaries"]) == 1
    assert "secret" not in response.text
    normal = client.get(f"/chats/{chat.id}")
    assert normal.status_code == 200
    assert normal.json() == body
    assert store._finish.await_count == 2

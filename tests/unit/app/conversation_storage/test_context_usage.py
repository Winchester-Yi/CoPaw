# -*- coding: utf-8 -*-
"""Numeric HTTP reads avoid NAS unless historical authorization needs it."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from mysql.connector.errors import InterfaceError
import pytest

from swe.app.conversation_storage.errors import StorageUnavailable
from swe.app.conversation_storage.history import HistoryStore
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.runner import api
from swe.app.runner.context_usage import ContextUsageUnavailable
from swe.app.runner.models import ChatSpec
from tests.unit.app.conversation_storage.test_chat_repository import (
    ReadDatabase,
    chat_row,
)

SNAPSHOT = {
    "used_tokens": 10,
    "max_tokens": 100,
    "remaining_tokens": 90,
    "usage_ratio": 0.1,
    "system_context_tokens": 2,
    "tool_definition_tokens": 3,
    "conversation_tokens": 5,
    "governance_threshold_ratio": 0.5,
    "active_threshold_ratio": 0.7,
    "emergency_threshold_ratio": 0.9,
    "status": "normal",
    "as_of": "2026-10-30T00:00:00Z",
}


def request(**changes):
    return SimpleNamespace(
        state=SimpleNamespace(
            **{
                "user_id": "alice",
                "source_id": "source",
                "agent_id": "agent",
                **changes,
            },
        ),
    )


async def indexed_route(reader, *, identity=None):
    manager = SimpleNamespace(
        get_chat=AsyncMock(side_effect=AssertionError("full Chat read")),
    )
    result = await api.get_chat_context_usage(
        identity or request(),
        "chat",
        mgr=manager,
        session=object(),
        workspace=SimpleNamespace(
            conversation_storage=SimpleNamespace(history=reader),
        ),
    )
    manager.get_chat.assert_not_awaited()
    return result


async def test_indexed_context_route_skips_full_chat_lookup():
    expected = ContextUsageUnavailable()
    reader = SimpleNamespace(context_usage=AsyncMock(return_value=expected))
    assert await indexed_route(reader) is expected
    reader.context_usage.assert_awaited_once()


@pytest.mark.parametrize(
    "error,status,detail",
    [
        (KeyError("chat"), 404, "Chat not found: chat"),
        (
            StorageUnavailable("offline"),
            503,
            "Conversation storage unavailable",
        ),
        (InterfaceError("offline"), 503, "Conversation storage unavailable"),
    ],
)
async def test_indexed_context_route_preserves_error_contract(
    error,
    status,
    detail,
):
    reader = SimpleNamespace(context_usage=AsyncMock(side_effect=error))
    with pytest.raises(HTTPException) as failure:
        await indexed_route(reader)
    assert (failure.value.status_code, failure.value.detail) == (
        status,
        detail,
    )


class UsageDatabase(ReadDatabase):
    def __init__(self, row):
        super().__init__([row])
        self.usage = {
            "context_usage_text": json.dumps(SNAPSHOT),
            "context_usage_invalid": False,
        }

    async def fetch_one(self, query, params=None):
        if "SELECT context_usage_text" in query:
            self.queries.append((query, params))
            return self.usage
        row = await super().fetch_one(query, params)
        return dict(row) if row is not None else None


class MetadataBatch:
    publications = [
        {"id": 7, "relative_path": "content.bundle", "byte_length": 1000},
    ]

    async def read(self, operation, *args, **kwargs):
        return operation(*args, **kwargs)


@pytest.fixture
def numeric_store():
    row = chat_row(
        user_key=b"alice",
        metadata_text=json.dumps({"source_id": "source", "agent_id": "agent"}),
        content_meta_publication_id=7,
        content_meta_offset=9,
        content_meta_length=99,
        projected_status="idle",
    )
    db = UsageDatabase(row)
    bundles = SimpleNamespace(
        read_extent=Mock(return_value=b'{"custom":"preserved"}'),
    )
    store = HistoryStore(
        db,
        WorkspaceScope("t", "s", "a", service_unit_id="schema-test-service"),
        bundles,
        cursor_secret=b"context-usage-test",
    )
    store.chats.retentions.pin = AsyncMock(return_value=MetadataBatch())
    store.chats.retentions.finish_reader = AsyncMock()
    return store, db, row, bundles


@pytest.mark.parametrize(
    "raw,invalid,status,available,stale",
    [
        (json.dumps(SNAPSHOT), False, "idle", True, False),
        (json.dumps(SNAPSHOT), True, "idle", True, True),
        (json.dumps(SNAPSHOT), False, "running", True, True),
        (json.dumps(SNAPSHOT), False, "stopping", True, True),
        (None, False, "idle", False, None),
        ("not-json", False, "idle", False, None),
        ('{"schema_version":99}', False, "idle", False, None),
    ],
)
async def test_numeric_http_path_with_content_metadata_does_not_read_nas(
    numeric_store,
    raw,
    invalid,
    status,
    available,
    stale,
):
    store, db, row, bundles = numeric_store
    db.usage.update(context_usage_text=raw, context_usage_invalid=invalid)
    row["projected_status"] = status
    result = await indexed_route(store)
    assert result.available is available
    if available:
        assert result.used_tokens == 10
        assert result.stale is stale
    bundles.read_extent.assert_not_called()
    store.chats.retentions.pin.assert_not_awaited()
    store.chats.retentions.finish_reader.assert_not_awaited()
    assert len(db.queries) == 3


@pytest.mark.parametrize("field", ["user_id", "source_id", "agent_id"])
async def test_numeric_http_path_rejects_identity_before_reading_nas(
    numeric_store,
    field,
):
    store, _, row, bundles = numeric_store
    if field == "user_id":
        row["metadata_text"] = "{}"
    with pytest.raises(HTTPException) as failure:
        await indexed_route(store, identity=request(**{field: "other"}))
    assert failure.value.status_code == 404
    bundles.read_extent.assert_not_called()
    store.chats.retentions.pin.assert_not_awaited()


@pytest.mark.parametrize("field", ["source_id", "agent_id"])
@pytest.mark.parametrize("raw", [None, json.dumps(SNAPSHOT)])
async def test_historical_authorization_on_nas_is_still_enforced(
    numeric_store,
    field,
    raw,
):
    store, db, row, bundles = numeric_store
    light = json.loads(row["metadata_text"])
    del light[field]
    row["metadata_text"] = json.dumps(light)
    db.usage["context_usage_text"] = raw
    bundles.read_extent.return_value = json.dumps({field: "x" * 1025}).encode()
    with pytest.raises(HTTPException) as failure:
        await store.context_usage(
            "chat",
            authorize=lambda chat: api._authorize_chat(request(), chat, None),
        )
    assert failure.value.status_code == 404
    bundles.read_extent.assert_called_once_with(
        "content.bundle",
        9,
        99,
        expected_length=1000,
    )
    store.chats.retentions.pin.assert_awaited_once_with([7], kind="reader")
    store.chats.retentions.finish_reader.assert_awaited_once()


async def test_no_content_metadata_needs_no_authorization_fallback(
    numeric_store,
):
    store, _, row, bundles = numeric_store
    row.update(metadata_text="{}", content_meta_publication_id=None)
    result = await indexed_route(store)
    assert result.available
    bundles.read_extent.assert_not_called()
    store.chats.retentions.pin.assert_not_awaited()


async def test_historical_metadata_fallback_preserves_numeric_result(
    numeric_store,
):
    store, _, row, bundles = numeric_store
    row["metadata_text"] = "{}"
    result = await indexed_route(store)
    assert result.available and result.used_tokens == 10
    bundles.read_extent.assert_called_once()
    store.chats.retentions.finish_reader.assert_awaited_once()


async def test_historical_fallback_deletion_is_not_numeric_success(
    numeric_store,
):
    store, db, row, _ = numeric_store
    row["metadata_text"] = "{}"
    original = db.fetch_one

    async def delete_after_usage(query, params=None):
        result = await original(query, params)
        if "SELECT context_usage_text" in query:
            db.rows = []
        return result

    db.fetch_one = delete_after_usage
    with pytest.raises(KeyError):
        await store.context_usage("chat", authorize=lambda chat: None)


async def test_context_revalidates_sql_revision(numeric_store):
    store, db, row, bundles = numeric_store
    original = db.fetch_all

    async def changed_metadata(query, params=None):
        row["metadata_revision"] += 1
        return await original(query, params)

    db.fetch_all = changed_metadata
    with pytest.raises(HTTPException) as failure:
        await indexed_route(store)
    assert failure.value.status_code == 503
    bundles.read_extent.assert_not_called()


async def test_historical_nas_failure_is_503_and_releases_pin(numeric_store):
    store, _, row, bundles = numeric_store
    row["metadata_text"] = "{}"
    bundles.read_extent.side_effect = OSError("NAS unavailable")
    with pytest.raises(HTTPException) as failure:
        await indexed_route(store)
    assert failure.value.status_code == 503
    store.chats.retentions.finish_reader.assert_awaited_once()


async def test_http_context_with_nas_metadata_uses_only_sql(numeric_store):
    store, _, _, bundles = numeric_store
    app = FastAPI()
    app.include_router(api.router)
    workspace = SimpleNamespace(
        conversation_storage=SimpleNamespace(history=store),
    )
    manager = SimpleNamespace(
        get_chat=AsyncMock(side_effect=AssertionError("full Chat read")),
    )
    app.dependency_overrides[api.get_workspace] = lambda: workspace
    app.dependency_overrides[api.get_chat_manager] = lambda: manager
    app.dependency_overrides[api.get_session] = object

    @app.middleware("http")
    async def identity(http_request, call_next):
        for key, value in vars(request().state).items():
            setattr(http_request.state, key, value)
        return await call_next(http_request)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/chats/chat/context-usage")
    assert response.status_code == 200
    assert response.json()["used_tokens"] == 10
    assert response.json()["stale"] is False
    manager.get_chat.assert_not_awaited()
    bundles.read_extent.assert_not_called()


async def test_legacy_context_route_keeps_file_state_and_authorization():
    chat = ChatSpec(id="chat", session_id="s", user_id="alice")
    manager = SimpleNamespace(get_chat=AsyncMock(return_value=chat))
    session = SimpleNamespace(
        get_persisted_session_state_dict=AsyncMock(return_value={}),
    )
    result = await api.get_chat_context_usage(
        request(),
        "chat",
        mgr=manager,
        session=session,
        workspace=SimpleNamespace(),
    )
    assert result == ContextUsageUnavailable()
    session.get_persisted_session_state_dict.assert_awaited_once_with(
        "s",
        "alice",
    )
    with pytest.raises(HTTPException) as failure:
        await api.get_chat_context_usage(
            request(user_id="other"),
            "chat",
            mgr=manager,
            session=session,
            workspace=SimpleNamespace(),
        )
    assert failure.value.status_code == 404
    assert session.get_persisted_session_state_dict.await_count == 1

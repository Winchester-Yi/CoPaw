# -*- coding: utf-8 -*-
"""Summary reads are one scoped SQL query without body hydration or counts."""

from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from swe.app.conversation_storage.errors import (
    IntegrityError,
    StorageUnavailable,
)
from swe.app.runner import api
from swe.app.runner.manager import ChatManager
from swe.app.runner.models import ChatSpec
from swe.app.runner.session import SafeJSONSession
from swe.app.answer_turn.models import TurnStatus
from swe.app.routers import tracing
from tests.unit.app.test_chat_pagination import _InMemoryChatRepository
from tests.unit.app.conversation_storage.test_chat_repository import (
    ReadDatabase,
    chat_row as stored_chat_row,
    repository,
)


def chat_row(key=b"chat", **changes):
    return stored_chat_row(key, **{"projected_status": "idle", **changes})


async def test_summary_query_uses_one_sql_and_no_nas(tmp_path):
    db = ReadDatabase([chat_row(content_meta_publication_id=7)])
    repo = repository(db, tmp_path)
    repo._hydrate = AsyncMock(side_effect=AssertionError("NAS hydrate"))
    repo.retentions.pin = AsyncMock(side_effect=AssertionError("reader pin"))
    page = await repo.list_summaries(
        page_size=1,
        user_id="",
        channel="console",
    )
    assert len(page.items) == 1
    assert len(db.queries) == 1
    query, params = db.queries[0]
    assert "c.*" not in query and "COUNT(" not in query
    assert params == (
        b"schema-test-service",
        b"t",
        b"s",
        b"a",
        b"",
        b"console",
        2,
    )
    assert "c.deleted_at IS NULL" in query
    assert "c.user_key=%s" in query and "c.channel_key=%s" in query
    assert "t.chat_id=c.id" in query
    assert "meta" not in page.items[0].model_dump()
    assert "total" not in page.model_dump() and "page" not in page.model_dump()
    assert not page.has_more and page.next_cursor is None


async def test_summary_cursor_uses_last_returned_row_and_session_filter(
    tmp_path,
):
    rows = [chat_row(b"z"), chat_row(b"a", id=2)]
    db = ReadDatabase(rows)
    repo = repository(db, tmp_path)
    page = await repo.list_summaries(page_size=1, session_id="session")
    assert [item.id for item in page.items] == ["z"]
    assert page.has_more
    version, (stamp, key) = repo._decode_cursor(page.next_cursor)
    assert version == "v2" and key == "z"
    assert stamp == rows[0]["updated_at"].replace(tzinfo=timezone.utc)
    assert "s.external_session_key=%s" in db.queries[0][0]
    assert db.queries[0][1][-2:] == (b"session", 2)
    db.rows = [rows[1]]
    second = await repo.list_summaries(page_size=1, cursor=page.next_cursor)
    assert [item.id for item in second.items] == ["a"]
    assert not second.has_more
    query, params = db.queries[-1]
    assert "c.updated_at<%s OR (c.updated_at=%s" in query
    assert "c.external_chat_key<%s" in query
    assert (
        "ORDER BY c.updated_at DESC,c.external_chat_key DESC LIMIT %s" in query
    )
    assert params[-2:] == (b"z", 2)


@pytest.mark.parametrize("size", [0, -1, 201])
async def test_summary_size_is_bounded_before_sql(tmp_path, size):
    db = ReadDatabase([])
    with pytest.raises(ValueError):
        await repository(db, tmp_path).list_summaries(page_size=size)
    assert not db.queries


@pytest.mark.parametrize("cursor", ["not-cursor", "x" * 2049, "v1"])
async def test_summary_rejects_invalid_and_legacy_cursor(tmp_path, cursor):
    db = ReadDatabase([])
    repo = repository(db, tmp_path)
    if cursor == "v1":
        cursor = repo._encode_cursor(
            (datetime.now(timezone.utc), "chat"),
            version="v1",
        )
    with pytest.raises(ValueError):
        await repo.list_summaries(cursor=cursor)
    assert not db.queries


async def test_summary_exposes_light_fields_and_typed_binding_only(tmp_path):
    db = ReadDatabase(
        [
            chat_row(
                metadata_text=json.dumps(
                    {
                        "session_kind": "task",
                        "task_job_id": "job",
                        "plan_mode_enabled": False,
                    },
                ),
                expert_id=b"expert",
                expert_binding_revision=3,
                projected_status="stopping",
                content_meta_publication_id=9,
            ),
        ],
    )
    page = await repository(db, tmp_path).list_summaries()
    item = page.items[0]
    assert item.display_metadata == {
        "session_kind": "task",
        "task_job_id": "job",
        "plan_mode_enabled": False,
    }
    assert item.expert_binding.model_dump() == {
        "expert_id": "expert",
        "revision": 3,
    }
    assert item.status == "stopping"
    assert item.created_at.tzinfo == timezone.utc
    db.rows = []
    empty = await repository(db, tmp_path).list_summaries()
    assert (
        empty.items == [] and not empty.has_more and empty.next_cursor is None
    )


async def test_summary_rejects_corrupt_sql_metadata(tmp_path):
    db = ReadDatabase([chat_row(metadata_text='{"private_prompt":"bad"}')])
    with pytest.raises(IntegrityError):
        await repository(db, tmp_path).list_summaries()


def summary_client(manager, *, user="alice"):
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(tracing.router)

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.user_id = user
        return await call_next(request)

    app.dependency_overrides[api.get_chat_manager] = lambda: manager
    app.dependency_overrides[api.get_workspace] = lambda: SimpleNamespace(
        conversation_storage=object(),
    )
    app.dependency_overrides[api.get_session] = object
    return TestClient(app, raise_server_exceptions=False)


def test_summary_http_filters_current_user_and_routes_before_chat_id(tmp_path):
    db = ReadDatabase([chat_row(user_key=b"alice")])
    client = summary_client(ChatManager(repo=repository(db, tmp_path)))
    response = client.get(
        "/chats/-/summaries?page_size=1&channel=console&session_id=session",
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["user_id"] == "alice"
    assert "total" not in response.json()
    assert b"alice" in db.queries[0][1]
    assert len(db.queries) == 1
    assert client.get("/chats/-/summaries?user_id=bob").status_code == 403
    assert len(db.queries) == 1


@pytest.mark.parametrize(
    "query",
    ["page_size=0", "page_size=201", "cursor=bad"],
)
def test_summary_http_validation(tmp_path, query):
    db = ReadDatabase([])
    client = summary_client(ChatManager(repo=repository(db, tmp_path)))
    assert client.get("/chats/-/summaries?" + query).status_code == 422
    assert not db.queries


def test_summary_http_storage_failure_is_503():
    manager = SimpleNamespace(
        list_summaries=AsyncMock(side_effect=StorageUnavailable("offline")),
    )
    assert summary_client(manager).get("/chats/-/summaries").status_code == 503


def test_tracing_summary_resolves_target_workspace_and_filters_target(
    tmp_path,
    monkeypatch,
):
    db = ReadDatabase([chat_row(user_key=b"target")])
    manager = ChatManager(repo=repository(db, tmp_path))
    target = AsyncMock(return_value=SimpleNamespace(chat_manager=manager))
    monkeypatch.setattr(tracing, "_get_target_workspace", target)
    response = summary_client(object()).get(
        "/tracing/chats/-/summaries?user_id=target&session_id=session",
    )
    assert response.status_code == 200
    assert target.await_args.args[1] == "target"
    assert response.json()["items"][0]["user_id"] == "target"
    assert db.queries[0][1][-3:] == (b"target", b"session", 101)


async def test_legacy_repository_projects_display_fields_and_paginates():
    repo = _InMemoryChatRepository(
        [
            ChatSpec(
                id=key,
                session_id="s",
                user_id="alice",
                meta={
                    "plan_mode_enabled": True,
                    "custom": "must not be listed",
                    "expert_main_binding": {"revision": 2, "expert_id": None},
                },
                updated_at=datetime(2026, 11, 4, tzinfo=timezone.utc),
            )
            for key in ("a", "z")
        ],
    )
    page = await repo.list_summaries(
        page_size=1,
        user_id="alice",
        session_id="s",
    )
    assert [item.id for item in page.items] == ["z"]
    assert page.items[0].display_metadata == {"plan_mode_enabled": True}
    assert page.items[0].expert_binding.revision == 2
    second = await repo.list_summaries(page_size=1, cursor=page.next_cursor)
    assert [item.id for item in second.items] == ["a"]
    assert not second.has_more
    assert not (await repo.list_summaries(session_id="missing")).items


def test_legacy_summary_route_projects_runtime_status():
    manager = ChatManager(
        repo=_InMemoryChatRepository(
            [
                ChatSpec(id="chat", session_id="s", user_id="alice"),
            ],
        ),
    )
    client = summary_client(manager)
    coordinator = SimpleNamespace(
        statuses=AsyncMock(return_value={"chat": TurnStatus.RUNNING}),
    )
    client.app.dependency_overrides[api.get_workspace] = (
        lambda: SimpleNamespace(answer_turn_coordinator=coordinator)
    )
    response = client.get("/chats/-/summaries")
    assert response.status_code == 200
    assert response.json()["items"][0]["status"] == "running"
    coordinator.statuses.assert_awaited_once_with(["chat"])


def test_summary_endpoint_does_not_take_over_existing_chat_id(tmp_path):
    manager = ChatManager(
        repo=_InMemoryChatRepository(
            [
                ChatSpec(id="summaries", session_id="s", user_id="alice"),
            ],
        ),
    )
    client = summary_client(manager)
    session = SafeJSONSession(str(tmp_path / "sessions"))
    client.app.dependency_overrides[api.get_session] = lambda: session
    response = client.get("/chats/summaries")
    assert response.status_code == 200
    assert response.json()["chat"]["id"] == "summaries"

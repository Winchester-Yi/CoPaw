# -*- coding: utf-8 -*-
"""Public HTTP contracts against real SQL metadata and immutable NAS bodies.

Only request identity and workspace resolution are supplied by this harness;
Chat, history, archive, Session and share reads use the production adapters.
"""

import base64
from datetime import timedelta
import json
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from swe.app.chat_sharing import router as sharing
from swe.app.chat_sharing.service import ChatSharingService
from swe.app.chat_sharing.store import ChatShareStore
from swe.app.conversation_storage.bundle import BundleBlock, new_operation_key
from swe.app.conversation_storage.factory import WorkspaceConversationStorage
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.publications import (
    CommitPlan,
    PublicationRequest,
)
from swe.app.conversation_storage.shares import ShareInstall
from swe.app.crons.managed_receipts import SessionResultReceipts
from swe.app.crons.managed_results import ExecutionOutcome
from swe.app.routers import tracing
from swe.app.runner import api
from swe.app.runner.manager import ChatManager
from swe.config.context import (
    get_current_source_id,
    get_current_tenant_id,
    get_current_user_id,
    tenant_context,
)
from tests.integrated.conversation_storage.conftest import (
    SQL_ROOT,
    apply_sql_script,
)
from tests.integrated.conversation_storage.test_history_mysql import (
    add_messages,
    source,
)
from tests.integrated.conversation_storage.test_schema import (
    NOW,
    insert_chat,
    insert_row,
    insert_session,
)


@pytest.fixture
async def api_stack(conversation_test_database, tmp_path, monkeypatch):
    db = conversation_test_database
    await ChatShareStore(db).ensure_schema()
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_service_unit_finalize.sql",
    )
    storage = WorkspaceConversationStorage(
        db,
        WorkspaceScope(
            "tenant",
            "source",
            "agent",
            service_unit_id="schema-test-service",
        ),
        tmp_path,
        cursor_secret=b"api-contract-secret",
    )
    manager = ChatManager(repo=storage.chats)
    workspace = SimpleNamespace(
        conversation_storage=storage,
        workspace_dir=tmp_path,
        agent_id="agent",
        tenant_id="tenant",
        chat_manager=manager,
        runner=SimpleNamespace(session=storage.session_for("console")),
    )
    app = FastAPI()

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.user_id = request.headers.get("X-Test-User", "alice")
        if request.state.user_id == "__admin__":
            request.state.user_id = None
        request.state.source_id = "source"
        request.state.agent_id = getattr(
            request.state,
            "agent_id",
            request.headers.get("X-Agent-Id", "agent"),
        )
        request.state.source_system_config = {
            "feature_switches": {
                "expert_as_main_enabled": request.headers.get(
                    "X-Test-Expert",
                    "true",
                )
                == "true",
            },
        }
        return await call_next(request)

    app.include_router(api.router)
    app.include_router(sharing.router)
    app.include_router(tracing.router)
    app.dependency_overrides[api.get_workspace] = lambda: workspace
    app.dependency_overrides[api.get_chat_manager] = lambda: manager
    app.dependency_overrides[api.get_session] = (
        lambda: workspace.runner.session
    )
    monkeypatch.setattr(
        sharing,
        "_service",
        ChatSharingService(
            storage.shares.store,
            tmp_path,
            conversation_shares=storage.shares,
        ),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://contract.test",
    ) as client:
        yield SimpleNamespace(
            db=db,
            storage=storage,
            manager=manager,
            workspace=workspace,
            client=client,
            app=app,
        )


async def history_dataset(stack):
    session = await insert_session(stack.db)
    a = await insert_chat(stack.db, session, b"chat-a", registry_order=1)
    b = await insert_chat(stack.db, session, b"chat-b", registry_order=2)
    await stack.db.execute(
        "UPDATE swe_conversation_sessions SET history_high=100 WHERE id=%s",
        (session,),
    )
    return session, a, b


def message_ids(payload):
    return [
        api._message_original_id(api.ChatMessage.model_validate(message))
        for message in payload["messages"]
    ]


async def test_http_list_full_array_exact_count_filters_and_page(api_stack):
    stack = api_stack
    session, _, _ = await history_dataset(stack)
    for i in range(12):
        await insert_chat(
            stack.db,
            session,
            f"extra-{i:02}".encode(),
            registry_order=i + 3,
        )
    other = await insert_session(stack.db, user_key=b"bob")
    await insert_chat(stack.db, other, b"bob", user_key=b"bob")
    cron = await insert_session(stack.db, channel_key=b"cron")
    await insert_chat(stack.db, cron, b"cron", channel_key=b"cron")
    admin = await stack.client.get(
        "/chats",
        headers={"X-Test-User": "__admin__"},
    )
    assert admin.status_code == 200
    assert isinstance(admin.json(), list) and len(admin.json()) == 16
    scoped = await stack.client.get("/chats", params={"channel": "console"})
    assert len(scoped.json()) == 14
    assert all(item["user_id"] == "alice" for item in scoped.json())
    page = await stack.client.get(
        "/chats",
        params={"channel": "console", "page": 2, "page_size": 10},
    )
    assert page.status_code == 200
    assert {
        key: page.json()[key]
        for key in (
            "total",
            "page",
            "page_size",
            "has_more",
        )
    } == {"total": 14, "page": 2, "page_size": 10, "has_more": False}
    assert len(page.json()["items"]) == 4
    assert all(item["status"] == "idle" for item in page.json()["items"])


@pytest.mark.parametrize(
    "params",
    [{}, {"page": 1, "page_size": 10}, {"cursor": "", "page_size": 10}],
)
async def test_http_list_preserves_sql_active_status_without_local_owner(
    api_stack,
    params,
):
    stack = api_stack
    session, chat, _ = await history_dataset(stack)
    turn = await insert_row(
        stack.db,
        "swe_conversation_turns",
        {
            "session_id": session,
            "chat_id": chat,
            "question_msg_key": b"q",
            "turn_order": 1,
            "context_epoch": 0,
            "generation": 1,
            "state": "running",
        },
    )
    await stack.db.execute(
        "UPDATE swe_conversation_sessions SET active_turn_id=%s WHERE id=%s",
        (turn, session),
    )
    result = await stack.client.get("/chats", params=params)
    assert result.status_code == 200
    items = result.json() if not params else result.json()["items"]
    assert {item["id"]: item["status"] for item in items} == {
        "chat-a": "running",
        "chat-b": "idle",
    }


@pytest.mark.parametrize(
    "params",
    [
        {"page": 1},
        {"page_size": 1},
        {"cursor": ""},
        {"page": 1, "page_size": 1, "cursor": ""},
        {"page_size": 1, "cursor": "bad-cursor"},
    ],
)
async def test_http_list_rejects_invalid_pagination(api_stack, params):
    result = await api_stack.client.get("/chats", params=params)
    assert result.status_code == 422


async def test_http_v2_and_legacy_creation_cursors_keep_total_and_advance(
    api_stack,
):
    stack = api_stack
    session = await insert_session(stack.db)
    for i, key in enumerate((b"z", b"m", b"a")):
        await insert_chat(
            stack.db,
            session,
            key,
            registry_order=i + 1,
            created_at=NOW - timedelta(days=i),
            updated_at=NOW + timedelta(days=i),
        )
    first = await stack.client.get(
        "/chats",
        params={"cursor": "", "page_size": 1},
    )
    assert first.status_code == 200
    assert first.json()["items"][0]["id"] == "a"
    second = await stack.client.get(
        "/chats",
        params={"cursor": first.json()["next_cursor"], "page_size": 1},
    )
    assert second.json()["items"][0]["id"] == "m"
    assert second.json()["total"] == 3 and second.json()["page"] == 1
    assert second.json()["next_cursor"] != first.json()["next_cursor"]
    legacy = base64.urlsafe_b64encode(
        json.dumps([NOW.isoformat() + "+00:00", "z"]).encode(),
    ).decode()
    older = await stack.client.get(
        "/chats",
        params={"cursor": legacy, "page_size": 1},
    )
    assert older.status_code == 200
    assert older.json()["items"][0]["id"] == "m"
    tail = await stack.client.get(
        "/chats",
        params={"cursor": older.json()["next_cursor"], "page_size": 1},
    )
    assert tail.json()["items"][0]["id"] == "a"
    assert tail.json()["total"] == 3 and not tail.json()["has_more"]


async def test_manager_no_user_latest_and_equal_timestamp_registry_order(
    api_stack,
):
    stack = api_stack
    alice = await insert_session(stack.db)
    bob = await insert_session(stack.db, user_key=b"bob")
    await insert_chat(stack.db, alice, b"later-registry", registry_order=2)
    await insert_chat(stack.db, bob, b"first-registry", user_key=b"bob")
    assert (
        await stack.manager.get_chat_by_session("session")
    ).id == "first-registry"
    assert (
        await stack.manager.get_chat_by_session(
            "session",
            user_id="alice",
        )
    ).id == "later-registry"
    await stack.db.execute(
        "UPDATE swe_conversation_chats SET updated_at=%s "
        "WHERE external_chat_key=%s",
        (NOW + timedelta(days=1), b"later-registry"),
    )
    assert (
        await stack.manager.get_chat_by_session("session")
    ).id == "later-registry"


async def test_http_detail_shared_layers_duplicate_ids_chat_anchors_and_meta(
    api_stack,
):
    stack = api_stack
    session, a, b = await history_dataset(stack)
    await add_messages(
        stack.db,
        stack.storage.history,
        session,
        a,
        [
            ("memory", source("same"), 1),
            ("memory", source("same"), 2),
            (
                "task",
                {"id": "task", "role": "assistant", "content": "task"},
                3,
            ),
            (
                "error",
                {
                    "id": "error",
                    "role": "assistant",
                    "code": "model_call_failed",
                    "message": "failed",
                },
                4,
            ),
            ("anchor", source("anchor-a", "user"), 5),
        ],
    )
    await add_messages(
        stack.db,
        stack.storage.history,
        session,
        b,
        [("anchor", source("anchor-b", "user"), 6)],
    )
    chat = await stack.manager.get_chat("chat-a")
    chat.meta = {
        "session_kind": "task",
        "task_job_id": "job",
        "management_owner": "cron",
        "custom": {"secret_prompt": "full"},
    }
    await stack.manager.update_chat(chat)
    first = await stack.client.get("/chats/chat-a")
    second = await stack.client.get("/chats/chat-b")
    assert first.status_code == second.status_code == 200
    assert message_ids(first.json()) == [
        "same",
        "same",
        "anchor-a",
        "task",
        "error",
    ]
    assert message_ids(second.json()) == [
        "same",
        "same",
        "anchor-b",
        "task",
        "error",
    ]
    assert first.json()["chat"]["meta"] == chat.meta
    forbidden = await stack.client.get(
        "/chats/chat-a",
        headers={"X-Test-User": "bob"},
    )
    assert forbidden.status_code == 404


async def test_http_archive_source_limit_fragment_identity_and_cursor(
    api_stack,
):
    stack = api_stack
    session, a, _ = await history_dataset(stack)
    await add_messages(
        stack.db,
        stack.storage.history,
        session,
        a,
        [
            ("archive", source("one", "user"), 1),
            (
                "archive",
                source(
                    "two",
                    content=[
                        {"type": "thinking", "thinking": "x"},
                        {"type": "text", "text": "y"},
                    ],
                ),
                2,
            ),
            ("archive", source("three"), 3),
        ],
        boundary=True,
    )
    result = await stack.client.get(
        "/chats/chat-a/history",
        params={"limit": 2},
    )
    assert result.status_code == 200
    assert message_ids(result.json()) == ["two", "two", "three"]
    assert result.json()["has_more"] and result.json()["next_cursor"]
    older = await stack.client.get(
        "/chats/chat-a/history",
        params={"limit": 2, "before": result.json()["next_cursor"]},
    )
    assert message_ids(older.json()) == ["one"]
    assert not older.json()["has_more"]
    wrong = await stack.client.get(
        "/chats/chat-b/history",
        params={"before": result.json()["next_cursor"]},
    )
    assert wrong.status_code == 422
    detail = await stack.client.get("/chats/chat-a")
    assert detail.json()["archive"]["has_more"]
    independent = await stack.client.get("/chats/chat-b")
    assert not independent.json()["archive"]["has_more"]


async def test_http_answer_turn_legacy_unknown_explicit_chat_and_stored_owner(
    api_stack,
):
    stack = api_stack
    session, a, b = await history_dataset(stack)
    await add_messages(
        stack.db,
        stack.storage.history,
        session,
        a,
        [
            ("memory", source("q", "user"), 1),
            ("memory", source("answer"), 2),
            ("memory", source("next", "user"), 3),
        ],
    )
    params = {"chat_id": "chat-a", "sessionid": "ignored-wrong", "msgid": "q"}
    result = await stack.client.get("/chats/answer-turn", params=params)
    assert result.status_code == 200 and message_ids(result.json()) == [
        "q",
        "answer",
    ]
    assert result.json()["turn_status"] is None
    not_question = await stack.client.get(
        "/chats/answer-turn",
        params={**params, "msgid": "answer"},
    )
    assert not_question.status_code == 404
    no_user = await stack.client.get(
        "/chats/answer-turn",
        params=params,
        headers={"X-Test-User": "__admin__"},
    )
    assert no_user.status_code == 400
    await insert_row(
        stack.db,
        "swe_conversation_turns",
        {
            "session_id": session,
            "chat_id": b,
            "question_msg_key": b"q",
            "turn_order": 1,
            "context_epoch": 0,
            "generation": 0,
            "state": "succeeded",
            "history_status": "cancelled",
        },
    )
    owned = await stack.client.get(
        "/chats/answer-turn",
        params={"sessionid": "session", "msgid": "q"},
    )
    assert owned.status_code == 200
    assert owned.json()["chat"]["id"] == "chat-b"
    assert owned.json()["turn_status"] == "cancelled"
    assert (
        await stack.client.get("/chats/answer-turn", params=params)
    ).status_code == 404


async def test_http_cold_turn_preserves_current_view_boundary(api_stack):
    stack = api_stack
    session, a, _ = await history_dataset(stack)
    await add_messages(
        stack.db,
        stack.storage.history,
        session,
        a,
        [
            ("memory", source("cold-q", "user"), 1),
            ("memory", source("cold-a"), 2),
        ],
    )
    turn = await insert_row(
        stack.db,
        "swe_conversation_turns",
        {
            "session_id": session,
            "chat_id": a,
            "question_msg_key": b"cold-q",
            "turn_order": 1,
            "context_epoch": 0,
            "generation": 0,
            "state": "succeeded",
            "history_status": "completed",
        },
    )
    await stack.db.execute(
        "UPDATE swe_conversation_messages SET turn_id=%s,current_visible=0,in_context=0 WHERE session_id=%s",
        (turn, session),
    )
    await add_messages(
        stack.db,
        stack.storage.history,
        session,
        a,
        [
            ("archive", source("cold-q", "user"), 1),
            ("archive", source("cold-a"), 2),
        ],
        boundary=True,
    )
    params = {"chat_id": "chat-a", "msgid": "cold-q"}
    hidden = await stack.client.get("/chats/answer-turn", params=params)
    assert hidden.status_code == 404
    archived = await stack.client.get("/chats/chat-a/history")
    assert message_ids(archived.json()) == ["cold-q", "cold-a"]
    await stack.db.execute(
        "UPDATE swe_conversation_messages SET current_visible=1 WHERE turn_id=%s AND layer='memory'",
        (turn,),
    )
    visible = await stack.client.get("/chats/answer-turn", params=params)
    assert visible.status_code == 200
    assert message_ids(visible.json()) == ["cold-q", "cold-a"]
    assert visible.json()["turn_status"] == "completed"


@pytest.mark.parametrize("raw", [None, "not-json", '{"schema_version":99}'])
async def test_http_context_missing_invalid_future_is_unavailable_without_nas(
    api_stack,
    raw,
):
    stack = api_stack
    session, _, _ = await history_dataset(stack)
    await stack.db.execute(
        "UPDATE swe_conversation_sessions SET context_usage_text=%s WHERE id=%s",
        (raw, session),
    )
    stack.storage.bundles.read_extent = lambda *args, **kwargs: pytest.fail(
        "context usage read NAS",
    )
    result = await stack.client.get("/chats/chat-a/context-usage")
    assert result.status_code == 200 and result.json() == {"available": False}


async def test_http_summaries_keep_content_metadata_on_nas_and_scope_pages(
    api_stack,
    monkeypatch,
):
    stack = api_stack
    await history_dataset(stack)
    other = await insert_session(stack.db, user_key=b"bob")
    await insert_chat(stack.db, other, b"bob", user_key=b"bob")
    chat = await stack.manager.get_chat("chat-a")
    chat.meta.update(plan_mode_enabled=True, custom={"must_survive": True})
    await stack.manager.update_chat(chat)
    assert (await stack.manager.get_chat("chat-a")).meta["custom"] == {
        "must_survive": True,
    }
    monkeypatch.setattr(
        stack.storage.bundles,
        "read_extent",
        lambda *a, **kw: pytest.fail("summary read NAS"),
    )
    monkeypatch.setattr(
        stack.storage.chats.retentions,
        "pin",
        lambda *a, **kw: pytest.fail("summary created pins"),
    )
    seen = []
    cursor = ""
    while True:
        response = await stack.client.get(
            "/chats/-/summaries",
            params={"page_size": 1, "cursor": cursor},
        )
        assert response.status_code == 200
        page = response.json()
        assert "total" not in page and "page" not in page
        seen.extend(item["id"] for item in page["items"])
        for item in page["items"]:
            assert (
                "custom" not in item["display_metadata"] and "meta" not in item
            )
        if not page["has_more"]:
            break
        assert page["next_cursor"] and page["next_cursor"] != cursor
        cursor = page["next_cursor"]
    assert sorted(seen) == ["chat-a", "chat-b"]
    assert (
        await stack.client.get("/chats/-/summaries?user_id=bob")
    ).status_code == 403


async def test_http_context_with_content_metadata_does_not_hydrate_nas(
    api_stack,
    monkeypatch,
):
    stack = api_stack
    await history_dataset(stack)
    chat = await stack.manager.get_chat("chat-a")
    chat.meta.update(
        source_id="source",
        agent_id="agent",
        custom={"must_survive": True},
    )
    await stack.manager.update_chat(chat)
    row = await stack.db.fetch_one(
        "SELECT content_meta_publication_id FROM swe_conversation_chats "
        "WHERE external_chat_key=%s",
        (b"chat-a",),
    )
    assert row["content_meta_publication_id"] is not None
    monkeypatch.setattr(
        stack.storage.bundles,
        "read_extent",
        lambda *args, **kwargs: pytest.fail("numeric usage read NAS metadata"),
    )
    result = await stack.client.get("/chats/chat-a/context-usage")
    assert result.status_code == 200
    assert result.json() == {"available": False}


async def test_http_context_numeric_stale_and_sql_failure_are_distinct(
    api_stack,
    monkeypatch,
):
    stack = api_stack
    session, a, _ = await history_dataset(stack)
    snapshot = {
        "schema_version": 1,
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
        "estimated": True,
        "as_of": "2026-10-30T00:00:00Z",
    }
    turn = await insert_row(
        stack.db,
        "swe_conversation_turns",
        {
            "session_id": session,
            "chat_id": a,
            "question_msg_key": b"q",
            "turn_order": 1,
            "context_epoch": 0,
            "generation": 1,
            "state": "running",
        },
    )
    await stack.db.execute(
        "UPDATE swe_conversation_sessions SET context_usage_text=%s,active_turn_id=%s WHERE id=%s",
        (json.dumps(snapshot), turn, session),
    )
    monkeypatch.setattr(
        stack.storage.bundles,
        "read_extent",
        lambda *a, **kw: pytest.fail("numeric usage read NAS"),
    )
    for key, stale in (("chat-a", True), ("chat-b", False)):
        result = await stack.client.get(f"/chats/{key}/context-usage")
        assert result.status_code == 200
        assert result.json()["available"] and result.json()["stale"] is stale
        assert result.json()["used_tokens"] == 10
    from swe.app.conversation_storage.errors import StorageUnavailable

    async def failed(*args, **kwargs):
        raise StorageUnavailable("database unavailable")

    monkeypatch.setattr(stack.storage.history, "context_usage", failed)
    unavailable = await stack.client.get("/chats/chat-a/context-usage")
    assert unavailable.status_code == 503


async def test_http_expert_binding_live_definition_and_disable_release(
    api_stack,
    monkeypatch,
):
    from swe.app.runner import expert_binding

    stack = api_stack
    await history_dataset(stack)
    definitions = {"expert": SimpleNamespace(name="Expert One", enabled=True)}
    resolve = lambda workspace, key: definitions.get(key)
    monkeypatch.setattr(api, "resolve_bound_expert", resolve)
    monkeypatch.setattr(expert_binding, "resolve_bound_expert", resolve)
    initial = await stack.client.get("/chats/chat-a/expert-binding")
    assert initial.status_code == 200 and initial.json()["revision"] == 0
    bound = await stack.client.put(
        "/chats/chat-a/expert-binding",
        json={"expected_revision": 0, "expert_id": "expert"},
    )
    assert (
        bound.status_code == 200
        and bound.json()["expert_name"] == "Expert One"
    )
    assert bound.json()["revision"] == 1
    definitions.clear()
    unavailable = await stack.client.get("/chats/chat-a/expert-binding")
    assert (
        unavailable.status_code == 200 and not unavailable.json()["available"]
    )
    assert unavailable.json()["reason"] == "expert_unavailable"
    disabled = await stack.client.get(
        "/chats/chat-a/expert-binding",
        headers={"X-Test-Expert": "false"},
    )
    assert disabled.status_code == 200 and disabled.json()["expert_id"] is None
    assert disabled.json()["revision"] == 2
    assert disabled.json()["reason"] == "expert_main_disabled"
    reenabled = await stack.client.get("/chats/chat-a/expert-binding")
    assert (
        reenabled.json()["expert_id"] is None
        and reenabled.json()["revision"] == 2
    )


async def test_http_tracing_target_scope_permissions_and_approval_overlay(
    api_stack,
    monkeypatch,
):
    stack = api_stack
    session, a, _ = await history_dataset(stack)
    approval = source("approval")
    approval["metadata"] = {
        "approval_action": {"requestId": "req", "status": "pending"},
    }
    await add_messages(
        stack.db,
        stack.storage.history,
        session,
        a,
        [
            ("memory", approval, 1),
            (
                "task",
                {"id": "task", "role": "assistant", "content": "task"},
                2,
            ),
            (
                "error",
                {
                    "id": "error",
                    "role": "assistant",
                    "code": "model_call_failed",
                    "message": "failed",
                },
                3,
            ),
            ("anchor", source("anchor", "user"), 4),
        ],
    )
    await add_messages(
        stack.db,
        stack.storage.history,
        session,
        a,
        [
            ("archive", source("old-question", "user"), 1),
            ("archive", source("old-answer"), 2),
        ],
        boundary=True,
    )
    resolutions = []

    class TargetManager:
        async def get_agent(self, agent_id, tenant_id=None):
            resolutions.append((agent_id, tenant_id))
            return stack.workspace

    stack.app.state.multi_agent_manager = TargetManager()
    monkeypatch.setattr(
        tracing,
        "resolve_request_effective_tenant_id",
        lambda user, source: "tenant",
    )
    overlays = []

    class Approvals:
        async def get_requests(self, ids):
            overlays.append(
                (
                    get_current_tenant_id(),
                    get_current_source_id(),
                    get_current_user_id(),
                    ids,
                ),
            )
            return {"req": SimpleNamespace(status="approved")}

    monkeypatch.setattr(api, "get_approval_service", Approvals)
    with tenant_context(
        tenant_id="operator",
        source_id="operator-source",
        user_id="operator",
    ):
        listed = await stack.client.get(
            "/tracing/chats",
            params={"user_id": "alice", "channel": "console"},
            headers={"X-Agent-Id": "agent", "X-Test-User": "operator"},
        )
        result = await stack.client.get(
            "/tracing/chats/chat-a",
            params={"user_id": "alice"},
            headers={"X-Agent-Id": "agent", "X-Test-User": "operator"},
        )
        archived = await stack.client.get(
            "/tracing/chats/chat-a/history",
            params={"user_id": "alice", "limit": 1},
            headers={"X-Agent-Id": "agent", "X-Test-User": "operator"},
        )
    assert listed.status_code == 200
    assert {item["id"] for item in listed.json()} == {"chat-a", "chat-b"}
    assert all(item["user_id"] == "alice" for item in listed.json())
    assert result.status_code == 200
    assert message_ids(result.json()) == [
        "approval",
        "task",
        "error",
        "anchor",
    ]
    assert result.json()["chat"]["user_id"] == "alice"
    assert result.json()["archive"]["has_more"] is True
    assert len(result.json()["archive"]["boundaries"]) == 1
    assert archived.status_code == 200
    assert message_ids(archived.json()) == ["old-answer"]
    assert archived.json()["has_more"] is True
    earlier = await stack.client.get(
        "/tracing/chats/chat-a/history",
        params={
            "user_id": "alice",
            "limit": 1,
            "before": archived.json()["next_cursor"],
        },
        headers={"X-Agent-Id": "agent", "X-Test-User": "operator"},
    )
    assert earlier.status_code == 200
    assert message_ids(earlier.json()) == ["old-question"]
    assert earlier.json()["has_more"] is False
    normal = await stack.client.get(
        "/chats/chat-a",
        headers={"X-Agent-Id": "agent"},
    )
    assert normal.status_code == 200
    assert normal.json() == result.json()
    action = result.json()["messages"][0]["metadata"]["metadata"][
        "approval_action"
    ]
    assert action["status"] == "approved"
    assert overlays == [("tenant", "source", "alice", ["req"])] * 2
    assert resolutions == [("agent", "tenant")] * 4
    denied = await stack.client.get(
        "/tracing/chats/chat-a",
        params={"user_id": "bob"},
        headers={"X-Agent-Id": "agent"},
    )
    assert denied.status_code == 404


@pytest.mark.parametrize(
    "cold,durable_turn",
    [(False, False), (True, False), (True, True)],
)
async def test_http_share_options_selected_turns_permanent_token_and_missing(
    api_stack,
    cold,
    durable_turn,
):
    stack = api_stack
    session, a, _ = await history_dataset(stack)
    await add_messages(
        stack.db,
        stack.storage.history,
        session,
        a,
        [
            ("archive" if cold else "memory", source("old-q", "user"), 1),
            ("archive" if cold else "memory", source("old-a"), 2),
        ],
        boundary=cold,
    )
    await add_messages(
        stack.db,
        stack.storage.history,
        session,
        a,
        [
            ("memory", source("new-q", "user"), 3),
            ("memory", source("new-a"), 4),
        ],
    )
    await stack.workspace.runner.session.save_merged_state(
        "session",
        "alice",
        state={
            "turn_states": {
                "old-q": {"status": "completed"},
                "new-q": {"status": "failed"},
            },
        },
    )
    if durable_turn:
        turn = await insert_row(
            stack.db,
            "swe_conversation_turns",
            {
                "session_id": session,
                "chat_id": a,
                "question_msg_key": b"old-q",
                "turn_order": 1,
                "context_epoch": 0,
                "generation": 0,
                "state": "succeeded",
                "history_status": "completed",
            },
        )
        await stack.db.execute(
            "UPDATE swe_conversation_messages SET turn_id=%s WHERE source_msg_key IN (%s,%s)",
            (turn, b"old-q", b"old-a"),
        )
    options = await stack.client.get("/chats/chat-a/share-options")
    assert options.status_code == 200
    assert message_ids(options.json()) == ["old-q", "old-a", "new-q", "new-a"]
    assert options.json()["turn_statuses"] == {
        "old-q": "completed",
        "new-q": "failed",
    }
    rejected = await stack.client.post(
        "/chats/chat-a/share",
        json={"turn_ids": ["new-q"]},
    )
    assert rejected.status_code == 400
    created = await stack.client.post(
        "/chats/chat-a/share",
        json={"turn_ids": ["old-q"]},
    )
    assert created.status_code == 200
    token = created.json()["token"]
    await stack.storage.chats.delete_chats(["chat-a"])
    snapshot = await stack.client.get(f"/chat-shares/{token}")
    assert snapshot.status_code == 200 and message_ids(snapshot.json()) == [
        "old-q",
        "old-a",
    ]
    assert (await stack.storage.shares.store.get(token)).access_count == 1
    assert (await stack.client.get("/chat-shares/missing")).status_code == 404


async def test_http_legacy_orphan_token_uses_publication_scope_and_preserves_counts(
    api_stack,
):
    stack = api_stack
    operation = new_operation_key()
    plan = ShareInstall(
        "orphan-token",
        "deleted-chat",
        "creator",
        "legacy-tenant",
        "original/opaque-key",
        "2026-10-30T00:00:00Z",
        access_count=7,
    )
    await stack.storage.publications.reserve(
        PublicationRequest(
            operation,
            WorkspaceScope(
                "legacy-tenant",
                "",
                "",
                service_unit_id="schema-test-service",
            ),
            "share_import",
            "offline",
            0,
            scope_kind="legacy_share",
        ),
        CommitPlan(share=plan),
    )
    await stack.storage.publications.write_bundle(
        operation,
        [
            BundleBlock(
                "share",
                "share",
                b'{"chat_name":"orphan","messages":[]}',
            ),
        ],
    )
    await stack.storage.publications.commit(operation)
    result = await stack.client.get(
        "/chat-shares/orphan-token",
        headers={"X-Test-User": "unrelated"},
    )
    assert result.status_code == 200 and result.json()["chat_name"] == "orphan"
    record = await stack.storage.shares.store.get("orphan-token")
    assert (
        record.access_count == 8
        and record.snapshot_key == "original/opaque-key"
    )


async def test_cron_receipts_recover_from_new_adapter_without_chats_json(
    api_stack,
):
    stack = api_stack
    run = {
        "spec_json": json.dumps(
            {
                "meta": {
                    "task_session_id": "cron-session",
                    "creator_user_id": "alice",
                },
            },
        ),
        "execution_id": "execution",
        "run_owner": "owner",
        "tenant_id": "tenant",
        "source_id": "source",
        "agent_id": "agent",
        "job_id": "job",
    }
    session = stack.storage.session_for("console")
    outcome = ExecutionOutcome(
        status="success",
        preview="done",
        execution_meta={"history": ["one"]},
    )
    await SessionResultReceipts(session).save(run, outcome)
    restarted = WorkspaceConversationStorage(
        stack.db,
        stack.storage.scope,
        stack.storage.workspace_root,
        cursor_secret=b"new-process-secret",
    )
    recovered = SessionResultReceipts(restarted.session_for("console"))
    assert await recovered.load(run) == outcome
    with pytest.raises(ValueError, match="identity_conflict"):
        await recovered.load({**run, "run_owner": "other-owner"})
    await recovered.clear(run)
    assert await recovered.load(run) is None
    assert not (stack.storage.workspace_root / "chats.json").exists()


async def test_wplus_event_tool_projects_sql_chat_without_chats_json(
    api_stack,
    monkeypatch,
):
    from swe.agents.tools.emit_wplus_sop_event import emit_wplus_sop_event
    from swe.app import agent_context
    from swe.app.conversation_storage import runtime
    from swe.app.wplus_sop.models import (
        CommandReceipt,
        OwnershipTuple,
        RunAttempt,
        RunStatus,
        SessionProjection,
        SessionState,
    )
    from swe.app.wplus_sop.runtime_context import (
        WPlusRuntimeContext,
        bind_wplus_runtime,
    )
    from swe.app.wplus_sop.service import store_path_for_workspace
    from swe.app.wplus_sop.store import WPlusSopStore
    from swe.config.context import (
        reset_current_task_progress_chat_id,
        set_current_task_progress_chat_id,
    )

    stack = api_stack
    await history_dataset(stack)
    root = stack.storage.workspace_root
    ownership = OwnershipTuple(
        tenant_id="tenant",
        source_id="source",
        user_id="alice",
        agent_id="agent",
        chat_id="chat-a",
        logical_chat_session_id="session",
    )
    store = WPlusSopStore(store_path_for_workspace(root))
    store.create_session(
        SessionProjection(
            sop_session_id="sop",
            ownership=ownership,
            skill_snapshot_id="sha256:miner",
            state=SessionState.GENERATING_STAGE_PROPOSAL,
            state_version=1,
            title="SOP",
            current_run_id="run",
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd",
            command="confirm_entry",
            sop_session_id="sop",
            resulting_state_version=1,
        ),
        run_attempt=RunAttempt(
            run_id="run",
            attempt_id="attempt",
            command_request_id="cmd",
            command="confirm_entry",
            status=RunStatus.CLAIMED,
        ),
    )
    installed = runtime.ConversationRuntime(
        stack.db,
        {
            "version": 2,
            "service_unit_id": "schema-test-service",
            "nas_root": str(root),
            "workspaces": [
                {
                    "root": str(root),
                    "scope": [
                        "schema-test-service",
                        "tenant",
                        "source",
                        "agent",
                    ],
                },
            ],
        },
        b"installed-secret",
        service_unit_id="schema-test-service",
    )
    monkeypatch.setenv(runtime.AUTHORITY_ENV, "mysql")
    monkeypatch.setattr(runtime, "_runtime", installed)

    # Any accidental old file repository access is a test failure.
    def reject_legacy(*args, **kwargs):
        pytest.fail("W+ tool constructed the legacy file Chat repository")

    monkeypatch.setattr(
        "swe.app.runner.repo.json_repo.JsonChatRepository",
        reject_legacy,
    )
    agent_token = agent_context._current_agent_id.set("agent")
    chat_token = set_current_task_progress_chat_id("chat-a")
    try:
        with tenant_context(
            tenant_id="tenant",
            source_id="source",
            user_id="alice",
            workspace_dir=root,
        ):
            with bind_wplus_runtime(
                WPlusRuntimeContext(
                    sop_session_id="sop",
                    run_id="run",
                    attempt_id="attempt",
                    command="confirm_entry",
                ),
            ):
                response = await emit_wplus_sop_event(
                    kind="stage_proposal",
                    event_key="stages-v1",
                    payload={
                        "stages": [
                            {"stage_id": "one", "name": "确认范围"},
                            {"stage_id": "two", "name": "生成结果"},
                        ],
                    },
                )
    finally:
        reset_current_task_progress_chat_id(chat_token)
        agent_context._current_agent_id.reset(agent_token)
    payload = json.loads(response.content[0]["text"])
    assert payload["ok"], payload
    assert payload["chat_projection_events"] > 0
    chat = await stack.manager.get_chat("chat-a")
    assert (
        chat.meta["wplus_sop_session"]["state"] == "AwaitingQueueConfirmation"
    )
    assert store.pending_outbox() == []
    assert not (root / "chats.json").exists()


async def completed_selection(stack):
    session, chat, _ = await history_dataset(stack)
    await add_messages(
        stack.db,
        stack.storage.history,
        session,
        chat,
        [("memory", source("q", "user"), 1), ("memory", source("answer"), 2)],
    )
    await stack.workspace.runner.session.save_merged_state(
        "session",
        "alice",
        state={"turn_states": {"q": {"status": "completed"}}},
    )
    return session, chat


async def test_http_share_uses_chat_channel_identity_not_console_or_meta(
    api_stack,
):
    stack = api_stack
    console_session = await insert_session(stack.db)
    await insert_chat(stack.db, console_session, b"console-chat")
    cron_session = await insert_session(stack.db, channel_key=b"cron")
    cron_chat = await insert_chat(
        stack.db,
        cron_session,
        b"cron-chat",
        channel_key=b"cron",
    )
    await stack.db.execute(
        "UPDATE swe_conversation_sessions SET history_high=2 WHERE id=%s",
        (cron_session,),
    )
    await add_messages(
        stack.db,
        stack.storage.history,
        cron_session,
        cron_chat,
        [("memory", source("q", "user"), 1), ("memory", source("a"), 2)],
    )
    chat = await stack.manager.get_chat("cron-chat")
    chat.meta = {"channel": "console", "session_id": "untrusted"}
    await stack.manager.update_chat(chat)
    for channel, status in (("console", "failed"), ("cron", "completed")):
        await stack.storage.session_for(channel).save_merged_state(
            "session",
            "alice",
            state={"turn_states": {"q": {"status": status}}},
        )
    options = await stack.client.get("/chats/cron-chat/share-options")
    assert options.status_code == 200
    assert options.json()["turn_statuses"] == {"q": "completed"}
    created = await stack.client.post(
        "/chats/cron-chat/share",
        json={"turn_ids": ["q"]},
    )
    assert created.status_code == 200
    snapshot = await stack.client.get(
        f"/chat-shares/{created.json()['token']}",
    )
    assert message_ids(snapshot.json()) == ["q", "a"]


async def test_http_share_options_rejects_changed_archive_epoch(
    api_stack,
    monkeypatch,
):
    stack = api_stack
    _, chat = await completed_selection(stack)
    original = sharing._load_share_messages

    async def changed(*args, **kwargs):
        messages = await original(*args, **kwargs)
        await stack.db.execute(
            "UPDATE swe_conversation_chats SET epoch=epoch+1 WHERE id=%s",
            (chat,),
        )
        return messages

    monkeypatch.setattr(sharing, "_load_share_messages", changed)
    result = await stack.client.get("/chats/chat-a/share-options")
    assert result.status_code == 503


async def test_cli_list_and_detail_use_real_sql_http_api(
    api_stack,
    monkeypatch,
):
    import asyncio
    from click.testing import CliRunner
    from swe.cli import chats_cmd

    stack = api_stack
    await completed_selection(stack)
    loop = asyncio.get_running_loop()

    class ClientBridge:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, path, **kwargs):
            return asyncio.run_coroutine_threadsafe(
                stack.client.get(path, **kwargs),
                loop,
            ).result(timeout=60)

    monkeypatch.setattr(chats_cmd, "client", lambda base_url: ClientBridge())
    runner = CliRunner()
    listed = await asyncio.to_thread(
        runner.invoke,
        chats_cmd.chats_group,
        ["list", "--agent-id", "agent", "--channel", "console"],
    )
    assert listed.exit_code == 0, listed.output
    assert {item["id"] for item in json.loads(listed.output)} == {
        "chat-a",
        "chat-b",
    }
    detail = await asyncio.to_thread(
        runner.invoke,
        chats_cmd.chats_group,
        ["get", "chat-a", "--agent-id", "agent"],
    )
    assert detail.exit_code == 0, detail.output
    assert message_ids(json.loads(detail.output)) == ["q", "answer"]


async def test_agent_scoped_route_and_header_select_sql_workspace(
    api_stack,
    monkeypatch,
):
    from swe.app import agent_context
    from swe.app.routers.agent_scoped import (
        AgentContextMiddleware,
        create_agent_scoped_router,
    )

    stack = api_stack
    await history_dataset(stack)
    other_root = stack.storage.workspace_root / "other"
    other_root.mkdir()
    other_storage = WorkspaceConversationStorage(
        stack.db,
        WorkspaceScope(
            "tenant",
            "source",
            "other",
            service_unit_id="schema-test-service",
        ),
        other_root,
        cursor_secret=b"other-secret",
    )
    other_manager = ChatManager(repo=other_storage.chats)
    session = await insert_session(stack.db, agent_key=b"other")
    await insert_chat(stack.db, session, b"other-chat", agent_key=b"other")
    other_workspace = SimpleNamespace(
        conversation_storage=other_storage,
        workspace_dir=other_root,
        agent_id="other",
        tenant_id="tenant",
        chat_manager=other_manager,
        runner=SimpleNamespace(session=other_storage.session_for("console")),
    )
    resolved = []

    class Manager:
        async def get_agent(self, agent_id, tenant_id=None):
            resolved.append(agent_id)
            return {"agent": stack.workspace, "other": other_workspace}[
                agent_id
            ]

    stack.app.state.multi_agent_manager = Manager()
    monkeypatch.setattr(
        agent_context,
        "_get_tenant_aware_config",
        lambda *args, **kwargs: SimpleNamespace(
            agents=SimpleNamespace(
                active_agent="agent",
                profiles={
                    key: SimpleNamespace(enabled=True)
                    for key in ("agent", "other")
                },
            ),
        ),
    )
    stack.app.dependency_overrides.clear()
    stack.app.include_router(create_agent_scoped_router(), prefix="/api")
    stack.app.add_middleware(AgentContextMiddleware)
    for path, headers in (
        ("/api/agents/other/chats", {}),
        ("/chats", {"X-Agent-Id": "other"}),
    ):
        result = await stack.client.get(path, headers=headers)
        assert result.status_code == 200
        assert [item["id"] for item in result.json()] == ["other-chat"]
    denied = await stack.client.get("/api/agents/other/chats/chat-a")
    assert denied.status_code == 404
    assert resolved and set(resolved) == {"other"}


@pytest.mark.parametrize("command", ["/new", "/clear"])
async def test_conversation_command_dispatch_resets_sql_epoch_and_complete_root(
    api_stack,
    command,
):
    from agentscope.message import Msg
    from reme.memory.file_based.reme_in_memory_memory import ReMeInMemoryMemory
    from swe.agents.memory.reme_light_memory_manager import (
        ReMeLightMemoryManager,
    )
    from swe.app.runner.command_dispatch import run_command_path

    stack = api_stack
    session, chat, _ = await history_dataset(stack)
    await stack.db.execute(
        "UPDATE swe_conversation_chats SET epoch=1 WHERE id=%s",
        (chat,),
    )
    message = Msg("user", "preserved visible history", "user")
    message.id = "q"
    adapter = stack.storage.session_for("console")
    await adapter.save_merged_state(
        "session",
        "alice",
        state={
            "future_module": {"preserve": [1, 2]},
            "managed_cron_result_receipts": {"run": {"preview": "done"}},
            "agent": {
                "memory": {
                    "content": [[message.to_dict(), []]],
                    "_compressed_summary": "old",
                },
            },
        },
    )
    memory = ReMeInMemoryMemory(token_counter=None)
    manager = object.__new__(ReMeLightMemoryManager)
    manager.get_in_memory_memory = lambda **kwargs: memory
    manager.add_async_summary_task = lambda **kwargs: None
    runner = SimpleNamespace(
        _workspace=stack.workspace,
        _chat_manager=stack.manager,
        session=adapter,
        memory_manager=manager,
    )
    request = SimpleNamespace(
        session_id="session",
        user_id="alice",
        channel="console",
        chat_id="chat-a",
        channel_meta={},
    )
    async with adapter.execution("session", "alice") as execution:
        frames = [
            item
            async for item in run_command_path(
                request,
                [Msg("user", command, "user")],
                runner,
                session_execution=execution,
            )
        ]
    assert len(frames) == 1 and frames[0][1] is True
    state = await adapter.get_session_state_dict("session", "alice")
    assert state["agent"]["memory"] == {
        "content": [],
        "_compressed_summary": "",
    }
    assert state["future_module"] == {"preserve": [1, 2]}
    assert state["managed_cron_result_receipts"] == {
        "run": {"preview": "done"},
    }
    assert (
        await stack.storage.archive.read_checkpoint_state("chat-a")
    ).current_epoch == 2
    assert not (stack.storage.workspace_root / "chats.json").exists()


@pytest.mark.parametrize(
    "state",
    ["reserved", "admitted", "running", "settling", "stopping"],
)
async def test_restarted_console_reconnect_does_not_invent_sql_terminal(
    api_stack,
    state,
):
    from fastapi import HTTPException
    from unittest.mock import AsyncMock
    from swe.app.answer_turn.coordinator import AnswerTurnCoordinator
    from swe.app.routers.console import _resolve_current_reconnect_target

    stack = api_stack
    session, chat, _ = await history_dataset(stack)
    turn = await insert_row(
        stack.db,
        "swe_conversation_turns",
        {
            "session_id": session,
            "chat_id": chat,
            "question_msg_key": b"q",
            "turn_order": 1,
            "context_epoch": 0,
            "generation": 1,
            "state": state,
        },
    )
    await stack.db.execute(
        "UPDATE swe_conversation_sessions SET active_turn_id=%s WHERE id=%s",
        (turn, session),
    )
    stack.workspace.answer_turn_coordinator = AnswerTurnCoordinator(
        **{
            key: SimpleNamespace()
            for key in (
                "stream",
                "execution",
                "session",
                "goal",
                "subagent",
                "approval",
            )
        },
    )
    stack.workspace.runner.session.mutate_session_state = AsyncMock(
        side_effect=AssertionError("reconnect must not mutate SQL root"),
    )
    with pytest.raises(HTTPException) as error:
        await _resolve_current_reconnect_target(
            workspace=stack.workspace,
            native_payload={
                "chat_id": "chat-a",
                "sender_id": "alice",
                "channel_id": "console",
                "meta": {},
            },
            session_id="session",
            identity={"source_id": "source", "agent_id": "agent"},
        )
    assert error.value.status_code == 503 and error.value.headers == {
        "Retry-After": "1",
    }
    assert (
        await stack.db.fetch_one(
            "SELECT state FROM swe_conversation_turns WHERE id=%s",
            (turn,),
        )
    )["state"] == state


async def test_console_terminal_recovery_uses_trusted_chat_channel(
    api_stack,
):
    from swe.app.routers.console import (
        _current_recovery_terminal_snapshot,
        _resolve_current_recovery_chat,
    )

    stack = api_stack
    console = await insert_session(stack.db)
    await insert_chat(stack.db, console, b"console-chat")
    cron = await insert_session(stack.db, channel_key=b"cron")
    await insert_chat(
        stack.db,
        cron,
        b"cron-chat",
        channel_key=b"cron",
    )
    for channel, status in (("console", "failed"), ("cron", "completed")):
        await stack.storage.session_for(channel).save_merged_state(
            "session",
            "alice",
            state={"turn_states": {"q": {"status": status}}},
        )
    selected = await stack.manager.get_chat("cron-chat")
    selected.meta = {"channel": "console", "session_id": "untrusted"}
    await stack.manager.update_chat(selected)
    _, msgid, status = await _current_recovery_terminal_snapshot(
        stack.workspace,
        selected,
    )
    assert (msgid, status) == ("q", "completed")
    recovered = await _resolve_current_recovery_chat(
        stack.workspace,
        requested_chat_id="cron-chat",
        session_id="session",
        sender_id="alice",
        channel_id="console",
        identity={"source_id": "source", "agent_id": "agent"},
    )
    assert recovered.id == "console-chat"


@pytest.mark.parametrize("status", ["completed", "interrupted"])
async def test_console_reconnect_reads_committed_v3_terminal_snapshot(
    api_stack,
    status,
):
    from agentscope.message import Msg
    from swe.app.answer_turn.coordinator import AnswerTurnCoordinator
    from swe.app.routers.console import _resolve_current_reconnect_target
    from tests.integrated.conversation_storage.test_execution_mysql import (
        identity,
    )

    stack = api_stack
    await history_dataset(stack)
    q = Msg("user", "hello", "user")
    q.id = "question"
    admission = await stack.storage.executions.reserve_admission(
        identity(),
        "chat-a",
        q.id,
        operation_key=new_operation_key(),
        runtime_turn_key="runtime",
    )
    state = {
        "schema_version": 3,
        "turn_states": {
            q.id: {
                "status": "admitted",
                "chat_id": "chat-a",
                "message": q.to_dict(),
            },
        },
    }
    await stack.storage.executions.publish_admission(
        admission,
        q.to_dict(),
        state,
    )
    await stack.storage.executions.start(admission.authority)
    state["turn_states"][q.id]["status"] = status
    await stack.storage.executions.finish(
        admission.authority,
        status,
        state,
        operation_key=new_operation_key(),
    )
    stack.workspace.answer_turn_coordinator = AnswerTurnCoordinator(
        **{
            key: SimpleNamespace()
            for key in (
                "stream",
                "execution",
                "session",
                "goal",
                "subagent",
                "approval",
            )
        },
    )
    response = await _resolve_current_reconnect_target(
        workspace=stack.workspace,
        native_payload={
            "chat_id": "chat-a",
            "sender_id": "alice",
            "channel_id": "console",
            "meta": {},
        },
        session_id="session",
        identity={"source_id": "source", "agent_id": "agent"},
    )
    events = [item async for item in response.body_iterator]
    assert len(events) == 1 and events[0].startswith("event: chat.snapshot\n")
    payload = json.loads(events[0].split("data: ", 1)[1])
    assert payload["msgid"] == "question" and payload["turn_status"] == (
        "failed" if status == "interrupted" else status
    )
    assert payload["history"]["status"] == "idle"
    assert message_ids(payload["history"]) == ["question"]
    assert response.headers["X-Swe-Chatid"] == "chat-a"


@pytest.mark.parametrize("changed", ["admission", "state_revision", "epoch"])
async def test_console_terminal_snapshot_rechecks_sql_watermark(
    api_stack,
    monkeypatch,
    changed,
):
    from fastapi import HTTPException
    from swe.app.routers.console import _current_recovery_terminal_snapshot

    stack = api_stack
    session_id, chat, _ = await history_dataset(stack)
    selected = await stack.manager.get_chat("chat-a")
    original = api._build_chat_history

    async def changed_history(chat_spec, *, session, workspace, **kwargs):
        result = await original(
            chat_spec,
            session=session,
            workspace=workspace,
            **kwargs,
        )
        if changed == "admission":
            turn = await insert_row(
                stack.db,
                "swe_conversation_turns",
                {
                    "session_id": session_id,
                    "chat_id": chat,
                    "question_msg_key": b"new",
                    "turn_order": 1,
                    "context_epoch": 0,
                    "generation": 1,
                    "state": "admitted",
                },
            )
            await stack.db.execute(
                "UPDATE swe_conversation_sessions SET active_turn_id=%s,generation=generation+1 WHERE id=%s",
                (turn, session_id),
            )
        elif changed == "state_revision":
            await stack.db.execute(
                "UPDATE swe_conversation_sessions SET state_revision=state_revision+1 WHERE id=%s",
                (session_id,),
            )
        else:
            await stack.db.execute(
                "UPDATE swe_conversation_chats SET epoch=epoch+1 WHERE id=%s",
                (chat,),
            )
        return result

    monkeypatch.setattr(api, "_build_chat_history", changed_history)
    with pytest.raises(HTTPException) as error:
        await _current_recovery_terminal_snapshot(stack.workspace, selected)
    assert error.value.status_code == 503


@pytest.mark.parametrize(
    "state,status",
    [("running", "completed"), ("succeeded", None)],
)
async def test_http_share_proof_never_overrides_active_or_unknown_turn(
    api_stack,
    state,
    status,
):
    stack = api_stack
    session, chat = await completed_selection(stack)
    await insert_row(
        stack.db,
        "swe_conversation_turns",
        {
            "session_id": session,
            "chat_id": chat,
            "question_msg_key": b"q",
            "turn_order": 1,
            "context_epoch": 0,
            "generation": 0,
            "state": state,
            "history_status": status,
        },
    )
    result = await stack.client.post(
        "/chats/chat-a/share",
        json={"turn_ids": ["q"]},
    )
    assert result.status_code == 503
    assert not await stack.db.fetch_all("SELECT token FROM swe_chat_shares")


@pytest.mark.parametrize("changed", ["state_revision", "history_revision"])
async def test_http_share_rejects_revision_change_during_payload_read(
    api_stack,
    monkeypatch,
    changed,
):
    stack = api_stack
    session, chat = await completed_selection(stack)
    original = sharing._share_context

    async def changed_context(*args, **kwargs):
        result = await original(*args, **kwargs)
        table, identity = (
            ("sessions", session)
            if changed == "state_revision"
            else ("chats", chat)
        )
        await stack.db.execute(
            f"UPDATE swe_conversation_{table} SET {changed}={changed}+1 WHERE id=%s",
            (identity,),
        )
        return result

    monkeypatch.setattr(sharing, "_share_context", changed_context)
    result = await stack.client.post(
        "/chats/chat-a/share",
        json={"turn_ids": ["q"]},
    )
    assert result.status_code == 503
    assert not await stack.db.fetch_all("SELECT token FROM swe_chat_shares")


async def test_http_duplicate_archive_question_uses_first_legacy_slice_and_source_proof(
    api_stack,
):
    stack = api_stack
    session, a, _ = await history_dataset(stack)
    await add_messages(
        stack.db,
        stack.storage.history,
        session,
        a,
        [
            ("archive", source("q", "user", "first question"), 1),
            ("archive", source("a", content="first answer"), 2),
            ("archive", source("q", "user", "second question"), 3),
            ("archive", source("later", content="second answer"), 4),
        ],
        boundary=True,
    )
    await add_messages(
        stack.db,
        stack.storage.history,
        session,
        a,
        [("anchor", source("q", "user", "suppressed"), 5)],
    )
    await stack.workspace.runner.session.save_merged_state(
        "session",
        "alice",
        state={"turn_states": {"q": {"status": "completed"}}},
    )
    created = await stack.client.post(
        "/chats/chat-a/share",
        json={"turn_ids": ["q"]},
    )
    assert created.status_code == 200
    snapshot = await stack.client.get(
        f"/chat-shares/{created.json()['token']}",
    )
    assert message_ids(snapshot.json()) == ["q", "a"]
    record = await stack.storage.shares.store.get(created.json()["token"])
    publication = await stack.db.fetch_one(
        "SELECT * FROM swe_conversation_publications WHERE id=%s",
        (record.publication_id,),
    )
    metadata = json.loads(publication["result_metadata_text"])
    proof = metadata["share"]["selection_proof"]
    row = await stack.db.fetch_one(
        "SELECT * FROM swe_conversation_messages WHERE id=%s",
        (proof["questions"]["q"]["message_id"],),
    )
    assert row["layer"] == "archive" and row["archive_order"] == 1
    assert (
        publication["base_state_revision"]
        == proof["snapshot"]["state_revision"]
    )
    assert (
        publication["base_history_revision"]
        == proof["snapshot"]["history_revision"]
    )


@pytest.mark.parametrize("changed", ["state_revision", "history_revision"])
async def test_http_share_rechecks_proof_after_durable_body(
    api_stack,
    monkeypatch,
    changed,
):
    from swe.app.conversation_storage.publications import PublicationStore

    stack = api_stack
    session, chat = await completed_selection(stack)
    original = PublicationStore.write_bundle

    async def changed_bundle(service, *args, **kwargs):
        result = await original(service, *args, **kwargs)
        table, identity = (
            ("sessions", session)
            if changed == "state_revision"
            else ("chats", chat)
        )
        await stack.db.execute(
            f"UPDATE swe_conversation_{table} SET {changed}={changed}+1 WHERE id=%s",
            (identity,),
        )
        return result

    monkeypatch.setattr(PublicationStore, "write_bundle", changed_bundle)
    result = await stack.client.post(
        "/chats/chat-a/share",
        json={"turn_ids": ["q"]},
    )
    assert result.status_code == 503
    assert not await stack.db.fetch_all("SELECT token FROM swe_chat_shares")

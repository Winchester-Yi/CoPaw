# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from swe.config.context import encode_scope_id
from src.swe.app.post_turn_continuation_store import (
    clear_pending_continuations,
    store_pending_continuation,
)
from src.swe.app.routers import console as console_router


@pytest_asyncio.fixture(autouse=True)
async def _clear_store():
    await clear_pending_continuations()
    yield
    await clear_pending_continuations()


def _client() -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def _tenant_middleware(request: Request, call_next):
        request.state.tenant_id = request.headers.get("X-Tenant-Id")
        request.state.scope_id = request.headers.get("X-Scope-Id")
        return await call_next(request)

    app.include_router(console_router.router)
    return TestClient(app)


@pytest.mark.asyncio
async def test_get_post_turn_validation_returns_latest_pending() -> None:
    scope_id = encode_scope_id("tenant-a", "source-a")
    await store_pending_continuation(
        session_id="session-1",
        user_message="user",
        assistant_response="assistant",
        reason="需要继续处理剩余步骤",
        follow_up_prompt="继续处理剩余步骤。",
        tenant_id=scope_id,
    )

    response = _client().get(
        "/console/post-turn-validation?session_id=session-1",
        headers={
            "X-Tenant-Id": "tenant-a",
            "X-Scope-Id": scope_id,
        },
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["status"] == "needs_confirmation"
    assert result["completed"] is False
    assert result["reason"] == "需要继续处理剩余步骤"
    assert "follow_up_prompt" not in result


@pytest.mark.asyncio
async def test_consume_post_turn_validation_is_one_time() -> None:
    scope_id = encode_scope_id("tenant-a", "source-a")
    stored = await store_pending_continuation(
        session_id="session-1",
        user_message="user",
        assistant_response="assistant",
        reason="需要继续",
        follow_up_prompt="继续。",
        tenant_id=scope_id,
    )
    client = _client()

    first = client.post(
        f"/console/post-turn-validation/{stored['id']}/consume",
        json={"session_id": "session-1"},
        headers={"X-Tenant-Id": "tenant-a", "X-Scope-Id": scope_id},
    )
    second = client.post(
        f"/console/post-turn-validation/{stored['id']}/consume",
        json={"session_id": "session-1"},
        headers={"X-Tenant-Id": "tenant-a", "X-Scope-Id": scope_id},
    )

    assert first.status_code == 200
    assert first.json()["result"]["status"] == "consumed"
    assert second.status_code == 404
    hidden_after_claim = client.get(
        "/console/post-turn-validation?session_id=session-1",
        headers={"X-Tenant-Id": "tenant-a", "X-Scope-Id": scope_id},
    )
    assert hidden_after_claim.json()["result"] is None


@pytest.mark.asyncio
async def test_consume_rejects_tenant_or_session_mismatch() -> None:
    scope_id = encode_scope_id("tenant-a", "source-a")
    stored = await store_pending_continuation(
        session_id="session-1",
        user_message="user",
        assistant_response="assistant",
        reason="需要继续",
        follow_up_prompt="继续。",
        tenant_id=scope_id,
    )
    client = _client()

    wrong_tenant = client.post(
        f"/console/post-turn-validation/{stored['id']}/consume",
        json={"session_id": "session-1"},
        headers={
            "X-Tenant-Id": "tenant-b",
            "X-Scope-Id": encode_scope_id("tenant-b", "source-a"),
        },
    )
    wrong_session = client.post(
        f"/console/post-turn-validation/{stored['id']}/consume",
        json={"session_id": "session-2"},
        headers={"X-Tenant-Id": "tenant-a", "X-Scope-Id": scope_id},
    )

    assert wrong_tenant.status_code == 404
    assert wrong_session.status_code == 404


def test_chat_payload_carries_resume_id_in_channel_meta() -> None:
    payload = console_router._extract_session_and_payload(
        {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "继续执行上一步任务"},
                    ],
                },
            ],
            "session_id": "session-1",
            "user_id": "user-1",
            "channel": "console",
            "post_turn_validation_resume_id": "validation_1",
        },
    )

    assert payload["meta"]["post_turn_validation_resume_id"] == "validation_1"


def test_chat_payload_carries_turn_id_in_channel_meta() -> None:
    payload = console_router._extract_session_and_payload(
        {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "帮我处理"},
                    ],
                },
            ],
            "session_id": "session-1",
            "user_id": "user-1",
            "channel": "console",
            "turn_id": "response-1",
        },
    )

    assert payload["meta"]["turn_id"] == "response-1"


def test_chat_payload_carries_chat_id_in_channel_meta() -> None:
    payload = console_router._extract_session_and_payload(
        {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "继续连接"},
                    ],
                },
            ],
            "session_id": "session-1",
            "chat_id": "chat-real-1",
            "user_id": "user-1",
            "channel": "console",
        },
    )

    assert payload["meta"]["chat_id"] == "chat-real-1"

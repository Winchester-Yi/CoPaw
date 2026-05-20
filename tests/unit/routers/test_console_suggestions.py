# -*- coding: utf-8 -*-
"""Console 猜你想问接口的回归测试。"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi import Request
from fastapi.testclient import TestClient

from swe.config.context import encode_scope_id
from src.swe.app.routers import console as console_router
from src.swe.app.suggestions import store_qa_content, store_suggestions


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
async def test_qa_content_endpoint_queries_by_user_message() -> None:
    chat_id = f"chat-{uuid.uuid4()}"
    scope_id = encode_scope_id("tenant-a", "source-a")
    await store_qa_content(
        chat_id=chat_id,
        user_message="帮我总结",
        assistant_response="总结完成",
        tenant_id=scope_id,
    )

    client = _client()

    response = client.post(
        "/console/suggestions/qa-content",
        json={
            "chat_id": chat_id,
            "user_message": "帮我总结",
        },
        headers={"X-Tenant-Id": "tenant-a", "X-Scope-Id": scope_id},
    )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "qa_content": {
            "user_message": "帮我总结",
            "assistant_response": "总结完成",
        },
    }


def test_qa_content_endpoint_returns_empty_for_unknown_message() -> None:
    client = _client()

    response = client.post(
        "/console/suggestions/qa-content",
        json={
            "chat_id": f"chat-{uuid.uuid4()}",
            "user_message": "不存在的问题",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "success": False,
        "qa_content": None,
    }


@pytest.mark.asyncio
async def test_suggestions_endpoint_prefers_request_scope_id() -> None:
    scope_id = encode_scope_id("tenant-a", "source-a")
    await store_suggestions(
        "session-1",
        ["继续追问"],
        tenant_id=scope_id,
    )
    client = _client()

    response = client.get(
        "/console/suggestions?session_id=session-1",
        headers={"X-Tenant-Id": "tenant-a", "X-Scope-Id": scope_id},
    )

    assert response.status_code == 200
    assert response.json() == {
        "suggestions": [
            {
                "id": response.json()["suggestions"][0]["id"],
                "suggestions": ["继续追问"],
            },
        ],
    }


@pytest.mark.asyncio
async def test_suggestions_endpoint_filters_by_turn_id() -> None:
    scope_id = encode_scope_id("tenant-a", "source-a")
    await store_suggestions(
        "session-1",
        ["第一轮建议"],
        tenant_id=scope_id,
        turn_id="response-1",
    )
    await store_suggestions(
        "session-1",
        ["第二轮建议"],
        tenant_id=scope_id,
        turn_id="response-2",
    )
    client = _client()

    response = client.get(
        "/console/suggestions?session_id=session-1&turn_id=response-2",
        headers={"X-Tenant-Id": "tenant-a", "X-Scope-Id": scope_id},
    )

    assert response.status_code == 200
    assert response.json() == {
        "suggestions": [
            {
                "id": response.json()["suggestions"][0]["id"],
                "suggestions": ["第二轮建议"],
                "turn_id": "response-2",
            },
        ],
    }

    response = client.get(
        "/console/suggestions?session_id=session-1&turn_id=response-1",
        headers={"X-Tenant-Id": "tenant-a", "X-Scope-Id": scope_id},
    )

    assert response.status_code == 200
    assert response.json()["suggestions"][0]["suggestions"] == ["第一轮建议"]

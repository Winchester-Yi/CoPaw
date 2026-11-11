# -*- coding: utf-8 -*-
"""验证聊天更新接口的部分字段更新能力。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.swe.app.runner.api import get_chat_manager, router
from src.swe.app.runner.models import ChatSpec


class _FakeChatManager:
    def __init__(self) -> None:
        self.chat = ChatSpec(
            id="chat-1",
            name="Existing Chat",
            session_id="console:user-1",
            user_id="user-1",
            channel="console",
            meta={"existing": "value"},
        )
        self.updated_chat: ChatSpec | None = None

    async def get_chat(self, chat_id: str) -> ChatSpec | None:
        if chat_id == self.chat.id:
            return self.chat
        return None

    async def update_chat(
        self,
        spec: ChatSpec,
        *,
        expected_chat=None,
    ) -> ChatSpec:
        self.updated_chat = spec
        self.chat = spec
        return spec

    async def create_chat(self, spec: ChatSpec) -> ChatSpec:
        self.updated_chat = spec
        return spec


def test_update_chat_allows_partial_meta_patch() -> None:
    manager = _FakeChatManager()
    app = FastAPI()
    app.include_router(router)

    async def _get_chat_manager_override() -> _FakeChatManager:
        return manager

    app.dependency_overrides[get_chat_manager] = _get_chat_manager_override
    client = TestClient(app)

    response = client.put(
        "/chats/chat-1",
        json={"meta": {"plan_mode_enabled": True}},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] == "console:user-1"
    assert payload["user_id"] == "user-1"
    assert payload["channel"] == "console"
    assert payload["meta"] == {
        "existing": "value",
        "plan_mode_enabled": True,
    }
    assert manager.updated_chat is not None
    assert manager.updated_chat.meta == payload["meta"]


def test_update_chat_rejects_server_owned_scenario_snapshot() -> None:
    manager = _FakeChatManager()
    app = FastAPI()
    app.include_router(router)

    async def _get_chat_manager_override() -> _FakeChatManager:
        return manager

    app.dependency_overrides[get_chat_manager] = _get_chat_manager_override
    response = TestClient(app).put(
        "/chats/chat-1",
        json={"meta": {"scenario_preset_snapshot": {"scenario_id": "fake"}}},
    )

    assert response.status_code == 400
    assert manager.updated_chat is None


def test_update_chat_rejects_server_owned_scenario_snapshot_source() -> None:
    manager = _FakeChatManager()
    app = FastAPI()
    app.include_router(router)

    async def _get_chat_manager_override() -> _FakeChatManager:
        return manager

    app.dependency_overrides[get_chat_manager] = _get_chat_manager_override
    response = TestClient(app).put(
        "/chats/chat-1",
        json={"meta": {"scenario_preset_snapshot_source": "chat_meta"}},
    )

    assert response.status_code == 400
    assert manager.updated_chat is None


def test_create_chat_rejects_server_owned_scenario_snapshot() -> None:
    from src.swe.app.runner.api import get_workspace

    manager = _FakeChatManager()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_workspace] = lambda: None

    async def _get_chat_manager_override() -> _FakeChatManager:
        return manager

    app.dependency_overrides[get_chat_manager] = _get_chat_manager_override
    response = TestClient(app).post(
        "/chats",
        json={
            "name": "forged",
            "session_id": "console:user-1",
            "user_id": "user-1",
            "channel": "console",
            "meta": {"scenario_preset_snapshot": {"scenario_id": "fake"}},
        },
    )

    assert response.status_code == 400
    assert manager.updated_chat is None


def test_update_chat_rejects_another_authenticated_users_chat() -> None:
    manager = _FakeChatManager()
    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def _identity(request, call_next):
        request.state.user_id = "user-2"
        return await call_next(request)

    async def _get_chat_manager_override() -> _FakeChatManager:
        return manager

    app.dependency_overrides[get_chat_manager] = _get_chat_manager_override
    response = TestClient(app).put(
        "/chats/chat-1",
        json={"name": "should not update"},
    )

    assert response.status_code == 404
    assert manager.updated_chat is None


def test_update_chat_reports_concurrent_patch_conflict() -> None:
    from src.swe.app.conversation_storage.errors import ConflictError

    class ConcurrentManager(_FakeChatManager):
        async def update_chat(self, spec, *, expected_chat=None):
            assert expected_chat.meta == {"existing": "value"}
            self.chat.meta["concurrent"] = "preserve"
            raise ConflictError("Chat changed since patch was read")

    manager = ConcurrentManager()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_chat_manager] = lambda: manager
    response = TestClient(app).put("/chats/chat-1", json={"name": "new"})
    assert response.status_code == 409
    assert manager.chat.meta["concurrent"] == "preserve"


def test_delete_passes_authorized_snapshot_and_maps_conflict():
    from src.swe.app.conversation_storage.errors import ConflictError

    manager = _FakeChatManager()

    async def delete_chats(chat_ids, *, expected_chats):
        assert chat_ids == ["chat-1"]
        assert expected_chats == {"chat-1": manager.chat}
        raise ConflictError("Chat ownership changed since authorization")

    manager.delete_chats = delete_chats
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_chat_manager] = lambda: manager
    response = TestClient(app).delete("/chats/chat-1")
    assert response.status_code == 409

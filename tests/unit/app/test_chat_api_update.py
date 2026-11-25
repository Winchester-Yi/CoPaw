# -*- coding: utf-8 -*-
"""验证聊天更新接口的部分字段更新能力。"""

from __future__ import annotations

from datetime import timedelta

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


def test_update_chat_normalizes_and_marks_manual_title() -> None:
    manager = _FakeChatManager()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_chat_manager] = lambda: manager
    client = TestClient(app)
    address = "/chats/chat-1"

    assert client.put(address, json={"name": "  "}).status_code == 422
    assert client.put(address, json={"name": "会" * 101}).status_code == 422
    response = client.put(address, json={"name": "  手动标题  "})
    assert response.status_code == 200
    assert response.json()["name"] == "手动标题"
    assert response.json()["meta"] == {
        "existing": "value",
        "session_title_edited": True,
    }

    # A full update can echo the marker but cannot clear it.
    echoed_chat = response.json()
    echoed_chat["meta"]["session_title_edited"] = False
    echoed_chat["meta"]["plan_mode_enabled"] = True
    response = client.put(address, json=echoed_chat)
    assert response.status_code == 200
    assert response.json()["meta"]["session_title_edited"] is True
    assert response.json()["meta"]["plan_mode_enabled"] is True


def test_update_chat_preserves_unchanged_legacy_title_and_ignores_echo() -> (
    None
):
    manager = _FakeChatManager()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_chat_manager] = lambda: manager
    client = TestClient(app)

    for original in ("New Chat", "  " + "旧" * 101):
        manager.chat.name = original
        echoed_chat = manager.chat.model_dump(mode="json")
        echoed_chat["meta"]["session_title_edited"] = True
        echoed_chat["status"] = "running"
        response = client.put("/chats/chat-1", json=echoed_chat)
        assert response.status_code == 200
        assert response.json()["name"] == original
        assert response.json()["status"] == "running"
        assert "session_title_edited" not in response.json()["meta"]


def test_update_chat_rejects_stale_full_update_over_new_title() -> None:
    manager = _FakeChatManager()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_chat_manager] = lambda: manager
    client = TestClient(app)
    address = "/chats/chat-1"
    old_chat = manager.chat.model_dump(mode="json")

    assert client.put(address, json={"name": "手动标题"}).status_code == 200
    manager.chat.updated_at += timedelta(seconds=1)
    old_chat["status"] = "running"
    assert client.put(address, json=old_chat).status_code == 409
    assert manager.chat.name == "手动标题"


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


def test_batch_delete_passes_authorized_snapshot_and_maps_conflict():
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
    response = TestClient(app).post(
        "/chats/batch-delete",
        json=["chat-1", "missing"],
    )
    assert response.status_code == 409

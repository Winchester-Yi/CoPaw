# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from swe.app.agent_context import FileManagerSourceScopeLocation
from swe.app.answer_turn.models import (
    StopClaim,
    TurnIdentity,
    TurnLease,
    TurnStatus,
)
from swe.app.channels.base import ContentType, TextContent
from swe.app.channels.console.channel import ConsoleChannel
from src.swe.app.file_manager import FileManagerService
from src.swe.app.routers import console as console_router


class _FakeConsoleChannel:
    def resolve_session_id(self, sender_id: str, channel_meta: dict) -> str:
        return channel_meta.get("session_id") or f"console:{sender_id}"

    async def stream_one(self, payload):
        yield payload


class _FakeChannelManager:
    async def get_channel(self, name: str):
        assert name == "console"
        return _FakeConsoleChannel()


class _FakeChatManager:
    async def get_or_create_chat(
        self,
        session_id: str,
        user_id: str,
        channel_id: str,
        name: str,
        meta=None,
    ):
        _ = meta
        return SimpleNamespace(
            id=f"chat:{session_id}",
            session_id=session_id,
            user_id=user_id,
            channel=channel_id,
            name=name,
        )


class _FakeTaskTracker:
    def __init__(self, *, status=None, is_new=True):
        self.status_value = status
        self.is_new = is_new
        self.payload = None

    async def attach_or_start(self, identity, payload, _stream_fn, **_kwargs):
        self.payload = payload
        return object(), self.is_new

    async def attach(self, _identity):
        return object()

    async def stream(self, _identity, _queue):
        await asyncio.sleep(0.03)
        yield 'data: {"done": true}\n\n'

    async def detach_subscriber(self, _identity, _queue):
        return None


class _FakeCoordinator:
    def __init__(self, tracker):
        self.tracker = tracker
        self.identity = TurnIdentity(
            chat_id="chat:session-1",
            msgid="msg-1",
            turn_id="turn-1",
        )

    async def start_or_attach(self, chat_id, payload, producer, **kwargs):
        if getattr(self.tracker, "is_new", True):
            msgid = kwargs.get("msgid") or self.identity.msgid
            self.identity = TurnIdentity(
                chat_id=chat_id,
                msgid=msgid,
                turn_id="turn-1",
            )
        queue, is_new = await self.tracker.attach_or_start(
            self.identity,
            payload,
            producer,
        )
        return TurnLease(self.identity, queue, is_new)

    async def status(self, _chat_id):
        return getattr(self.tracker, "status_value", None)

    async def attach(self, chat_id, *, msgid=None):
        if msgid is not None and msgid != self.identity.msgid:
            return None
        queue = await self.tracker.attach(self.identity)
        return TurnLease(self.identity, queue, False) if queue else None

    async def current_identity(self, _chat_id):
        return self.identity

    async def claim_stop(self, identity, *, msgid=None, internal=False):
        _ = internal
        return StopClaim(
            True,
            identity=identity,
            status=TurnStatus.STOPPING,
        )


class _TaskStartCountingTracker:
    def __init__(self) -> None:
        self.start_calls = 0

    async def attach_or_start(
        self,
        _identity,
        _payload,
        _stream_fn,
        **_kwargs,
    ):
        self.start_calls += 1
        return object(), True

    async def stream(self, _identity, _queue):
        yield 'data: {"done": true}\n\n'

    async def detach_subscriber(self, _identity, _queue):
        return None


def test_build_console_chat_meta_carries_agent_and_source() -> None:
    payload = {"meta": {"source_id": "source-a"}}
    workspace = SimpleNamespace(agent_id="agent-1")

    assert console_router._build_console_chat_meta(workspace, payload) == {
        "agent_id": "agent-1",
        "source_id": "source-a",
    }


def _with_coordinator(workspace, tracker):
    workspace.task_tracker = tracker
    workspace.answer_turn_coordinator = _FakeCoordinator(tracker)
    return workspace


def _build_authenticated_console_chat_client(
    monkeypatch,
    *,
    authenticated_user_id: str,
    authenticated_agent_id: str,
    workspace_agent_id: str,
) -> tuple[TestClient, _TaskStartCountingTracker]:
    app = FastAPI()

    @app.middleware("http")
    async def _set_authenticated_identity(request, call_next):
        request.state.user_id = authenticated_user_id
        request.state.agent_id = authenticated_agent_id
        return await call_next(request)

    app.include_router(console_router.router)
    tracker = _TaskStartCountingTracker()
    workspace = SimpleNamespace(
        agent_id=workspace_agent_id,
        channel_manager=_FakeChannelManager(),
        chat_manager=_FakeChatManager(),
        task_tracker=tracker,
        answer_turn_coordinator=_FakeCoordinator(tracker),
    )

    async def _fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        _fake_get_agent_for_request,
    )
    return TestClient(app), tracker


def _console_chat_payload(user_id: str) -> dict[str, Any]:
    return {
        "input": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ],
        "session_id": "session-1",
        "user_id": user_id,
        "channel": "console",
    }


def test_console_chat_authenticated_user_mismatch_is_rejected_before_starting_task(
    monkeypatch,
) -> None:
    client, tracker = _build_authenticated_console_chat_client(
        monkeypatch,
        authenticated_user_id="authenticated-user",
        authenticated_agent_id="agent-1",
        workspace_agent_id="agent-1",
    )

    response = client.post(
        "/console/chat",
        headers={"X-Source-Id": "src-a"},
        json=_console_chat_payload("other-user"),
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Console sender does not match authenticated user",
    }
    assert tracker.start_calls == 0


def test_console_chat_authenticated_agent_mismatch_is_rejected_before_starting_task(
    monkeypatch,
) -> None:
    client, tracker = _build_authenticated_console_chat_client(
        monkeypatch,
        authenticated_user_id="user-1",
        authenticated_agent_id="authenticated-agent",
        workspace_agent_id="workspace-agent",
    )

    response = client.post(
        "/console/chat",
        headers={"X-Source-Id": "src-a"},
        json=_console_chat_payload("user-1"),
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Console Agent does not match authenticated Agent",
    }
    assert tracker.start_calls == 0


@pytest.mark.asyncio
async def test_start_new_chat_propagates_created_chat_id_to_compaction_sse():
    """A new-chat /compact stream must carry the created chat ID to SSE."""

    class _ChatManager:
        async def get_or_create_chat(self, *_args, **_kwargs):
            return SimpleNamespace(
                id="chat-created-by-router",
                channel="console",
            )

    class _TaskTracker:
        payload = None

        async def attach_or_start(
            self,
            _identity,
            payload,
            _stream_fn,
            **_kwargs,
        ):
            self.payload = payload
            return object(), True

        async def attach(self, _identity):
            return None

        async def stream(self, _identity, _queue):
            yield 'data: {"done": true}\n\n'

    async def process(_request):
        yield SimpleNamespace(
            object="message",
            status=None,
            type="message",
            output=[],
            metadata={
                "conversation_compaction_boundary": {
                    "id": "boundary-1",
                    "archived_message_count": 1,
                },
            },
        )

    console_channel = ConsoleChannel(
        process=process,
        enabled=True,
        bot_prefix="Friday",
    )
    tracker = _TaskTracker()
    workspace = SimpleNamespace(
        agent_id="agent-1",
        chat_manager=_ChatManager(),
        answer_turn_coordinator=_FakeCoordinator(tracker),
    )
    native_payload = {
        "sender_id": "user-1",
        "channel_id": "console",
        "content_parts": [
            TextContent(type=ContentType.TEXT, text="/compact"),
        ],
        "meta": {"session_id": "session-1", "existing": "preserved"},
    }

    _queue, chat_id, _msgid = await console_router._start_new_chat(
        workspace,
        tracker,
        console_channel,
        "session-1",
        native_payload,
    )
    events = [
        event async for event in console_channel.stream_one(tracker.payload)
    ]

    assert chat_id == "chat-created-by-router"
    assert tracker.payload["meta"]["chat_id"] == chat_id
    assert tracker.payload["meta"]["existing"] == "preserved"
    assert any(f'"chat_id": "{chat_id}"' in event for event in events)


@pytest.mark.asyncio
async def test_start_new_chat_rejects_submission_while_chat_is_stopping():
    class _ChatManager:
        async def get_or_create_chat(self, *_args, **_kwargs):
            return SimpleNamespace(
                id="chat-stopping",
                channel="console",
                meta={},
            )

    class _TaskTracker:
        start_calls = 0
        status_value = "stopping"

        async def attach_or_start(self, *_args, **_kwargs):
            self.start_calls += 1
            return object(), True

        async def attach(self, _identity):
            return None

    tracker = _TaskTracker()
    workspace = SimpleNamespace(
        agent_id="agent-1",
        chat_manager=_ChatManager(),
        answer_turn_coordinator=_FakeCoordinator(tracker),
    )
    payload = {
        "sender_id": "user-1",
        "channel_id": "console",
        "content_parts": [TextContent(type=ContentType.TEXT, text="hello")],
        "meta": {"session_id": "session-1"},
    }

    with pytest.raises(console_router.HTTPException) as error:
        await console_router._start_new_chat(
            workspace,
            tracker,
            _FakeConsoleChannel(),
            "session-1",
            payload,
        )

    assert error.value.status_code == 409
    assert error.value.detail == "Chat is stopping"
    assert tracker.start_calls == 0


@pytest.mark.asyncio
async def test_start_new_chat_persists_first_submit_scenario_snapshot(
    monkeypatch,
):
    """The server-created snapshot, not the client ID, reaches the runner."""

    class _ChatManager:
        created = None

        async def get_or_create_scenario_chat(self, *_args):
            factory = _args[-1]
            chat = SimpleNamespace(
                id="00000000-0000-0000-0000-000000000001",
                channel="console",
                meta={},
            )
            self.created = chat
            chat.meta["scenario_preset_snapshot"] = await factory(chat)
            return chat, True

    class _TaskTracker:
        payload = None

        async def attach_or_start(
            self,
            _identity,
            payload,
            _stream_fn,
            **_kwargs,
        ):
            self.payload = payload
            return object(), True

        async def attach(self, _identity):
            return None

        async def stream(self, _identity, _queue):
            yield 'data: {"done": true}\n\n'

    snapshot = {
        "scenario_id": "scenario-a",
        "capability_name": "信息提取",
        "resources": [],
    }
    from src.swe.app.scenario_preset import runtime as scenario_runtime

    scenario_router = importlib.import_module(
        "src.swe.app.scenario_preset.router",
    )

    async def _initialize(**_kwargs):
        return snapshot

    monkeypatch.setattr(
        scenario_runtime,
        "initialize_scenario_snapshot",
        _initialize,
    )

    def _get_service():
        return object()

    monkeypatch.setattr(scenario_router, "get_service", _get_service)
    tracker = _TaskTracker()
    chat_manager = _ChatManager()
    workspace = SimpleNamespace(
        agent_id="agent-1",
        chat_manager=chat_manager,
        answer_turn_coordinator=_FakeCoordinator(tracker),
    )
    native_payload = {
        "sender_id": "user-1",
        "channel_id": "console",
        "content_parts": [
            TextContent(type=ContentType.TEXT, text="@信息提取"),
        ],
        "meta": {
            "session_id": "session-1",
            "source_id": "source-a",
            "scenario_preset_id": "scenario-a",
        },
    }

    await console_router._start_new_chat(
        workspace,
        tracker,
        SimpleNamespace(stream_one=None),
        "session-1",
        native_payload,
    )

    assert chat_manager.created.meta["scenario_preset_snapshot"] == snapshot
    assert tracker.payload["meta"]["scenario_preset_snapshot"] == snapshot


@pytest.mark.asyncio
async def test_start_new_chat_cleans_created_scenario_when_tracker_fails(
    monkeypatch,
):
    class _ChatManager:
        deleted: list[str] = []

        async def get_or_create_scenario_chat(self, *_args):
            factory = _args[-1]
            chat = SimpleNamespace(
                id="00000000-0000-0000-0000-000000000001",
                channel="console",
                meta={"scenario_preset_snapshot": await factory(None)},
            )
            return chat, True

        async def delete_chats(self, chat_ids):
            self.deleted.extend(chat_ids)
            return True

    async def _initialize(**_kwargs):
        return {"scenario_id": "scenario-a", "resources": []}

    scenario_runtime = importlib.import_module(
        "src.swe.app.scenario_preset.runtime",
    )
    scenario_router = importlib.import_module(
        "src.swe.app.scenario_preset.router",
    )
    monkeypatch.setattr(
        scenario_runtime,
        "initialize_scenario_snapshot",
        _initialize,
    )

    def _get_service():
        return object()

    monkeypatch.setattr(scenario_router, "get_service", _get_service)

    class _TaskTracker:
        async def attach_or_start(self, *_args):
            raise RuntimeError("tracker unavailable")

        async def attach(self, _identity):
            return None

    chat_manager = _ChatManager()
    native_payload = {
        "sender_id": "user-1",
        "channel_id": "console",
        "content_parts": [TextContent(type=ContentType.TEXT, text="@能力")],
        "meta": {
            "session_id": "session-1",
            "source_id": "source-a",
            "scenario_preset_id": "scenario-a",
        },
    }

    with pytest.raises(RuntimeError, match="tracker unavailable"):
        await console_router._start_new_chat(
            SimpleNamespace(
                agent_id="agent-1",
                chat_manager=chat_manager,
                answer_turn_coordinator=_FakeCoordinator(_TaskTracker()),
            ),
            _TaskTracker(),
            SimpleNamespace(stream_one=None),
            "session-1",
            native_payload,
        )

    assert chat_manager.deleted == ["00000000-0000-0000-0000-000000000001"]


@pytest.mark.asyncio
async def test_start_new_chat_rejects_scenario_on_existing_plain_chat():
    """A preset cannot be injected into a chat that was started without it."""

    class _ChatManager:
        async def get_or_create_scenario_chat(self, *_args):
            return (
                SimpleNamespace(
                    id="chat-existing",
                    channel="console",
                    meta={},
                ),
                False,
            )

    native_payload = {
        "sender_id": "user-1",
        "channel_id": "console",
        "content_parts": [
            TextContent(type=ContentType.TEXT, text="@信息提取"),
        ],
        "meta": {
            "session_id": "session-1",
            "source_id": "source-a",
            "scenario_preset_id": "scenario-a",
        },
    }

    from fastapi import HTTPException

    with pytest.raises(HTTPException, match="only available for a new chat"):
        await console_router._start_new_chat(
            SimpleNamespace(agent_id="agent-1", chat_manager=_ChatManager()),
            SimpleNamespace(attach_or_start=None),
            SimpleNamespace(stream_one=None),
            "session-1",
            native_payload,
        )


@pytest.mark.asyncio
async def test_start_new_chat_rejects_switching_scenario_on_locked_chat():
    class _ChatManager:
        async def get_or_create_scenario_chat(self, *_args):
            return (
                SimpleNamespace(
                    id="chat-existing",
                    channel="console",
                    meta={
                        "scenario_preset_snapshot": {
                            "scenario_id": "scenario-a",
                            "agent_id": "agent-1",
                        },
                    },
                ),
                False,
            )

    native_payload = {
        "sender_id": "user-1",
        "channel_id": "console",
        "content_parts": [
            TextContent(type=ContentType.TEXT, text="@另一个能力"),
        ],
        "meta": {
            "session_id": "session-1",
            "source_id": "source-a",
            "scenario_preset_id": "scenario-b",
        },
    }

    from fastapi import HTTPException

    with pytest.raises(HTTPException, match="locked for this chat"):
        await console_router._start_new_chat(
            SimpleNamespace(agent_id="agent-1", chat_manager=_ChatManager()),
            SimpleNamespace(attach_or_start=None),
            SimpleNamespace(stream_one=None),
            "session-1",
            native_payload,
        )


def _build_upload_client(monkeypatch, media_dir):
    app = FastAPI()
    app.include_router(console_router.router)

    class _FakeUploadChannelManager:
        async def get_channel(self, name: str):
            assert name == "console"
            return SimpleNamespace(media_dir=media_dir)

    workspace = SimpleNamespace(
        channel_manager=_FakeUploadChannelManager(),
    )

    async def _fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        _fake_get_agent_for_request,
    )
    return TestClient(app)


def _build_file_manager_client(monkeypatch, workspace_dir):
    app = FastAPI()
    app.include_router(console_router.router)
    source_scope_base = workspace_dir.parent / "source-scope"
    source_scope_base.mkdir(exist_ok=True)

    async def _fake_resolve_file_manager_workspace_dir(_request):
        return workspace_dir

    def _fake_resolve_file_manager_source_scope_location(_request):
        return FileManagerSourceScopeLocation(
            base_dir=source_scope_base,
            component="tenant-a",
        )

    async def _fail_if_runtime_requested(_request):
        raise AssertionError("File Manager must not resolve an Agent runtime")

    monkeypatch.setattr(
        console_router,
        "resolve_file_manager_workspace_dir",
        _fake_resolve_file_manager_workspace_dir,
    )
    monkeypatch.setattr(
        console_router,
        "resolve_file_manager_source_scope_location",
        _fake_resolve_file_manager_source_scope_location,
    )
    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        _fail_if_runtime_requested,
    )
    monkeypatch.setattr(
        console_router,
        "get_file_manager_service",
        lambda directory, **kwargs: FileManagerService(
            directory,
            cursor_secret=b"test-file-manager-secret",
            **kwargs,
        ),
    )
    return TestClient(app)


def test_file_manager_listing_is_bound_to_request_workspace(
    tmp_path,
    monkeypatch,
) -> None:
    (tmp_path / "visible.txt").write_text("visible", encoding="utf-8")
    (tmp_path / "sessions").mkdir()
    (tmp_path / "governance").mkdir()
    client = _build_file_manager_client(monkeypatch, tmp_path)

    response = client.get("/console/file-manager/directories?root=working")

    assert response.status_code == 200
    body = response.json()
    assert body["root"] == "working"
    assert body["path"] == ""
    assert [item["name"] for item in body["items"]] == ["visible.txt"]
    assert str(tmp_path) not in response.text


def test_file_manager_routes_bind_the_tenant_source_scope_location(
    tmp_path,
    monkeypatch,
) -> None:
    source_scope_base = tmp_path / "source-scope"
    source_scope_base.mkdir()
    observed_locations: list[tuple[Path, Path | None, str | None]] = []
    app = FastAPI()
    app.include_router(console_router.router)

    async def _fake_resolve_file_manager_workspace_dir(_request):
        return tmp_path

    def _fake_resolve_file_manager_source_scope_location(_request):
        return FileManagerSourceScopeLocation(
            base_dir=source_scope_base,
            component="tenant-a",
        )

    def _fake_get_file_manager_service(directory, **kwargs):
        observed_locations.append(
            (
                directory,
                kwargs.get("source_scope_base_dir"),
                kwargs.get("source_scope_component"),
            ),
        )
        return FileManagerService(
            directory,
            cursor_secret=b"test-file-manager-secret",
            **kwargs,
        )

    monkeypatch.setattr(
        console_router,
        "resolve_file_manager_workspace_dir",
        _fake_resolve_file_manager_workspace_dir,
    )
    monkeypatch.setattr(
        console_router,
        "resolve_file_manager_source_scope_location",
        _fake_resolve_file_manager_source_scope_location,
        raising=False,
    )
    monkeypatch.setattr(
        console_router,
        "get_file_manager_service",
        _fake_get_file_manager_service,
    )
    client = TestClient(app)

    listed = client.get(
        "/console/file-manager/directories",
        params={"root": "source_scope"},
    )
    assert listed.status_code == 200

    uploaded = client.post(
        "/console/file-manager/files/upload?root=source_scope&path=",
        files={"file": ("report.txt", b"before", "text/plain")},
    )
    assert uploaded.status_code == 200
    preview = client.get(
        "/console/file-manager/files/read",
        params={"root": "source_scope", "path": "report.txt"},
    )
    assert preview.status_code == 200
    downloaded = client.get(
        "/console/file-manager/files/download",
        params={"root": "source_scope", "path": "report.txt"},
    )
    assert downloaded.content == b"before"
    saved = client.put(
        "/console/file-manager/files/text",
        json={
            "root": "source_scope",
            "path": "report.txt",
            "content": "after",
            "revision": preview.json()["revision"],
        },
    )
    assert saved.status_code == 200

    archived = client.delete(
        "/console/file-manager/files",
        params={"root": "source_scope", "path": "report.txt"},
    )
    assert archived.status_code == 200
    archive_item_id = archived.json()["archive_item_id"]
    restored = client.post(
        f"/console/file-manager/recycle/{archive_item_id}/restore",
    )
    assert restored.status_code == 200
    archived_again = client.delete(
        "/console/file-manager/files",
        params={"root": "source_scope", "path": "report.txt"},
    )
    assert archived_again.status_code == 200
    purged = client.delete(
        f"/console/file-manager/recycle/{archived_again.json()['archive_item_id']}",
    )
    assert purged.status_code == 200

    assert observed_locations
    assert all(
        location == (tmp_path, source_scope_base, "tenant-a")
        for location in observed_locations
    )


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd"])
def test_file_manager_rejects_escaping_paths(
    tmp_path,
    monkeypatch,
    path,
) -> None:
    client = _build_file_manager_client(monkeypatch, tmp_path)

    response = client.get(
        "/console/file-manager/directories",
        params={"root": "working", "path": path},
    )

    assert response.status_code == 403


def test_file_manager_read_returns_bounded_preview_and_revision(
    tmp_path,
    monkeypatch,
) -> None:
    large_text = "x" * (1024 * 1024 + 1)
    (tmp_path / "large.txt").write_text(large_text, encoding="utf-8")
    client = _build_file_manager_client(monkeypatch, tmp_path)

    response = client.get(
        "/console/file-manager/files/read",
        params={"root": "working", "path": "large.txt"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["is_text"] is True
    assert body["is_truncated"] is True
    assert body["editable"] is False
    assert len(body["content"].encode("utf-8")) == 1024 * 1024
    assert body["revision"]


def test_file_manager_conversation_can_read_and_download_regular_file(
    tmp_path,
    monkeypatch,
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "chat.txt").write_text("conversation", encoding="utf-8")
    client = _build_file_manager_client(monkeypatch, tmp_path)

    read_response = client.get(
        "/console/file-manager/files/read",
        params={"root": "conversation", "path": "chat.txt"},
    )
    download_response = client.get(
        "/console/file-manager/files/download",
        params={"root": "conversation", "path": "chat.txt"},
    )

    assert read_response.status_code == 200
    assert read_response.json()["content"] == "conversation"
    assert read_response.json()["editable"] is False
    assert download_response.status_code == 200
    assert download_response.content == b"conversation"
    assert download_response.headers["content-disposition"] == (
        'attachment; filename="chat.txt"'
    )
    assert download_response.headers["content-length"] == str(
        len(b"conversation"),
    )


def test_file_manager_recycle_is_listable_but_not_available_for_read_or_download(
    tmp_path,
    monkeypatch,
) -> None:
    client = _build_file_manager_client(monkeypatch, tmp_path)

    assert (
        client.get(
            "/console/file-manager/directories?root=recycle",
        ).status_code
        == 200
    )
    for endpoint in (
        "/console/file-manager/files/read?root=recycle&path=file.txt",
        "/console/file-manager/files/download?root=recycle&path=file.txt",
    ):
        assert client.get(endpoint).status_code == 403


def test_file_manager_text_save_requires_current_revision_and_audits_without_content(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    (tmp_path / "note.md").write_text("before", encoding="utf-8")
    client = _build_file_manager_client(monkeypatch, tmp_path)
    preview = client.get(
        "/console/file-manager/files/read",
        params={"root": "working", "path": "note.md"},
    ).json()

    with caplog.at_level("INFO", logger="src.swe.app.routers.console"):
        saved = client.put(
            "/console/file-manager/files/text",
            headers={"X-Actor": "tester"},
            json={
                "root": "working",
                "path": "note.md",
                "content": "after",
                "revision": preview["revision"],
            },
        )

    assert saved.status_code == 200
    assert saved.json()["revision"] != preview["revision"]
    assert (tmp_path / "note.md").read_text(encoding="utf-8") == "after"
    assert all("after" not in record.getMessage() for record in caplog.records)
    audit = next(
        record
        for record in caplog.records
        if record.getMessage() == "file_manager.audit"
    )
    assert audit.actor == "tester"
    assert audit.action == "save"
    assert audit.path == "note.md"
    assert audit.outcome == "success"

    conflict = client.put(
        "/console/file-manager/files/text",
        json={
            "root": "working",
            "path": "note.md",
            "content": "lost update",
            "revision": preview["revision"],
        },
    )
    assert conflict.status_code == 409
    assert (tmp_path / "note.md").read_text(encoding="utf-8") == "after"


def test_file_manager_upload_and_delete_enforce_root_and_name_contracts(
    tmp_path,
    monkeypatch,
) -> None:
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "duplicate.txt").write_text("old", encoding="utf-8")
    (tmp_path / "folder").mkdir()
    (tmp_path / "sessions").mkdir()
    client = _build_file_manager_client(monkeypatch, tmp_path)

    uploaded = client.post(
        "/console/file-manager/files/upload?root=upload&path=",
        files={"file": ("fresh.txt", b"fresh", "text/plain")},
    )
    assert uploaded.status_code == 200
    assert (tmp_path / "media" / "fresh.txt").read_bytes() == b"fresh"
    collision = client.post(
        "/console/file-manager/files/upload?root=upload&path=",
        files={"file": ("duplicate.txt", b"new", "text/plain")},
    )
    assert collision.status_code == 409
    assert (tmp_path / "media" / "duplicate.txt").read_text(
        encoding="utf-8",
    ) == "old"
    forbidden = client.post(
        "/console/file-manager/files/upload?root=conversation&path=",
        files={"file": ("no.txt", b"no", "text/plain")},
    )
    assert forbidden.status_code == 403

    directory_delete = client.delete(
        "/console/file-manager/files",
        params={"root": "working", "path": "folder"},
    )
    assert directory_delete.status_code == 403
    deleted = client.delete(
        "/console/file-manager/files",
        params={"root": "upload", "path": "fresh.txt"},
    )
    assert deleted.status_code == 200
    assert not (tmp_path / "media" / "fresh.txt").exists()


def test_file_manager_deletes_directory_and_audits_outcome(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    (tmp_path / "reports" / "weekly").mkdir(parents=True)
    client = _build_file_manager_client(monkeypatch, tmp_path)

    with caplog.at_level("INFO", logger="src.swe.app.routers.console"):
        response = client.delete(
            "/console/file-manager/directories",
            params={"root": "working", "path": "reports"},
        )

    assert response.status_code == 204
    assert not (tmp_path / "reports").exists()
    audit = next(
        record
        for record in caplog.records
        if record.getMessage() == "file_manager.audit"
    )
    assert (audit.action, audit.path, audit.outcome) == (
        "delete_directory",
        "reports",
        "success",
    )


def test_file_manager_oversized_upload_audits_failure_without_content(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    (tmp_path / "media").mkdir()
    client = _build_file_manager_client(monkeypatch, tmp_path)

    with caplog.at_level("INFO", logger="src.swe.app.routers.console"):
        response = client.post(
            "/console/file-manager/files/upload?root=upload&path=folder",
            files={
                "file": (
                    "oversized.txt",
                    b"x" * (console_router.MAX_UPLOAD_BYTES + 1),
                    "text/plain",
                ),
            },
        )

    assert response.status_code == 400
    audit = next(
        record
        for record in caplog.records
        if record.getMessage() == "file_manager.audit"
    )
    assert audit.action == "upload"
    assert audit.path == "folder/oversized.txt"
    assert audit.outcome == "failure"
    assert "x" * 32 not in audit.getMessage()


def test_file_manager_recycle_restores_only_original_path_and_purges(
    tmp_path,
    monkeypatch,
) -> None:
    (tmp_path / "report.txt").write_text("report", encoding="utf-8")
    client = _build_file_manager_client(monkeypatch, tmp_path)
    deleted = client.delete(
        "/console/file-manager/files",
        params={"root": "working", "path": "report.txt"},
    )
    assert deleted.status_code == 200
    archive_item_id = deleted.json()["archive_item_id"]

    recycle = client.get("/console/file-manager/directories?root=recycle")
    assert recycle.status_code == 200
    item = recycle.json()["items"][0]
    assert item["original_path"] == "report.txt"
    assert item["archived_at"]
    assert "archive_path" not in recycle.text

    (tmp_path / "report.txt").write_text("collision", encoding="utf-8")
    conflict = client.post(
        f"/console/file-manager/recycle/{archive_item_id}/restore",
    )
    assert conflict.status_code == 409
    (tmp_path / "report.txt").unlink()
    restored = client.post(
        f"/console/file-manager/recycle/{archive_item_id}/restore",
    )
    assert restored.status_code == 200
    assert (tmp_path / "report.txt").read_text(encoding="utf-8") == "report"

    second = client.delete(
        "/console/file-manager/files",
        params={"root": "working", "path": "report.txt"},
    ).json()["archive_item_id"]
    purged = client.delete(f"/console/file-manager/recycle/{second}")
    assert purged.status_code == 200
    assert (
        client.get("/console/file-manager/directories?root=recycle").json()[
            "items"
        ]
        == []
    )


def test_file_manager_download_rejects_symbolic_links(
    tmp_path,
    monkeypatch,
) -> None:
    outside = tmp_path.parent / "outside-download.txt"
    outside.write_text("not downloadable", encoding="utf-8")
    (tmp_path / "outside-link.txt").symlink_to(outside)
    client = _build_file_manager_client(monkeypatch, tmp_path)

    response = client.get(
        "/console/file-manager/files/download",
        params={"root": "working", "path": "outside-link.txt"},
    )

    assert response.status_code == 403


def test_file_manager_download_response_closes_if_response_start_fails() -> (
    None
):
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    stream = console_router._FileManagerDownloadStream(read_fd, 1)
    response = console_router._FileManagerDownloadResponse(stream)

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            raise RuntimeError("response start failed")

    with pytest.raises(RuntimeError, match="response start failed"):
        asyncio.run(
            response(
                {
                    "type": "http",
                    "asgi": {"spec_version": "2.4"},
                    "method": "GET",
                    "path": "/download",
                    "headers": [],
                },
                receive,
                send,
            ),
        )

    with pytest.raises(OSError):
        os.fstat(read_fd)


def test_file_manager_download_response_closes_if_response_start_is_cancelled() -> (
    None
):
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    response = console_router._FileManagerDownloadResponse(
        console_router._FileManagerDownloadStream(read_fd, 1),
    )

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            response(
                {
                    "type": "http",
                    "asgi": {"spec_version": "2.4"},
                    "method": "GET",
                    "path": "/download",
                    "headers": [],
                },
                receive,
                send,
            ),
        )

    with pytest.raises(OSError):
        os.fstat(read_fd)


def test_file_manager_download_stream_only_sends_open_snapshot_size(
    tmp_path,
) -> None:
    file_path = tmp_path / "document.txt"
    file_path.write_bytes(b"before")
    download = FileManagerService(
        tmp_path,
        cursor_secret=b"test-file-manager-secret",
    ).open_file_for_download("working", "document.txt")
    with file_path.open("ab") as handle:
        handle.write(b"-after")

    stream = console_router._FileManagerDownloadStream(
        download.file_descriptor,
        download.size_bytes,
    )

    assert b"".join(stream) == b"before"
    with pytest.raises(OSError):
        os.fstat(download.file_descriptor)


@pytest.mark.parametrize(
    "file_name",
    ["script.py", "Example.JAVA", "app.min.js", "Program.cs"],
)
def test_console_upload_rejects_executable_code_extensions_without_writing(
    tmp_path,
    monkeypatch,
    file_name,
) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    client = _build_upload_client(monkeypatch, media_dir)

    response = client.post(
        "/console/upload",
        files={"file": (file_name, b"code", "application/octet-stream")},
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Unsupported file type for chat attachment upload",
    }
    assert list(media_dir.iterdir()) == []


@pytest.mark.parametrize(
    "file_name",
    ["archive.zip", "script.py.zip", "report.pdf"],
)
def test_console_upload_allows_archive_and_document_extensions(
    tmp_path,
    monkeypatch,
    file_name,
) -> None:
    media_dir = tmp_path / "media"
    client = _build_upload_client(monkeypatch, media_dir)

    response = client.post(
        "/console/upload",
        files={"file": (file_name, b"content", "application/octet-stream")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["file_name"] == file_name
    assert body["size"] == len(b"content")

    stored_files = list(media_dir.iterdir())
    assert len(stored_files) == 1
    assert stored_files[0].name.endswith(f"_{file_name}")
    assert stored_files[0].read_bytes() == b"content"


def test_console_upload_uses_workspace_copy_for_context_reference(
    tmp_path,
    monkeypatch,
) -> None:
    app = FastAPI()
    app.include_router(console_router.router)
    custom_media_dir = tmp_path / "custom-media"
    workspace_dir = tmp_path / "workspace"

    class _FakeUploadChannelManager:
        async def get_channel(self, name: str):
            assert name == "console"
            return SimpleNamespace(media_dir=custom_media_dir)

    workspace = SimpleNamespace(
        channel_manager=_FakeUploadChannelManager(),
        workspace_dir=workspace_dir,
    )

    async def _fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        _fake_get_agent_for_request,
    )
    client = TestClient(app)

    response = client.post(
        "/console/upload",
        files={"file": ("report.pdf", b"content", "application/pdf")},
    )

    assert response.status_code == 200
    body = response.json()
    referenced_path = Path(body["url"])
    assert referenced_path.parent == workspace_dir / "media"
    assert referenced_path.read_bytes() == b"content"
    assert (custom_media_dir / referenced_path.name).read_bytes() == b"content"

    native_payload: dict[str, Any] = {
        "content_parts": [
            {"type": "file", "file_url": str(referenced_path)},
        ],
        "meta": {
            "context_references": [
                {
                    "type": "skill",
                    "id": "skill:document_reader",
                    "name": "document_reader",
                },
            ],
        },
    }
    asyncio.run(
        console_router._append_uploaded_attachment_references(
            native_payload,
            workspace,
        ),
    )
    assert native_payload["meta"]["context_references"][-1] == {
        "type": "workspace_file",
        "id": f"workspace_file:media/{referenced_path.name}",
        "root": "media",
        "relative_path": referenced_path.name,
    }


def test_console_upload_uses_bounded_filesystem_worker(
    tmp_path,
    monkeypatch,
) -> None:
    media_dir = tmp_path / "media"
    client = _build_upload_client(monkeypatch, media_dir)
    calls = []

    async def _run_worker(function, *args, **kwargs):
        calls.append(function)
        return function(*args, **kwargs)

    monkeypatch.setattr(
        console_router,
        "run_file_manager_mutation",
        _run_worker,
    )

    response = client.post(
        "/console/upload",
        files={"file": ("report.pdf", b"content", "application/pdf")},
    )

    assert response.status_code == 200
    assert len(calls) == 1


def test_console_chat_stream_emits_keepalive_and_disables_proxy_buffering(
    monkeypatch,
) -> None:
    app = FastAPI()
    app.include_router(console_router.router)

    workspace = SimpleNamespace(
        channel_manager=_FakeChannelManager(),
        chat_manager=_FakeChatManager(),
        task_tracker=_FakeTaskTracker(),
    )
    workspace.answer_turn_coordinator = _FakeCoordinator(
        workspace.task_tracker,
    )

    async def _fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        _fake_get_agent_for_request,
    )
    monkeypatch.setattr(
        console_router,
        "_CONSOLE_SSE_HEARTBEAT_SECONDS",
        0.01,
        raising=False,
    )

    client = TestClient(app)
    payload = {
        "input": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ],
        "session_id": "session-1",
        "user_id": "user-1",
        "channel": "console",
    }

    with client.stream(
        "POST",
        "/console/chat",
        headers={"X-Source-Id": "src-a"},
        json=payload,
    ) as response:
        assert response.status_code == 200
        assert response.headers["x-accel-buffering"] == "no"

        lines = response.iter_lines()
        first_line = next(lines)
        if first_line == ": keep-alive":
            assert next(lines) == ""
        else:
            assert first_line == 'data: {"done": true}'
            return

        for line in lines:
            if not line or line == ": keep-alive":
                continue
            assert line == 'data: {"done": true}'
            break
        else:
            raise AssertionError(
                "expected streamed data event after keepalive",
            )


def test_console_chat_stream_exposes_server_turn_identity(monkeypatch) -> None:
    app = FastAPI()
    app.include_router(console_router.router)

    class _IdentityTracker(_FakeTaskTracker):
        async def attach_or_start(
            self,
            _run_key,
            _payload,
            _stream_fn,
            *,
            msgid=None,
            **_kwargs,
        ):
            return object(), True

    workspace = SimpleNamespace(
        channel_manager=_FakeChannelManager(),
        chat_manager=_FakeChatManager(),
        task_tracker=_IdentityTracker(),
    )
    workspace.answer_turn_coordinator = _FakeCoordinator(
        workspace.task_tracker,
    )

    async def _fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        _fake_get_agent_for_request,
    )

    response = TestClient(app).post(
        "/console/chat",
        headers={"X-Source-Id": "src-a"},
        json={
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "hi"}],
                },
            ],
            "session_id": "session-1",
            "user_id": "user-1",
            "channel": "console",
        },
    )

    assert response.status_code == 200
    assert response.headers["x-swe-chatid"] == "chat:session-1"
    assert response.headers["x-swe-msgid"]
    assert response.headers["x-swe-sessionid"] == "session-1"


def test_console_chat_stream_reuses_the_active_turn_identity(
    monkeypatch,
) -> None:
    app = FastAPI()
    app.include_router(console_router.router)

    class _AttachedRunTracker(_FakeTaskTracker):
        async def attach_or_start(
            self,
            _run_key,
            _payload,
            _stream_fn,
            *,
            msgid=None,
            **_kwargs,
        ):
            return object(), False

        async def get_run_identity(self, run_key: str):
            assert run_key == "chat:session-1"
            return run_key, "server-turn-1"

    workspace = SimpleNamespace(
        channel_manager=_FakeChannelManager(),
        chat_manager=_FakeChatManager(),
        task_tracker=_AttachedRunTracker(is_new=False),
    )
    workspace.answer_turn_coordinator = _FakeCoordinator(
        workspace.task_tracker,
    )
    workspace.answer_turn_coordinator.identity = TurnIdentity(
        chat_id="chat:session-1",
        msgid="server-turn-1",
        turn_id="turn-1",
    )

    async def _fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        _fake_get_agent_for_request,
    )

    response = TestClient(app).post(
        "/console/chat",
        headers={"X-Source-Id": "src-a"},
        json=_console_chat_payload("user-1"),
    )

    assert response.status_code == 200
    assert response.headers["x-swe-chatid"] == "chat:session-1"
    assert response.headers["x-swe-msgid"] == "server-turn-1"


def test_console_chat_copies_complete_b3_context_to_native_meta(
    monkeypatch,
) -> None:
    app = FastAPI()
    app.include_router(console_router.router)

    class _CapturingTaskTracker:
        def __init__(self) -> None:
            self.payload = None

        async def attach_or_start(
            self,
            _identity,
            payload,
            _stream_fn,
            **_kwargs,
        ):
            self.payload = payload
            return object(), True

        async def attach(self, _identity):
            return None

        async def stream(self, _identity, _queue):
            yield 'data: {"done": true}\n\n'

    tracker = _CapturingTaskTracker()
    workspace = SimpleNamespace(
        channel_manager=_FakeChannelManager(),
        chat_manager=_FakeChatManager(),
        task_tracker=tracker,
    )
    workspace.answer_turn_coordinator = _FakeCoordinator(tracker)

    async def _fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        _fake_get_agent_for_request,
    )

    client = TestClient(app)
    payload = {
        "input": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ],
        "session_id": "session-1",
        "user_id": "user-1",
        "channel": "console",
    }

    with client.stream(
        "POST",
        "/console/chat",
        headers={
            "X-Source-Id": "src-a",
            "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
            "X-B3-Spanid": "32befd146889a61a",
            "X-B3-Sampled": "1",
        },
        json=payload,
    ) as response:
        assert response.status_code == 200
        next(response.iter_lines())

    assert tracker.payload is not None
    assert tracker.payload["meta"]["b3_trace_id"] == (
        "8267fd70bacf497704fec30eaa353979"
    )
    assert tracker.payload["meta"]["b3_context"] == {
        "X-B3-Traceid": "8267fd70bacf497704fec30eaa353979",
        "X-B3-Spanid": "32befd146889a61a",
        "X-B3-Sampled": "1",
    }


def test_console_chat_rejects_partial_b3_context() -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/console/chat",
            "headers": [
                (b"x-source-id", b"src-a"),
                (b"x-b3-traceid", b"8267fd70bacf497704fec30eaa353979"),
            ],
        },
    )

    with pytest.raises(console_router.HTTPException) as exc_info:
        console_router._inject_request_metadata(request, {"meta": {}})

    assert exc_info.value.status_code == 400


def test_console_chat_copies_structured_context_references_to_native_meta(
    monkeypatch,
) -> None:
    app = FastAPI()
    app.include_router(console_router.router)

    class _CapturingTaskTracker:
        def __init__(self) -> None:
            self.payload = None

        async def attach_or_start(
            self,
            _identity,
            payload,
            _stream_fn,
            **_kwargs,
        ):
            self.payload = payload
            return object(), True

        async def attach(self, _identity):
            return None

        async def stream(self, _identity, _queue):
            yield 'data: {"done": true}\n\n'

    tracker = _CapturingTaskTracker()
    workspace = SimpleNamespace(
        channel_manager=_FakeChannelManager(),
        chat_manager=_FakeChatManager(),
        task_tracker=tracker,
    )
    workspace.answer_turn_coordinator = _FakeCoordinator(tracker)

    async def _fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        _fake_get_agent_for_request,
    )
    client = TestClient(app)
    references = [
        {"type": "skill", "id": "skill:writer", "name": "writer"},
        {
            "type": "workspace_file",
            "id": "workspace_file:media/report.txt",
            "root": "media",
            "relative_path": "report.txt",
        },
    ]

    with client.stream(
        "POST",
        "/console/chat",
        headers={"X-Source-Id": "src-a"},
        json={
            "input": [
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            ],
            "session_id": "session-1",
            "user_id": "user-1",
            "context_references": references,
        },
    ) as response:
        assert response.status_code == 200
        next(response.iter_lines())

    assert tracker.payload is not None
    assert tracker.payload["meta"]["context_references"] == references


def test_console_chat_adds_uploaded_attachment_as_workspace_reference(
    tmp_path,
    monkeypatch,
) -> None:
    """A selected skill must be able to read a file attached in the same turn."""

    app = FastAPI()
    app.include_router(console_router.router)

    class _CapturingTaskTracker:
        def __init__(self) -> None:
            self.payload = None

        async def attach_or_start(
            self,
            _identity,
            payload,
            _stream_fn,
            **_kwargs,
        ):
            self.payload = payload
            return object(), True

        async def attach(self, _identity):
            return None

        async def stream(self, _identity, _queue):
            yield 'data: {"done": true}\n\n'

    media_dir = tmp_path / "media"
    media_dir.mkdir()
    uploaded_file = media_dir / "stored_report.pdf"
    uploaded_file.write_bytes(b"report")
    tracker = _CapturingTaskTracker()

    class _FakeChannelManager:
        async def get_channel(self, name: str):
            assert name == "console"
            channel = _FakeConsoleChannel()
            channel.media_dir = media_dir
            return channel

    workspace = SimpleNamespace(
        channel_manager=_FakeChannelManager(),
        chat_manager=_FakeChatManager(),
        task_tracker=tracker,
        workspace_dir=tmp_path,
    )
    workspace.answer_turn_coordinator = _FakeCoordinator(tracker)

    async def _fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        _fake_get_agent_for_request,
    )
    client = TestClient(app)
    skill_reference = {
        "type": "skill",
        "id": "skill:document_reader",
        "name": "document_reader",
    }

    with client.stream(
        "POST",
        "/console/chat",
        headers={"X-Source-Id": "src-a"},
        json={
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "请分析附件"},
                        {
                            "type": "file",
                            "file_url": (
                                "http://testserver/files/preview"
                                f"{uploaded_file.as_posix()}"
                            ),
                            "file_name": "report.pdf",
                        },
                    ],
                },
            ],
            "session_id": "session-1",
            "user_id": "user-1",
            "context_references": [skill_reference],
        },
    ) as response:
        assert response.status_code == 200
        next(response.iter_lines())

    assert tracker.payload is not None
    assert tracker.payload["meta"]["context_references"] == [
        skill_reference,
        {
            "type": "workspace_file",
            "id": "workspace_file:media/stored_report.pdf",
            "root": "media",
            "relative_path": "stored_report.pdf",
        },
    ]


def test_attachment_reference_keeps_selected_skill_within_runner_limit(
    tmp_path,
) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    uploaded_file = media_dir / "stored_report.pdf"
    uploaded_file.write_bytes(b"report")

    class _FakeChannelManager:
        async def get_channel(self, name: str):
            assert name == "console"
            channel = _FakeConsoleChannel()
            channel.media_dir = media_dir
            return channel

    skill_reference = {
        "type": "skill",
        "id": "skill:document_reader",
        "name": "document_reader",
    }
    native_payload: dict[str, Any] = {
        "content_parts": [
            {
                "type": "file",
                "file_url": (
                    "http://testserver/files/preview"
                    f"{uploaded_file.as_posix()}"
                ),
            },
        ],
        "meta": {
            "context_references": [
                *[
                    {"type": "mcp_tool", "id": f"mcp:tool-{index}"}
                    for index in range(10)
                ],
                skill_reference,
                {
                    "type": "workspace_file",
                    "id": "workspace_file:media/stored_report.pdf",
                    "root": "wrong-root",
                    "relative_path": "wrong-path",
                },
            ],
        },
    }
    workspace = SimpleNamespace(
        channel_manager=_FakeChannelManager(),
        workspace_dir=tmp_path,
    )

    asyncio.run(
        console_router._append_uploaded_attachment_references(
            native_payload,
            workspace,
        ),
    )

    references = native_payload["meta"]["context_references"]
    assert len(references) == 12
    assert references[0] == skill_reference
    assert references[-1] == {
        "type": "workspace_file",
        "id": "workspace_file:media/stored_report.pdf",
        "root": "media",
        "relative_path": "stored_report.pdf",
    }


def test_generated_files_returns_chat_files_sorted_by_time(
    tmp_path,
    monkeypatch,
) -> None:
    app = FastAPI()
    app.include_router(console_router.router)

    static_dir = tmp_path / "static"
    media_dir = tmp_path / "media"
    static_dir.mkdir()
    media_dir.mkdir()
    old_file = static_dir / "old.txt"
    new_file = media_dir / "new"
    old_file.write_text("old", encoding="utf-8")
    new_file.write_text("new", encoding="utf-8")
    os.utime(old_file, (100, 100))
    os.utime(new_file, (200, 200))

    workspace = SimpleNamespace(workspace_dir=tmp_path)

    async def _fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        _fake_get_agent_for_request,
    )

    client = TestClient(app)

    desc_response = client.get("/console/generated-files?sort=desc")
    assert desc_response.status_code == 200
    desc_files = desc_response.json()["files"]
    assert [item["name"] for item in desc_files] == ["new", "old.txt"]
    assert [item["display_name"] for item in desc_files] == [
        "new",
        "old.txt",
    ]
    assert [item["source"] for item in desc_files] == [
        "uploaded",
        "generated",
    ]
    assert desc_files[0]["preview_type"] == "text"

    asc_response = client.get("/console/generated-files?sort=asc")
    assert asc_response.status_code == 200
    assert [item["name"] for item in asc_response.json()["files"]] == [
        "old.txt",
        "new",
    ]

    uploaded_response = client.get(
        "/console/generated-files?source=uploaded",
    )
    assert uploaded_response.status_code == 200
    assert uploaded_response.json()["files"] == [
        {
            **desc_files[0],
            "name": "new",
            "source": "uploaded",
            "preview_type": "text",
        },
    ]


def test_generated_files_returns_empty_when_static_dir_missing(
    tmp_path,
    monkeypatch,
) -> None:
    app = FastAPI()
    app.include_router(console_router.router)
    workspace = SimpleNamespace(workspace_dir=tmp_path)

    async def _fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        _fake_get_agent_for_request,
    )

    client = TestClient(app)
    response = client.get("/console/generated-files")

    assert response.status_code == 200
    assert response.json() == {"files": []}


def test_generated_files_uses_console_channel_media_dir(
    tmp_path,
    monkeypatch,
) -> None:
    app = FastAPI()
    app.include_router(console_router.router)

    media_dir = tmp_path / "custom-media"
    media_dir.mkdir()
    uploaded_file = media_dir / "uploaded.txt"
    uploaded_file.write_text("uploaded", encoding="utf-8")

    class _FakeChannelManager:
        async def get_channel(self, _name):
            return SimpleNamespace(media_dir=media_dir)

    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        channel_manager=_FakeChannelManager(),
    )

    async def _fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        _fake_get_agent_for_request,
    )

    client = TestClient(app)
    response = client.get("/console/generated-files?source=uploaded")

    assert response.status_code == 200
    files = response.json()["files"]
    assert len(files) == 1
    assert files[0]["name"] == "uploaded.txt"
    assert files[0]["display_name"] == "uploaded.txt"
    assert files[0]["source"] == "uploaded"


def test_generated_files_hides_uploaded_uuid_prefix(
    tmp_path,
    monkeypatch,
) -> None:
    app = FastAPI()
    app.include_router(console_router.router)

    media_dir = tmp_path / "media"
    media_dir.mkdir()
    stored_name = "5b2dc838632e4be48f1fd39a08f50bb6_report.txt"
    uploaded_file = media_dir / stored_name
    uploaded_file.write_text("uploaded", encoding="utf-8")

    workspace = SimpleNamespace(workspace_dir=tmp_path)

    async def _fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        _fake_get_agent_for_request,
    )

    client = TestClient(app)
    response = client.get("/console/generated-files?source=uploaded")

    assert response.status_code == 200
    files = response.json()["files"]
    assert len(files) == 1
    assert files[0]["name"] == stored_name
    assert files[0]["display_name"] == "report.txt"
    assert files[0]["file_url"].endswith(stored_name)

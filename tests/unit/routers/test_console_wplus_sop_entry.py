# -*- coding: utf-8 -*-
"""Console pre-Agent interception tests for W+ SOP entry."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.swe.app.routers import console as console_router
from swe.app.answer_turn.models import TurnIdentity, TurnLease
from swe.app.wplus_sop.models import (
    CommandReceipt,
    OwnershipTuple,
    SessionProjection,
    SessionState,
)
from swe.app.wplus_sop.service import WPlusSopService
from swe.app.wplus_sop.store import WPlusSopStore


class FakeConsoleChannel:
    def resolve_session_id(self, sender_id: str, channel_meta: dict) -> str:
        return channel_meta.get("session_id") or f"console:{sender_id}"

    async def stream_one(self, payload):
        yield payload


class FakeChannelManager:
    async def get_channel(self, name: str):
        assert name == "console"
        return FakeConsoleChannel()


class FakeChatManager:
    def __init__(self) -> None:
        self.chat = None

    async def release_current_expert_binding(self, _chat_id: str):
        return None

    async def get_chat(self, chat_id: str):
        return self.chat if self.chat and self.chat.id == chat_id else None

    async def get_chat_by_session(
        self,
        session_id: str,
        *,
        channel: str,
        user_id: str,
    ):
        if (
            self.chat is not None
            and self.chat.session_id == session_id
            and self.chat.channel == channel
            and self.chat.user_id == user_id
        ):
            return self.chat
        return None

    async def get_or_create_chat(
        self,
        session_id: str,
        user_id: str,
        channel_id: str,
        name: str,
        meta=None,
    ):
        if self.chat is None:
            self.chat = SimpleNamespace(
                id="chat-1",
                session_id=session_id,
                user_id=user_id,
                channel=channel_id,
                name=name,
                meta=meta or {},
            )
        return self.chat

    async def update_chat(self, chat) -> None:
        self.chat = chat


class FakeTaskTracker:
    def __init__(self) -> None:
        self.started = 0
        self.fail_start = False

    async def attach_or_start(
        self,
        _identity,
        _payload,
        _stream_fn,
        *,
        before_start=None,
    ):
        if self.fail_start:
            raise RuntimeError("task start failed")
        if before_start is not None:
            before_start()
        self.started += 1
        return object(), True

    async def attach(self, _identity):
        return None

    async def stream(self, _identity, _queue):
        yield 'data: {"done": true}\n\n'


class FakeCoordinator:
    def __init__(self, tracker: FakeTaskTracker) -> None:
        self.tracker = tracker

    async def current_identity(self, _chat_id):
        return None

    async def status(self, _chat_id):
        return None

    async def start_or_attach(self, chat_id, payload, producer, **kwargs):
        identity = TurnIdentity(
            chat_id=chat_id,
            msgid=kwargs.get("msgid") or "msg-1",
            turn_id="turn-1",
        )
        queue, is_new = await self.tracker.attach_or_start(
            identity,
            payload,
            producer,
            before_start=kwargs.get("before_start"),
        )
        return TurnLease(identity, queue, is_new)


def _build_client(
    tmp_path,
    monkeypatch,
    *,
    state_agent_id: str | None = "agent-1",
):
    app = FastAPI()
    app.include_router(console_router.router)

    @app.middleware("http")
    async def add_identity(request: Request, call_next):
        request.state.tenant_id = "tenant-1"
        request.state.source_id = "console"
        request.state.user_id = "user-1"
        if state_agent_id is not None:
            request.state.agent_id = state_agent_id
        return await call_next(request)

    tracker = FakeTaskTracker()
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        agent_id="agent-1",
        channel_manager=FakeChannelManager(),
        chat_manager=FakeChatManager(),
        task_tracker=tracker,
    )
    workspace.answer_turn_coordinator = FakeCoordinator(tracker)

    async def fake_get_agent_for_request(_request):
        return workspace

    monkeypatch.setattr(
        console_router,
        "get_agent_for_request",
        fake_get_agent_for_request,
    )
    return TestClient(app), workspace


def _proposal_from_response(response) -> dict:
    for line in response.iter_lines():
        if line.startswith("data:"):
            return json.loads(line.removeprefix("data:").strip())
    raise AssertionError("missing W+ proposal event")


def test_explicit_wplus_selection_returns_card_before_agent_or_session(
    tmp_path,
    monkeypatch,
) -> None:
    legacy_store_path = tmp_path / ".copaw" / "wplus-sop.json"
    legacy_store_path.parent.mkdir()
    legacy_store_sentinel = (
        b"legacy W+ store: deliberately invalid JSON\x00\xff"
    )
    legacy_store_path.write_bytes(legacy_store_sentinel)

    current_store_path = tmp_path / ".sop" / "wplus-sop.json"
    assert not current_store_path.exists()

    client, workspace = _build_client(tmp_path, monkeypatch)
    payload = {
        "input": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "帮我创建客户经营 SOP"}],
            },
        ],
        "session_id": "logical-1",
        "user_id": "user-1",
        "channel": "console",
        "selected_skill_names": ["wplus-sop-miner"],
    }

    with client.stream(
        "POST",
        "/console/chat",
        headers={"X-Source-Id": "console"},
        json=payload,
    ) as response:
        assert response.status_code == 200
        card = _proposal_from_response(response)

    assert card["object"] == "wplus_sop_entry_proposal"
    assert card["mode"] == "explicit"
    assert card["chat_id"] == "chat-1"
    assert workspace.task_tracker.started == 0
    assert legacy_store_path.read_bytes() == legacy_store_sentinel

    persisted_store = json.loads(
        current_store_path.read_text(encoding="utf-8"),
    )
    assert card["proposal_id"] in persisted_store["entry_proposals"]
    persisted_proposal = persisted_store["entry_proposals"][
        card["proposal_id"]
    ]
    assert persisted_proposal["proposal_id"] == card["proposal_id"]
    assert persisted_proposal["detection_mode"] == "explicit"
    assert persisted_proposal["ownership"]["chat_id"] == card["chat_id"]

    service = WPlusSopService(
        workspace=workspace,
        ownership=OwnershipTuple(
            tenant_id="tenant-1",
            source_id="console",
            user_id="user-1",
            agent_id="agent-1",
            chat_id="chat-1",
            logical_chat_session_id="logical-1",
        ),
    )
    assert service.store.list_sessions() == []
    assert service.store.get_entry_proposal(card["proposal_id"]) is not None


def test_caller_anonymous_user_scope_is_persisted_on_entry_proposal(
    tmp_path,
    monkeypatch,
) -> None:
    client, workspace = _build_client(tmp_path, monkeypatch)
    payload = {
        "input": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "创建客户经营 SOP"}],
            },
        ],
        "session_id": "logical-1",
        "user_id": "user-1",
        "channel": "console",
        "channel_meta": {"user_scope": "anon_scope_123456"},
        "selected_skill_names": ["wplus-sop-miner"],
    }

    with client.stream(
        "POST",
        "/console/chat",
        headers={"X-Source-Id": "console"},
        json=payload,
    ) as response:
        card = _proposal_from_response(response)

    service = WPlusSopService(
        workspace=workspace,
        ownership=OwnershipTuple(
            tenant_id="tenant-1",
            source_id="console",
            user_id="user-1",
            agent_id="agent-1",
            chat_id="chat-1",
            logical_chat_session_id="logical-1",
        ),
    )
    proposal = service.store.get_entry_proposal(card["proposal_id"])
    assert proposal is not None
    assert proposal.memory_user_scope == "anon_scope_123456"


def test_manual_wplus_mention_returns_card_without_skill_selection(
    tmp_path,
    monkeypatch,
) -> None:
    client, workspace = _build_client(tmp_path, monkeypatch)

    with client.stream(
        "POST",
        "/console/chat",
        headers={"X-Source-Id": "console"},
        json={
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "@wplus-sop-miner 帮我梳理客户筛选 SOP",
                        },
                    ],
                },
            ],
            "session_id": "logical-1",
            "user_id": "user-1",
            "channel": "console",
            "selected_skill_names": [],
        },
    ) as response:
        card = _proposal_from_response(response)

    assert response.status_code == 200
    assert card["object"] == "wplus_sop_entry_proposal"
    assert card["mode"] == "explicit"
    assert workspace.task_tracker.started == 0


def test_unselected_plain_sop_text_continues_as_ordinary_chat(
    tmp_path,
    monkeypatch,
) -> None:
    client, workspace = _build_client(tmp_path, monkeypatch)

    with client.stream(
        "POST",
        "/console/chat",
        headers={"X-Source-Id": "console"},
        json={
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "帮我梳理客户筛选 SOP",
                        },
                    ],
                },
            ],
            "session_id": "logical-1",
            "user_id": "user-1",
            "channel": "console",
            "selected_skill_names": [],
        },
    ) as response:
        lines = list(response.iter_lines())

    assert response.status_code == 200
    assert not any("wplus_sop_entry_proposal" in line for line in lines)
    assert workspace.task_tracker.started == 1


def test_explicit_wplus_selection_uses_resolved_workspace_agent_identity(
    tmp_path,
    monkeypatch,
) -> None:
    client, workspace = _build_client(
        tmp_path,
        monkeypatch,
        state_agent_id=None,
    )

    with client.stream(
        "POST",
        "/console/chat",
        headers={"X-Source-Id": "console"},
        json={
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Create a customer SOP"},
                    ],
                },
            ],
            "session_id": "logical-1",
            "user_id": "user-1",
            "channel": "console",
            "selected_skill_names": ["wplus-sop-miner"],
        },
    ) as response:
        assert response.status_code == 200
        card = _proposal_from_response(response)

    assert card["object"] == "wplus_sop_entry_proposal"
    assert card["mode"] == "explicit"
    assert card["chat_id"] == "chat-1"
    assert workspace.chat_manager.chat.meta["agent_id"] == "agent-1"
    assert workspace.task_tracker.started == 0


def test_explicit_state_agent_must_match_resolved_workspace_agent(
    tmp_path,
    monkeypatch,
) -> None:
    client, workspace = _build_client(
        tmp_path,
        monkeypatch,
        state_agent_id="agent-2",
    )

    response = client.post(
        "/console/chat",
        headers={"X-Source-Id": "console"},
        json={
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Create a customer SOP"},
                    ],
                },
            ],
            "session_id": "logical-1",
            "user_id": "user-1",
            "channel": "console",
            "selected_skill_names": ["wplus-sop-miner"],
        },
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Console Agent does not match authenticated Agent",
    }
    assert workspace.chat_manager.chat is None
    assert workspace.task_tracker.started == 0


def test_rejected_proposal_replays_original_request_without_reinterception(
    tmp_path,
    monkeypatch,
) -> None:
    client, workspace = _build_client(tmp_path, monkeypatch)
    text = "@wplus-sop-miner 帮我梳理客户筛选 SOP"
    initial = {
        "input": [
            {
                "role": "user",
                "content": [{"type": "text", "text": text}],
            },
        ],
        "session_id": "logical-1",
        "user_id": "user-1",
        "channel": "console",
        "selected_skill_names": [],
    }
    with client.stream(
        "POST",
        "/console/chat",
        headers={"X-Source-Id": "console"},
        json=initial,
    ) as response:
        proposal = _proposal_from_response(response)

    ownership = OwnershipTuple(
        tenant_id="tenant-1",
        source_id="console",
        user_id="user-1",
        agent_id="agent-1",
        chat_id="chat-1",
        logical_chat_session_id="logical-1",
    )
    service = WPlusSopService(workspace=workspace, ownership=ownership)
    rejected = service.reject_entry(
        proposal_id=proposal["proposal_id"],
        command_request_id="cmd-reject",
    )

    replay = {
        "input": [
            {
                "role": "user",
                "content": [{"type": "text", "text": text}],
            },
        ],
        "session_id": "logical-1",
        "user_id": "user-1",
        "channel": "console",
        "wplus_sop_suppression": {
            "proposal_id": proposal["proposal_id"],
            "token": rejected.suppression_token,
        },
    }
    with client.stream(
        "POST",
        "/console/chat",
        headers={"X-Source-Id": "console"},
        json=replay,
    ) as response:
        lines = list(response.iter_lines())

    assert response.status_code == 200
    assert 'data: {"done": true}' in lines
    assert not any("wplus_sop_entry_proposal" in line for line in lines)
    assert workspace.task_tracker.started == 1
    assert service.store.list_sessions() == []
    persisted = service.store.get_entry_proposal(proposal["proposal_id"])
    assert persisted is not None
    assert persisted.suppression_consumed_at is not None


def test_failed_replay_start_does_not_consume_suppression(
    tmp_path,
    monkeypatch,
) -> None:
    client, workspace = _build_client(tmp_path, monkeypatch)
    text = "帮我梳理客户筛选 SOP"
    initial = {
        "input": [
            {
                "role": "user",
                "content": [{"type": "text", "text": text}],
            },
        ],
        "session_id": "logical-1",
        "user_id": "user-1",
        "channel": "console",
        "selected_skill_names": ["wplus-sop-miner"],
    }
    with client.stream(
        "POST",
        "/console/chat",
        headers={"X-Source-Id": "console"},
        json=initial,
    ) as response:
        proposal = _proposal_from_response(response)

    ownership = OwnershipTuple(
        tenant_id="tenant-1",
        source_id="console",
        user_id="user-1",
        agent_id="agent-1",
        chat_id="chat-1",
        logical_chat_session_id="logical-1",
    )
    service = WPlusSopService(workspace=workspace, ownership=ownership)
    rejected = service.reject_entry(
        proposal_id=proposal["proposal_id"],
        command_request_id="cmd-reject-failed-start",
    )
    workspace.task_tracker.fail_start = True

    with pytest.raises(RuntimeError, match="task start failed"):
        client.post(
            "/console/chat",
            headers={"X-Source-Id": "console"},
            json={
                **initial,
                "selected_skill_names": [],
                "wplus_sop_suppression": {
                    "proposal_id": proposal["proposal_id"],
                    "token": rejected.suppression_token,
                },
            },
        )

    persisted = service.store.get_entry_proposal(proposal["proposal_id"])
    assert persisted is not None
    assert persisted.suppression_consumed_at is None
    assert persisted.suppression_claim_id is None


def test_authenticated_user_mismatch_cannot_bind_another_users_chat(
    tmp_path,
    monkeypatch,
) -> None:
    client, workspace = _build_client(tmp_path, monkeypatch)

    response = client.post(
        "/console/chat",
        headers={"X-Source-Id": "console"},
        json={
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "帮我创建 SOP"}],
                },
            ],
            "session_id": "victim-logical-session",
            "user_id": "victim-user",
            "channel": "console",
            "selected_skill_names": ["wplus-sop-miner"],
        },
    )

    assert response.status_code == 403
    assert workspace.chat_manager.chat is None
    assert workspace.task_tracker.started == 0


def test_paused_wplus_session_does_not_lock_ordinary_chat_input(
    tmp_path,
    monkeypatch,
) -> None:
    client, workspace = _build_client(tmp_path, monkeypatch)
    workspace.chat_manager.chat = SimpleNamespace(
        id="chat-1",
        session_id="logical-1",
        user_id="user-1",
        channel="console",
        name="Existing",
        meta={},
    )
    ownership = OwnershipTuple(
        tenant_id="tenant-1",
        source_id="console",
        user_id="user-1",
        agent_id="agent-1",
        chat_id="chat-1",
        logical_chat_session_id="logical-1",
    )
    WPlusSopStore(tmp_path / ".sop" / "wplus-sop.json").create_session(
        SessionProjection(
            sop_session_id="sop-paused",
            ownership=ownership,
            skill_snapshot_id="sha256:miner-v1",
            state=SessionState.PAUSED,
            state_version=1,
            title="Paused SOP",
            resume_state=SessionState.AWAITING_ANSWER,
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-create-paused",
            command="confirm_entry",
            sop_session_id="sop-paused",
            resulting_state_version=1,
        ),
    )

    with client.stream(
        "POST",
        "/console/chat",
        headers={"X-Source-Id": "console"},
        json={
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "普通聊天消息"}],
                },
            ],
            "session_id": "logical-1",
            "user_id": "user-1",
            "channel": "console",
        },
    ) as response:
        lines = list(response.iter_lines())

    assert response.status_code == 200
    assert 'data: {"done": true}' in lines
    assert workspace.task_tracker.started == 1


def test_paused_wplus_session_still_holds_the_single_session_slot(
    tmp_path,
    monkeypatch,
) -> None:
    client, workspace = _build_client(tmp_path, monkeypatch)
    workspace.chat_manager.chat = SimpleNamespace(
        id="chat-1",
        session_id="logical-1",
        user_id="user-1",
        channel="console",
        name="Existing",
        meta={},
    )
    ownership = OwnershipTuple(
        tenant_id="tenant-1",
        source_id="console",
        user_id="user-1",
        agent_id="agent-1",
        chat_id="chat-1",
        logical_chat_session_id="logical-1",
    )
    store = WPlusSopStore(tmp_path / ".sop" / "wplus-sop.json")
    store.create_session(
        SessionProjection(
            sop_session_id="sop-paused",
            ownership=ownership,
            skill_snapshot_id="sha256:miner-v1",
            state=SessionState.PAUSED,
            state_version=1,
            title="Paused SOP",
            resume_state=SessionState.AWAITING_ANSWER,
        ),
        command_receipt=CommandReceipt(
            command_request_id="cmd-create-paused",
            command="confirm_entry",
            sop_session_id="sop-paused",
            resulting_state_version=1,
        ),
    )

    response = client.post(
        "/console/chat",
        headers={"X-Source-Id": "console"},
        json={
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "创建新的 SOP"}],
                },
            ],
            "session_id": "logical-1",
            "user_id": "user-1",
            "channel": "console",
            "selected_skill_names": ["wplus-sop-miner"],
        },
    )

    assert response.status_code == 409
    assert store.list_sessions()[0].projection.sop_session_id == "sop-paused"
    persisted = json.loads(
        (tmp_path / ".sop" / "wplus-sop.json").read_text(encoding="utf-8"),
    )
    assert persisted["entry_proposals"] == {}
    assert workspace.task_tracker.started == 0

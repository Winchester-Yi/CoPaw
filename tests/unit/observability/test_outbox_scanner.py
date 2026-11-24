# -*- coding: utf-8 -*-
"""Committed workspace outbox scanning and revalidation."""

import json
import hashlib
from io import StringIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from agentscope.message import Msg

from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.runner.manager import ChatManager
from swe.app.runner.models import ChatSpec
from swe.app.runner.repo.json_repo import JsonChatRepository
from swe.app.runner.session import SafeJSONSession
from swe.agents.memory.conversation_archive import ConversationArchiveStore
from swe.observability import Scope, bind_context
from swe.observability.emitter import JsonlEmitter
from swe.observability.instrumentation import (
    Observability,
    bind_tool_span,
    mark_pending_message_evidence,
)
from swe.observability.outbox_scanner import WorkspaceOutboxScanner


async def _check_recovery_directory(
    scanner,
    tmp_path,
    monkeypatch,
    chat_id,
    record,
    event,
):
    directory = tmp_path / "chats.json"
    original = json.loads(directory.read_text())
    broken = json.loads(json.dumps(original))
    broken["chats"].append({"id": str(uuid4()), "session_id": "broken"})
    directory.write_text(json.dumps(broken))
    recovered = await scanner.scan_page(None, 1)
    assert recovered.records == [record]
    assert await scanner.verified_event(record) == event
    with pytest.raises(ValueError):
        await scanner.scan_offline_complete()
    broken["chats"][-1]["id"] = chat_id
    directory.write_text(json.dumps(broken))
    assert await scanner.verified_event(record) is None
    assert (await scanner.scan_page(None, 1)).records == []
    for invalid_id in ("", None):
        broken = json.loads(json.dumps(original))
        invalid = {"session_id": "broken", "user_id": "user"}
        if invalid_id is not None:
            invalid["id"] = invalid_id
        broken["chats"].append(invalid)
        directory.write_text(json.dumps(broken))
        assert (await scanner.scan_page(None, 1)).records == [record]
        with pytest.raises(ValueError, match="identity"):
            await scanner.scan_offline_complete()
    broken = json.loads(json.dumps(original))
    broken["chats"].extend(
        ChatSpec(
            id=str(uuid4()),
            session_id=f"session-{i}",
            user_id="user",
        ).model_dump(mode="json")
        for i in range(51)
    )
    broken["chats"].append(dict(broken["chats"][0]))
    directory.write_text(json.dumps(broken))
    with pytest.raises(ValueError, match="duplicate"):
        await scanner.scan_offline_complete()
    directory.write_text(json.dumps(original))
    offline = await scanner.scan_offline_complete()
    assert offline == [
        {"event": event, "content_sha256": record["content_sha256"]},
    ]
    assert "private" not in json.dumps(offline)

    async def change_directory(_record, *, strict=False):
        assert strict
        changed = json.loads(json.dumps(original))
        changed["chats"][0]["name"] = "changed"
        directory.write_text(json.dumps(changed))
        return event

    with monkeypatch.context() as patch:
        patch.setattr(scanner, "verified_event", change_directory)
        with pytest.raises(ValueError, match="changed during offline scan"):
            await scanner.scan_offline_complete()
    directory.write_text(json.dumps(original))
    return offline


async def _check_tampering(context):
    scanner, msg, save, record, event, offline = context
    msg.content[0]["output"][0]["text"] = "tampered"
    await save([msg])
    assert await scanner.verified_event(record) is None
    tampered = await scanner.scan_offline_complete()
    assert tampered[0]["event"] == event
    assert tampered[0]["content_sha256"] != offline[0]["content_sha256"]
    assert "tampered" not in json.dumps(tampered)
    msg.content[0]["output"][0]["text"] = "private"
    await save([msg])
    msg.metadata["visible_note"] = "changed metadata"
    await save([msg])
    assert await scanner.verified_event(record) is None
    metadata_changed = await scanner.scan_offline_complete()
    assert metadata_changed[0]["event"] == event
    assert (
        metadata_changed[0]["content_sha256"] != offline[0]["content_sha256"]
    )
    assert "changed metadata" not in json.dumps(metadata_changed)
    del msg.metadata["visible_note"]
    await save([msg])
    original_mac = msg.metadata["agent_observability_outbox"]["mac"]
    msg.metadata["agent_observability_outbox"]["mac"] = "bad"
    await save([msg])
    with pytest.raises(ValueError, match="invalid outbox"):
        await scanner.scan_offline_complete()
    msg.metadata["agent_observability_outbox"]["mac"] = original_mac
    await save([msg])
    token = msg.metadata.pop("agent_observability_evidence_ref")
    await save([msg])
    with pytest.raises(ValueError, match="invalid outbox"):
        await scanner.scan_offline_complete()
    msg.metadata["agent_observability_evidence_ref"] = token
    await save([msg])


async def _check_archive(context):
    from swe.observability.query import _evidence_message_payload

    scanner, msg, save, record, event, offline, tmp_path, chat_id, manager = (
        context
    )
    archive = ConversationArchiveStore(
        tmp_path / "dialog",
        cursor_secret=b"secret",
    )
    await archive.commit(chat_id, [msg])
    archived_raw = (
        (await archive.read_page(chat_id, limit=1)).messages[0].to_dict()
    )
    assert _evidence_message_payload(
        msg.to_dict(),
        "session",
    ) == _evidence_message_payload(archived_raw, "session")
    assert scanner._candidate(chat_id, "session", archived_raw) == record
    assert await scanner.verified_event(record) is None
    with pytest.raises(ValueError, match="duplicate"):
        await scanner.scan_offline_complete()
    await save([])
    assert await scanner.verified_event(record) == event
    assert await scanner.scan_offline_complete() == offline
    with pytest.raises(ValueError, match="limit"):
        await scanner.scan_offline_complete(max_entries=0)
    page = await scanner.scan_page(None, 1)
    assert page.records == [] and page.next_cursor
    archived_page = await scanner.scan_page(page.next_cursor, 1)
    assert archived_page.records == [record]
    assert archived_page.next_cursor is None
    # A Chat inserted after a cursor was issued is encountered in a later cycle.
    cursor_page = await scanner.scan_page(None, 1)
    new_chat = ChatSpec(id=str(uuid4()), session_id="new", user_id="user")
    await manager.create_chat(new_chat)
    finished = await scanner.scan_page(cursor_page.next_cursor, 1)
    assert finished.next_cursor is None
    next_cycle = await scanner.scan_page(None, 1)
    assert (
        next_cycle.next_cursor
        and json.loads(next_cycle.next_cursor)["chat_id"] == new_chat.id
    )
    await archive.delete_chat(chat_id)
    assert await scanner.verified_event(record) is None
    with pytest.raises(ValueError, match="deleted archive"):
        await scanner.scan_offline_complete()


async def _check_archive_errors(context):
    scanner, retried, monkeypatch = context
    calls = []

    async def corrupt_archive(_archive, _chat_id, before=None, limit=1):
        calls.append(before)
        if before == "stale":
            raise ValueError("Invalid conversation archive cursor")
        raise ValueError("corrupt archive")

    monkeypatch.setattr(ConversationArchiveStore, "read_page", corrupt_archive)
    state = scanner._cursor(retried.next_cursor)
    state["archive_cursor"] = "stale"
    skipped_archive = await scanner.scan_page(scanner._encode(state), 1)
    assert calls == ["stale", None]
    assert skipped_archive.next_cursor
    healthy = await scanner.scan_page(skipped_archive.next_cursor, 1)
    assert healthy.records == ["healthy"]

    calls.clear()

    async def corrupt_later_page(_archive, _chat_id, before=None, limit=1):
        calls.append(before)
        if before:
            raise ValueError("corrupt archive batch")
        raise AssertionError("must not restart from the healthy first page")

    monkeypatch.setattr(
        ConversationArchiveStore,
        "read_page",
        corrupt_later_page,
    )
    state["archive_cursor"] = "valid-cursor"
    skipped = await scanner.scan_page(scanner._encode(state), 1)
    assert calls == ["valid-cursor"] and skipped.next_cursor
    assert (await scanner.scan_page(skipped.next_cursor, 1)).records == [
        "healthy",
    ]


@pytest.mark.asyncio
async def test_scanner_rechecks_committed_root_archive_and_deletion(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "swe.observability.outbox_recovery.scope_registered",
        lambda *_: True,
    )
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_KEY_ID", "test")
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_SECRET", "s" * 32)
    monkeypatch.setenv("SWE_CONVERSATION_ARCHIVE_CURSOR_SECRET", "secret")
    chat_id = str(uuid4())
    manager = ChatManager(repo=JsonChatRepository(tmp_path / "chats.json"))
    await manager.create_chat(
        ChatSpec(
            id=chat_id,
            session_id="session",
            user_id="user",
            meta={"source_id": "source", "agent_id": "agent"},
        ),
    )
    session = SafeJSONSession(str(tmp_path / "sessions"))
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        tenant_id="tenant",
        source_id="source",
        agent_id="agent",
        chat_manager=manager,
        runner=SimpleNamespace(session=session),
    )
    scanner = WorkspaceOutboxScanner(
        workspace,
        WorkspaceScope("tenant", "source", "agent", service_unit_id="swe"),
    )
    emitter = JsonlEmitter(stream=StringIO())
    obs = Observability(emitter)
    run = obs.admit_run(
        scope=Scope("swe", "source", "tenant", "agent"),
        business_ref={"chat_id": chat_id, "session_id": "session"},
        enabled=True,
    )
    attempt = obs.start_attempt(run)
    msg = Msg(
        "system",
        [
            {
                "type": "tool_result",
                "id": "call",
                "name": "tool",
                "output": [{"type": "text", "text": "private"}],
            },
        ],
        "system",
    )
    with bind_context(attempt):
        span = obs.span("Tool", "execute", name="tool")
        with bind_tool_span(span):
            mark_pending_message_evidence(msg)
    emitter.close(timeout=2)

    async def save(messages):
        await session.save_merged_state(
            "session",
            user_id="user",
            state={
                "agent": {
                    "memory": {
                        "content": [[m.to_dict(), []] for m in messages],
                    },
                },
            },
        )

    await save([msg])
    async with session.execution("session", "user") as tx:
        tx.state["turn_states"] = {
            f"turn-{i}": {
                "status": "completed",
                "chat_id": chat_id,
                "message": {
                    "id": f"turn-{i}",
                    "role": "user",
                    "content": "question",
                },
            }
            for i in range(70)
        }
        await tx.commit_state(tx.state)
    await session.migrate_terminal_turns("session", "user", dry_run=False)
    root = await session.get_persisted_session_state_dict("session", "user")
    assert root["schema_version"] == 3 and len(root["turn_states"]) < 70
    page = await scanner.scan_page(None, 1)
    assert len(page.records) == 1
    record = page.records[0]
    from swe.observability.query import _evidence_message_payload

    public = _evidence_message_payload(msg.to_dict(), "session")
    canonical = json.dumps(
        public,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert record["content_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert "private" not in json.dumps(record)
    event = await scanner.verified_event(record)
    assert (
        event
        and event["reference_id"]
        == msg.metadata["agent_observability_evidence_ref"]
    )
    offline = await _check_recovery_directory(
        scanner,
        tmp_path,
        monkeypatch,
        chat_id,
        record,
        event,
    )
    await _check_tampering((scanner, msg, save, record, event, offline))
    await _check_archive(
        (
            scanner,
            msg,
            save,
            record,
            event,
            offline,
            tmp_path,
            chat_id,
            manager,
        ),
    )


@pytest.mark.asyncio
async def test_scanner_rejects_mismatched_workspace_scope(tmp_path):
    workspace = SimpleNamespace(
        tenant_id="wrong",
        source_id="source",
        agent_id="agent",
    )
    with pytest.raises(ValueError, match="scope"):
        WorkspaceOutboxScanner(
            workspace,
            WorkspaceScope("tenant", "source", "agent", service_unit_id="swe"),
        )


@pytest.mark.asyncio
async def test_archive_cursor_from_another_key_restarts_and_continues(
    tmp_path,
):
    chat = SimpleNamespace(
        id=str(uuid4()),
        session_id="session",
        user_id="user",
    )
    archive = ConversationArchiveStore(
        tmp_path / "dialog",
        cursor_secret=b"old-key",
    )
    await archive.commit(
        chat.id,
        [Msg("assistant", str(i), "assistant") for i in range(3)],
    )
    old_page = await archive.read_page(chat.id, limit=1)
    assert old_page.next_cursor

    class Manager:
        async def get_chat(self, _chat_id):
            return chat

    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        tenant_id="tenant",
        source_id="source",
        agent_id="agent",
        chat_manager=Manager(),
    )
    scanner = WorkspaceOutboxScanner(
        workspace,
        WorkspaceScope("tenant", "source", "agent", service_unit_id="swe"),
    )
    scanner._candidate = lambda _chat, _session, raw: raw["id"]
    state = scanner._cursor(None)
    state.update(
        chat_id=chat.id,
        phase="archive",
        archive_cursor=old_page.next_cursor,
    )
    page = await scanner.scan_page(scanner._encode(state), 1)
    assert page.records == [old_page.messages[0].id]
    assert page.next_cursor
    # A newly constructed scanner on another replica can resume the checkpoint.
    other = WorkspaceOutboxScanner(workspace, scanner.scope)
    other._candidate = scanner._candidate
    second = await other.scan_page(page.next_cursor, 1)
    assert len(second.records) == 1 and second.records != page.records
    third = await scanner.scan_page(second.next_cursor, 1)
    assert len(third.records) == 1 and third.next_cursor is None
    assert len(set(page.records + second.records + third.records)) == 3


@pytest.mark.asyncio
async def test_corrupt_chat_does_not_block_later_chats_and_is_retried(
    tmp_path,
    monkeypatch,
):
    chats = [
        SimpleNamespace(id=str(uuid4()), session_id=name, user_id="user")
        for name in ("damaged", "healthy")
    ]

    class Manager:
        async def list_chats_cursor(self, *, page_size, cursor):
            assert page_size == 1
            try:
                index = int(cursor or 0)
            except ValueError as exc:
                raise ValueError("Invalid chat pagination cursor") from exc
            return SimpleNamespace(
                items=[chats[index]],
                next_cursor="1" if index == 0 else None,
            )

        async def get_chat(self, chat_id):
            return next(chat for chat in chats if chat.id == chat_id)

    class Session:
        damaged = True

        async def get_persisted_session_state_dict(self, session_id, _user_id):
            if session_id == "damaged" and self.damaged:
                raise ValueError("corrupt session")
            return {
                "agent": {"memory": {"content": [[{"id": session_id}, []]]}},
            }

    session = Session()
    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        tenant_id="tenant",
        source_id="source",
        agent_id="agent",
        chat_manager=Manager(),
        runner=SimpleNamespace(session=session),
    )
    scanner = WorkspaceOutboxScanner(
        workspace,
        WorkspaceScope("tenant", "source", "agent", service_unit_id="swe"),
    )
    scanner._candidate = lambda _chat, _session, raw: raw["id"]
    skipped = await scanner.scan_page(None, 1)
    assert skipped.records == [] and skipped.next_cursor
    healthy = await scanner.scan_page(skipped.next_cursor, 1)
    assert healthy.records == ["healthy"]
    finished = await scanner.scan_page(healthy.next_cursor, 1)
    assert finished.next_cursor is None
    session.damaged = False
    retried = await scanner.scan_page(None, 1)
    assert retried.records == ["damaged"]
    reset_checkpoint = await scanner.scan_page("not-json", 1)
    assert reset_checkpoint.records == ["damaged"]
    stale_list = scanner._cursor(None)
    stale_list["chat_cursor"] = "not-an-index"
    reset_list = await scanner.scan_page(scanner._encode(stale_list), 1)
    assert reset_list.records == ["damaged"]

    await _check_archive_errors((scanner, retried, monkeypatch))


@pytest.mark.asyncio
async def test_mysql_recovery_skips_invalid_row_using_raw_ordering(tmp_path):
    from datetime import datetime, timezone, timedelta
    from swe.app.conversation_storage.errors import IntegrityError
    from swe.app.conversation_storage.metadata_repository import (
        MySQLMetadataRepository,
    )

    scope = WorkspaceScope("tenant", "source", "agent", service_unit_id="swe")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    bad_id, good_id = str(uuid4()), str(uuid4())
    rows = [
        {"external_chat_key": bad_id.encode(), "updated_at": now, "bad": True},
        {
            "external_chat_key": good_id.encode(),
            "updated_at": now - timedelta(seconds=1),
        },
    ]

    class Database:
        async def fetch_all(self, sql, params):
            if "c.external_chat_key=%s" in sql:
                return [
                    row
                    for row in rows
                    if row["external_chat_key"] == params[-1]
                ]
            assert "ORDER BY c.updated_at DESC,c.external_chat_key DESC" in sql
            return rows[1:] if len(params) > 5 else rows

    repo = MySQLMetadataRepository(Database(), scope)

    def spec(row):
        if row.get("bad"):
            raise IntegrityError("invalid metadata")
        return ChatSpec(id=good_id, session_id="session", user_id="user")

    repo._spec = spec
    first = await repo.list_recovery_chats_cursor(page_size=1)
    assert first.items == [] and first.next_cursor
    second = await repo.list_recovery_chats_cursor(
        page_size=1,
        cursor=first.next_cursor,
    )
    assert [chat.id for chat in second.items] == [good_id]
    assert second.next_cursor is None
    with pytest.raises(IntegrityError):
        await repo._rows(
            "",
            [],
            " ORDER BY c.updated_at DESC,c.external_chat_key DESC",
        )

    class Session:
        async def get_persisted_session_state_dict(self, _session, _user):
            return {
                "agent": {"memory": {"content": [[{"id": "evidence"}, []]]}},
            }

    workspace = SimpleNamespace(
        workspace_dir=tmp_path,
        tenant_id="tenant",
        source_id="source",
        agent_id="agent",
        chat_manager=ChatManager(repo=repo),
        runner=SimpleNamespace(session=Session()),
    )
    scanner = WorkspaceOutboxScanner(workspace, scope)
    scanner._candidate = lambda _chat, _session, raw: raw["id"]
    skipped = await scanner.scan_page(None, 1)
    assert skipped.records == [] and skipped.next_cursor
    found = await scanner.scan_page(skipped.next_cursor, 1)
    assert found.records == ["evidence"]

# -*- coding: utf-8 -*-
"""Real post-install writes and current-tree legacy rollback rehearsal."""

import json

import pytest
from agentscope.message import Msg

from swe.app.conversation_storage.backup import ConversationBackup
from swe.app.conversation_storage.bundle import new_operation_key
from swe.app.conversation_storage.chat_repository import MySQLChatRepository
from swe.app.conversation_storage.errors import StorageUnavailable
from swe.app.conversation_storage.execution import ExecutionStore
from swe.app.conversation_storage.factory import WorkspaceConversationStorage
from swe.app.conversation_storage.identity import (
    SessionIdentity,
    WorkspaceScope,
)
from swe.app.conversation_storage.migration.exporter import (
    ConversationExporter,
    OldFormatTarget,
)
from swe.app.conversation_storage.publications import PublicationStore
from swe.app.conversation_storage.shares import ShareStorage
from swe.app.runner.models import ChatSpec
from swe.app.runner.session import SafeJSONSession

CHAT_ID = "00000000-0000-4000-8000-000000000001"
SESSION_ID = "00000000-0000-4000-8000-000000000002"


async def post_install_new_data(db, bundles):
    scope = WorkspaceScope("tenant", "source", "agent")
    repo = MySQLChatRepository(db, scope, bundles)
    await repo.upsert_chat(
        ChatSpec(
            id=CHAT_ID,
            session_id=SESSION_ID,
            user_id="alice",
            meta={
                "expert_main_binding": {
                    "revision": 1,
                    "expert_id": "expert",
                },
            },
        ),
    )
    question = Msg("user", "post-install question", "user")
    question.id = "new-question"
    answer = Msg("assistant", "post-install answer", "assistant")
    answer.id = "new-answer"
    question, answer = question.to_dict(), answer.to_dict()
    state = {
        "schema_version": 2,
        "future_module": {"nested": [1, True, {"keep": "unknown"}]},
        "agent": {"memory": {"content": [[question, []]]}},
        "turn_states": {
            "new-question": {
                "status": "admitted",
                "chat_id": CHAT_ID,
                "message": question,
            },
        },
    }
    execution = ExecutionStore(db, publications=PublicationStore(db, bundles))
    admission = await execution.reserve_admission(
        SessionIdentity(scope, "alice", "console", SESSION_ID),
        CHAT_ID,
        "new-question",
        operation_key=new_operation_key(),
        runtime_turn_key="new-runtime",
    )
    await execution.publish_admission(admission, question, state)
    await execution.start(admission.authority)
    state["agent"]["memory"]["content"].append([answer, []])
    state["turn_states"]["new-question"]["status"] = "completed"
    await execution.finish(
        admission.authority,
        "completed",
        state,
        operation_key=new_operation_key(),
    )
    await repo.release_current_expert_binding(CHAT_ID)
    shares = ShareStorage(db, bundles, scope=scope)
    share = await shares.create_snapshot(
        chat_id=CHAT_ID,
        creator_id="alice",
        tenant_id="tenant",
        payload={"chat_name": "new-share", "messages": [question, answer]},
        selected_question_keys=("new-question",),
    )
    await shares.get_snapshot(share.token)
    await shares.get_snapshot("permanent")
    return scope, share, state


async def post_install_smoke_failure(db, bundles, root, monkeypatch):
    execute = db.execute

    async def no_ddl(query, params=None):
        assert query.lstrip().split()[0].upper() not in {
            "CREATE",
            "ALTER",
            "RENAME",
            "DROP",
        }, "startup/new-write rollback must preserve schema and table names"
        return await execute(query, params)

    with monkeypatch.context() as guard:
        guard.setattr(db, "execute", no_ddl)
        scope, share, state = await post_install_new_data(db, bundles)
        publication = await db.fetch_one(
            "SELECT relative_path FROM swe_conversation_publications WHERE id=%s",
            (share.publication_id,),
        )
        path = root / publication["relative_path"].decode()
        body = path.read_bytes()
        try:
            path.write_bytes(b"injected truncated smoke body")
            with pytest.raises(StorageUnavailable):
                await ShareStorage(db, bundles).get_snapshot(share.token)
        finally:
            path.write_bytes(body)
        assert (
            await db.fetch_one(
                "SELECT access_count FROM swe_chat_shares WHERE token=%s",
                (share.token,),
            )
        )["access_count"] == 1
        backup = root / "post-write-backup"
        assert (await ConversationBackup(db, bundles, scope).capture(backup))[
            "passed"
        ]
        destination = root / "post-write-export"
        result = ConversationExporter(
            backup,
            OldFormatTarget("local-unverified@sha256:" + "a" * 64, 2, 1),
        ).export(destination)
        assert result["passed"]
        assert not result["image_verified"] and not result["rollback_ready"]
        await assert_post_write_export(destination, share.token, state)


async def assert_post_write_export(destination, token, state):
    old = SafeJSONSession(str(destination / "sessions"))
    loaded = await old.get_session_state_dict(SESSION_ID, "alice", False)
    assert loaded["future_module"] == state["future_module"]
    assert loaded["agent"] == state["agent"]
    assert loaded["turn_states"]["new-question"]["status"] == "completed"
    await old.update_session_state(
        SESSION_ID,
        "continued",
        {"ok": True},
        "alice",
    )
    reloaded = await SafeJSONSession(
        str(destination / "sessions"),
    ).get_session_state_dict(SESSION_ID, "alice", False)
    assert reloaded["continued"] == {"ok": True}
    assert reloaded["agent"] == loaded["agent"]
    assert reloaded["future_module"] == loaded["future_module"]
    shares = {
        item["token"]: item
        for item in json.loads((destination / "shares.json").read_text())
    }
    assert shares["permanent"]["access_count"] == 18
    assert shares[token]["access_count"] == 1
    chats = json.loads((destination / "chats.json").read_text())["chats"]
    assert [chat["id"] for chat in chats] == [CHAT_ID]
    binding = chats[0]["meta"]["expert_main_binding"]
    assert binding["expert_id"] is None and binding["revision"] > 1


async def post_install_startup_failure(db, bundles, root, monkeypatch):
    scope = WorkspaceScope("tenant", "source", "agent")
    storage = WorkspaceConversationStorage(
        db,
        scope,
        root,
        cursor_secret=b"test-stable",
    )
    await storage.verify_schema()
    await db.execute(
        "ALTER TABLE swe_conversation_sessions DROP INDEX ix_conv_session_recovery",
    )
    execute = db.execute

    async def no_ddl(query, params=None):
        assert query.lstrip().split()[0].upper() not in {
            "CREATE",
            "ALTER",
            "RENAME",
            "DROP",
        }, "readiness failure requires preserving the installed schema"
        return await execute(query, params)

    with monkeypatch.context() as guard:
        guard.setattr(db, "execute", no_ddl)
        with pytest.raises(RuntimeError, match="schema indexes"):
            await storage.verify_schema()
        backup = root / "startup-failure-backup"
        assert (await ConversationBackup(db, bundles, scope).capture(backup))[
            "passed"
        ]
        destination = root / "startup-failure-export"
        result = ConversationExporter(
            backup,
            OldFormatTarget("local-unverified@sha256:" + "a" * 64, 2, 1),
        ).export(destination)
        assert result["passed"]
        assert not result["image_verified"] and not result["rollback_ready"]
        shares = json.loads((destination / "shares.json").read_text())
        assert len(shares) == 1
        assert shares[0]["token"] == "permanent"
        assert shares[0]["access_count"] == 17
        assert (await db.fetch_one("SELECT id FROM unrelated_business"))[
            "id"
        ] == 91

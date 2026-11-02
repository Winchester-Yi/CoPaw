# -*- coding: utf-8 -*-
"""Two services share SQL while keeping identical business identities and NAS apart."""

import asyncio
import json
from unittest.mock import Mock

from agentscope.message import Msg
import pytest

from swe.app.chat_sharing.store import ChatShareStore
from swe.app.conversation_storage.bundle import BundleBlock, new_operation_key
from swe.app.conversation_storage.errors import ConflictError, IntegrityError
from swe.app.conversation_storage.factory import WorkspaceConversationStorage
from swe.app.conversation_storage.identity import (
    SessionIdentity,
    WorkspaceScope,
)
from swe.app.conversation_storage.publications import (
    CommitPlan,
    PublicationRequest,
    _request,
)
from swe.app.conversation_storage.retention import RetentionStore
from swe.app.conversation_storage.shares import ShareInstall
from swe.app.runner.models import ChatSpec
from tests.integrated.conversation_storage.conftest import (
    SQL_ROOT,
    apply_sql_script,
)


async def services(database, root):
    await ChatShareStore(database).ensure_schema()
    for script in (
        "conversation_share_publication_extension.sql",
        "conversation_share_service_unit_finalize.sql",
    ):
        await apply_sql_script(database, SQL_ROOT / script)
    result = []
    for service in ("service-A", "service-B"):
        workspace = root / (service + "-workspace")
        nas = root / (service + "-nas")
        workspace.mkdir()
        nas.mkdir()
        storage = WorkspaceConversationStorage(
            database,
            WorkspaceScope(
                "tenant",
                "source",
                "agent",
                service_unit_id=service,
            ),
            workspace,
            nas_root=nas,
            cursor_secret=b"same-cursor-secret",
        )
        await storage.verify_schema()
        await storage.chats.upsert_chat(
            ChatSpec(
                id="chat",
                session_id="session",
                user_id="alice",
                name=service,
            ),
        )
        result.append(storage)
    return result


async def save_body(storage, text):
    message = Msg("user", text, "user", timestamp="2026-11-02T00:00:00Z")
    message.id = "same-message"
    return await storage.session_for("console").save_merged_state(
        "session",
        "alice",
        {
            "agent": {"memory": {"content": [[message.to_dict(), []]]}},
            "future_module": {"owner": storage.scope.service_unit_id},
        },
    )


async def test_service_recovery_history_and_chat_mutation_are_isolated(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    a, b = await services(conversation_test_database, tmp_path)
    expected = {}
    for storage in (a, b):
        expected[storage.scope.service_unit_id] = await save_body(
            storage,
            "body-" + storage.scope.service_unit_id,
        )
    foreign_read = Mock(side_effect=AssertionError("foreign NAS was read"))
    with monkeypatch.context() as patch:
        patch.setattr(b.bundles, "inspect", foreign_read)
        patch.setattr(b.bundles, "read_extent", foreign_read)
        state = await a.session_for("console").get_session_state_dict(
            "session",
            "alice",
        )
        assert state == expected["service-A"]
        history = await a.history.current("chat")
        assert len(history.messages) == 1
        assert "body-service-A" in json.dumps(
            history.model_dump(),
            default=str,
        )
        assert "body-service-B" not in json.dumps(
            history.model_dump(),
            default=str,
        )
    foreign_read.assert_not_called()
    history = await b.history.current("chat")
    assert "body-service-B" in json.dumps(history.model_dump(), default=str)
    await a.chats.upsert_chat(
        ChatSpec(
            id="chat",
            session_id="session",
            user_id="alice",
            name="changed-A",
        ),
    )
    assert (await a.chats.get_chat("chat")).name == "changed-A"
    assert (await b.chats.get_chat("chat")).name == "service-B"
    await a.chats.delete_chats(["chat"])
    assert await a.chats.get_chat("chat") is None
    assert (await b.chats.get_chat("chat")).name == "service-B"
    assert (
        await b.session_for("console").get_session_state_dict(
            "session",
            "alice",
        )
        == expected["service-B"]
    )


async def test_service_admissions_can_use_identical_question_runtime_and_operation(
    conversation_test_database,
    tmp_path,
):
    a, b = await services(conversation_test_database, tmp_path)
    operation = new_operation_key()

    async def reserve(storage):
        return await storage.executions.reserve_admission(
            SessionIdentity(storage.scope, "alice", "console", "session"),
            "chat",
            "same-question",
            operation_key=operation,
            runtime_turn_key="same-runtime",
        )

    first, second = await asyncio.gather(reserve(a), reserve(b))
    assert first.created and second.created
    assert first.authority.session_id != second.authority.session_id
    assert first.authority.turn_id != second.authority.turn_id
    with pytest.raises(ConflictError):
        await a.executions.reserve_admission(
            SessionIdentity(a.scope, "alice", "console", "session"),
            "chat",
            "another-question",
            operation_key=new_operation_key(),
            runtime_turn_key="another-runtime",
        )


async def publish_share(storage, operation):
    service = storage.scope.service_unit_id
    request = PublicationRequest(
        operation,
        WorkspaceScope("tenant", "", "", service_unit_id=service),
        "share_import",
        "same-owner",
        0,
        scope_kind="legacy_share",
    )
    await storage.publications.reserve(
        request,
        CommitPlan(
            share=ShareInstall(
                "token-" + service,
                "chat",
                "alice",
                "tenant",
                "legacy-key",
                "2026-11-02T00:00:00Z",
            ),
        ),
    )
    await storage.publications.write_bundle(
        operation,
        [
            BundleBlock(
                "share",
                "share",
                json.dumps({"chat_name": service, "messages": []}).encode(),
            ),
        ],
    )
    await storage.publications.commit(operation)


async def test_foreign_publication_pin_and_token_fail_before_body_or_count_write(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    db = conversation_test_database
    a, b = await services(db, tmp_path)
    operation = new_operation_key()
    await publish_share(a, operation)
    await publish_share(b, operation)
    assert a.bundles.path_for(operation) == b.bundles.path_for(operation)
    assert (a.bundles.root / a.bundles.path_for(operation)).read_bytes() != (
        b.bundles.root / b.bundles.path_for(operation)
    ).read_bytes()
    for storage in (a, b):
        assert (
            await storage.shares.verify_snapshot(
                "token-" + storage.scope.service_unit_id,
            )
        )["chat_name"] == storage.scope.service_unit_id
    foreign = await b.publications.get(operation)
    with pytest.raises(IntegrityError):
        await a.publications.abort(_request(foreign), owner_stopped=True)
    with pytest.raises(IntegrityError):
        await RetentionStore(db, a.scope).pin([foreign["id"]], kind="reader")
    reader_count = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM swe_conversation_retentions WHERE kind='reader'",
    )
    before = await b.shares.store.get("token-service-B")
    nas_read = Mock(side_effect=AssertionError("foreign token reached NAS"))
    with monkeypatch.context() as patch:
        patch.setattr(a.bundles, "inspect", nas_read)
        patch.setattr(a.bundles, "read_extent", nas_read)
        with pytest.raises(KeyError):
            await a.shares.get_snapshot("token-service-B")
    nas_read.assert_not_called()
    assert (
        await b.shares.store.get("token-service-B")
    ).access_count == before.access_count
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_retentions WHERE kind='reader'",
        )
        == reader_count
    )

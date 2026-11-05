# -*- coding: utf-8 -*-
"""SQL Chat contracts independent of a live database."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import asyncio
import json

import pytest

from swe.app.conversation_storage.bundle import BundleStore
from swe.app.conversation_storage.chat_repository import MySQLChatRepository
from swe.app.conversation_storage.errors import IntegrityError, UnknownCommit
from swe.app.conversation_storage.errors import ConflictError
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.runner.manager import ChatManager
from swe.app.runner.models import ChatSpec
from unittest.mock import AsyncMock, Mock


def chat_row(key=b"chat", **changes):
    row = {
        "id": 1,
        "session_id": 1,
        "external_chat_key": key,
        "registry_order": 1,
        "service_unit_key": b"schema-test-service",
        "tenant_key": b"t",
        "source_key": b"s",
        "agent_key": b"a",
        "user_key": b"",
        "channel_key": b"console",
        "external_session_key": b"session",
        "title": "title",
        "metadata_text": "{}",
        "metadata_revision": 0,
        "expert_binding_revision": 0,
        "expert_id": None,
        "content_meta_publication_id": None,
        "content_meta_offset": None,
        "content_meta_length": None,
        "created_at": datetime(2026, 10, 30),
        "updated_at": datetime(2026, 10, 30),
        "status": "idle",
        "deleted_at": None,
    }
    return {**row, **changes}


async def test_draft_final_read_rejects_concurrent_deletion(tmp_path):
    repo = MySQLChatRepository(
        ReadDatabase([]),
        WorkspaceScope("t", "s", "a", service_unit_id="schema-test-service"),
        BundleStore(tmp_path),
    )
    repo._session_for = AsyncMock(return_value={"id": 1})
    repo._freeze_draft_choice = AsyncMock(return_value="deleted-chat")
    repo.get_chat = AsyncMock(return_value=None)
    spec = ChatSpec(
        id="request",
        session_id="session",
        user_id="",
        channel="console",
    )
    with pytest.raises(ConflictError, match="deleted"):
        await repo.create_chat_if_absent_by_session(spec)


class ReadDatabase:
    max_allowed_packet = 1024 * 1024

    @asynccontextmanager
    async def acquire(self):
        yield self

    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetch_all(self, query, params=None):
        self.queries.append((query, params))
        if "service_unit_key=%s AND id IN (" in query:
            return [row for row in self.rows if row["id"] in params[1:]]
        return self.rows

    async def fetch_one(self, query, params=None):
        self.queries.append((query, params))
        if "COUNT(*)" in query:
            return {"total": len(self.rows)}
        return self.rows[0] if self.rows else None


@pytest.mark.parametrize(
    "method,args,kwargs",
    [
        ("compare_and_set_expert_binding", ("chat", 0, "expert"), {}),
        ("release_current_expert_binding", ("chat",), {}),
        ("update_title", ("chat", "new"), {"expected_revision": 0}),
        ("delete_chats", (["chat"],), {}),
        ("_freeze_draft_choice", (1,), {}),
        (
            "_session_for",
            (
                ChatSpec(
                    id="chat",
                    session_id="session",
                    user_id="",
                    channel="console",
                ),
            ),
            {},
        ),
    ],
)
async def test_direct_chat_writes_check_backup_fence_before_session_locks(
    tmp_path,
    method,
    args,
    kwargs,
):
    db = ReadDatabase([chat_row()])
    repo = repository(db, tmp_path)
    repo.retentions.assert_writes_allowed = AsyncMock(
        side_effect=ConflictError("backup fence"),
    )
    with pytest.raises(ConflictError, match="backup fence"):
        await getattr(repo, method)(*args, **kwargs)
    assert not db.queries


def repository(db, tmp_path):
    return MySQLChatRepository(
        db,
        WorkspaceScope("t", "s", "a", service_unit_id="schema-test-service"),
        BundleStore(tmp_path),
    )


async def test_get_uses_exact_trusted_scope_and_preserves_empty_user(tmp_path):
    db = ReadDatabase([chat_row()])
    repo = repository(db, tmp_path)
    chat = await repo.get_chat("chat")
    assert chat.user_id == ""
    assert chat.created_at.tzinfo == timezone.utc
    query, params = db.queries[0]
    assert params == (b"schema-test-service", b"t", b"s", b"a", b"chat")
    assert "deleted_at IS NULL" in query
    assert "LIMIT 1" in query


async def test_get_by_session_uses_all_identity_parts_and_registry_order(
    tmp_path,
):
    db = ReadDatabase([chat_row()])
    await repository(db, tmp_path).get_chat_by_id("session", "", "console")
    query, params = db.queries[0]
    assert params == (
        b"schema-test-service",
        b"t",
        b"s",
        b"a",
        b"",
        b"console",
        b"session",
    )
    assert "ORDER BY c.registry_order ASC" in query


async def test_filter_pushes_empty_user_and_channel_into_sql(tmp_path):
    db = ReadDatabase([chat_row()])
    assert (
        len(
            await repository(db, tmp_path).filter_chats(
                user_id="",
                channel="console",
            ),
        )
        == 1
    )
    query, params = db.queries[0]
    assert "c.user_key=%s" in query and "c.channel_key=%s" in query
    assert params == (
        b"schema-test-service",
        b"t",
        b"s",
        b"a",
        b"",
        b"console",
    )


@pytest.mark.parametrize(
    "version,column",
    [("v1", "created_at"), ("v2", "updated_at")],
)
async def test_cursor_uses_sql_boundary_and_keeps_legacy_version(
    tmp_path,
    version,
    column,
):
    db = ReadDatabase([chat_row(b"a"), chat_row(b"b", id=2)])
    repo = repository(db, tmp_path)
    cursor = repo._encode_cursor(
        (datetime(2026, 10, 31, tzinfo=timezone.utc), "z"),
        version=version,
    )
    page = await repo.paginate_chats_cursor(page_size=1, cursor=cursor)
    query, params = next((q, p) for q, p in db.queries if "COUNT(*)" not in q)
    assert f"c.{column}<%s" in query
    assert f"ORDER BY c.{column} DESC,c.external_chat_key DESC" in query
    assert params[-1] == 2
    assert (
        page.has_more and repo._decode_cursor(page.next_cursor)[0] == version
    )


async def test_typed_binding_overrides_untrusted_meta_projection(tmp_path):
    db = ReadDatabase(
        [chat_row(expert_id=b"expert", expert_binding_revision=3)],
    )
    chat = await repository(db, tmp_path).get_chat("chat")
    assert chat.meta["expert_main_binding"] == {
        "expert_id": "expert",
        "revision": 3,
    }


async def test_sql_metadata_cannot_hide_unknown_content(tmp_path):
    db = ReadDatabase(
        [chat_row(metadata_text=json.dumps({"prompt": "private body"}))],
    )
    with pytest.raises(IntegrityError, match="metadata"):
        await repository(db, tmp_path).get_chat("chat")


async def test_whole_registry_save_is_not_supported(tmp_path):
    repo = repository(ReadDatabase([]), tmp_path)
    with pytest.raises(NotImplementedError, match="registry"):
        await repo.save(None)


async def test_manager_sql_deletion_does_not_remove_recovery_resources(
    tmp_path,
):
    repo = AsyncMock()
    repo.deferred_resource_cleanup = True
    repo.delete_chats.return_value = True
    archive = AsyncMock()
    manager = ChatManager(
        repo=repo,
        archive_store=archive,
        resource_root=tmp_path,
    )
    assert await manager.delete_chats(["chat"])
    repo.list_chats.assert_not_called()
    archive.delete_chat.assert_not_called()


async def test_manager_rename_does_not_upsert_deleted_chat(tmp_path):
    repo = AsyncMock()
    repo.get_chat.return_value = ChatSpec(
        id="chat",
        session_id="session",
        user_id="",
    )
    repo.update_chat_if_exists.return_value = None
    manager = ChatManager(repo=repo)
    assert await manager.update_chat_name("chat", "new title") is False
    repo.upsert_chat.assert_not_called()


async def test_session_status_projection_joins_only_current_chat_turn(
    tmp_path,
):
    db = ReadDatabase([chat_row()])
    await repository(db, tmp_path).get_chat("chat")
    query, _ = db.queries[0]
    assert "LEFT JOIN swe_conversation_turns" in query
    assert "t.chat_id=c.id" in query


async def test_page_count_uses_same_session_association_as_items(tmp_path):
    db = ReadDatabase([chat_row()])
    await repository(db, tmp_path).paginate_chats(page=1, page_size=1)
    query, _ = next((q, p) for q, p in db.queries if "COUNT(*)" in q)
    assert "JOIN swe_conversation_sessions" in query


async def test_scenario_unknown_commit_does_not_delete_recovery_resources(
    tmp_path,
):
    repo = AsyncMock()
    repo.deferred_resource_cleanup = True
    repo.get_chat_by_id.return_value = None
    repo.create_chat_if_absent_by_session.side_effect = UnknownCommit("0" * 64)
    manager = ChatManager(repo=repo, resource_root=tmp_path)
    manager._delete_session_resources = Mock()
    with pytest.raises(UnknownCommit):
        await manager.get_or_create_scenario_chat(
            "session",
            "alice",
            "console",
            "scenario",
            {},
            AsyncMock(return_value={}),
        )
    manager._delete_session_resources.assert_not_called()


async def test_existing_prepared_chat_operation_is_reported_before_new_reserve(
    tmp_path,
):
    db = ReadDatabase([])
    repo = repository(db, tmp_path)
    pending = {"operation_key": b"0" * 64, "result_metadata_text": "pending"}
    db.fetch_all = AsyncMock(return_value=[pending])
    with pytest.raises(UnknownCommit) as failure:
        await repo._ensure_no_pending_metadata(1)
    assert failure.value.operation_key == "0" * 64


async def test_full_array_batches_more_than_maximum_publication_pins(tmp_path):
    rows = [
        chat_row(str(i).encode(), id=i, content_meta_publication_id=i)
        for i in range(1, 4100)
    ]
    db = ReadDatabase(rows)
    repo = repository(db, tmp_path)
    repo.retentions.pin = AsyncMock()
    repo.retentions.finish_reader = AsyncMock()
    repo._hydrate = AsyncMock(return_value=[])
    repo._revalidate = AsyncMock()
    await repo.list_chats()
    calls = repo.retentions.pin.await_args_list
    assert len(calls) == 2
    assert len(calls[0].args[0]) == 4096 and len(calls[1].args[0]) == 3


async def test_all_reader_batches_finish_when_one_cleanup_is_cancelled(
    tmp_path,
):
    repo = repository(ReadDatabase([]), tmp_path)
    repo.retentions.finish_reader = AsyncMock(
        side_effect=[asyncio.CancelledError(), None],
    )
    with pytest.raises(asyncio.CancelledError):
        await repo._finish_batches(["first", "second"])
    assert repo.retentions.finish_reader.await_count == 2

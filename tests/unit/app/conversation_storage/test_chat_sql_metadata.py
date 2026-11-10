# -*- coding: utf-8 -*-
"""Metadata-only writes use the existing SQL installation transaction."""

from contextlib import asynccontextmanager
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.errors import ConflictError
from swe.app.conversation_storage.retention import RetentionStore
from swe.app.runner.models import ChatSpec
from tests.unit.app.conversation_storage.test_chat_repository import (
    chat_row,
    repository,
)


class MetadataDatabase:
    max_allowed_packet = 1024 * 1024

    def __init__(self, chat=None):
        self.chat = chat
        self.session = {
            "id": 1,
            "service_unit_key": b"schema-test-service",
            "tenant_key": b"t",
            "source_key": b"s",
            "agent_key": b"a",
            "user_key": b"",
            "channel_key": b"console",
            "external_session_key": b"session",
            "generation": 0,
            "active_turn_id": None,
            "draft_chat_id": None,
        }
        self.queries = []
        self.in_transaction = False

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        assert not self.in_transaction
        self.in_transaction = True
        before = deepcopy((self.chat, self.session))
        try:
            yield self
        except BaseException:
            self.chat, self.session = before
            raise
        finally:
            self.in_transaction = False

    async def fetch_one(self, query, params=None):
        assert query.count("%s") == len(params or ())
        self.queries.append((query, params))
        if "MAX(registry_order)" in query:
            return {"high": 0}
        if "FROM swe_conversation_chats" in query:
            return deepcopy(self.chat)
        return deepcopy(self.session)

    async def fetch_all(self, query, params=None):
        assert query.count("%s") == len(params or ())
        self.queries.append((query, params))
        if "swe_conversation_publications" in query:
            return []
        return [deepcopy(self.chat)] if self.chat else []

    async def insert(self, query, params=None):
        assert self.in_transaction
        assert query.count("%s") == len(params or ())
        self.queries.append((query, params))
        columns = query.split("(", 1)[1].split(")", 1)[0].split(",")
        self.chat = chat_row(**dict(zip(columns, params)))
        return self.chat["id"]

    async def execute(self, query, params=None):
        assert self.in_transaction
        assert query.count("%s") == len(params or ())
        self.queries.append((query, params))
        if "SET draft_chat_id=%s" in query:
            self.session["draft_chat_id"] = params[0]
        elif "metadata_revision=metadata_revision+1" in query:
            assignments = query.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
            index = 0
            for assignment in assignments.split(","):
                name, expression = assignment.split("=", 1)
                if expression == "%s":
                    self.chat[name] = params[index]
                    index += 1
                elif name == "metadata_revision":
                    self.chat[name] += 1
                elif name == "status":
                    self.chat[name] = expression.strip("'")
        return 1


@pytest.fixture(autouse=True)
def sql_guard(monkeypatch):
    monkeypatch.setattr(RetentionStore, "lock_guard", AsyncMock())


def sql_repository(db, tmp_path):
    repo = repository(db, tmp_path)
    repo.retentions.assert_writes_allowed = AsyncMock()
    repo.retentions.lock_guard = AsyncMock()
    repo._session_for = AsyncMock(return_value=db.session)
    repo.publications.reserve = AsyncMock(
        side_effect=AssertionError("metadata must not publish to NAS"),
    )
    repo.bundles = object()
    return repo


def spec(**changes):
    return ChatSpec(
        **{
            "id": "chat",
            "session_id": "session",
            "user_id": "",
            "channel": "console",
            "name": "new title",
            **changes,
        },
    )


async def test_new_light_chat_never_reserves_or_writes_a_bundle(tmp_path):
    db = MetadataDatabase()
    repo = sql_repository(db, tmp_path)
    await repo.upsert_chat(
        spec(meta={"plan_mode_enabled": True}, status="running"),
    )
    assert db.chat["metadata_text"] == '{"plan_mode_enabled":true}'
    assert db.chat["content_meta_publication_id"] is None
    assert db.chat["status"] == "idle"
    assert db.chat["external_session_key"] == b"session"


async def test_light_update_does_not_hydrate_and_preserves_execution_status(
    tmp_path,
):
    db = MetadataDatabase(chat_row(status="stopping"))
    repo = sql_repository(db, tmp_path)
    await repo.update_chat_if_exists(spec(meta={"goal_mode_enabled": True}))
    assert db.chat["metadata_revision"] == 1
    assert db.chat["metadata_text"] == '{"goal_mode_enabled":true}'
    assert db.chat["status"] == "stopping"


async def test_replacing_content_with_light_metadata_only_clears_sql_locator(
    tmp_path,
):
    db = MetadataDatabase(
        chat_row(
            content_meta_publication_id=7,
            content_meta_offset=100,
            content_meta_length=30,
        ),
    )
    repo = sql_repository(db, tmp_path)
    result = await repo.update_chat_if_exists(
        spec(meta={"session_kind": "task"}),
    )
    assert result.meta == {"session_kind": "task"}
    assert db.chat["content_meta_publication_id"] is None
    assert db.chat["content_meta_offset"] is None
    assert db.chat["content_meta_length"] is None


async def test_sql_metadata_cas_failure_rolls_back_without_publication(
    tmp_path,
):
    db = MetadataDatabase(chat_row(metadata_revision=2))
    repo = sql_repository(db, tmp_path)
    repo._raw_chat = AsyncMock(return_value=chat_row(metadata_revision=1))
    with pytest.raises(ConflictError, match="revision"):
        await repo.update_chat_if_exists(
            spec(meta={"goal_mode_enabled": True}),
        )
    assert db.chat["metadata_revision"] == 2
    assert db.chat["metadata_text"] == "{}"


async def test_sql_metadata_obeys_backup_fence_before_install(tmp_path):
    db = MetadataDatabase(chat_row())
    repo = sql_repository(db, tmp_path)
    repo.retentions.assert_writes_allowed.side_effect = ConflictError(
        "backup fence",
    )
    with pytest.raises(ConflictError, match="backup fence"):
        await repo.update_chat_if_exists(
            spec(meta={"goal_mode_enabled": True}),
        )
    assert not any(q.startswith("UPDATE") for q, _ in db.queries)


async def test_sql_draft_retries_reuse_committed_identity_without_nas(
    tmp_path,
):
    db = MetadataDatabase()
    repo = sql_repository(db, tmp_path)
    first, created = await repo.create_chat_if_absent_by_session(spec())
    assert created and first.id == "chat"
    assert db.session["draft_chat_id"] == db.chat["id"]
    second, created = await repo.create_chat_if_absent_by_session(
        spec(id="other"),
    )
    assert not created and second.id == first.id


async def test_sql_metadata_cannot_override_server_owned_binding(tmp_path):
    db = MetadataDatabase(
        chat_row(expert_id=b"expert", expert_binding_revision=2),
    )
    repo = sql_repository(db, tmp_path)
    result = await repo.update_chat_if_exists(
        spec(
            meta={
                "expert_main_binding": {
                    "expert_id": "attacker",
                    "revision": 99,
                },
            },
        ),
    )
    assert result.meta["expert_main_binding"] == {
        "expert_id": "expert",
        "revision": 2,
    }
    result = await repo.update_chat_if_exists(
        spec(meta={"goal_mode_enabled": True}),
    )
    assert result.meta["expert_main_binding"] == {
        "expert_id": None,
        "revision": 3,
    }


async def test_sql_metadata_failed_commit_restores_all_fields(tmp_path):
    db = MetadataDatabase(chat_row())
    repo = sql_repository(db, tmp_path)
    original = db.transaction

    @asynccontextmanager
    async def failed_commit():
        async with original():
            yield db
            raise RuntimeError("commit failed")

    db.transaction = failed_commit
    with pytest.raises(RuntimeError, match="commit failed"):
        await repo.update_chat_if_exists(
            spec(meta={"goal_mode_enabled": True}),
        )
    assert db.chat["metadata_revision"] == 0
    assert db.chat["title"] == "title"
    assert db.chat["metadata_text"] == "{}"


async def test_sql_metadata_checks_pending_publication_after_locks(tmp_path):
    from swe.app.conversation_storage.errors import UnknownCommit

    db = MetadataDatabase(chat_row())
    repo = sql_repository(db, tmp_path)
    db.fetch_all = AsyncMock(return_value=[{"operation_key": b"a" * 64}])
    with pytest.raises(UnknownCommit):
        await repo.update_chat_if_exists(
            spec(meta={"goal_mode_enabled": True}),
        )
    assert not any(q.startswith("UPDATE") for q, _ in db.queries)
    assert any("FOR UPDATE" in q for q, _ in db.queries)


async def test_sql_rebind_copies_target_session_and_resets_display_status(
    tmp_path,
):
    db = MetadataDatabase(chat_row(status="running"))
    source = deepcopy(db.session)
    source["id"] = 2
    source["external_session_key"] = b"target"
    repo = sql_repository(db, tmp_path)
    repo._session_for.return_value = source
    fetch_one = db.fetch_one

    async def target_row(query, params=None):
        if (
            "FROM swe_conversation_sessions WHERE id=%s" in query
            and params[0] == 2
        ):
            return deepcopy(source)
        return await fetch_one(query, params)

    db.fetch_one = target_row
    result = await repo.update_chat_if_exists(spec(session_id="target"))
    assert result.session_id == "target"
    assert db.chat["session_id"] == 2
    assert db.chat["external_session_key"] == b"target"
    assert db.chat["status"] == "idle"


@pytest.mark.parametrize("side", ["source", "target"])
async def test_sql_rebind_rejects_busy_session_without_updates(tmp_path, side):
    db = MetadataDatabase(chat_row())
    target = {**db.session, "id": 2, "external_session_key": b"target"}
    (db.session if side == "source" else target)["active_turn_id"] = 9
    repo = sql_repository(db, tmp_path)
    repo._session_for.return_value = target
    fetch_one = db.fetch_one

    async def target_row(query, params=None):
        if (
            "FROM swe_conversation_sessions WHERE id=%s" in query
            and params[0] == 2
        ):
            return deepcopy(target)
        return await fetch_one(query, params)

    db.fetch_one = target_row
    with pytest.raises(ConflictError, match="busy"):
        await repo.update_chat_if_exists(spec(session_id="target"))
    assert db.chat["session_id"] == 1
    assert not any(q.startswith("UPDATE") for q, _ in db.queries)

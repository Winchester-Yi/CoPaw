# -*- coding: utf-8 -*-
"""Real SQL Chat behavior in generated disposable schemas."""

import asyncio

import pytest

from swe.app.conversation_storage.bundle import BundleStore
from swe.app.conversation_storage.chat_repository import MySQLChatRepository
from swe.app.conversation_storage.errors import ConflictError, UnknownCommit
from swe.app.conversation_storage.publications import PublicationStore
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.runner.expert_binding import ExpertBindingConflict
from swe.app.runner.models import ChatSpec
from tests.integrated.conversation_storage.test_schema import (
    insert_chat,
    insert_session,
)
from tests.integrated.conversation_storage.test_publications_mysql import (
    LostCommitDatabase,
)


def repository(db, tmp_path, source="source"):
    return MySQLChatRepository(
        db,
        WorkspaceScope("tenant", source, "agent"),
        BundleStore(tmp_path),
    )


async def test_registry_selection_scope_empty_user_and_sql_pagination(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session = await insert_session(db, user_key=b"")
    await insert_chat(db, session, b"z", user_key=b"", registry_order=2)
    await insert_chat(db, session, b"a", user_key=b"", registry_order=1)
    other = await insert_session(
        db,
        user_key=b"alice",
        external_session_key=b"other",
    )
    await insert_chat(db, other, b"other", registry_order=3)
    repo = repository(db, tmp_path)
    assert (await repo.get_chat_by_id("session", "", "console")).id == "a"
    assert [c.id for c in await repo.filter_chats(user_id="")] == ["a", "z"]
    assert await repository(db, tmp_path, "other").list_chats() == []
    page = await repo.paginate_chats(page=1, page_size=1, user_id="")
    assert (
        [c.id for c in page.items] == ["z"]
        and page.total == 2
        and page.has_more
    )
    first = await repo.paginate_chats_cursor(page_size=1, user_id="")
    second = await repo.paginate_chats_cursor(
        page_size=1,
        user_id="",
        cursor=first.next_cursor,
    )
    assert [c.id for c in second.items] == ["a"] and not second.has_more


async def test_binding_cas_has_one_winner_and_keeps_metadata_history_revisions(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session = await insert_session(db)
    chat = await insert_chat(db, session)
    repo = repository(db, tmp_path)
    results = await asyncio.gather(
        repo.compare_and_set_expert_binding("chat", 0, "one"),
        repo.compare_and_set_expert_binding("chat", 0, "two"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, ExpertBindingConflict) for r in results) == 1
    row = await db.fetch_one(
        "SELECT * FROM swe_conversation_chats WHERE id=%s",
        (chat,),
    )
    assert row["expert_binding_revision"] == 1
    assert row["metadata_revision"] == row["history_revision"] == 0
    released = await repo.release_current_expert_binding("chat")
    assert released.revision == 2 and released.expert_id is None


async def test_busy_session_blocks_binding_and_delete_keeps_session(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session = await insert_session(db, active_turn_id=999)
    await insert_chat(db, session)
    repo = repository(db, tmp_path)
    with pytest.raises(ConflictError, match="busy"):
        await repo.compare_and_set_expert_binding("chat", 0, "expert")
    assert await repo.delete_chats(["chat"])
    assert await repo.get_chat("chat") is None
    assert await db.fetch_one(
        "SELECT id FROM swe_conversation_sessions WHERE id=%s",
        (session,),
    )
    with pytest.raises(KeyError):
        await repo.release_current_expert_binding("chat")


async def test_title_update_is_conditional_and_never_resurrects(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session = await insert_session(db)
    await insert_chat(db, session)
    repo = repository(db, tmp_path)
    assert await repo.update_title("chat", "renamed", expected_revision=0)
    with pytest.raises(ConflictError):
        await repo.update_title("chat", "stale", expected_revision=0)
    await repo.delete_chats(["chat"])
    assert not await repo.update_title(
        "chat",
        "resurrect",
        expected_revision=1,
    )
    assert await repo.get_chat("chat") is None


async def test_create_update_full_meta_nas_and_typed_binding(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    repo = repository(db, tmp_path)
    original = ChatSpec(
        id="new-chat",
        session_id="full_session_with_underscores",
        user_id="",
        channel="console",
        meta={
            "plan_mode_enabled": False,
            "scenario_prompt": "private prompt",
            "unknown": {"nested": [1, 2]},
            "expert_main_binding": {"expert_id": "expert", "revision": 1},
        },
    )
    await repo.upsert_chat(original)
    loaded = await repo.get_chat("new-chat")
    assert loaded.meta == original.meta
    row = await db.fetch_one(
        "SELECT * FROM swe_conversation_chats WHERE external_chat_key=%s",
        (b"new-chat",),
    )
    assert (
        "private prompt" not in row["metadata_text"]
        and "unknown" not in row["metadata_text"]
    )
    assert row["content_meta_publication_id"] is not None
    assert row["epoch"] == 1
    loaded.name = "changed"
    loaded.meta["unknown"] = {"retained": True}
    loaded.meta["expert_main_binding"] = {
        "expert_id": "attacker",
        "revision": 999,
    }
    updated = await repo.update_chat_if_exists(loaded)
    assert updated.name == "changed" and updated.meta["unknown"] == {
        "retained": True,
    }
    assert updated.meta["expert_main_binding"] == {
        "expert_id": "expert",
        "revision": 1,
    }
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_publications",
        )
    )["n"] == 2
    title_only = updated.model_copy(update={"name": "title only"})
    assert (await repo.update_chat_if_exists(title_only)).name == "title only"
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_publications",
        )
    )["n"] == 2


async def test_draft_retry_keeps_identity_and_deleted_draft_cannot_recreate(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    repo = repository(db, tmp_path)
    spec = ChatSpec(
        id="draft-one",
        session_id="draft-session",
        user_id="alice",
        channel="console",
        meta={"expert_main_binding": {"expert_id": "expert", "revision": 1}},
    )
    first, inserted = await repo.create_chat_if_absent_by_session(spec)
    assert inserted and first.id == "draft-one"
    second, inserted = await repo.create_chat_if_absent_by_session(
        spec.model_copy(update={"id": "different-id"}),
    )
    assert not inserted and second.id == first.id
    await repo.delete_chats([first.id])
    with pytest.raises(ConflictError, match="draft"):
        await repo.create_chat_if_absent_by_session(
            spec.model_copy(update={"id": "third-id"}),
        )
    with pytest.raises(ConflictError):
        await repo.upsert_chat(spec)


async def test_atomic_rebind_and_busy_target_rejection(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    repo = repository(db, tmp_path)
    spec = ChatSpec(
        id="move-chat",
        session_id="source-session",
        user_id="alice",
        channel="console",
        meta={"private": "before"},
    )
    await repo.upsert_chat(spec)
    moved = (await repo.get_chat("move-chat")).model_copy(
        update={
            "session_id": "target-session",
            "user_id": "bob",
            "meta": {"private": "after"},
        },
    )
    result = await repo.update_chat_if_exists(moved)
    assert (
        result.session_id == "target-session"
        and result.user_id == "bob"
        and result.meta == {"private": "after"}
    )
    await insert_session(
        db,
        user_key=b"busy",
        external_session_key=b"busy-target",
        active_turn_id=999,
    )
    rejected = result.model_copy(
        update={
            "session_id": "busy-target",
            "user_id": "busy",
            "meta": {"private": "must-not-install"},
        },
    )
    with pytest.raises(ConflictError, match="busy"):
        await repo.update_chat_if_exists(rejected)
    unchanged = await repo.get_chat("move-chat")
    assert (
        unchanged.session_id == "target-session"
        and unchanged.user_id == "bob"
        and unchanged.meta == {"private": "after"}
    )


async def test_delete_does_not_capture_chat_created_after_candidate_read(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    session = await insert_session(db)
    selected = asyncio.Event()
    resume = asyncio.Event()

    class PausedDatabase:
        def __getattr__(self, name):
            return getattr(db, name)

        async def fetch_all(self, query, params=None):
            rows = await db.fetch_all(query, params)
            if query.startswith("SELECT id,session_id FROM"):
                selected.set()
                await resume.wait()
            return rows

    repo = repository(PausedDatabase(), tmp_path)
    deletion = asyncio.create_task(repo.delete_chats(["late-chat"]))
    try:
        await asyncio.wait_for(selected.wait(), 5)
        await insert_chat(db, session, b"late-chat")
        resume.set()
        assert not await deletion
        assert await repository(db, tmp_path).get_chat("late-chat") is not None
    finally:
        resume.set()
        await asyncio.gather(deletion, return_exceptions=True)


async def test_metadata_update_coexists_with_active_turn_and_releases_binding(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    repo = repository(db, tmp_path)
    spec = ChatSpec(
        id="active-chat",
        session_id="active-session",
        user_id="alice",
        channel="console",
        meta={"expert_main_binding": {"revision": 1, "expert_id": "expert"}},
    )
    await repo.upsert_chat(spec)
    chat = await db.fetch_one("SELECT * FROM swe_conversation_chats")
    await db.execute(
        "UPDATE swe_conversation_sessions SET active_turn_id=999,"
        "state_revision=3 WHERE id=%s",
        (chat["session_id"],),
    )
    updated = (await repo.get_chat("active-chat")).model_copy(
        update={
            "name": "active rename",
            "meta": {"plan_mode_enabled": True, "private": "state"},
        },
    )
    result = await repo.update_chat_if_exists(updated)
    assert result.name == "active rename" and result.meta["private"] == "state"
    assert result.meta["expert_main_binding"] == {
        "expert_id": None,
        "revision": 2,
    }
    session = await db.fetch_one(
        "SELECT * FROM swe_conversation_sessions WHERE id=%s",
        (chat["session_id"],),
    )
    assert session["active_turn_id"] == 999 and session["state_revision"] == 3


async def test_lost_chat_creation_commit_retries_without_duplicate(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    repo = repository(db, tmp_path)

    class LostFinalCommit(LostCommitDatabase):
        async def execute(self, query, params=None):
            result = await self.database.execute(query, params)
            if "SET state='committed'" in query:
                self.lose_next_commit = True
            return result

    fault = LostFinalCommit(db)
    repo.publications = PublicationStore(fault, repo.bundles)
    spec = ChatSpec(
        id="unknown-create",
        session_id="unknown-session",
        user_id="alice",
        channel="console",
        meta={"private": "kept"},
    )
    with pytest.raises(UnknownCommit) as failure:
        await repo.upsert_chat(spec)
    original = failure.value.operation_key
    assert (await repo.publications.get(original))["state"] == "committed"
    repo.publications = PublicationStore(db, repo.bundles)
    await repo.upsert_chat(spec)
    assert (await repo.get_chat(spec.id)).meta == spec.meta
    assert (
        await db.fetch_one("SELECT COUNT(*) AS n FROM swe_conversation_chats")
    )["n"] == 1
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_publications",
        )
    )["n"] == 1


async def test_draft_commit_uses_ordinary_chat_created_after_preflight(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    repo = repository(db, tmp_path)
    before_commit = asyncio.Event()
    proceed = asyncio.Event()

    class PausedPublication(PublicationStore):
        async def commit(self, operation_key):
            before_commit.set()
            await proceed.wait()
            return await super().commit(operation_key)

    repo.publications = PausedPublication(db, repo.bundles)
    draft = ChatSpec(
        id="draft-loser",
        session_id="shared",
        user_id="alice",
        channel="console",
    )
    task = asyncio.create_task(repo.create_chat_if_absent_by_session(draft))
    try:
        await asyncio.wait_for(before_commit.wait(), 10)
        session = await db.fetch_one(
            "SELECT id FROM swe_conversation_sessions "
            "WHERE external_session_key=%s",
            (b"shared",),
        )
        winner = await insert_chat(db, session["id"], b"ordinary-winner")
        proceed.set()
        result, created = await task
        assert not created and result.id == "ordinary-winner"
        assert (
            await db.fetch_one(
                "SELECT COUNT(*) AS n FROM swe_conversation_chats",
            )
        )["n"] == 1
        assert (
            await db.fetch_one(
                "SELECT draft_chat_id FROM swe_conversation_sessions "
                "WHERE id=%s",
                (session["id"],),
            )
        )["draft_chat_id"] == winner
    finally:
        proceed.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_existing_draft_choice_survives_deletion_as_tombstone(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    repo = repository(db, tmp_path)
    session = await insert_session(db)
    original = await insert_chat(
        db,
        session,
        b"existing-choice",
        registry_order=1,
    )
    spec = ChatSpec(
        id="new-draft",
        session_id="session",
        user_id="alice",
        channel="console",
    )
    found, created = await repo.create_chat_if_absent_by_session(spec)
    assert not created and found.id == "existing-choice"
    assert (
        await db.fetch_one(
            "SELECT draft_chat_id FROM swe_conversation_sessions WHERE id=%s",
            (session,),
        )
    )["draft_chat_id"] == original
    await repo.delete_chats(["existing-choice"])
    with pytest.raises(ConflictError, match="draft"):
        await repo.create_chat_if_absent_by_session(spec)


async def test_rebound_draft_mapping_rejects_original_identity_retry(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    repo = repository(db, tmp_path)
    spec = ChatSpec(
        id="mapped-draft",
        session_id="source",
        user_id="alice",
        channel="console",
    )
    original, _ = await repo.create_chat_if_absent_by_session(spec)
    moved = original.model_copy(
        update={"session_id": "target", "user_id": "bob"},
    )
    assert (await repo.update_chat_if_exists(moved)).user_id == "bob"
    with pytest.raises(ConflictError, match="draft"):
        await repo.create_chat_if_absent_by_session(
            spec.model_copy(update={"id": "new-id"}),
        )
    assert (
        await db.fetch_one("SELECT COUNT(*) AS n FROM swe_conversation_chats")
    )["n"] == 1


async def test_conflicting_metadata_abort_does_not_fence_active_execution(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    repo = repository(db, tmp_path)
    spec = ChatSpec(
        id="cas-chat",
        session_id="cas-session",
        user_id="alice",
        channel="console",
        meta={"private": "old"},
    )
    await repo.upsert_chat(spec)
    row = await db.fetch_one("SELECT * FROM swe_conversation_chats")
    await db.execute(
        "UPDATE swe_conversation_sessions SET generation=7,"
        "owner_key=%s,active_turn_id=999 WHERE id=%s",
        (b"executing-owner", row["session_id"]),
    )

    class CompetingMetadata(PublicationStore):
        async def commit(self, operation_key):
            await db.execute(
                "UPDATE swe_conversation_chats SET "
                "metadata_revision=metadata_revision+1 WHERE id=%s",
                (row["id"],),
            )
            return await super().commit(operation_key)

    repo.publications = CompetingMetadata(db, repo.bundles)
    changed = (await repo.get_chat(spec.id)).model_copy(
        update={"meta": {"private": "new"}},
    )
    with pytest.raises(ConflictError, match="revision"):
        await repo.update_chat_if_exists(changed)
    session = await db.fetch_one(
        "SELECT * FROM swe_conversation_sessions WHERE id=%s",
        (row["session_id"],),
    )
    assert (
        session["generation"] == 7
        and session["owner_key"] == b"executing-owner"
        and session["active_turn_id"] == 999
    )
    states = await db.fetch_all(
        "SELECT state FROM swe_conversation_publications ORDER BY id",
    )
    assert [r["state"] for r in states] == ["committed", "aborted"]

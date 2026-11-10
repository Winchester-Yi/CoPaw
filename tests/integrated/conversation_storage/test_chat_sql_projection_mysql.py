# -*- coding: utf-8 -*-
"""Projection matches authority and metadata-only mutations require no NAS."""

from swe.app.conversation_storage.bundle import new_operation_key
from swe.app.conversation_storage.chat_repository import (
    MySQLChatRepository,
    SELECT_CHAT,
    SCOPE_WHERE,
)
from swe.app.conversation_storage.migration.preflight import DMLDatabase
from swe.app.runner.models import ChatSpec
from tests.integrated.conversation_storage.test_execution_mysql import (
    identity,
    seed,
    store,
)


async def test_metadata_mutations_and_draft_retry_with_nas_disabled(
    conversation_test_database,
):
    db = conversation_test_database
    repo = MySQLChatRepository(db, identity().scope, object())
    spec = ChatSpec(
        id="metadata",
        session_id="session",
        user_id="alice",
        channel="console",
        status="running",
        meta={"plan_mode_enabled": False},
    )
    result, created = await repo.create_chat_if_absent_by_session(spec)
    assert created and result.status == "idle"
    again, created = await repo.create_chat_if_absent_by_session(
        spec.model_copy(update={"id": "retry"}),
    )
    assert not created and again.id == result.id
    spec.meta["goal_mode_enabled"] = True
    updated = await repo.update_chat_if_exists(spec)
    assert updated.meta == spec.meta
    assert updated.status == "idle"
    page = await repo.list_summaries(user_id="alice", session_id="session")
    assert [item.id for item in page.items] == ["metadata"]
    assert page.items[0].display_metadata == spec.meta
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_publications",
        )
    )["n"] == 0
    # The CLI DML facade must retain a leased session's packet budget.
    cli_repo = MySQLChatRepository(DMLDatabase(db), identity().scope, object())
    assert (await cli_repo.get_chat(spec.id)).meta == spec.meta


async def test_projection_matches_case_through_execution_and_preserves_recency(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    await seed(db)
    service = store(db, tmp_path, "owner")
    repo = MySQLChatRepository(
        db,
        identity().scope,
        service.publications.bundles,
    )

    async def compare(expected):
        rows = await db.fetch_all(
            SELECT_CHAT + SCOPE_WHERE + " ORDER BY c.external_chat_key",
            identity().scope.sql_values,
        )
        assert {
            row["external_chat_key"].decode(): row["status"] for row in rows
        } == expected
        assert all(row["status"] == row["projected_status"] for row in rows)
        summaries = await repo.list_summaries(user_id="alice")
        assert {item.id: item.status for item in summaries.items} == expected
        return rows[0]["updated_at"]

    before = await compare({"one": "idle", "two": "idle"})
    admission = await service.reserve_admission(
        identity(),
        "one",
        "question",
        operation_key=new_operation_key(),
        runtime_turn_key="runtime",
    )
    assert await compare({"one": "running", "two": "idle"}) == before
    await service.publish_admission(
        admission,
        {"id": "question", "role": "user", "content": "hello"},
        {},
    )
    admitted_at = await compare({"one": "running", "two": "idle"})
    await service.start(admission.authority)
    assert await compare({"one": "running", "two": "idle"}) == admitted_at
    await service.request_stop(identity(), "question")
    assert await compare({"one": "stopping", "two": "idle"}) == admitted_at
    await service.finish(
        admission.authority,
        "stopped",
        {},
        operation_key=new_operation_key(),
    )
    assert await compare({"one": "idle", "two": "idle"}) >= admitted_at

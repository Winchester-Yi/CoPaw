# -*- coding: utf-8 -*-
"""Opt-in SQL shape, lease and FOR SHARE checks in a disposable test schema."""

import asyncio
import os

import pytest

from swe.app.conversation_storage.errors import IntegrityError
from swe.app.conversation_storage.projection_references import (
    ProjectionReferences,
)
from swe.app.conversation_storage.reference_queries import (
    MESSAGE_LOCATOR_FIELDS,
    ReferenceQuery,
    fetch_reference_rows,
)
from swe.database.prepared import PreparedInsert
from tests.integrated.conversation_storage.test_schema import (
    insert_chat,
    insert_publication,
    insert_row,
    insert_session,
)

pytestmark = pytest.mark.skipif(
    not (
        os.environ.get("SWE_TEST_DB_HOST")
        and os.environ.get("SWE_TEST_DB_NAME")
    ),
    reason="isolated MySQL not configured; B1a acceptance not executed",
)
SERVICE = b"schema-test-service"


async def seed_references(db, count):
    session = await insert_session(db)
    chat = await insert_chat(db, session)
    publication = await insert_publication(
        db,
        session_id=session,
        result_metadata_text="x" * (1024 * 1024),
    )
    turn = await insert_row(
        db,
        "swe_conversation_turns",
        {
            "session_id": session,
            "chat_id": chat,
            "question_msg_key": b"q",
            "turn_order": 1,
            "context_epoch": 0,
            "generation": 0,
            "state": "completed",
            "history_status": "completed",
            "terminal_publication_id": publication,
        },
    )
    rows = [
        {
            "id": i,
            "service_unit_key": SERVICE,
            "session_id": session,
            "origin_chat_id": chat,
            "turn_id": turn,
            "source_msg_key": b"q",
            "source_occurrence_no": i,
            "role_key": b"user",
            "kind_key": b"message",
            "task_run_key": None,
            "publication_id": publication,
            "byte_offset": i,
            "byte_length": 1,
            "context_epoch": 0,
            "message_timestamp": None,
            "session_order": i,
            "layer": "memory",
            "current_visible": 1,
            "in_context": 1,
            "deleted_at": None,
        }
        for i in range(1, count + 1)
    ]
    template = PreparedInsert(
        "INSERT INTO swe_conversation_messages ("
        + ",".join(MESSAGE_LOCATOR_FIELDS)
        + ") VALUES ",
        "(" + ",".join(["%s"] * len(MESSAGE_LOCATOR_FIELDS)) + ")",
    )
    async with db.transaction():
        await db.insert_prepared(
            template,
            [
                tuple(row[key] for key in MESSAGE_LOCATOR_FIELDS)
                for row in rows
            ],
        )
    return rows, publication, turn


async def test_real_reference_templates_and_transaction_lease(
    conversation_test_database,
    monkeypatch,
    record_property,
):
    db = conversation_test_database
    rows, publication, turn = await seed_references(db, 257)
    async with db.transaction() as session:
        original = session.fetch_all
        calls = []

        async def capture(query, params):
            calls.append((query, params))
            return await original(query, params)

        monkeypatch.setattr(session, "fetch_all", capture)
        expected_queries = 0
        for kind in ReferenceQuery:
            table = kind.value[0]
            if table == "publications":
                identities = [publication]
            elif table == "turns":
                identities = [turn]
            elif table == "chats":
                identities = [rows[0]["origin_chat_id"]]
            else:
                identities = list(range(1, 258))
            expected_queries += (len(identities) + 255) // 256
            result = await fetch_reference_rows(db, kind, identities, SERVICE)
            assert set(result) == set(identities)
            assert all(
                "result_metadata_text" not in row
                and "execution_identity_text" not in row
                for row in result.values()
            )
        assert len(calls) == expected_queries
        assert (
            await fetch_reference_rows(
                db,
                ReferenceQuery.MESSAGE_LOCATOR,
                [1],
                b"foreign",
            )
            == {}
        )
        assert (
            await fetch_reference_rows(
                db,
                ReferenceQuery.MESSAGE_LOCATOR,
                [999],
                SERVICE,
            )
            == {}
        )
        query, params = calls[0]
        record_property(
            "reference_explain",
            await original("EXPLAIN " + query, params),
        )
        assert (
            await fetch_reference_rows(
                db,
                ReferenceQuery.MESSAGE_LOCATOR,
                [1],
                SERVICE,
            )
        )[1] == rows[0]


async def test_batched_current_locator_locks_block_writer_until_commit(
    conversation_test_database,
):
    db = conversation_test_database
    rows, _, _ = await seed_references(db, 257)
    release_writer = asyncio.Event()
    started = asyncio.Event()

    async def write():
        await release_writer.wait()
        async with db.transaction():
            started.set()
            await db.execute(
                "UPDATE swe_conversation_messages SET in_context=0 WHERE id=257",
            )

    # Spawn before the reader transaction so no task-owned lease is inherited.
    task = asyncio.create_task(write())
    try:
        async with db.transaction():
            snapshot = ProjectionReferences(
                rows={row["id"]: row for row in rows},
            )
            await snapshot.revalidate(db)
            release_writer.set()
            await asyncio.wait_for(started.wait(), timeout=5)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
        await asyncio.wait_for(task, timeout=5)
        async with db.transaction():
            with pytest.raises(IntegrityError, match="locator changed"):
                await snapshot.revalidate(db)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

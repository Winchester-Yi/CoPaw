# -*- coding: utf-8 -*-
"""SQL contract only; runtime service isolation requires separate implementation."""

import pytest
from mysql.connector.errors import IntegrityError, ProgrammingError

from tests.integrated.conversation_storage.conftest import (
    SQL_ROOT,
    TABLES,
    apply_sql_script,
)
from tests.integrated.conversation_storage.test_schema import (
    NOW,
    insert_chat,
    insert_publication,
    insert_row,
    insert_session,
    legacy_share,
)

SERVICE_A = b"LQ13.03@service_a_prd"
SERVICE_B = b"LQ13.03@service_b_prd"
FINALIZE = SQL_ROOT / "conversation_share_service_unit_finalize.sql"


async def test_service_unit_separates_session_chat_and_publication_keys(
    conversation_test_database,
):
    db = conversation_test_database
    for service in (SERVICE_A, SERVICE_B):
        session = await insert_session(db, service_unit_key=service)
        await insert_chat(db, session, service_unit_key=service)
        await insert_publication(db, service_unit_key=service)
        with pytest.raises(IntegrityError):
            await insert_session(db, service_unit_key=service)
        with pytest.raises(IntegrityError):
            await insert_chat(db, session, service_unit_key=service)
        with pytest.raises(IntegrityError):
            await insert_publication(
                db,
                service_unit_key=service,
                relative_path=b"another-path",
            )
        with pytest.raises(IntegrityError):
            await insert_publication(
                db,
                operation=b"another-operation",
                service_unit_key=service,
                relative_path=b"operation.bundle",
            )
    for table in (
        "swe_conversation_sessions",
        "swe_conversation_chats",
        "swe_conversation_publications",
    ):
        assert (await db.fetch_one(f"SELECT COUNT(*) AS n FROM {table}"))[
            "n"
        ] == 2


async def test_service_unit_separates_runtime_turn_and_retention_keys(
    conversation_test_database,
):
    db = conversation_test_database
    for service in (SERVICE_A, SERVICE_B):
        session = await insert_session(db, service_unit_key=service)
        chat = await insert_chat(db, session, service_unit_key=service)
        turn = {
            "service_unit_key": service,
            "chat_id": chat,
            "session_id": session,
            "question_msg_key": b"question",
            "runtime_turn_key": b"same-runtime-turn",
            "turn_order": 1,
            "context_epoch": 0,
            "generation": 1,
            "state": "reserved",
        }
        await insert_row(db, "swe_conversation_turns", turn)
        with pytest.raises(IntegrityError):
            await insert_row(
                db,
                "swe_conversation_turns",
                {**turn, "question_msg_key": b"another", "turn_order": 2},
            )
        guard = {
            "service_unit_key": service,
            "retention_key": b"same-retention-key",
            "member_key": 0,
            "kind": "gc_guard",
            "tenant_key": b"tenant",
            "source_key": b"source",
            "agent_key": b"agent",
            "guard_marker": 1,
            "state": "active",
            "created_at": NOW,
        }
        await insert_row(db, "swe_conversation_retentions", guard)
        with pytest.raises(IntegrityError):
            await insert_row(db, "swe_conversation_retentions", guard)
        with pytest.raises(IntegrityError):
            await insert_row(
                db,
                "swe_conversation_retentions",
                {**guard, "retention_key": b"another-guard"},
            )


async def test_six_tables_require_explicit_service_without_default(
    conversation_test_database,
):
    rows = await conversation_test_database.fetch_all(
        "SELECT TABLE_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
        "AND COLUMN_NAME='service_unit_key'",
    )
    assert {row["TABLE_NAME"] for row in rows} == set(TABLES)
    assert all(
        (row["COLUMN_TYPE"], row["IS_NULLABLE"], row["COLUMN_DEFAULT"])
        == ("varbinary(256)", "NO", None)
        for row in rows
    )


@pytest.mark.parametrize("invalid", [None, b"", SERVICE_B])
async def test_share_finalize_rejects_missing_or_mismatched_service(
    conversation_test_database,
    invalid,
):
    db = conversation_test_database
    await legacy_share(db)
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    publication = await insert_publication(db, service_unit_key=SERVICE_A)
    await db.execute(
        "UPDATE swe_chat_shares SET service_unit_key=%s,publication_id=%s,"
        "byte_offset=0,byte_length=1 WHERE token='token'",
        (invalid, publication),
    )
    with pytest.raises(ProgrammingError):
        await apply_sql_script(db, FINALIZE)
    column = await db.fetch_one(
        "SHOW COLUMNS FROM swe_chat_shares LIKE 'service_unit_key'",
    )
    assert column["Null"] == "YES"


@pytest.mark.parametrize(
    "invalid_locator",
    ["missing_publication", "null_extent"],
)
async def test_share_finalize_rejects_incomplete_locators(
    conversation_test_database,
    invalid_locator,
):
    db = conversation_test_database
    await legacy_share(db)
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    publication = await insert_publication(db, service_unit_key=SERVICE_A)
    await db.execute(
        "UPDATE swe_chat_shares SET service_unit_key=%s,publication_id=%s,"
        "byte_offset=0,byte_length=%s WHERE token='token'",
        (
            SERVICE_A,
            (
                publication + 100
                if invalid_locator == "missing_publication"
                else publication
            ),
            None if invalid_locator == "null_extent" else 1,
        ),
    )
    with pytest.raises(ProgrammingError):
        await apply_sql_script(db, FINALIZE)


async def test_share_finalize_preserves_token_and_counters(
    conversation_test_database,
):
    db = conversation_test_database
    await legacy_share(db)
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    publication = await insert_publication(db, service_unit_key=SERVICE_A)
    await db.execute(
        "UPDATE swe_chat_shares SET service_unit_key=%s,publication_id=%s,"
        "byte_offset=0,byte_length=1 WHERE token='token'",
        (SERVICE_A, publication),
    )
    before = await db.fetch_one("SELECT * FROM swe_chat_shares")
    await apply_sql_script(db, FINALIZE)
    assert await db.fetch_one("SELECT * FROM swe_chat_shares") == before
    column = await db.fetch_one(
        "SHOW COLUMNS FROM swe_chat_shares LIKE 'service_unit_key'",
    )
    assert (column["Type"], column["Null"], column["Default"]) == (
        "varbinary(256)",
        "NO",
        None,
    )
    with pytest.raises(IntegrityError):
        await db.execute(
            "UPDATE swe_chat_shares SET service_unit_key=NULL WHERE token='token'",
        )
    with pytest.raises(IntegrityError):
        await db.execute(
            "INSERT INTO swe_chat_shares (token,chat_id,creator_id,tenant_id,"
            "snapshot_key,created_at,access_count,last_access_at,publication_id,"
            "byte_offset,byte_length,service_unit_key) "
            "SELECT token,chat_id,creator_id,tenant_id,"
            "snapshot_key,created_at,access_count,last_access_at,publication_id,"
            "byte_offset,byte_length,%s FROM swe_chat_shares WHERE token='token'",
            (SERVICE_B,),
        )


async def test_share_finalize_expires_legacy_rows_without_locators(
    conversation_test_database,
):
    db = conversation_test_database
    await legacy_share(db)
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )

    await apply_sql_script(db, FINALIZE)

    assert await db.fetch_one(
        "SELECT COUNT(*) AS n FROM swe_chat_shares",
    ) == {"n": 0}
    column = await db.fetch_one(
        "SHOW COLUMNS FROM swe_chat_shares LIKE 'service_unit_key'",
    )
    assert column["Null"] == "NO"

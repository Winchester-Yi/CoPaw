# -*- coding: utf-8 -*-
"""Execute the exact six-table DDL in a generated isolated MySQL schema."""

from datetime import datetime
from fnmatch import fnmatchcase
import re

import pytest
from mysql.connector.errors import IntegrityError, ProgrammingError

from swe.app.chat_sharing.store import ChatShareStore
from swe.app.conversation_storage.schema_contract import (
    CHARSETS,
    COLUMNS,
    DEFAULTS,
    EXTRAS,
    INDEXES,
)
from tests.integrated.conversation_storage.conftest import (
    SQL_ROOT,
    TABLES,
    apply_sql_script,
)

NOW = datetime(2026, 10, 30)


async def insert_row(db, table, values):
    assert table in TABLES or table == "swe_chat_shares"
    if table in TABLES:
        values = {"service_unit_key": b"schema-test-service", **values}
    columns = ", ".join(values)
    placeholders = ", ".join(["%s"] * len(values))
    return await db.insert(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )


async def insert_session(db, **changes):
    values = {
        "tenant_key": b"tenant",
        "source_key": b"source",
        "agent_key": b"agent",
        "user_key": b"alice",
        "channel_key": b"console",
        "external_session_key": b"session",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return await insert_row(db, "swe_conversation_sessions", values)


async def insert_chat(db, session_id, key=b"chat", **changes):
    session = await db.fetch_one(
        "SELECT external_session_key FROM swe_conversation_sessions WHERE id=%s",
        (session_id,),
    )
    values = {
        "session_id": session_id,
        "external_chat_key": key,
        "external_session_key": session["external_session_key"],
        "registry_order": 1,
        "tenant_key": b"tenant",
        "source_key": b"source",
        "agent_key": b"agent",
        "user_key": b"alice",
        "channel_key": b"console",
        "title": "会话",
        "metadata_text": "{}",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return await insert_row(db, "swe_conversation_chats", values)


async def insert_publication(db, operation=b"operation", **changes):
    values = {
        "operation_key": operation,
        "tenant_key": b"tenant",
        "source_key": b"source",
        "agent_key": b"agent",
        "kind": "import",
        "state": "prepared",
        "generation": 0,
        "owner_key": b"test-owner",
        "relative_path": operation + b".bundle",
        "format_key": b"bundle-v1",
        "created_at": NOW,
    }
    values.update(changes)
    return await insert_row(db, "swe_conversation_publications", values)


async def test_mysql_capabilities_and_exact_six_table_schema(
    conversation_test_database,
    record_property,
):
    db = conversation_test_database
    server = await db.fetch_one(
        "SELECT VERSION() AS version, @@innodb_page_size AS page_size, "
        "@@SESSION.sql_mode AS sql_mode, @@SESSION.time_zone AS time_zone",
    )
    record_property("mysql_capabilities", server)
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", server["version"])
    assert match and tuple(map(int, match.groups())) >= (8, 0, 21)
    assert "mariadb" not in server["version"].lower()
    assert server["page_size"] >= 16384
    assert any(
        mode in server["sql_mode"]
        for mode in ("STRICT_TRANS_TABLES", "STRICT_ALL_TABLES")
    )
    assert server["time_zone"] == "+00:00"
    columns = await db.fetch_all(
        "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_COMMENT, "
        "COLUMN_DEFAULT, EXTRA, CHARACTER_SET_NAME, COLLATION_NAME "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
        "ORDER BY TABLE_NAME, ORDINAL_POSITION",
    )
    assert {row["TABLE_NAME"] for row in columns} == set(TABLES)
    ddl = (SQL_ROOT / "conversation_metadata_tables.sql").read_text(
        encoding="utf-8",
    )
    expected_comments = {}
    for table, body in re.findall(
        r"CREATE TABLE (\w+) \((.*?)\) ENGINE",
        ddl,
        re.S,
    ):
        for name, comment in re.findall(
            r"^    (\w+) .*? COMMENT '((?:[^']|'')*)',?$",
            body,
            re.M,
        ):
            expected_comments[table, name] = comment
    expected_columns = {
        (table, name): definition
        for table, definitions in COLUMNS.items()
        for name, definition in definitions.items()
    }
    assert set(expected_comments) == set(expected_columns)
    assert len(columns) == len(expected_columns)
    actual_order = {table: [] for table in COLUMNS}
    for row in columns:
        identity = row["TABLE_NAME"], row["COLUMN_NAME"]
        actual_order[row["TABLE_NAME"]].append(row["COLUMN_NAME"])
        expected_type, expected_nullable = expected_columns[identity]
        assert row["COLUMN_TYPE"].lower() == expected_type
        assert row["IS_NULLABLE"] == expected_nullable
        assert row["COLUMN_COMMENT"] == expected_comments[identity]
        table, name = identity
        assert row["COLUMN_DEFAULT"] == DEFAULTS[table].get(name)
        assert row["EXTRA"] == EXTRAS[table].get(name, "")
        charset, collation = CHARSETS[table][name]
        assert row["CHARACTER_SET_NAME"] == charset
        assert (
            row["COLLATION_NAME"] is None
            if collation is None
            else fnmatchcase(row["COLLATION_NAME"], collation)
        )
        assert re.search("[\u4e00-\u9fff]", row["COLUMN_COMMENT"])
    assert actual_order == {
        table: list(definitions) for table, definitions in COLUMNS.items()
    }
    for table in TABLES:
        indexes = await db.fetch_all(
            "SELECT INDEX_NAME, NON_UNIQUE, COLUMN_NAME, SEQ_IN_INDEX "
            "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
            "AND TABLE_NAME=%s ORDER BY INDEX_NAME, SEQ_IN_INDEX",
            (table,),
        )
        actual_indexes = {}
        for index in indexes:
            entry = actual_indexes.setdefault(
                index["INDEX_NAME"],
                [int(index["NON_UNIQUE"]), []],
            )
            entry[1].append(index["COLUMN_NAME"])
        assert {
            name: (unique, tuple(names))
            for name, (unique, names) in actual_indexes.items()
        } == INDEXES[table]
        create = await db.fetch_one(f"SHOW CREATE TABLE {table}")
        ddl = create["Create Table"]
        assert "ENGINE=InnoDB" in ddl and "ROW_FORMAT=DYNAMIC" in ddl
        assert "DEFAULT CHARSET=utf8mb4" in ddl
        assert "FOREIGN KEY" not in ddl and "CONSTRAINT" not in ddl
        assert not re.search(r"`\w+` json\b", ddl, re.I)
    record_property(
        "show_create_tables",
        {
            table: (await db.fetch_one(f"SHOW CREATE TABLE {table}"))[
                "Create Table"
            ]
            for table in TABLES
        },
    )


async def test_full_session_identity_is_binary_unique_and_empty_is_valid(
    conversation_test_database,
):
    db = conversation_test_database
    baseline = {
        "tenant_key": b"",
        "source_key": b"",
        "agent_key": b"",
        "user_key": b"",
        "channel_key": b"",
        "external_session_key": b"",
    }
    first = await insert_session(db, **baseline)
    assert first
    with pytest.raises(IntegrityError):
        await insert_session(db, **baseline)
    for field in baseline:
        values = {**baseline, field: b"A"}
        assert await insert_session(db, **values)
        assert await insert_session(db, **{**baseline, field: b"a"})
    assert await insert_session(db, external_session_key="中".encode() * 341)
    with pytest.raises(Exception) as error:
        await insert_session(db, external_session_key=b"x" * 1025)
    assert "Data too long" in str(error.value)


async def test_session_accepts_multiple_chats_and_nullable_message_order_keys(
    conversation_test_database,
):
    db = conversation_test_database
    session_id = await insert_session(db)
    first_chat = await insert_chat(db, session_id)
    second_chat = await insert_chat(
        db,
        session_id,
        b"other-chat",
        registry_order=2,
    )
    assert first_chat != second_chat
    publication_id = await insert_publication(db)
    values = {
        "session_id": session_id,
        "layer": "memory",
        "source_msg_key": b"duplicate-id",
        "source_occurrence_no": 0,
        "role_key": b"user",
        "kind_key": b"message",
        "publication_id": publication_id,
        "byte_offset": 10,
        "byte_length": 2,
        "context_epoch": 0,
        "in_context": 1,
    }
    await insert_row(db, "swe_conversation_messages", values)
    await insert_row(
        db,
        "swe_conversation_messages",
        {**values, "source_occurrence_no": 1},
    )
    await insert_row(
        db,
        "swe_conversation_messages",
        {**values, "session_order": 1},
    )
    with pytest.raises(IntegrityError):
        await insert_row(
            db,
            "swe_conversation_messages",
            {**values, "session_order": 1},
        )
    for chat in (first_chat, second_chat):
        await insert_row(
            db,
            "swe_conversation_messages",
            {
                **values,
                "archive_chat_id": chat,
                "archive_order": 1,
                "layer": "archive",
            },
        )


async def test_turn_operation_and_guard_unique_keys(
    conversation_test_database,
):
    db = conversation_test_database
    session_id = await insert_session(db)
    chat_id = await insert_chat(db, session_id)
    turn = {
        "chat_id": chat_id,
        "session_id": session_id,
        "question_msg_key": b"question",
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
            {**turn, "turn_order": 2},
        )
    await insert_publication(db)
    with pytest.raises(IntegrityError):
        await insert_publication(db, relative_path=b"other-path")
    for key in (b"prepared-one", b"prepared-two"):
        await insert_publication(db, operation=key, chat_id=chat_id)
    guard = {
        "retention_key": b"guard-one",
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
        await insert_row(
            db,
            "swe_conversation_retentions",
            {**guard, "retention_key": b"guard-two"},
        )
    for key in (b"pin-one", b"pin-two"):
        await insert_row(
            db,
            "swe_conversation_retentions",
            {
                **guard,
                "retention_key": key,
                "guard_marker": None,
                "kind": "reader",
            },
        )


async def legacy_share(db):
    await ChatShareStore(db).ensure_schema()
    await db.execute(
        "ALTER TABLE swe_chat_shares MODIFY token VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL",
    )
    await insert_row(
        db,
        "swe_chat_shares",
        {
            "token": "token",
            "chat_id": "deleted-chat",
            "creator_id": "alice",
            "tenant_id": "tenant",
            "snapshot_key": "token.json",
            "created_at": NOW,
            "access_count": 7,
            "last_access_at": NOW,
        },
    )


async def test_share_extension_preserves_legacy_schema_tokens_and_counters(
    conversation_test_database,
):
    db = conversation_test_database
    await legacy_share(db)
    before = await db.fetch_all("SHOW FULL COLUMNS FROM swe_chat_shares")
    indexes = await db.fetch_all("SHOW INDEX FROM swe_chat_shares")
    record = await db.fetch_one("SELECT * FROM swe_chat_shares")
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    after = await db.fetch_all("SHOW FULL COLUMNS FROM swe_chat_shares")
    assert len(after) == 12
    for old, new in zip(before, after):
        for field in (
            "Field",
            "Type",
            "Collation",
            "Null",
            "Key",
            "Default",
            "Extra",
        ):
            assert old[field] == new[field]
        assert re.search("[\u4e00-\u9fff]", new["Comment"])
    assert [
        row
        for row in await db.fetch_all("SHOW INDEX FROM swe_chat_shares")
        if row["Key_name"]
        not in {
            "ix_swe_chat_shares_publication",
            "ix_swe_chat_shares_service_chat",
            "ix_swe_chat_shares_service_creator",
        }
    ] == indexes
    updated = await db.fetch_one("SELECT * FROM swe_chat_shares")
    assert {key: updated[key] for key in record} == record
    assert all(
        updated[key] is None
        for key in (
            "publication_id",
            "byte_offset",
            "byte_length",
            "service_unit_key",
        )
    )


async def test_share_extension_rejects_unexpected_schema_before_adding_columns(
    conversation_test_database,
):
    db = conversation_test_database
    await legacy_share(db)
    await db.execute("ALTER TABLE swe_chat_shares ADD unexpected TEXT")
    with pytest.raises(ProgrammingError):
        await apply_sql_script(
            db,
            SQL_ROOT / "conversation_share_publication_extension.sql",
        )
    columns = await db.fetch_all("SHOW COLUMNS FROM swe_chat_shares")
    assert "publication_id" not in {row["Field"] for row in columns}


async def test_share_and_publication_share_one_transaction_connection(
    conversation_test_database,
):
    db = conversation_test_database
    await legacy_share(db)
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    with pytest.raises(ValueError, match="rollback"):
        async with db.transaction() as transaction:
            first_connection = await transaction.fetch_one(
                "SELECT CONNECTION_ID() AS id",
            )
            assert (
                await db.fetch_one("SELECT CONNECTION_ID() AS id")
                == first_connection
            )
            publication = await insert_publication(db)
            await db.execute(
                "UPDATE swe_chat_shares SET publication_id=%s WHERE token=%s",
                (publication, "token"),
            )
            raise ValueError("rollback")
    assert (
        await db.fetch_one("SELECT id FROM swe_conversation_publications")
        is None
    )
    assert (await db.fetch_one("SELECT publication_id FROM swe_chat_shares"))[
        "publication_id"
    ] is None
    async with db.transaction():
        publication = await insert_publication(db)
        await db.execute(
            "UPDATE swe_chat_shares SET publication_id=%s WHERE token=%s",
            (publication, "token"),
        )
    assert (await db.fetch_one("SELECT publication_id FROM swe_chat_shares"))[
        "publication_id"
    ] == publication

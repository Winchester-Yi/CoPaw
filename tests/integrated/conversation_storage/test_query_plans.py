# -*- coding: utf-8 -*-
"""Representative metadata cardinalities; no FORCE INDEX or NAS hydration."""

import json

from tests.integrated.conversation_storage.test_schema import NOW

SERVICE = b"schema-test-service"


async def test_query_paths_use_scope_and_point_indexes(
    conversation_test_database,
    record_property,
):
    db = conversation_test_database
    await db.execute_many(
        "INSERT INTO swe_conversation_sessions (service_unit_key,id,tenant_key,source_key,agent_key,user_key,channel_key,external_session_key,created_at,updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        [
            (
                SERVICE,
                i,
                b"tenant",
                b"source",
                b"agent",
                f"user{i}".encode(),
                b"console",
                f"session{i}".encode(),
                NOW,
                NOW,
            )
            for i in range(1, 31)
        ],
    )
    await db.execute_many(
        "INSERT INTO swe_conversation_chats (service_unit_key,id,session_id,external_chat_key,registry_order,tenant_key,source_key,agent_key,user_key,channel_key,title,metadata_text,external_session_key,created_at,updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        [
            (
                SERVICE,
                i,
                (i - 1) % 30 + 1,
                f"chat{i}".encode(),
                i,
                b"tenant",
                b"source",
                b"agent",
                f"user{(i - 1) % 30 + 1}".encode(),
                b"console",
                "title",
                "{}",
                f"session{(i - 1) % 30 + 1}".encode(),
                NOW,
                NOW,
            )
            for i in range(1, 1501)
        ],
    )
    await db.execute_many(
        "INSERT INTO swe_conversation_publications (service_unit_key,id,operation_key,tenant_key,source_key,agent_key,session_id,chat_id,kind,state,generation,owner_key,relative_path,format_key,created_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        [
            (
                SERVICE,
                i,
                f"op{i}".encode(),
                b"tenant",
                b"source",
                b"agent",
                (i - 1) % 30 + 1,
                (i - 1) % 30 + 1,
                "history",
                "committed",
                1,
                b"owner",
                f"bundles/{i}".encode(),
                b"bundle-v1",
                NOW,
            )
            for i in range(1, 2001)
        ],
    )
    await db.execute_many(
        "INSERT INTO swe_conversation_messages (service_unit_key,session_id,archive_chat_id,layer,session_order,archive_order,source_msg_key,source_occurrence_no,role_key,kind_key,publication_id,byte_offset,byte_length,context_epoch,in_context) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        [
            (
                SERVICE,
                (i - 1) % 30 + 1,
                (i - 1) % 30 + 1 if i % 2 else None,
                "archive" if i % 2 else "memory",
                None if i % 2 else i,
                i if i % 2 else None,
                f"msg{i}".encode(),
                i,
                b"assistant",
                b"message",
                (i - 1) % 2000 + 1,
                100,
                20,
                0,
                0 if i % 2 else 1,
            )
            for i in range(1, 6001)
        ],
    )
    await db.execute_many(
        "INSERT INTO swe_conversation_turns (service_unit_key,chat_id,session_id,question_msg_key,turn_order,context_epoch,generation,state) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        [
            (
                SERVICE,
                (i - 1) % 30 + 1,
                (i - 1) % 30 + 1,
                f"q{i}".encode(),
                i,
                0,
                1,
                "succeeded",
            )
            for i in range(1, 901)
        ],
    )
    for table in (
        "swe_conversation_chats",
        "swe_conversation_messages",
        "swe_conversation_publications",
        "swe_conversation_turns",
    ):
        await db.fetch_all(f"ANALYZE TABLE {table}")
    queries = {
        "sidebar_user_channel": (
            "SELECT id,external_chat_key FROM swe_conversation_chats WHERE service_unit_key=%s AND tenant_key=%s AND source_key=%s AND agent_key=%s AND user_key=%s AND channel_key=%s AND deleted_at IS NULL ORDER BY updated_at DESC,external_chat_key DESC LIMIT 20",
            (SERVICE, b"tenant", b"source", b"agent", b"user1", b"console"),
        ),
        "archive_page": (
            "SELECT publication_id,byte_offset,byte_length FROM swe_conversation_messages WHERE service_unit_key=%s AND archive_chat_id=%s AND deleted_at IS NULL AND archive_order<%s AND archive_order<=%s ORDER BY archive_order DESC LIMIT 20",
            (SERVICE, 1, 5000, 6000),
        ),
        "turn_question": (
            "SELECT id,state FROM swe_conversation_turns WHERE service_unit_key=%s AND session_id=%s AND question_msg_key=%s",
            (SERVICE, 1, b"q1"),
        ),
        "source_message": (
            "SELECT publication_id,byte_offset,byte_length FROM swe_conversation_messages WHERE service_unit_key=%s AND session_id=%s AND source_msg_key=%s AND layer=%s",
            (SERVICE, 2, b"msg2", "memory"),
        ),
        "publication_reverse_reference": (
            "SELECT id FROM swe_conversation_messages WHERE service_unit_key=%s AND publication_id=%s",
            (SERVICE, 1),
        ),
        "context_lookup": (
            "SELECT id,publication_id FROM swe_conversation_messages WHERE service_unit_key=%s AND session_id=%s AND context_epoch=%s AND in_context=%s ORDER BY session_order",
            (SERVICE, 2, 0, 1),
        ),
        "publication_pending": (
            "SELECT id FROM swe_conversation_publications WHERE service_unit_key=%s AND state=%s AND created_at<%s ORDER BY created_at,id LIMIT 20",
            (SERVICE, "prepared", NOW),
        ),
    }
    plans = {}
    for name, (query, params) in queries.items():
        rows = await db.fetch_all("EXPLAIN " + query, params)
        plans[name] = rows
        assert rows and all(
            row["type"] != "ALL" and row["key"] for row in rows
        ), (
            name,
            rows,
        )
    admin = await db.fetch_all(
        "EXPLAIN SELECT id FROM swe_conversation_chats WHERE service_unit_key=%s AND tenant_key=%s AND source_key=%s AND agent_key=%s AND deleted_at IS NULL ORDER BY updated_at DESC,external_chat_key DESC",
        (SERVICE, b"tenant", b"source", b"agent"),
    )
    plans["admin_full_scope_array_cost"] = admin
    record_property("query_plans", json.dumps(plans, default=str))
    record_property(
        "cardinality",
        "30 Sessions, 1500 Chats, 900 Turns, 2000 Publications, 6000 Messages",
    )

# -*- coding: utf-8 -*-
"""Direct body references bound repeated recovery writes without root chains."""

import json

from swe.app.conversation_storage.bundle import BundleStore, new_operation_key
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.session_adapter import MySQLSessionAdapter
from tests.integrated.conversation_storage.test_archive_runtime_mysql import (
    setup,
    source,
)


async def test_recovery_append_reuses_old_body_and_restores_exact_complete_state(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    _, _, _, store = await setup(db, tmp_path)
    first = source("duplicate", "A" * 100000)
    second = source("duplicate", "B" * 100000)
    state = {
        "revision": 0,
        "agent": {
            "memory": {
                "content": [[first.to_dict(), ["important"]]],
                "_compressed_summary": "keep",
            },
        },
        "future_module": {"unknown": [1, 2]},
    }
    await store.session_adapter._save_state(
        store.session_adapter.identity("session", "alice"),
        state,
    )
    loaded = await store.session_adapter.get_session_state_dict(
        "session",
        "alice",
    )
    assert loaded["future_module"] == state["future_module"]
    assert loaded["agent"]["memory"] == state["agent"]["memory"]
    loaded["agent"]["memory"]["content"].append([second.to_dict(), []])
    saved = await store.session_adapter._save_state(
        store.session_adapter.identity("session", "alice"),
        loaded,
    )
    rows = await db.fetch_all(
        "SELECT * FROM swe_conversation_publications WHERE state='committed' ORDER BY id",
    )
    assert len(rows) == 2
    assert rows[1]["byte_length"] < 120000
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_messages",
        )
    )["n"] == 2
    assert (
        await store.session_adapter.get_session_state_dict("session", "alice")
        == saved
    )
    pins = await db.fetch_all(
        "SELECT publication_id FROM swe_conversation_retentions WHERE kind='dependency' AND state='active' ORDER BY publication_id",
    )
    assert [row["publication_id"] for row in pins] == [rows[0]["id"]]
    saved["agent"]["memory"]["content"] = [[second.to_dict(), []]]
    trimmed = await store.session_adapter._save_state(
        store.session_adapter.identity("session", "alice"),
        saved,
    )
    assert (
        await store.session_adapter.get_session_state_dict("session", "alice")
        == trimmed
    )
    visible = await db.fetch_all(
        "SELECT source_msg_key FROM swe_conversation_messages WHERE current_visible=1 ORDER BY session_order",
    )
    assert len(visible) == 1
    third = await db.fetch_one(
        "SELECT * FROM swe_conversation_publications ORDER BY id DESC LIMIT 1",
    )
    assert third["byte_length"] < 10000
    deps = await db.fetch_all(
        "SELECT publication_id FROM swe_conversation_retentions WHERE retention_key=%s AND kind='dependency'",
        (("dep:" + str(third["id"])).encode(),),
    )
    assert [row["publication_id"] for row in deps] == [rows[1]["id"]]


async def test_changed_message_and_identical_occurrences_remain_separate_delta_bodies(
    conversation_test_database,
    tmp_path,
):
    db = conversation_test_database
    _, _, _, store = await setup(db, tmp_path)
    raw = source("q", "same").to_dict()
    second = json.loads(json.dumps(raw))
    state = {
        "revision": 0,
        "agent": {"memory": {"content": [[raw, []], [second, []]]}},
    }
    saved = await store.session_adapter._save_state(
        store.session_adapter.identity("session", "alice"),
        state,
    )
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_messages",
        )
    )["n"] == 2
    saved["agent"]["memory"]["content"][1][0]["content"] = "changed"
    saved = await store.session_adapter._save_state(
        store.session_adapter.identity("session", "alice"),
        saved,
    )
    assert (
        await db.fetch_one(
            "SELECT COUNT(*) AS n FROM swe_conversation_messages",
        )
    )["n"] == 3
    assert (
        await store.session_adapter.get_session_state_dict("session", "alice")
        == saved
    )
    from swe.app.conversation_storage.history import HistoryStore

    history = HistoryStore(
        db,
        store.scope,
        store.bundles,
        cursor_secret=b"projection-test",
    )
    current = await history.current("first")
    assert len(current.messages) == 2


async def test_cold_turn_independent_block_roundtrip_and_modified_terminal_stays_inline(
    conversation_test_database,
    tmp_path,
):
    from swe.app.conversation_storage.execution import ExecutionStore
    from swe.app.conversation_storage.identity import SessionIdentity
    from swe.app.conversation_storage.publications import PublicationStore

    db = conversation_test_database
    _, _, _, store = await setup(db, tmp_path)
    execution = ExecutionStore(
        db,
        owner_key="owner",
        lease_seconds=60,
        publications=store.publications,
        service_unit_id="schema-test-service",
    )
    identity = SessionIdentity(store.scope, "alice", "console", "session")
    admission = await execution.reserve_admission(
        identity,
        "first",
        "q",
        operation_key=new_operation_key(),
        runtime_turn_key="runtime-q",
    )
    turn = {
        "status": "completed",
        "chat_id": "first",
        "message": {"id": "q", "role": "user", "content": "question"},
        "unknown": {"preserve": True},
    }
    state = {
        "turn_states": {"q": turn},
        "future": {"keep": True},
        "agent": {"memory": {"content": []}},
    }
    await execution.publish_admission(
        admission,
        {"id": "q", "role": "user", "content": "question"},
        state,
    )
    await execution.finish(
        admission.authority,
        "completed",
        state,
        operation_key=new_operation_key(),
    )
    loaded = await store.session_adapter.get_session_state_dict(
        "session",
        "alice",
    )
    saved = await store.session_adapter._save_state(identity, loaded)
    current = await db.fetch_one(
        "SELECT * FROM swe_conversation_publications ORDER BY id DESC LIMIT 1",
    )
    index = store.bundles.inspect(
        current["relative_path"].decode(),
        expected_length=current["byte_length"],
    )
    root = index.blocks["recovery"]
    envelope = json.loads(
        store.bundles.read_extent(
            index.relative_path,
            root.offset,
            root.length,
            expected_length=index.byte_length,
        ),
    )
    assert envelope["state"]["turn_states"] == {}
    assert envelope["turns"][0]["ref"]["block_name"] == "turn:q"
    assert (
        await store.session_adapter.get_session_state_dict("session", "alice")
        == saved
    )
    assert (
        await store.session_adapter.get_turn_state("session", "alice", {}, "q")
        == turn
    )
    saved["turn_states"]["q"]["unknown"]["preserve"] = False
    saved = await store.session_adapter._save_state(identity, saved)
    assert (
        await store.session_adapter.get_session_state_dict("session", "alice")
        == saved
    )


async def test_request_cache_avoids_rereading_old_message_bodies_after_confirmed_append(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    from swe.app.conversation_storage.projection_cache import cache_scope

    db = conversation_test_database
    _, _, _, store = await setup(db, tmp_path)
    raw = source("q", "A" * 10000).to_dict()
    state = {"revision": 0, "agent": {"memory": {"content": [[raw, []]]}}}
    calls = []
    original = store.bundles.read_extent

    def tracked(path, offset, length, **kwargs):
        calls.append((path, offset, length))
        return original(path, offset, length, **kwargs)

    monkeypatch.setattr(store.bundles, "read_extent", tracked)
    with cache_scope():
        state = await store.session_adapter._save_state(
            store.session_adapter.identity("session", "alice"),
            state,
        )
        first = await db.fetch_one(
            "SELECT * FROM swe_conversation_messages ORDER BY id LIMIT 1",
        )
        pub = await db.fetch_one(
            "SELECT * FROM swe_conversation_publications WHERE id=%s",
            (first["publication_id"],),
        )
        calls.clear()
        state["agent"]["memory"]["content"].append(
            [source("a", "new").to_dict(), []],
        )
        await store.session_adapter._save_state(
            store.session_adapter.identity("session", "alice"),
            state,
        )
        assert (
            pub["relative_path"].decode(),
            first["byte_offset"],
            first["byte_length"],
        ) not in calls

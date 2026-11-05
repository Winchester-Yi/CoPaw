# -*- coding: utf-8 -*-
"""Cost and integrity contracts for simplified reads and batched writes."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.errors import ConflictError
from swe.app.conversation_storage.history import HistorySnapshot
from swe.app.runner.models import ChatSpec
from tests.unit.app.conversation_storage.test_chat_repository import (
    chat_row,
    repository,
)
from tests.unit.app.conversation_storage.test_history import history
from tests.unit.app.conversation_storage.test_reference_queries import (
    ReferenceDatabase,
)


@pytest.mark.parametrize("count", [1, 257, 4097])
async def test_chat_revalidation_is_narrow_bounded_and_scoped(tmp_path, count):
    rows = {i: chat_row(id=i) for i in range(1, count + 1)}
    db = ReferenceDatabase(chats=rows)
    await repository(db, tmp_path)._revalidate(list(rows.values()))
    assert len(db.calls) == (count + 255) // 256
    assert db.acquisitions == 1
    assert all("metadata_text" not in query for query, _ in db.calls)
    rows[count]["metadata_revision"] += 1
    with pytest.raises(ConflictError):
        await repository(db, tmp_path)._revalidate(
            [chat_row(id=i) for i in rows],
        )


async def test_chat_revalidation_rejects_foreign_scope(tmp_path):
    expected = chat_row()
    db = ReferenceDatabase(chats={1: chat_row(tenant_key=b"other")})
    with pytest.raises(ConflictError):
        await repository(db, tmp_path)._revalidate([expected])


async def test_history_assembly_checks_chat_twice_without_message_requery(
    tmp_path,
):
    store = history(tmp_path)
    store._validate_chat_row = AsyncMock()
    store.chats._hydrate = AsyncMock(
        return_value=[
            ChatSpec(id="chat", session_id="session", user_id="alice"),
        ],
    )
    store._payloads = AsyncMock(return_value=[])
    store._revalidate_rows = AsyncMock(
        side_effect=AssertionError("full requery"),
    )
    store._finish = AsyncMock()
    result = await store._assemble(HistorySnapshot({}, [], [], []))
    assert result.messages == []
    assert store._validate_chat_row.await_count == 2
    store._finish.assert_awaited_once()


async def test_history_capture_does_not_lock_or_requery_messages(tmp_path):
    store = history(tmp_path)

    @asynccontextmanager
    async def transaction():
        yield

    store.db = SimpleNamespace(
        transaction=transaction,
        fetch_one=AsyncMock(return_value={"history_high": 0}),
        fetch_all=AsyncMock(return_value=[]),
    )
    store._chat = AsyncMock(return_value={"id": 1, "session_id": 2})
    store._pins = AsyncMock(return_value=[])
    store._boundaries = AsyncMock(return_value=[])
    store._revalidate_rows = AsyncMock(
        side_effect=AssertionError("full requery"),
    )
    snapshot = await store._snapshot("chat")
    assert snapshot.rows == []
    assert store.db.fetch_all.await_count == 1


@pytest.mark.parametrize("count", [1, 257, 4097])
async def test_source_publication_locks_are_batched_before_complete_current(
    count,
):
    from swe.app.conversation_storage.source_messages import SourceMessages
    from tests.unit.app.conversation_storage.test_reference_queries import (
        SERVICE,
    )

    rows = {
        i: {
            "id": i,
            "service_unit_key": SERVICE,
            "scope_kind": "workspace",
            "tenant_key": b"t",
            "source_key": b"s",
            "agent_key": b"a",
            "session_id": 1,
            "chat_id": None,
            "turn_id": None,
            "kind": "state",
            "operation_key": str(i).encode(),
            "relative_path": str(i).encode(),
            "byte_length": 100,
            "state": "committed",
            "gc_state": "live",
        }
        for i in range(1, count + 1)
    }

    class SourceDatabase(ReferenceDatabase):
        async def fetch_one(self, query, params):
            assert len(self.calls) == (count + 255) // 256
            assert "FOR UPDATE" in query
            assert params == (count + 1, SERVICE)
            return {"id": count + 1, "result_metadata_text": "exact-plan"}

    db = SourceDatabase(publications=rows)
    service = SimpleNamespace(db=db, service_unit_key=SERVICE)
    source = SourceMessages(dependency_publications=rows)
    current, locked = await source.lock_publications(
        service,
        {"id": count + 1},
    )
    assert current["result_metadata_text"] == "exact-plan"
    assert locked == rows
    assert all(query.endswith("FOR UPDATE") for query, _ in db.calls)


async def test_pin_member_writes_use_prepared_and_keep_exact_set():
    from tests.unit.app.conversation_storage.test_retention import (
        RetentionDatabase,
        store,
    )

    db = RetentionDatabase()
    calls = []

    async def prepared(template, rows, **kwargs):
        values = list(rows)
        calls.append(values)
        fields = template.prefix.split("(", 1)[1].split(")", 1)[0].split(",")
        for values_row in values:
            row = dict(zip(fields, values_row))
            db.pins.append({**row, "id": len(db.pins) + 1})
        return len(values)

    db.insert_prepared = prepared
    original = db.fetch_one

    async def fetch(query, params=None):
        if "UTC_TIMESTAMP" in query:
            return {"created_at": "database-time"}
        return await original(query, params)

    db.fetch_one = fetch
    await store(db).pin([1, 2, 3], kind="reader")
    assert len(calls) == 1
    assert len(calls[0]) == 3
    assert [row["publication_id"] for row in db.pins] == [1, 2, 3]
    assert all(row["created_at"] == "database-time" for row in db.pins)


async def test_message_batch_preserves_duplicate_occurrences_and_order():
    from swe.app.conversation_storage.message_installs import (
        MessageInstall,
        install_messages,
    )
    from swe.app.conversation_storage.bundle import BlockExtent, BundleIndex
    from swe.app.conversation_storage.identity import WorkspaceScope

    captured = []

    async def prepared(template, rows):
        captured.extend(rows)

    db = SimpleNamespace(
        insert=AsyncMock(),
        insert_prepared=AsyncMock(side_effect=prepared),
        execute=AsyncMock(),
    )
    request = SimpleNamespace(
        scope=WorkspaceScope(
            "t",
            "s",
            "a",
            service_unit_id="schema-test-service",
        ),
        session_id=1,
        chat_id=2,
        turn_id=None,
    )
    messages = [
        MessageInstall(f"m:{i}", "memory", "user", source_msg_key="same")
        for i in range(101)
    ]
    index = BundleIndex(
        "bundle",
        10000,
        {},
        {
            item.block_name: BlockExtent("message", 50 + i, 1)
            for i, item in enumerate(messages)
        },
    )
    await install_messages(
        db,
        request,
        messages,
        {"id": 3},
        index,
        ({"history_high": 9}, {"history_high": 0}, None),
    )
    db.insert.assert_not_awaited()
    db.insert_prepared.assert_awaited_once()
    template, _rows = db.insert_prepared.await_args.args
    rows = captured
    fields = template.prefix.split("(", 1)[1].split(")", 1)[0].split(",")
    decoded = [dict(zip(fields, row)) for row in rows]
    assert [row["session_order"] for row in decoded] == list(range(10, 111))
    assert all(row["source_msg_key"] == b"same" for row in decoded)
    assert db.execute.await_args_list[0].args[1] == (110, 1)


async def test_anchor_suppression_batches_binary_keys_by_packet(tmp_path):
    from swe.app.conversation_storage.collection_queries import (
        CollectionQuery,
        query_collection,
    )

    class Database:
        max_allowed_packet = 4096

        def __init__(self):
            self.calls = []

        @asynccontextmanager
        async def acquire(self):
            yield self

        async def fetch_all(self, query, params):
            self.calls.append((query, params))
            assert query.count("%s") == len(params)
            return [{"source_msg_key": key} for key in params[2:]]

    db = Database()
    keys = [b"\x00\xff\\'" + str(i).encode() + b"x" * 100 for i in range(257)]
    rows = await query_collection(
        db,
        CollectionQuery.ARCHIVED_KEYS,
        keys,
        (b"schema-test-service", 1),
    )
    assert {row["source_msg_key"] for row in rows} == set(keys)
    assert len(db.calls) > 2
    assert all("DISTINCT source_msg_key" in query for query, _ in db.calls)
    assert all(len(params) <= 258 for _, params in db.calls)
    assert (
        await query_collection(
            db,
            CollectionQuery.ARCHIVED_KEYS,
            [],
            (b"schema-test-service", 1),
        )
        == []
    )


async def test_share_occurrence_keys_are_bounded_and_scoped():
    from swe.app.conversation_storage.collection_queries import (
        CollectionQuery,
        query_collection,
    )

    class Database:
        max_allowed_packet = 8192

        @asynccontextmanager
        async def acquire(self):
            yield self

        def __init__(self):
            self.calls = []

        async def fetch_all(self, query, params):
            self.calls.append((query, params))
            return [{"id": key} for key in params[6:]]

    db = Database()
    keys = [str(i).encode().ljust(256, b"x") for i in range(257)]
    params = (b"service", 2, 10, 1, 2, 20)
    rows = await query_collection(
        db,
        CollectionQuery.SHARE_OCCURRENCES,
        keys,
        params,
    )
    assert {row["id"] for row in rows} == set(keys)
    assert len(db.calls) > 2
    for query, arguments in db.calls:
        assert arguments[:6] == params
        assert len(arguments) - 6 <= 256
        assert query.count("%s") == len(arguments)
        assert "m.service_unit_key=%s" in query


@pytest.mark.parametrize("corruption", ["missing", "extra", "released"])
async def test_dependency_members_validate_tail_after_4096(corruption):
    from tests.unit.app.conversation_storage.test_retention import (
        RetentionDatabase,
        store,
    )

    db = RetentionDatabase()
    service = store(db)
    key = b"dep:5000"
    db.pins = [
        {
            **service.scope.fields,
            "scope_kind": "workspace",
            "retention_key": key,
            "member_key": i,
            "publication_id": i,
            "kind": "dependency",
            "state": "active",
            "guard_marker": None,
        }
        for i in range(1, 4098)
    ]
    calls = []
    original = db.fetch_all

    async def fetch(query, params):
        calls.append(params)
        return await original(query, params)

    db.fetch_all = fetch
    await service._validate_members(key, list(range(1, 4098)), "dependency")
    assert len(calls) == 17
    assert all(params[-1] == 256 for params in calls)
    if corruption == "missing":
        db.pins.pop()
    elif corruption == "extra":
        db.pins.append(
            {**db.pins[-1], "publication_id": 4098, "member_key": 4098},
        )
    else:
        db.pins[-1]["state"] = "released"
    with pytest.raises(ConflictError):
        await service._validate_members(
            key,
            list(range(1, 4098)),
            "dependency",
        )


async def test_locked_dependencies_do_not_requery_publications():
    from tests.unit.app.conversation_storage.test_retention import (
        RetentionDatabase,
        store,
    )

    db = RetentionDatabase()
    service = store(db)
    with pytest.raises(RuntimeError, match="transaction"):
        await service._install_locked_dependencies(
            db.objects[4],
            [db.objects[1]],
        )
    async with db.transaction():
        await service._install_locked_dependencies(
            db.objects[4],
            [db.objects[1], db.objects[2]],
        )
    assert not any(
        "FROM swe_conversation_publications" in query for query in db.queries
    )
    assert [row["publication_id"] for row in db.pins] == [1, 2]


# Use the real facade/session/PreparedInsert with the protocol-level connection
# substitute: unlike AsyncMock this exercises chunking and poisoned transactions.
from tests.unit.database.test_connection import (
    db as protocol_db,
)  # noqa: F401,E402


def message_batch(*, bad_at=None):
    from swe.app.conversation_storage.message_installs import MessageInstall
    from swe.app.conversation_storage.bundle import BlockExtent, BundleIndex
    from swe.app.conversation_storage.identity import WorkspaceScope

    request = SimpleNamespace(
        scope=WorkspaceScope(
            "t",
            "s",
            "a",
            service_unit_id="schema-test-service",
        ),
        session_id=1,
        chat_id=2,
        turn_id=None,
    )
    messages = [
        MessageInstall(
            str(i),
            "memory",
            "user",
            context_epoch=int(i == bad_at),
        )
        for i in range(201)
    ]
    index = BundleIndex(
        "bundle",
        10000,
        {},
        {
            item.block_name: BlockExtent("message", 50 + i, 1)
            for i, item in enumerate(messages)
        },
    )
    return request, messages, index


@pytest.mark.parametrize("bad_at", [0, 100, 200])
async def test_late_message_validation_failure_prevents_partial_commit(
    protocol_db,
    monkeypatch,
    bad_at,
):
    from swe.app.conversation_storage.message_installs import install_messages
    from tests.unit.database.test_connection import Connection, Cursor

    original = Connection.cursor

    async def cursor(self, **kwargs):
        return (
            Cursor(self)
            if kwargs == {"prepared": True}
            else await original(self, **kwargs)
        )

    monkeypatch.setattr(Connection, "cursor", cursor)
    request, messages, index = message_batch(bad_at=bad_at)
    with pytest.raises(RuntimeError, match="session"):
        async with protocol_db.transaction():
            with pytest.raises(ConflictError):
                await install_messages(
                    protocol_db,
                    request,
                    messages,
                    {"id": 3},
                    index,
                    ({"history_high": 9}, {"history_high": 0}, None),
                )
    calls = Connection.instances[0].calls
    inserts = [
        sql
        for sql, _ in calls
        if sql.startswith("INSERT INTO swe_conversation_messages")
    ]
    assert len(inserts) == bad_at // 100
    assert "ROLLBACK" in [sql for sql, _ in calls]
    assert "COMMIT" not in [sql for sql, _ in calls]
    assert not any(
        sql.startswith("UPDATE swe_conversation_") for sql, _ in calls
    )


async def test_messages_use_three_wire_statements_for_201_rows(
    protocol_db,
    monkeypatch,
):
    from swe.app.conversation_storage.message_installs import (
        install_messages,
        MESSAGE_FIELDS,
    )
    from tests.unit.database.test_connection import Connection, Cursor

    original = Connection.cursor

    async def cursor(self, **kwargs):
        return (
            Cursor(self)
            if kwargs == {"prepared": True}
            else await original(self, **kwargs)
        )

    monkeypatch.setattr(Connection, "cursor", cursor)
    request, messages, index = message_batch()
    async with protocol_db.transaction():
        await install_messages(
            protocol_db,
            request,
            messages,
            {"id": 3},
            index,
            ({"history_high": 9}, {"history_high": 0}, None),
        )
    calls = Connection.instances[0].calls
    inserted = [
        params
        for sql, params in calls
        if sql.startswith("INSERT INTO swe_conversation_messages")
    ]
    assert [len(params) // len(MESSAGE_FIELDS) for params in inserted] == [
        100,
        100,
        1,
    ]
    assert (
        "UPDATE swe_conversation_sessions SET history_high=%s WHERE id=%s",
        (210, 1),
    ) in calls
    assert "COMMIT" in [sql for sql, _ in calls]


@pytest.mark.parametrize("failure_at", [None, 2])
async def test_delete_batches_freeze_candidates_and_keep_one_transaction(
    tmp_path,
    failure_at,
):
    from copy import deepcopy

    class Database:
        max_allowed_packet = 1024 * 1024
        _in_transaction = False

        def __init__(self):
            self.rows = {
                i: {
                    "id": i,
                    "session_id": i,
                    "key": str(i).encode(),
                    "deleted": False,
                }
                for i in range(1, 514)
            }
            self.calls = []
            self.updates = 0

        @asynccontextmanager
        async def acquire(self):
            yield self

        @asynccontextmanager
        async def transaction(self):
            before = deepcopy(self.rows)
            self._in_transaction = True
            try:
                yield self
            except BaseException:
                self.rows = before
                raise
            finally:
                self._in_transaction = False

        async def fetch_all(self, sql, params):
            self.calls.append((sql, params))
            if "external_chat_key IN" in sql:
                return [
                    dict(row)
                    for row in self.rows.values()
                    if row["key"] in params[4:]
                ]
            rows = [dict(self.rows[i]) for i in params[1:]]
            if "swe_conversation_chats" in sql:
                self.rows[999] = {
                    "id": 999,
                    "session_id": 999,
                    "key": b"late",
                    "deleted": False,
                }
            return rows

        async def execute(self, sql, params):
            self.calls.append((sql, params))
            assert self._in_transaction
            self.updates += 1
            if self.updates == failure_at:
                raise RuntimeError("later update failed")
            for identity in params[4:]:
                self.rows[identity]["deleted"] = True
            return len(params) - 4

    db = Database()
    repo = repository(db, tmp_path)
    repo.retentions.assert_writes_allowed = AsyncMock()
    keys = [str(i) for i in range(1, 514)] + ["late"]
    if failure_at:
        with pytest.raises(RuntimeError, match="later update"):
            await repo.delete_chats(keys)
        assert not any(row["deleted"] for row in db.rows.values())
    else:
        assert await repo.delete_chats(keys)
        assert all(db.rows[i]["deleted"] for i in range(1, 514))
        assert not db.rows[999]["deleted"]
        assert db.updates == 3
    queries = [sql for sql, _ in db.calls]
    last_session = max(
        i
        for i, sql in enumerate(queries)
        if "swe_conversation_sessions" in sql
    )
    first_chat = min(
        i
        for i, sql in enumerate(queries)
        if "swe_conversation_chats" in sql and "FOR UPDATE" in sql
    )
    assert last_session < first_chat
    assert all(sql.count("%s") == len(params) for sql, params in db.calls)


@pytest.mark.parametrize("failure_at", [1, 2, 3])
async def test_prune_later_batch_sql_failure_cannot_commit_prefix(
    protocol_db,
    monkeypatch,
    failure_at,
):
    from swe.app.conversation_storage.archive import _prune
    from tests.unit.app.conversation_storage.test_publications import request
    from tests.unit.database.test_connection import Connection, Cursor

    updates = []
    original_execute = Cursor.execute

    async def execute(self, sql, params=None):
        await original_execute(self, sql, params)
        if sql.startswith("UPDATE swe_conversation_messages"):
            updates.append(params)
            if len(updates) == failure_at:
                raise RuntimeError("prune SQL failed")

    async def fetchall(self):
        sql, params = self.conn.calls[-1]
        assert "FOR UPDATE" in sql
        return [
            {
                "id": i,
                "service_unit_key": params[0],
                "session_id": 1,
                "layer": "memory",
                "context_epoch": 0,
            }
            for i in params[1:]
        ]

    monkeypatch.setattr(Cursor, "execute", execute)
    monkeypatch.setattr(Cursor, "fetchall", fetchall)
    with pytest.raises(RuntimeError, match="prune SQL failed"):
        async with protocol_db.transaction():
            await _prune(
                protocol_db,
                request(),
                tuple(range(1, 514)),
                context_epoch=0,
            )
    assert len(updates) == failure_at
    commands = [sql for sql, _ in Connection.instances[0].calls]
    assert "ROLLBACK" in commands
    assert "COMMIT" not in commands


async def test_share_read_keeps_full_identity_plan_with_narrow_pin_rows(
    tmp_path,
):
    from datetime import datetime
    from swe.app.chat_sharing.models import ChatShareRecord
    from swe.app.conversation_storage.bundle import (
        BundleStore,
        BundleBlock,
        new_operation_key,
    )
    from swe.app.conversation_storage.commit_plan import CommitPlan
    from swe.app.conversation_storage.retention import PinBatch
    from swe.app.conversation_storage.reference_queries import (
        PUBLICATION_LOCK_FIELDS,
    )
    from swe.app.conversation_storage.shares import ShareStorage
    from tests.unit.app.conversation_storage.test_shares import (
        mutation,
        share_request,
    )

    bundles = BundleStore(tmp_path)
    raw = b'{"chat_name":"shared","messages":[]}'
    index = bundles.publish(
        new_operation_key(),
        [BundleBlock("share", "share", raw)],
    )
    plan = mutation()
    row = {
        **share_request().scope.fields,
        "id": 1,
        "scope_kind": "workspace",
        "session_id": 1,
        "chat_id": 2,
        "turn_id": None,
        "state": "committed",
        "gc_state": "live",
        "kind": "share",
        "operation_key": b"operation",
        "relative_path": index.relative_path.encode(),
        "byte_length": index.byte_length,
        "result_metadata_text": CommitPlan(share=plan).serialize(),
    }
    pin = PinBatch(
        b"reader",
        ({name: row[name] for name in PUBLICATION_LOCK_FIELDS},),
    )
    retentions = SimpleNamespace(
        pin=AsyncMock(return_value=pin),
        finish_reader=AsyncMock(),
    )

    @asynccontextmanager
    async def transaction():
        yield

    db = SimpleNamespace(
        transaction=transaction,
        fetch_one=AsyncMock(return_value=row),
    )
    storage = ShareStorage(
        db,
        bundles,
        service_unit_id="schema-test-service",
        retention_factory=lambda *_args, **_kwargs: retentions,
    )
    extent = index.blocks["share"]
    storage.store.get = AsyncMock(
        return_value=ChatShareRecord(
            token=plan.token,
            chat_id=plan.chat_id,
            creator_id=plan.creator_id,
            snapshot_key=plan.snapshot_key,
            tenant_id=plan.tenant_id,
            service_unit_id="schema-test-service",
            created_at=datetime(2026, 10, 30),
            publication_id=1,
            byte_offset=extent.offset,
            byte_length=extent.length,
        ),
    )
    storage._finish_snapshot_read = AsyncMock()
    assert await storage.read_snapshot(plan.token, record_access=False) == {
        "chat_name": "shared",
        "messages": [],
    }
    assert "result_metadata_text" not in pin.publications[0]
    retentions.finish_reader.assert_awaited_once_with(pin)


async def test_read_only_lease_does_not_expose_mutations(protocol_db):
    from swe.app.conversation_storage.migration.manifest import SourceBoundary
    from swe.app.conversation_storage.migration.readonly import (
        ReadOnlyDatabase,
    )

    boundary = SourceBoundary(
        "snapshot",
        "2026-11-05T00:00:00Z",
        "old@sha256:" + "1" * 64,
        ("writers and GC stopped",),
    )
    db = ReadOnlyDatabase(protocol_db, boundary)
    async with db.acquire() as session:
        assert not hasattr(session, "execute")
        assert not hasattr(session, "insert_prepared")
        with pytest.raises(RuntimeError, match="read-only"):
            await session.fetch_all("DELETE FROM swe_conversation_messages")
        assert await session.fetch_all("SELECT 1") == [
            {"value": 1},
            {"value": 2},
        ]


@pytest.mark.parametrize("missing_last", [False, True])
async def test_share_proof_batches_exact_occurrences_and_checks_last_batch(
    missing_last,
):
    from swe.app.conversation_storage.reference_queries import (
        MESSAGE_LOCATOR_FIELDS,
    )
    from swe.app.conversation_storage.shares import lock_share_selection
    from tests.unit.app.conversation_storage.test_shares import (
        mutation,
        share_request,
    )

    request = share_request(base_state_revision=0, base_history_revision=0)
    snapshot = {
        "session_id": 1,
        "chat_id": 2,
        "state_revision": 0,
        "history_revision": 0,
        "session_history_high": 257,
        "archive_history_high": 0,
        "epoch": 0,
    }
    keys = tuple(str(i) for i in range(1, 258))
    proof = {
        "snapshot": snapshot,
        "questions": {
            str(i): {
                "message_id": i,
                "publication_id": 500,
                "layer": "memory",
                "status": "completed",
            }
            for i in range(1, 258)
        },
    }
    rows = {
        i: {
            **dict.fromkeys(MESSAGE_LOCATOR_FIELDS),
            "id": i,
            "service_unit_key": request.scope.sql_values[0],
            "session_id": 1,
            "source_msg_key": str(i).encode(),
            "role_key": b"user",
            "publication_id": 500,
            "layer": "memory",
            "current_visible": 1,
            "session_order": i,
            "archive_chat_id": None,
            "archive_order": None,
        }
        for i in range(1, 258)
    }
    if missing_last:
        rows.pop(257)

    class Database(ReferenceDatabase):
        async def fetch_one(self, query, params):
            return snapshot

        async def fetch_all(self, query, params):
            if "swe_conversation_turns" in query:
                self.calls.append((query, params))
                return (
                    []
                )  # Historical completed questions may have no typed Turn.
            return await super().fetch_all(query, params)

    db = Database(messages=rows)
    plan = mutation(selected_question_keys=keys, selection_proof=proof)
    if missing_last:
        with pytest.raises(ConflictError, match="occurrence changed"):
            await lock_share_selection(db, request, plan)
    else:
        await lock_share_selection(db, request, plan)
    assert len(db.calls) == 4
    assert all(query.endswith("FOR UPDATE") for query, _ in db.calls)
    assert all(len(params) <= 258 for _, params in db.calls)

# -*- coding: utf-8 -*-
"""Batching must retain each domain's locator and ownership checks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.errors import IntegrityError
from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.projection_references import (
    PROJECTION_FIELDS,
    TURN_FIELDS,
    ProjectionReferences,
    _snapshot_rows,
)
from swe.app.conversation_storage.publications import PublicationStore
from swe.app.conversation_storage.source_messages import (
    SOURCE_FIELDS,
    SourceMessages,
    snapshot_sources,
)
from swe.app.conversation_storage.message_installs import MessageInstall
from swe.app.conversation_storage.retention import RetentionStore
from tests.unit.app.conversation_storage.test_reference_queries import (
    ReferenceDatabase,
    SERVICE,
)

SCOPE = WorkspaceScope("t", "s", "a", service_unit_id=SERVICE.decode())


def message_row(identity):
    return {
        **dict.fromkeys(SOURCE_FIELDS, None),
        "id": identity,
        "service_unit_key": SERVICE,
        "session_id": 1,
        "publication_id": 1,
        "byte_offset": identity,
        "byte_length": 10,
        "layer": "memory",
        "source_msg_key": b"q",
        "source_occurrence_no": 0,
        "role_key": b"user",
        "kind_key": b"message",
        "context_epoch": 0,
        "current_visible": 1,
        "in_context": 1,
        "session_order": identity,
    }


def message_ref(identity):
    return {
        "message_id": identity,
        "publication_id": 1,
        "offset": identity,
        "length": 10,
    }


async def test_projection_capture_batches_4097_locators():
    db = ReferenceDatabase(
        messages={i: message_row(i) for i in range(1, 4098)},
    )
    snapshot = ProjectionReferences()
    await _snapshot_rows(
        SimpleNamespace(db=db),
        SimpleNamespace(scope=SCOPE, session_id=1),
        [message_ref(i) for i in db.tables["messages"]],
        snapshot,
    )
    assert snapshot.rows == db.tables["messages"]
    assert len(db.calls) == 17


async def test_projection_conflicting_duplicate_ref_is_not_hidden_by_dedup():
    db = ReferenceDatabase(messages={1: message_row(1)})
    with pytest.raises(IntegrityError, match="locator"):
        await _snapshot_rows(
            SimpleNamespace(db=db),
            SimpleNamespace(scope=SCOPE, session_id=1),
            [message_ref(1), {**message_ref(1), "offset": 999}],
            ProjectionReferences(),
        )
    assert len(db.calls) == 1


@pytest.mark.parametrize("kind", ["source", "projection"])
async def test_current_message_revalidation_batches_and_keeps_share_lock(kind):
    rows = {i: message_row(i) for i in range(1, 258)}
    snapshot = (
        SourceMessages(rows=rows)
        if kind == "source"
        else ProjectionReferences(rows=rows)
    )
    db = ReferenceDatabase(messages=rows)
    await snapshot.revalidate(db)
    assert len(db.calls) == 2
    assert all(query.endswith("FOR SHARE") for query, _ in db.calls)


async def test_plan_ownership_batches_message_and_publication_sets():
    messages = {i: message_row(i) for i in range(1, 258)}
    publication = {
        "id": 1,
        **SCOPE.fields,
        "scope_kind": "workspace",
        "session_id": 1,
    }
    db = ReferenceDatabase(messages=messages, publications={1: publication})
    store = PublicationStore(
        db,
        SimpleNamespace(),
        service_unit_id=SERVICE.decode(),
    )
    plan = SimpleNamespace(messages=[], prune_ids=list(messages), share=None)
    await store._validate_plan_reference_ownership(
        SimpleNamespace(scope=SCOPE, session_id=1),
        plan,
    )
    assert len(db.calls) == 3
    assert (
        sum("swe_conversation_publications" in query for query, _ in db.calls)
        == 1
    )


@pytest.mark.parametrize(
    "kind,field",
    [
        *(("source", field) for field in SOURCE_FIELDS),
        *(("projection", field) for field in PROJECTION_FIELDS),
    ],
)
async def test_each_original_message_comparison_field_is_retained(kind, field):
    expected = message_row(1)
    changed = {
        **expected,
        field: b"changed" if isinstance(expected[field], bytes) else 999,
    }
    db = ReferenceDatabase(messages={1: changed})
    snapshot = (
        SourceMessages(rows={1: expected})
        if kind == "source"
        else ProjectionReferences(rows={1: expected})
    )
    with pytest.raises(IntegrityError):
        await snapshot.revalidate(db)


@pytest.mark.parametrize("missing", [257, 513])
@pytest.mark.parametrize("kind", ["source", "projection"])
async def test_missing_middle_or_last_batch_locator_cannot_pass(kind, missing):
    rows = {i: message_row(i) for i in range(1, 514)}
    db = ReferenceDatabase(
        messages={i: row for i, row in rows.items() if i != missing},
    )
    snapshot = (
        SourceMessages(rows=rows)
        if kind == "source"
        else ProjectionReferences(rows=rows)
    )
    with pytest.raises(IntegrityError, match="changed"):
        await snapshot.revalidate(db)
    assert len(db.calls) == 3


def turn_row(identity=3):
    return {
        "id": identity,
        "service_unit_key": SERVICE,
        "session_id": 1,
        "chat_id": 2,
        "terminal_publication_id": 1,
        "history_status": "completed",
        "question_msg_key": b"q",
    }


@pytest.mark.parametrize("field", TURN_FIELDS)
async def test_turn_revalidation_keeps_original_fields_and_ordinary_read(
    field,
):
    expected = turn_row()
    db = ReferenceDatabase(
        turns={
            3: {
                **expected,
                field: (
                    b"changed" if isinstance(expected[field], bytes) else 999
                ),
            },
        },
    )
    with pytest.raises(IntegrityError):
        await ProjectionReferences(turns={3: expected}).revalidate(db)
    assert "FOR SHARE" not in db.calls[0][0]
    assert "FOR UPDATE" not in db.calls[0][0]


async def test_projection_dedup_preserves_occurrences_and_turns():
    db = ReferenceDatabase(
        messages={1: message_row(1), 2: message_row(2)},
        turns={3: turn_row()},
    )
    snapshot = ProjectionReferences()
    await _snapshot_rows(
        SimpleNamespace(db=db),
        SimpleNamespace(scope=SCOPE, session_id=1),
        [
            message_ref(2),
            message_ref(1),
            message_ref(2),
            {"turn_id": 3, "publication_id": 1, "block_name": "turn:q"},
            {"block_name": "same-publication"},
        ],
        snapshot,
    )
    assert set(snapshot.rows) == {1, 2}
    assert snapshot.turns == {3: turn_row()}
    assert len(db.calls) == 2
    assert db.calls[0][1] == (SERVICE, 1, 2)


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", 99),
        ("deleted_at", 1),
        ("byte_offset", 99),
        ("service_unit_key", b"other"),
    ],
)
async def test_projection_capture_rejects_foreign_or_changed_locator(
    field,
    value,
):
    db = ReferenceDatabase(messages={1: {**message_row(1), field: value}})
    with pytest.raises(IntegrityError):
        await _snapshot_rows(
            SimpleNamespace(db=db),
            SimpleNamespace(scope=SCOPE, session_id=1),
            [message_ref(1)],
            ProjectionReferences(),
        )


@pytest.mark.parametrize(
    "row",
    [
        None,
        {**turn_row(), "session_id": 99},
        {**turn_row(), "terminal_publication_id": 99},
    ],
)
async def test_projection_capture_rejects_missing_or_foreign_turn(row):
    db = ReferenceDatabase(turns={} if row is None else {3: row})
    with pytest.raises(IntegrityError, match="association"):
        await _snapshot_rows(
            SimpleNamespace(db=db),
            SimpleNamespace(scope=SCOPE, session_id=1),
            [{"turn_id": 3, "publication_id": 1}],
            ProjectionReferences(),
        )


def source_plan(ids):
    return SimpleNamespace(
        messages=[
            MessageInstall(
                f"source:{identity}",
                "archive",
                "user",
                source_message_id=identity,
                source_msg_key="q",
                archive_chat_id=2,
            )
            for identity in ids
        ],
    )


async def test_source_capture_batches_and_reads_only_after_transaction(
    monkeypatch,
):
    rows = {i: message_row(i) for i in range(1, 258)}
    db = ReferenceDatabase(messages=rows)
    request = SimpleNamespace(scope=SCOPE, session_id=1)

    async def read(*_args, **_kwargs):
        assert db.depth == 0
        return b"{}"

    batch = SimpleNamespace(
        publications=[
            {
                "id": 1,
                "session_id": 1,
                "relative_path": b"bundle",
                "byte_length": 1000,
            },
        ],
        read=AsyncMock(side_effect=read),
    )

    async def pin(_store, identities, *, kind):
        assert db.depth == 1
        assert identities == [1]
        assert kind == "reader"
        return batch

    monkeypatch.setattr(RetentionStore, "pin", pin)
    service = SimpleNamespace(
        db=db,
        bundles=SimpleNamespace(read_extent=object()),
    )
    snapshot = await snapshot_sources(service, request, source_plan(rows))
    assert snapshot.rows == rows
    assert len(db.calls) == 2
    assert list(snapshot.payloads) == [f"source:{i}" for i in rows]
    assert batch.read.await_count == len(rows)


@pytest.mark.parametrize("ids", [[1, 1], [2, 1], [1, 3]])
async def test_invalid_sources_rejected_before_pin_or_nas(monkeypatch, ids):
    db = ReferenceDatabase(messages={1: message_row(1), 2: message_row(2)})
    pin = AsyncMock(side_effect=AssertionError("invalid source reached pin"))
    monkeypatch.setattr(RetentionStore, "pin", pin)
    service = SimpleNamespace(db=db)
    with pytest.raises(IntegrityError):
        await snapshot_sources(
            service,
            SimpleNamespace(scope=SCOPE, session_id=1),
            source_plan(ids),
        )
    pin.assert_not_awaited()
    assert len(db.calls) == (0 if ids == [1, 1] else 1)


async def test_ownership_unions_source_prune_and_share_and_rechecks_next_phase():
    rows = {i: message_row(i) for i in (1, 2, 3)}
    publication = {
        "id": 1,
        **SCOPE.fields,
        "scope_kind": "workspace",
        "session_id": 1,
    }
    db = ReferenceDatabase(messages=rows, publications={1: publication})
    store = PublicationStore(
        db,
        SimpleNamespace(),
        service_unit_id=SERVICE.decode(),
    )
    request = SimpleNamespace(scope=SCOPE, session_id=1, chat_id=2)
    plan = SimpleNamespace(
        messages=[SimpleNamespace(source_message_id=3)],
        prune_ids=[1],
        share=SimpleNamespace(
            selection_proof={
                "snapshot": {"session_id": 1, "chat_id": 2},
                "questions": {"q": {"message_id": 2, "publication_id": 1}},
            },
        ),
    )
    await store._validate_plan_reference_ownership(request, plan)
    assert db.calls[0][1] == (SERVICE, 1, 2, 3)
    rows[3]["session_id"] = 99
    with pytest.raises(IntegrityError, match="Session"):
        await store._validate_plan_reference_ownership(request, plan)
    assert len(db.calls) == 3


@pytest.mark.parametrize(
    "field",
    [
        "service_unit_key",
        "tenant_key",
        "source_key",
        "agent_key",
        "session_id",
        "missing",
    ],
)
async def test_publication_ownership_retains_full_scope_and_session(field):
    publication = {
        "id": 1,
        **SCOPE.fields,
        "scope_kind": "workspace",
        "session_id": 1,
    }
    if field != "missing":
        publication[field] = 99 if field == "session_id" else b"foreign"
    db = ReferenceDatabase(
        messages={1: message_row(1)},
        publications={} if field == "missing" else {1: publication},
    )
    store = PublicationStore(
        db,
        SimpleNamespace(),
        service_unit_id=SERVICE.decode(),
    )
    with pytest.raises(IntegrityError):
        await store._validate_plan_reference_ownership(
            SimpleNamespace(scope=SCOPE, session_id=1),
            SimpleNamespace(messages=[], prune_ids=[1], share=None),
        )


async def test_missing_message_in_plan_ownership_is_not_a_partial_success():
    db = ReferenceDatabase(messages={1: message_row(1)})
    store = PublicationStore(
        db,
        SimpleNamespace(),
        service_unit_id=SERVICE.decode(),
    )
    with pytest.raises(IntegrityError, match="Message reference"):
        await store._validate_plan_reference_ownership(
            SimpleNamespace(scope=SCOPE, session_id=1),
            SimpleNamespace(messages=[], prune_ids=[1, 2], share=None),
        )
    assert len(db.calls) == 1

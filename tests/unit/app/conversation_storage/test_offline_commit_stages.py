# -*- coding: utf-8 -*-
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.bundle import BundleBlock, BundleStore
from swe.app.conversation_storage.errors import (
    ConflictError,
    IntegrityError,
    UnknownCommit,
)
from swe.app.conversation_storage.migration.install_records import (
    OfflineImportStore,
    plan_block,
)
from swe.app.conversation_storage.migration.parallel import MigrationIO
from swe.app.conversation_storage.publications import (
    CommitPlan,
    PublicationStore,
)
from swe.app.conversation_storage.shares import ShareInstall
from tests.unit.app.conversation_storage.test_publications import (
    PublicationDatabase,
    request,
)
from tests.unit.app.conversation_storage.test_offline_confirm_stages import (
    ForbiddenSurface,
)


class Database(PublicationDatabase):
    async def fetch_one(self, query, params=None):
        if "SELECT DATABASE()" in query:
            return {"schema_name": "test_conversation_evidence"}
        return await super().fetch_one(query, params)


async def prepared(tmp_path, kind="rows"):
    db = Database()
    db.config = SimpleNamespace(database="test_conversation_evidence")
    db.session["active_turn_id"] = None
    target = SimpleNamespace(
        schema=db.config.database,
        migration_id="migration",
    )
    bundles = BundleStore(tmp_path)
    store = OfflineImportStore(
        db,
        bundles,
        target,
        service_unit_id="schema-test-service",
    )
    operation = replace(request(), kind="offline_import")
    if kind == "turn":
        plan = CommitPlan(recovery_block="recovery")
        extra = [
            BundleBlock(
                "recovery",
                "recovery",
                b'{"turn_states":{"q":{"status":"completed"}}}',
            ),
        ]
    elif kind == "share":
        operation = replace(operation, kind="share_import", turn_id=None)
        db.chat["external_chat_key"] = b"chat"
        plan = CommitPlan(
            share=ShareInstall(
                token="token",
                chat_id="chat",
                creator_id="creator",
                tenant_id="t",
                snapshot_key="t/chat_shares/token.json",
                created_at="2026-01-01T00:00:00Z",
            ),
        )
        extra = [BundleBlock("share", "share", b'{"messages":[]}')]
    else:
        plan = CommitPlan(result_ids=(1,))
        extra = [BundleBlock("message", "message", b'{"id":"original"}')]
    await PublicationStore(
        db,
        bundles,
        service_unit_id="schema-test-service",
    ).reserve(operation, plan)
    rows = []
    if kind == "rows":
        rows = [
            {
                "table": "messages",
                "values": {
                    "id": 100,
                    "service_unit_key": "schema-test-service",
                    "session_id": 1,
                    "publication_id": {"publication": True},
                    "byte_offset": {"block": "message", "part": "offset"},
                    "byte_length": {"block": "message", "part": "length"},
                },
            },
        ]
    updates = []
    if kind == "turn":
        updates = [
            {
                "table": "turns",
                "values": {
                    "id": 3,
                    "admission_publication_id": {"publication": True},
                    "terminal_publication_id": {"publication": True},
                },
            },
        ]
    body = plan_block(
        target,
        rows,
        updates=updates,
        locks={"sessions": [1], "chats": [2], "turns": [3]},
    )
    index = bundles.publish(
        operation.operation_key,
        [body, *extra],
        scope=operation.scope,
        metadata={"operation_key": operation.operation_key},
    )
    await store._mark_durable(operation.operation_key, index)
    return store, operation, plan


@pytest.mark.parametrize("kind", ["rows", "turn", "share"])
async def test_three_plans_separate_sql_and_nas(tmp_path, monkeypatch, kind):
    store, operation, plan = await prepared(tmp_path, kind)
    installed = AsyncMock()
    if kind == "share":
        # Keep authority and common install real; isolate share-specific SQL
        # already exercised by the share/direct-import contract tests.
        monkeypatch.setattr(store, "_install_share", installed)
    bundles, database = store.bundles, store.db
    store.bundles = ForbiddenSurface()
    ticket = await store.prepare_commit(operation.operation_key)
    store.bundles, store.db = bundles, ForbiddenSurface()
    async with MigrationIO(2) as io:
        evidence = await store.read_commit_evidence(ticket, io)
    store.db, store.bundles = database, ForbiddenSurface()
    result = await store.commit_evidence(evidence)
    assert result["state"] == "committed"
    if kind == "rows":
        assert database.messages == [
            (
                100,
                b"schema-test-service",
                1,
                1,
                evidence.index.blocks["message"].offset,
                evidence.index.blocks["message"].length,
            ),
        ]
    if kind == "share":
        installed.assert_awaited_once()
    if kind == "turn":
        assert any(
            query.startswith(
                "UPDATE swe_conversation_turns SET admission_publication_id=%s,terminal_publication_id=%s",
            )
            for query in database.queries
        )
    assert not store._commit_stages.tickets
    assert not store._commit_stages.receipts


@pytest.mark.parametrize(
    "mutation",
    ["ticket", "evidence", "index", "row_length", "row_plan"],
)
async def test_commit_evidence_rejects_mutation(tmp_path, mutation):
    store, operation, _ = await prepared(tmp_path)
    ticket = await store.prepare_commit(operation.operation_key)
    if mutation == "ticket":
        async with MigrationIO(1) as io:
            with pytest.raises(IntegrityError, match="ticket"):
                await store.read_commit_evidence(
                    replace(ticket, publication_id=9),
                    io,
                )
        return
    async with MigrationIO(1) as io:
        evidence = await store.read_commit_evidence(ticket, io)
    if mutation == "evidence":
        evidence = replace(evidence, import_plan=b"{}")
    elif mutation == "index":
        evidence.index.metadata["operation_key"] = "f" * 64
    elif mutation == "row_length":
        store.db.rows[operation.operation_key.encode()]["byte_length"] += 1
    else:
        store.db.rows[operation.operation_key.encode()][
            "result_metadata_text"
        ] = CommitPlan().serialize()
    with pytest.raises((IntegrityError, ConflictError)):
        await store.commit_evidence(evidence)
    assert (
        store.db.rows[operation.operation_key.encode()]["state"] == "prepared"
    )


@pytest.mark.parametrize(
    "failure",
    [ConnectionError("lost"), asyncio.CancelledError()],
)
async def test_commit_evidence_preserves_unknown_commit(tmp_path, failure):
    store, operation, _ = await prepared(tmp_path)
    ticket = await store.prepare_commit(operation.operation_key)
    async with MigrationIO(1) as io:
        evidence = await store.read_commit_evidence(ticket, io)
    # The final helper owns the transaction; emulate a lost response after its commit.
    original = store._commit_install

    async def lost_response(*args, **kwargs):
        await original(*args, **kwargs)
        raise failure

    store._commit_install = lost_response
    with pytest.raises(UnknownCommit) as raised:
        await store.commit_evidence(evidence)
    assert raised.value.operation_key == operation.operation_key
    assert raised.value.__cause__ is failure
    assert (
        store.db.rows[operation.operation_key.encode()]["state"] == "committed"
    )


@pytest.mark.parametrize("mixed", [False, True])
async def test_unsupported_or_mixed_plan_requires_original_protocol(
    tmp_path,
    mixed,
):
    store, operation, _ = await prepared(tmp_path)
    plan = (
        CommitPlan(recovery_block="recovery", result_ids=(1,))
        if mixed
        else CommitPlan()
    )
    store.db.rows[operation.operation_key.encode()][
        "result_metadata_text"
    ] = plan.serialize()
    with pytest.raises(IntegrityError, match="general publication protocol"):
        await store.prepare_commit(operation.operation_key)


async def test_projection_recovery_is_rejected_instead_of_skipping_pins(
    tmp_path,
    monkeypatch,
):
    import json
    from swe.app.conversation_storage.session_projection import FORMAT

    store, operation, _ = await prepared(tmp_path, "turn")
    ticket = await store.prepare_commit(operation.operation_key)
    original = store.bundles.read_extent

    def projected(path, offset, length, **kwargs):
        index = store.bundles.inspect(path)
        if offset == index.blocks["recovery"].offset:
            return json.dumps({"format": FORMAT}).encode()
        return original(path, offset, length, **kwargs)

    monkeypatch.setattr(store.bundles, "read_extent", projected)
    async with MigrationIO(1) as io:
        with pytest.raises(
            IntegrityError,
            match="general projection protocol",
        ):
            await store.read_commit_evidence(ticket, io)
    assert not store._commit_stages.receipts


async def test_locked_publication_length_is_revalidated(tmp_path, monkeypatch):
    from swe.app.conversation_storage.source_messages import SourceMessages

    store, operation, _ = await prepared(tmp_path)
    ticket = await store.prepare_commit(operation.operation_key)
    async with MigrationIO(1) as io:
        evidence = await store.read_commit_evidence(ticket, io)
    original = SourceMessages.lock_current

    async def changed(sources, service, initial, key):
        service.db.rows[key.encode()]["byte_length"] += 1
        return await original(sources, service, initial, key)

    monkeypatch.setattr(SourceMessages, "lock_current", changed)
    with pytest.raises(IntegrityError, match="publication changed"):
        await store.commit_evidence(evidence)
    assert (
        store.db.rows[operation.operation_key.encode()]["state"] == "prepared"
    )


async def test_raw_first_turn_stays_raw_without_frozen_terminal_refs(
    tmp_path,
    monkeypatch,
):
    from swe.app.conversation_storage.projection_publication import (
        prepare_recovery_blocks,
    )

    store, operation, plan = await prepared(tmp_path, "turn")
    original = store.db.fetch_all

    async def fetch_all(query, params=None):
        if "terminal_publication_id IS NOT NULL" in query:
            return []
        return await original(query, params)

    monkeypatch.setattr(store.db, "fetch_all", fetch_all)
    row = await store.get(operation.operation_key)
    block = BundleBlock(
        "recovery",
        "recovery",
        b'{"turn_states":{"new":{"status":"completed"}}}',
    )
    assert await prepare_recovery_blocks(store, row, plan, [block]) == [block]
    ticket = await store.prepare_commit(operation.operation_key)
    async with MigrationIO(1) as io:
        evidence = await store.read_commit_evidence(ticket, io)
    assert evidence.recovery.startswith(b'{"turn_states":')


async def test_direct_journal_batch_finishes_inside_install_transaction(
    tmp_path,
):
    from swe.app.conversation_storage.migration.direct_import import (
        DirectImportStore,
    )

    original, operation, _ = await prepared(tmp_path)
    calls = []

    async def record(position, state, **kwargs):
        assert original.db.transaction_depth > 0
        calls.append((position, state, kwargs))

    store = DirectImportStore(
        original.db,
        original.bundles,
        original.target,
        service_unit_id="schema-test-service",
        journal=SimpleNamespace(set_batch_state=record),
    )
    store.batch_positions[operation.operation_key] = "source/batch"
    ticket = await store.prepare_commit(operation.operation_key)
    async with MigrationIO(1) as io:
        evidence = await store.read_commit_evidence(ticket, io)
    await store.commit_evidence(evidence)
    assert calls == [("source/batch", "committed", {"mirror": False})]

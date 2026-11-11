# -*- coding: utf-8 -*-
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from swe.app.conversation_storage.bundle import BundleBlock, BundleStore
from swe.app.conversation_storage.errors import (
    ConflictError,
    IntegrityError,
    UnknownCommit,
)
from swe.app.conversation_storage.migration.install_records import (
    OfflineImportStore,
)
from swe.app.conversation_storage.migration.parallel import MigrationIO
from swe.app.conversation_storage.publications import (
    CommitPlan,
    PublicationStore,
)
from tests.unit.app.conversation_storage.test_publications import (
    PublicationDatabase,
    request,
)


async def prepared_store(tmp_path):
    database = PublicationDatabase()
    bundles = BundleStore(tmp_path)
    store = OfflineImportStore(
        database,
        bundles,
        SimpleNamespace(),
        service_unit_id="schema-test-service",
    )
    operation = request()
    await PublicationStore(
        database,
        bundles,
        service_unit_id="schema-test-service",
    ).reserve(operation, CommitPlan())
    index = bundles.publish(
        operation.operation_key,
        [BundleBlock("root", "recovery", b"original")],
        scope=operation.scope,
        metadata={"operation_key": operation.operation_key},
    )
    return store, operation, index


class ForbiddenSurface:
    def __getattr__(self, name):
        raise AssertionError("forbidden phase access: " + name)


async def test_confirmation_stages_keep_sql_and_nas_separate(tmp_path):
    store, operation, original = await prepared_store(tmp_path)
    bundles = store.bundles
    store.bundles = ForbiddenSurface()
    ticket = await store.prepare_confirm_bundle(operation.operation_key)
    store.bundles = bundles
    database = store.db
    store.db = ForbiddenSurface()
    async with MigrationIO(workers=1) as io:
        receipt = await store.confirm_bundle_nas(ticket, io)
    store.db = database
    store.bundles = ForbiddenSurface()
    assert await store.commit_confirm_bundle(receipt) == original
    assert database.rows[operation.operation_key.encode()]["durable_at"]


@pytest.mark.parametrize("field", ["scope", "path", "operation"])
async def test_forged_ticket_is_rejected_before_nas(tmp_path, field):
    store, operation, _ = await prepared_store(tmp_path)
    ticket = await store.prepare_confirm_bundle(operation.operation_key)
    if field == "scope":
        forged = replace(
            ticket,
            request=replace(
                ticket.request,
                scope=replace(ticket.request.scope, tenant_id="other"),
            ),
        )
    elif field == "path":
        forged = replace(ticket, relative_path="foreign.bundle")
    else:
        forged = replace(
            ticket,
            request=replace(
                ticket.request,
                operation_key="f" * 64,
            ),
        )
    io = SimpleNamespace(call=Mock(side_effect=AssertionError("NAS called")))
    with pytest.raises(IntegrityError, match="ticket"):
        await store.confirm_bundle_nas(forged, io)
    io.call.assert_not_called()


@pytest.mark.parametrize(
    "mutation",
    ["index", "metadata", "row_scope", "row_path", "row_id", "row_plan"],
)
async def test_final_sql_rejects_receipt_or_prepared_row_changes(
    tmp_path,
    mutation,
):
    store, operation, _ = await prepared_store(tmp_path)
    ticket = await store.prepare_confirm_bundle(operation.operation_key)
    async with MigrationIO(workers=1) as io:
        receipt = await store.confirm_bundle_nas(ticket, io)
    if mutation == "index":
        receipt = replace(receipt, index=replace(receipt.index, byte_length=1))
    elif mutation == "metadata":
        receipt.index.metadata["operation_key"] = "f" * 64
    else:
        row = store.db.rows[operation.operation_key.encode()]
        key, value = {
            "row_scope": ("tenant_key", b"foreign"),
            "row_path": ("relative_path", b"foreign.bundle"),
            "row_id": ("id", 999),
            "row_plan": (
                "result_metadata_text",
                CommitPlan(result_ids=(999,)).serialize(),
            ),
        }[mutation]
        row[key] = value
    store.bundles = ForbiddenSurface()
    with pytest.raises((ConflictError, IntegrityError)):
        await store.commit_confirm_bundle(receipt)
    assert not store.db.rows[operation.operation_key.encode()].get(
        "durable_at",
    )


async def test_compatibility_confirmation_returns_original_index(tmp_path):
    store, operation, original = await prepared_store(tmp_path)
    assert await store.confirm_bundle(operation.operation_key) == original


@pytest.mark.parametrize("field", ["path", "operation"])
async def test_nas_result_must_match_frozen_ticket(
    tmp_path,
    monkeypatch,
    field,
):
    store, operation, original = await prepared_store(tmp_path)
    ticket = await store.prepare_confirm_bundle(operation.operation_key)
    wrong = (
        replace(original, relative_path="foreign.bundle")
        if field == "path"
        else replace(original, metadata={"operation_key": "f" * 64})
    )
    monkeypatch.setattr(store.bundles, "confirm_durable", lambda path: wrong)
    async with MigrationIO(workers=1) as io:
        with pytest.raises(IntegrityError, match="identity or path"):
            await store.confirm_bundle_nas(ticket, io)
    assert not store.db.rows[operation.operation_key.encode()].get(
        "durable_at",
    )


async def test_final_sql_revalidates_reference_ownership(
    tmp_path,
    monkeypatch,
):
    store, operation, _ = await prepared_store(tmp_path)
    ticket = await store.prepare_confirm_bundle(operation.operation_key)
    async with MigrationIO(workers=1) as io:
        receipt = await store.confirm_bundle_nas(ticket, io)
    validation = AsyncMock(side_effect=IntegrityError("reference changed"))
    monkeypatch.setattr(store, "_validate_reference_ownership", validation)
    store.bundles = ForbiddenSurface()
    with pytest.raises(IntegrityError, match="reference changed"):
        await store.commit_confirm_bundle(receipt)
    validation.assert_awaited_once()
    assert not store.db.rows[operation.operation_key.encode()].get(
        "durable_at",
    )


@pytest.mark.parametrize(
    "failure",
    [ConnectionError("commit response lost"), asyncio.CancelledError()],
)
async def test_final_transaction_exit_failure_preserves_unknown_outcome(
    tmp_path,
    monkeypatch,
    failure,
):
    store, operation, _ = await prepared_store(tmp_path)
    ticket = await store.prepare_confirm_bundle(operation.operation_key)
    async with MigrationIO(workers=1) as io:
        receipt = await store.confirm_bundle_nas(ticket, io)
    transaction = store.db.transaction

    @asynccontextmanager
    async def lose_outer_commit_response():
        async with transaction() as session:
            yield session
        # Only the outermost exit actually commits on DatabaseConnection.
        if store.db.transaction_depth == 0:
            raise failure

    monkeypatch.setattr(store.db, "transaction", lose_outer_commit_response)
    with pytest.raises(UnknownCommit) as raised:
        await store.commit_confirm_bundle(receipt)

    assert raised.value.operation_key == operation.operation_key
    assert raised.value.__cause__ is failure
    assert store.db.rows[operation.operation_key.encode()]["durable_at"]
    assert id(ticket) in store._confirm_tickets
    assert id(receipt) in store._confirm_receipts


async def test_final_transaction_exit_preserves_storage_error(
    tmp_path,
    monkeypatch,
):
    store, operation, _ = await prepared_store(tmp_path)
    ticket = await store.prepare_confirm_bundle(operation.operation_key)
    async with MigrationIO(workers=1) as io:
        receipt = await store.confirm_bundle_nas(ticket, io)
    transaction = store.db.transaction
    failure = IntegrityError("final transaction failed")

    @asynccontextmanager
    async def fail_outer_exit():
        async with transaction() as session:
            yield session
        if store.db.transaction_depth == 0:
            raise failure

    monkeypatch.setattr(store.db, "transaction", fail_outer_exit)
    with pytest.raises(IntegrityError) as raised:
        await store.commit_confirm_bundle(receipt)
    assert raised.value is failure

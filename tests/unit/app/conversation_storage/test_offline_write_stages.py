# -*- coding: utf-8 -*-
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from io import BytesIO
import threading
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
from swe.app.conversation_storage.migration import offline_write
from swe.app.conversation_storage.publications import (
    CommitPlan,
    PublicationStore,
)
from tests.unit.app.conversation_storage.test_offline_confirm_stages import (
    ForbiddenSurface,
)
from tests.unit.app.conversation_storage.test_publications import (
    PublicationDatabase,
    request,
)


async def prepared_store(tmp_path):
    database = PublicationDatabase()
    store = OfflineImportStore(
        database,
        BundleStore(tmp_path),
        SimpleNamespace(),
        service_unit_id="schema-test-service",
    )
    operation = request()
    await PublicationStore(
        database,
        store.bundles,
        service_unit_id="schema-test-service",
    ).reserve(operation, CommitPlan())
    return store, operation


async def published(store, operation):
    ticket, blocks = await store.prepare_write_bundle(
        operation.operation_key,
        [BundleBlock("body", "message", b"original")],
    )
    async with MigrationIO(1) as io:
        receipt = await store.publish_prepared_bundle(ticket, blocks, io)
    return ticket, receipt


async def test_write_stages_separate_sql_nas_and_keep_stream(tmp_path):
    store, operation = await prepared_store(tmp_path)
    bundles = store.bundles
    store.bundles = ForbiddenSurface()
    stream = BytesIO(b"borrowed")
    block = BundleBlock("body", "message", stream, length=8)
    ticket, blocks = await store.prepare_write_bundle(
        operation.operation_key,
        [block],
    )
    assert blocks == (block,)
    assert blocks[0].content is stream
    assert stream.tell() == 0
    store.bundles = bundles
    database = store.db
    store.db = ForbiddenSurface()
    async with MigrationIO(1) as io:
        receipt = await store.publish_prepared_bundle(ticket, blocks, io)
    assert not stream.closed
    store.db = database
    store.bundles = ForbiddenSurface()
    assert await store.commit_written_bundle(receipt) == receipt.index
    assert database.rows[operation.operation_key.encode()]["durable_at"]
    assert id(ticket) not in store._confirm_tickets
    assert id(ticket) not in store._write_stages.prepared
    assert id(receipt) not in store._confirm_receipts
    with pytest.raises(IntegrityError):
        await store.commit_written_bundle(receipt)


@pytest.mark.parametrize("mutation", ["copy", "scope", "operation", "blocks"])
async def test_write_rejects_forged_ticket_or_blocks(tmp_path, mutation):
    store, operation = await prepared_store(tmp_path)
    ticket, blocks = await store.prepare_write_bundle(
        operation.operation_key,
        [BundleBlock("body", "message", b"body")],
    )
    if mutation == "copy":
        ticket = replace(ticket)
    elif mutation == "scope":
        ticket = replace(
            ticket,
            request=replace(
                ticket.request,
                scope=replace(ticket.request.scope, tenant_id="other"),
            ),
        )
    elif mutation == "operation":
        ticket = replace(
            ticket,
            request=replace(
                ticket.request,
                operation_key="f" * 64,
            ),
        )
    else:
        blocks = (BundleBlock("body", "message", b"changed"),)
    io = SimpleNamespace(call=Mock(side_effect=AssertionError("NAS called")))
    with pytest.raises(IntegrityError):
        await store.publish_prepared_bundle(ticket, blocks, io)
    io.call.assert_not_called()


@pytest.mark.parametrize(
    "mutation",
    ["scope", "operation", "plan", "id", "path", "state", "receipt"],
)
async def test_written_commit_revalidates_row_and_receipt(tmp_path, mutation):
    store, operation = await prepared_store(tmp_path)
    _, receipt = await published(store, operation)
    row = store.db.rows[operation.operation_key.encode()]
    if mutation == "receipt":
        receipt = replace(receipt, index=replace(receipt.index, byte_length=1))
    else:
        key, value = {
            "scope": ("tenant_key", b"other"),
            "operation": ("operation_key", b"f" * 64),
            "plan": (
                "result_metadata_text",
                CommitPlan(result_ids=(99,)).serialize(),
            ),
            "id": ("id", 99),
            "path": ("relative_path", b"foreign.bundle"),
            "state": ("state", "aborted"),
        }[mutation]
        row[key] = value
    store.bundles = ForbiddenSurface()
    with pytest.raises((ConflictError, IntegrityError)):
        await store.commit_written_bundle(receipt)
    assert not store.db.rows[operation.operation_key.encode()].get(
        "durable_at",
    )


@pytest.mark.parametrize(
    "failure",
    [ConnectionError("lost commit"), asyncio.CancelledError()],
)
async def test_written_outer_transaction_failure_is_unknown(
    tmp_path,
    monkeypatch,
    failure,
):
    store, operation = await prepared_store(tmp_path)
    ticket, receipt = await published(store, operation)
    transaction = store.db.transaction

    @asynccontextmanager
    async def fail_outer_exit():
        async with transaction() as session:
            yield session
        if store.db.transaction_depth == 0:
            raise failure

    monkeypatch.setattr(store.db, "transaction", fail_outer_exit)
    with pytest.raises(UnknownCommit) as error:
        await store.commit_written_bundle(receipt)
    assert error.value.__cause__ is failure
    assert id(ticket) in store._confirm_tickets
    assert id(receipt) in store._confirm_receipts


async def test_cancelled_publish_drains_before_unknown(tmp_path, monkeypatch):
    store, operation = await prepared_store(tmp_path)
    ticket, blocks = await store.prepare_write_bundle(
        operation.operation_key,
        [BundleBlock("body", "message", b"body")],
    )
    entered, release = threading.Event(), threading.Event()
    publish = store.bundles.publish

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return publish(*args, **kwargs)

    monkeypatch.setattr(store.bundles, "publish", delayed)
    async with MigrationIO(1) as io:
        task = asyncio.create_task(
            store.publish_prepared_bundle(ticket, blocks, io),
        )
        while not entered.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(UnknownCommit):
            await task
    assert store.bundles.inspect(ticket.relative_path)
    assert not store._confirm_receipts
    assert not store.db.rows[operation.operation_key.encode()].get(
        "durable_at",
    )


async def test_prepare_freezes_sequence_before_sql(
    tmp_path,
    monkeypatch,
):
    store, operation = await prepared_store(tmp_path)
    block = BundleBlock("root", "message", b"before")
    supplied = [block]
    validate = store._validate_prepared_references

    async def references(row):
        plan = await validate(row)
        supplied.clear()
        return plan

    monkeypatch.setattr(store, "_validate_prepared_references", references)
    ticket, blocks = await store.prepare_write_bundle(
        operation.operation_key,
        supplied,
    )
    assert blocks == (block,)
    assert store._confirm_tickets[id(ticket)] is ticket


async def test_prepare_cancel_does_not_issue_ticket(tmp_path, monkeypatch):
    store, operation = await prepared_store(tmp_path)
    monkeypatch.setattr(
        store,
        "get",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )
    with pytest.raises(UnknownCommit):
        await store.prepare_write_bundle(operation.operation_key, [])
    assert not store._confirm_tickets
    assert not store._write_stages.prepared


async def test_default_write_bundle_still_uses_existing_protocol(tmp_path):
    store, operation = await prepared_store(tmp_path)
    index = await store.write_bundle(
        operation.operation_key,
        [BundleBlock("body", "message", b"body")],
    )
    assert index.metadata["operation_key"] == operation.operation_key
    assert store.db.rows[operation.operation_key.encode()]["durable_at"]
    assert not store._confirm_tickets
    assert not store._write_stages.prepared


async def test_turn_recovery_no_candidate_uses_raw_write_ticket(tmp_path):
    store, operation = await prepared_store(tmp_path)
    plan = CommitPlan(recovery_block="recovery")
    row = store.db.rows[operation.operation_key.encode()]
    row["kind"] = "offline_import"
    row["session_id"] = 1
    row["turn_id"] = 3
    store._validate_prepared_references = AsyncMock(return_value=plan)
    store.db.fetch_all = AsyncMock(return_value=[])
    blocks = [
        BundleBlock(
            "recovery",
            "recovery",
            b'{"turn_states":{"new":{"status":"done"}}}',
        ),
    ]

    ticket, prepared = await store.prepare_write_bundle(
        operation.operation_key,
        blocks,
    )

    assert ticket.request.turn_id == 3
    assert prepared == tuple(blocks)
    store.db.fetch_all.assert_awaited_once()


async def turn_recovery_fixture(tmp_path):
    store, operation = await prepared_store(tmp_path)
    plan = CommitPlan(recovery_block="recovery")
    row = store.db.rows[operation.operation_key.encode()]
    row.update(session_id=1, turn_id=3, kind="offline_import")
    store._validate_prepared_references = AsyncMock(return_value=plan)
    turn = {
        "id": 3,
        "service_unit_key": b"schema-test-service",
        "session_id": 1,
        "question_msg_key": b"question",
        "history_status": "done",
        "terminal_publication_id": 9,
    }
    publication = {
        "id": 9,
        "service_unit_key": b"schema-test-service",
        "session_id": 1,
        "turn_id": 3,
        "operation_key": b"a" * 64,
        "relative_path": BundleStore.path_for(
            "a" * 64,
            operation.scope,
        ).encode(),
        "byte_length": 100,
        "state": "committed",
        "gc_state": "live",
    }
    store.db.fetch_all = AsyncMock(return_value=[turn])
    original_fetch = store.db.fetch_one

    async def fetch(query, params=None):
        if "swe_conversation_turns WHERE id=" in query:
            return dict(turn)
        return await original_fetch(query, params)

    store.db.fetch_one = fetch
    batch = SimpleNamespace(
        retention_key=b"reader-key",
        publications=(publication,),
    )
    retentions = SimpleNamespace(
        pin=AsyncMock(return_value=batch),
        finish_reader=AsyncMock(),
        _lock_publications=AsyncMock(return_value=[publication]),
        _validate_members=AsyncMock(),
    )
    import swe.app.conversation_storage.migration.offline_write as module
    from unittest.mock import patch

    with patch.object(module, "RetentionStore", return_value=retentions):
        candidate = await store.prepare_write_bundle(
            operation.operation_key,
            [
                BundleBlock(
                    "recovery",
                    "recovery",
                    b'{"turn_states":{"question":{"status":"done"}}}',
                ),
            ],
        )
    assert isinstance(candidate, module.TurnRecoveryTicket)
    return store, operation, turn, batch, retentions, candidate


@pytest.mark.parametrize(
    "scenario",
    [
        "projection",
        "missing",
        "different",
        "changed",
        "cleanup",
        "forged",
        "cancel",
    ],
)
async def test_turn_recovery_candidate_reads_and_owner_revalidates(
    tmp_path,
    scenario,
):
    store, operation, turn, batch, retentions, candidate = (
        await turn_recovery_fixture(tmp_path)
    )
    from swe.app.conversation_storage.migration import offline_write as module

    if scenario == "cancel":
        entered, release = threading.Event(), threading.Event()
        original_inspect = store.bundles.inspect

        def delayed_inspect(*args, **kwargs):
            entered.set()
            assert release.wait(3)
            return original_inspect(*args, **kwargs)

        store.bundles.inspect = delayed_inspect
        async with MigrationIO(1) as io:
            reading = asyncio.create_task(
                store._write_stages.read_recovery(candidate, io),
            )
            while not entered.is_set():
                await asyncio.sleep(0)
            reading.cancel()
            await asyncio.sleep(0)
            assert not reading.done()
            retentions.finish_reader.assert_not_awaited()
            release.set()
            with pytest.raises(UnknownCommit):
                await reading
        await store._write_stages.close()
        retentions.finish_reader.assert_awaited_once_with(batch)
        assert not store._write_stages.recoveries
        return
    if scenario == "cleanup":
        await store._write_stages.close()
        retentions.finish_reader.assert_awaited_once_with(batch)
        assert not store._write_stages.recoveries
        return
    nas_calls = []
    store.bundles.inspect = lambda *args, **kwargs: SimpleNamespace(
        metadata={"operation_key": "a" * 64},
        blocks=(
            {}
            if scenario == "missing"
            else {
                "turn:question": SimpleNamespace(
                    kind="turn",
                    offset=0,
                    length=9,
                ),
            }
        ),
    )
    store.bundles.read_extent = lambda *args, **kwargs: nas_calls.append(
        threading.current_thread().name,
    ) or (
        b'{"status":"other"}'
        if scenario == "different"
        else b'{"status":"done"}'
    )
    async with MigrationIO(1) as io:
        evidence = await store._write_stages.read_recovery(candidate, io)
    store.bundles = ForbiddenSurface()
    if scenario != "missing":
        assert nas_calls and nas_calls[0].startswith("migration-io")
    if scenario == "changed":
        turn["terminal_publication_id"] = 99
    if scenario == "forged":
        evidence = replace(evidence)
    if scenario in ("projection", "changed", "forged"):
        message = {
            "projection": "general projection protocol",
            "changed": "cold Turn changed",
            "forged": "altered recovery evidence",
        }[scenario]
        with pytest.raises(IntegrityError, match=message):
            await store._write_stages.finish_recovery(candidate, evidence)
    else:
        ticket, prepared = await store._write_stages.finish_recovery(
            candidate,
            evidence,
        )
        assert (
            prepared[0].content
            == b'{"turn_states":{"question":{"status":"done"}}}'
        )
        assert store._confirm_tickets[id(ticket)] is ticket
    retentions.finish_reader.assert_awaited_once_with(batch)
    assert not store._write_stages.recoveries
    assert not store._write_stages.recovery_evidence

# -*- coding: utf-8 -*-
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.bundle import BundleBlock
from swe.app.conversation_storage.chat_mutations import load_metadata_json
from swe.app.conversation_storage.errors import (
    ConflictError,
    IntegrityError,
    UnknownCommit,
)
from swe.app.conversation_storage.migration.offline_verify import (
    OfflineVerifyStages,
)
from swe.app.conversation_storage.migration.parallel import MigrationIO
from tests.unit.app.conversation_storage.test_offline_commit_stages import (
    prepared,
)
from tests.unit.app.conversation_storage.test_offline_confirm_stages import (
    ForbiddenSurface,
)


async def committed(tmp_path, kind="rows"):
    store, operation, plan = await prepared(tmp_path, kind)
    row = await store.get(operation.operation_key)
    expected = await store._load_plan(row)
    index = store.bundles.inspect(row["relative_path"].decode())
    blocks = [
        BundleBlock(
            name,
            extent.kind,
            store.bundles.read_extent(
                index.relative_path,
                extent.offset,
                extent.length,
            ),
        )
        for name, extent in index.blocks.items()
        if name != "import_plan"
    ]
    if kind == "share":
        store._install_share = AsyncMock()
    await store.commit(operation.operation_key)
    fetch = store.db.fetch_one

    async def fetch_one(query, params=None):
        if "FROM swe_conversation_messages" in query:
            keys = (
                "id",
                "service_unit_key",
                "session_id",
                "publication_id",
                "byte_offset",
                "byte_length",
            )
            return next(
                (
                    dict(zip(keys, values))
                    for values in store.db.messages
                    if values[0] == params[0]
                ),
                None,
            )
        return await fetch(query, params)

    store.db.fetch_one = fetch_one
    return store, operation, plan, expected, blocks


async def issue(stages, operation, plan, expected, blocks):
    return await stages.prepare(operation, 1, plan, expected, blocks)


async def test_verification_stages_separate_sql_and_nas(tmp_path):
    store, operation, plan, expected, blocks = await committed(tmp_path)
    stages = OfflineVerifyStages(store)
    bundles, db = store.bundles, store.db
    store.bundles = ForbiddenSurface()
    ticket = await issue(stages, operation, plan, expected, blocks)
    expected["rows"].clear()
    blocks.clear()
    store.bundles, store.db = bundles, ForbiddenSurface()
    async with MigrationIO(1) as io:
        evidence = await stages.read(ticket, io)
    store.db, store.bundles = db, ForbiddenSurface()
    result = await stages.verify(evidence)
    assert result["state"] == "committed"
    assert not stages.tickets and not stages.receipts


@pytest.mark.parametrize(
    "mutation",
    ["ticket", "evidence", "index", "length", "state", "plan", "target"],
)
async def test_verification_rejects_altered_identity(tmp_path, mutation):
    store, operation, plan, expected, blocks = await committed(tmp_path)
    stages = OfflineVerifyStages(store)
    ticket = await issue(stages, operation, plan, expected, blocks)
    if mutation == "ticket":
        async with MigrationIO(1) as io:
            with pytest.raises(IntegrityError, match="ticket"):
                await stages.read(replace(ticket, publication_id=8), io)
        return
    async with MigrationIO(1) as io:
        evidence = await stages.read(ticket, io)
    row = store.db.rows[operation.operation_key.encode()]
    if mutation == "evidence":
        evidence = replace(evidence, import_plan=b"{}")
    elif mutation == "index":
        evidence.index.metadata.clear()
    elif mutation == "length":
        row["byte_length"] += 1
    elif mutation == "state":
        row["state"] = "prepared"
    elif mutation == "plan":
        row["result_metadata_text"] = "{}"
    else:
        store.target = SimpleNamespace(
            schema=store.target.schema,
            migration_id="other",
        )
    with pytest.raises((IntegrityError, ConflictError)):
        await stages.verify(evidence)


@pytest.mark.parametrize(
    "mutation",
    ["import_plan", "body", "recovery", "kind"],
)
async def test_nas_evidence_requires_exact_expected_contents(
    tmp_path,
    mutation,
):
    kind = "turn" if mutation == "recovery" else "rows"
    store, operation, plan, expected, blocks = await committed(tmp_path, kind)
    if mutation == "import_plan":
        expected["updates"] = []
        expected["rows"] = []
    else:
        blocks[0] = replace(
            blocks[0],
            **(
                {"kind": "other"}
                if mutation == "kind"
                else {"content": b"different"}
            ),
        )
    stages = OfflineVerifyStages(store)
    ticket = await issue(stages, operation, plan, expected, blocks)
    async with MigrationIO(1) as io:
        with pytest.raises(IntegrityError):
            await stages.read(ticket, io)
    assert not stages.receipts


async def test_nas_evidence_rejects_unexpected_bundle_blocks(tmp_path):
    store, operation, plan, expected, blocks = await committed(tmp_path)
    stages = OfflineVerifyStages(store)
    ticket = await issue(stages, operation, plan, expected, blocks)
    inspect = store.bundles.inspect

    def inspect_with_extra_block(*args, **kwargs):
        index = inspect(*args, **kwargs)
        return replace(
            index,
            blocks={
                **index.blocks,
                "unreferenced": next(iter(index.blocks.values())),
            },
        )

    store.bundles.inspect = inspect_with_extra_block
    async with MigrationIO(1) as io:
        with pytest.raises(IntegrityError, match="block set"):
            await stages.read(ticket, io)


async def test_borrowed_stream_expectations_are_not_admitted(tmp_path):
    store, operation, plan, expected, _ = await committed(tmp_path)
    stages = OfflineVerifyStages(store)
    with pytest.raises(TypeError, match="immutable byte"):
        await issue(
            stages,
            operation,
            plan,
            expected,
            [BundleBlock("x", "message", BytesIO(b"x"), 1)],
        )


async def test_sql_immutable_rows_are_rechecked(tmp_path):
    store, operation, plan, expected, blocks = await committed(tmp_path)
    stages = OfflineVerifyStages(store)
    ticket = await issue(stages, operation, plan, expected, blocks)
    async with MigrationIO(1) as io:
        evidence = await stages.read(ticket, io)
    store.db.messages.clear()
    with pytest.raises(IntegrityError, match="immutable typed row"):
        await stages.verify(evidence)


async def test_direct_share_identity_locator_and_journal_are_preserved(
    tmp_path,
):
    from swe.app.conversation_storage.migration.direct_import import (
        DirectImportStore,
    )
    from swe.app.conversation_storage.chat_mutations import utc_datetime

    original, operation, plan, expected, blocks = await committed(
        tmp_path,
        "share",
    )
    record = {
        key: getattr(plan.share, key)
        for key in (
            "token",
            "chat_id",
            "creator_id",
            "tenant_id",
            "snapshot_key",
            "created_at",
            "access_count",
            "last_access_at",
        )
    }
    record["created_at"] = utc_datetime(record["created_at"])
    index = original.bundles.inspect(
        original.db.rows[operation.operation_key.encode()][
            "relative_path"
        ].decode(),
    )
    extent = index.blocks["share"]
    record.update(
        service_unit_key=operation.scope.sql_values[0],
        publication_id=1,
        byte_offset=extent.offset,
        byte_length=extent.length,
    )
    fetch = original.db.fetch_one

    async def fetch_one(query, params=None):
        if "FROM swe_chat_shares" in query:
            return record.copy()
        return await fetch(query, params)

    original.db.fetch_one = fetch_one
    journal = SimpleNamespace(record_share=AsyncMock())
    store = DirectImportStore(
        original.db,
        original.bundles,
        original.target,
        service_unit_id="schema-test-service",
        journal=journal,
    )
    stages = OfflineVerifyStages(store)
    ticket = await issue(stages, operation, plan, expected, blocks)
    async with MigrationIO(1) as io:
        evidence = await stages.read(ticket, io)
    await stages.verify(evidence)
    locator = (operation.scope.sql_values[0], 1, extent.offset, extent.length)
    journal.record_share.assert_awaited_once_with(
        plan.share.token,
        locator,
        locator,
    )
    ticket = await issue(stages, operation, plan, expected, blocks)
    async with MigrationIO(1) as io:
        evidence = await stages.read(ticket, io)
    record["access_count"] += 1
    with pytest.raises(IntegrityError, match="identity or counter"):
        await stages.verify(evidence)
    record["access_count"] -= 1
    record["byte_offset"] += 1
    with pytest.raises(IntegrityError, match="locator changed"):
        await stages.verify(evidence)


async def test_failed_final_sql_is_unknown_and_receipt_is_retained(tmp_path):
    store, operation, plan, expected, blocks = await committed(tmp_path)
    stages = OfflineVerifyStages(store)
    ticket = await issue(stages, operation, plan, expected, blocks)
    async with MigrationIO(1) as io:
        evidence = await stages.read(ticket, io)
    store._verify_records_sql = AsyncMock(side_effect=ConnectionError("lost"))
    with pytest.raises(UnknownCommit) as error:
        await stages.verify(evidence)
    assert error.value.operation_key == operation.operation_key
    assert stages.receipts and stages.tickets
    assert load_metadata_json(evidence.import_plan) == expected

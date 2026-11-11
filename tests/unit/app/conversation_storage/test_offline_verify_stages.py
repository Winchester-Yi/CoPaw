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


async def projection_fixture(tmp_path, monkeypatch, *, external=False):
    import json
    from copy import deepcopy
    from pathlib import Path
    from swe.app.conversation_storage.bundle import BundleStore
    from swe.app.conversation_storage.migration.install_records import (
        plan_block,
    )
    from swe.app.conversation_storage.session_projection import project_state
    from swe.app.conversation_storage.retention import RetentionStore, PinBatch

    store, operation, plan, expected, blocks = await committed(tmp_path)
    projected_root = tmp_path / "projected"
    projected_root.mkdir()
    store.bundles = BundleStore(projected_root)
    previous_key = operation.operation_key
    operation = replace(operation, operation_key="a" * 64)
    row = store.db.rows.pop(previous_key.encode())
    row["operation_key"] = operation.operation_key.encode()
    row["relative_path"] = BundleStore.path_for(
        operation.operation_key,
        operation.scope,
    ).encode()
    store.db.rows[operation.operation_key.encode()] = row
    row["id"] = 3
    message = {"id": "original"}
    body = blocks[0]
    prior = store.bundles.publish(
        "f" * 64,
        [body],
        scope=operation.scope,
        metadata={"operation_key": "f" * 64},
    )
    extent = prior.blocks[body.name]
    reference = (
        {
            "publication_id": 1,
            "message_id": 100,
            "offset": extent.offset,
            "length": extent.length,
        }
        if external
        else {
            "operation_key": operation.operation_key,
            "block_name": body.name,
        }
    )
    state = {"agent": {"memory": {"messages": [message]}}}
    projection = project_state(
        state,
        operation_key=operation.operation_key,
        resolve_message=lambda *_: reference,
    )
    raw = json.dumps(projection.envelope).encode()
    imported = plan_block(
        store.target,
        expected["rows"],
        expected["updates"],
        expected["locks"],
    )
    index = store.bundles.publish(
        operation.operation_key,
        [imported, body, BundleBlock("recovery", "recovery", raw)],
        scope=operation.scope,
        metadata={"operation_key": operation.operation_key},
    )
    row["byte_length"] = index.byte_length
    current = index.blocks[body.name]
    store.db.messages[:] = [
        (
            100,
            operation.scope.sql_values[0],
            1,
            3,
            current.offset,
            current.length,
        ),
    ]
    prior_row = {
        **deepcopy(row),
        "id": 1,
        "operation_key": b"f" * 64,
        "relative_path": prior.relative_path.encode(),
        "byte_length": prior.byte_length,
    }
    locator = {
        "id": 100,
        "service_unit_key": operation.scope.sql_values[0],
        "session_id": 1,
        "publication_id": 1,
        "byte_offset": extent.offset,
        "byte_length": extent.length,
        "deleted_at": None,
        "source_msg_key": b"original",
        "role_key": b"assistant",
        "layer": "memory",
        "context_epoch": 0,
        "current_visible": 1,
        "in_context": 1,
    }
    fetch = store.db.fetch_all

    async def fetch_all(query, params=None):
        if "FROM swe_conversation_messages" in query:
            return [deepcopy(locator)]
        return await fetch(query, params)

    store.db.fetch_all = fetch_all
    calls = []

    async def pin(retentions, ids, *, kind):
        import asyncio

        assert ids == [1] and kind == "reader"
        calls.append(("pin", asyncio.current_task()))
        return PinBatch(b"reader", (deepcopy(prior_row),))

    async def finish(retentions, batch):
        import asyncio

        calls.append(("release", asyncio.current_task()))

    monkeypatch.setattr(RetentionStore, "pin", pin)
    monkeypatch.setattr(RetentionStore, "finish_reader", finish)
    blocks.append(
        BundleBlock("recovery", "recovery", json.dumps(state).encode()),
    )
    return store, operation, plan, expected, blocks, calls, locator


@pytest.mark.parametrize("external", [False, True])
async def test_projection_matches_original_protocol_and_separates_io(
    tmp_path,
    monkeypatch,
    external,
):
    import asyncio

    store, op, plan, expected, blocks, calls, _ = await projection_fixture(
        tmp_path,
        monkeypatch,
        external=external,
    )
    row = await store.get(op.operation_key)
    index = store.bundles.inspect(row["relative_path"].decode())
    extent = index.blocks["recovery"]
    raw = store.bundles.read_extent(
        index.relative_path,
        extent.offset,
        extent.length,
    )
    assert await store._same_recovery(row, index, raw, blocks[-1].content)
    calls.clear()
    stages = OfflineVerifyStages(store)
    ticket = await stages.prepare(op, 3, plan, expected, blocks)
    async with MigrationIO(1) as io:
        evidence = await stages.read(ticket, io)
        bundles = store.bundles
        store.bundles = ForbiddenSurface()
        capture = await stages.capture_recovery(evidence)
        with pytest.raises(IntegrityError, match="incomplete"):
            await stages.verify(evidence)
        store.bundles = bundles
        db = store.db
        store.db = ForbiddenSurface()
        assert await stages.compare_recovery(capture, io) is evidence
        store.db, store.bundles = db, ForbiddenSurface()
        await stages.verify(evidence)
    assert not stages.recoveries
    assert all(task is asyncio.current_task() for _, task in calls)
    assert [name for name, _ in calls] == (
        ["pin", "release"] if external else []
    )


async def test_projection_failure_and_capture_tampering_release_pins(
    tmp_path,
    monkeypatch,
):
    store, op, plan, expected, blocks, calls, _ = await projection_fixture(
        tmp_path,
        monkeypatch,
        external=True,
    )
    blocks[-1] = replace(blocks[-1], content=b"{}")
    stages = OfflineVerifyStages(store)
    ticket = await stages.prepare(op, 3, plan, expected, blocks)
    async with MigrationIO(1) as io:
        evidence = await stages.read(ticket, io)
        capture = await stages.capture_recovery(evidence)
        with pytest.raises(IntegrityError, match="altered"):
            await stages.compare_recovery(replace(capture, snapshots=()), io)
        with pytest.raises(IntegrityError, match="differs from frozen source"):
            await stages.compare_recovery(capture, io)
        assert [name for name, _ in calls] == ["pin"]
        await stages.close()
    assert [name for name, _ in calls] == ["pin", "release"]
    assert not stages.recoveries


async def test_projection_locator_is_revalidated_before_sql_acceptance(
    tmp_path,
    monkeypatch,
):
    store, op, plan, expected, blocks, calls, locator = (
        await projection_fixture(
            tmp_path,
            monkeypatch,
            external=True,
        )
    )
    stages = OfflineVerifyStages(store)
    ticket = await stages.prepare(op, 3, plan, expected, blocks)
    async with MigrationIO(1) as io:
        evidence = await stages.read(ticket, io)
        capture = await stages.capture_recovery(evidence)
        await stages.compare_recovery(capture, io)
    locator["byte_offset"] += 1
    with pytest.raises(IntegrityError, match="locator changed"):
        await stages.verify(evidence)
    assert [name for name, _ in calls] == ["pin", "release"]


async def test_prepared_blocks_verifies_projection_without_typed_rows(
    tmp_path,
    monkeypatch,
):
    from swe.app.conversation_storage.migration.install_records import (
        plan_block,
    )

    store, op, _, expected, blocks, calls, _ = await projection_fixture(
        tmp_path,
        monkeypatch,
        external=True,
    )
    store.db.rows[op.operation_key.encode()]["state"] = "prepared"
    stages = OfflineVerifyStages(store)
    ticket = await stages.prepare_blocks(
        op.operation_key,
        [
            plan_block(
                store.target,
                expected["rows"],
                expected["updates"],
                expected["locks"],
            ),
            *blocks,
        ],
    )
    store._verify_records_sql = AsyncMock(
        side_effect=AssertionError("not installed"),
    )
    async with MigrationIO(1) as io:
        evidence = await stages.read(ticket, io)
        capture = await stages.capture_recovery(evidence)
        await stages.compare_recovery(capture, io)
        assert (await stages.verify(evidence))["state"] == "prepared"
    store._verify_records_sql.assert_not_awaited()
    assert [name for name, _ in calls] == ["pin", "release"]


async def test_cancelled_projection_comparison_drains_before_owner_close(
    tmp_path,
    monkeypatch,
):
    import asyncio
    from threading import Event

    store, op, plan, expected, blocks, calls, _ = await projection_fixture(
        tmp_path,
        monkeypatch,
        external=True,
    )
    stages = OfflineVerifyStages(store)
    ticket = await stages.prepare(op, 3, plan, expected, blocks)
    entered, release = Event(), Event()
    async with MigrationIO(1) as io:
        evidence = await stages.read(ticket, io)
        capture = await stages.capture_recovery(evidence)
        read = store.bundles.read_extent

        def delayed(*args, **kwargs):
            entered.set()
            assert release.wait(3)
            return read(*args, **kwargs)

        store.bundles.read_extent = delayed
        task = asyncio.create_task(stages.compare_recovery(capture, io))
        while not entered.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(IntegrityError, match="not drained"):
            await stages.close()
        assert [name for name, _ in calls] == ["pin"]
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    await stages.close()
    assert [name for name, _ in calls] == ["pin", "release"]
    assert not stages.recoveries


async def test_cold_turn_projection_compares_and_revalidates_authority(
    tmp_path,
    monkeypatch,
):
    import json
    from copy import deepcopy
    from swe.app.conversation_storage.retention import PinBatch, RetentionStore
    from swe.app.conversation_storage.session_projection import FORMAT

    store, op, plan, expected, blocks, _, _ = await projection_fixture(
        tmp_path,
        monkeypatch,
        external=True,
    )
    prior = store.bundles.publish(
        "b" * 64,
        [BundleBlock("turn:q", "turn", b'{"status":"completed"}')],
        scope=op.scope,
        metadata={"operation_key": "b" * 64},
    )
    reference = {
        "publication_id": 1,
        "operation_key": "b" * 64,
        "block_name": "turn:q",
        "turn_id": 7,
    }
    envelope = {
        "format": FORMAT,
        "version": 1,
        "state": {},
        "lists": [],
        "turns": [{"key": "q", "ref": reference}],
    }
    current_row = store.db.rows[op.operation_key.encode()]
    current = store.bundles.inspect(current_row["relative_path"].decode())
    extent = current.blocks["recovery"]
    read = store.bundles.read_extent

    def read_extent(path, offset, length, **kwargs):
        if path == current.relative_path and offset == extent.offset:
            return json.dumps(envelope).encode()
        return read(path, offset, length, **kwargs)

    store.bundles.read_extent = read_extent
    blocks[-1] = replace(
        blocks[-1],
        content=b'{"turn_states":{"q":{"status":"completed"}}}',
    )
    turn = {
        "id": 7,
        "service_unit_key": op.scope.sql_values[0],
        "session_id": 1,
        "chat_id": 2,
        "terminal_publication_id": 1,
        "history_status": "completed",
        "question_msg_key": b"q",
    }

    async def fetch_all(query, params=None):
        if "FROM swe_conversation_turns" in query:
            return [deepcopy(turn)]
        return []

    async def pin(retentions, ids, *, kind):
        publication = {
            **current_row,
            "id": 1,
            "operation_key": b"b" * 64,
            "relative_path": prior.relative_path.encode(),
            "byte_length": prior.byte_length,
        }
        return PinBatch(b"reader", (deepcopy(publication),))

    store.db.fetch_all = fetch_all
    monkeypatch.setattr(RetentionStore, "pin", pin)
    stages = OfflineVerifyStages(store)
    ticket = await stages.prepare(op, 3, plan, expected, blocks)
    async with MigrationIO(1) as io:
        evidence = await stages.read(ticket, io)
        capture = await stages.capture_recovery(evidence)
        await stages.compare_recovery(capture, io)
    turn["history_status"] = "failed"
    with pytest.raises(IntegrityError, match="authority changed"):
        await stages.verify(evidence)
    assert not stages.recoveries

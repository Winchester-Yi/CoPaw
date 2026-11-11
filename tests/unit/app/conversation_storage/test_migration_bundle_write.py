# -*- coding: utf-8 -*-
import asyncio
from dataclasses import replace
from io import BytesIO
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from swe.app.conversation_storage.bundle import BundleBlock, BundleStore
from swe.app.conversation_storage.errors import IntegrityError, UnknownCommit
from swe.app.conversation_storage.migration.bundle_write import publish_bundle
from swe.app.conversation_storage.migration.install_records import (
    OfflineConfirmTicket,
)
from swe.app.conversation_storage.migration.parallel import MigrationIO
from tests.unit.app.conversation_storage.test_publications import request


def ticket():
    operation = request()
    return OfflineConfirmTicket(
        operation,
        1,
        BundleStore.path_for(operation.operation_key, operation.scope),
        "{}",
    )


async def test_publish_prepared_blocks_uses_existing_bundle_protocol(tmp_path):
    store = BundleStore(tmp_path)
    permit = ticket()
    stream = BytesIO(b"streamed")
    blocks = [
        BundleBlock("recovery", "recovery", b"already-transformed"),
        BundleBlock("artifact", "artifact", stream, length=8),
    ]
    async with MigrationIO(1) as io:
        index = await publish_bundle(store, permit, blocks, io)
    assert index.relative_path == permit.relative_path
    assert index.metadata == {"operation_key": permit.request.operation_key}
    for name, expected in (
        ("recovery", b"already-transformed"),
        ("artifact", b"streamed"),
    ):
        extent = index.blocks[name]
        assert (
            store.read_extent(
                index.relative_path,
                extent.offset,
                extent.length,
            )
            == expected
        )
    assert not stream.closed


@pytest.mark.parametrize("mutation", ["path", "scope", "operation"])
async def test_mismatched_ticket_rejected_before_io(tmp_path, mutation):
    permit = ticket()
    if mutation == "path":
        permit = replace(permit, relative_path="foreign.bundle")
    elif mutation == "scope":
        permit = replace(
            permit,
            request=replace(
                permit.request,
                scope=replace(permit.request.scope, tenant_id="other"),
            ),
        )
    else:
        permit = replace(
            permit,
            request=replace(permit.request, operation_key="f" * 64),
        )
    io = SimpleNamespace(call=Mock(side_effect=AssertionError("NAS called")))
    with pytest.raises(IntegrityError, match="ticket"):
        await publish_bundle(BundleStore(tmp_path), permit, [], io)
    io.call.assert_not_called()


async def test_container_is_snapshotted_before_io_queue_wait(tmp_path):
    permit = ticket()
    blocks = [BundleBlock("body", "message", b"original")]

    async def call(function, *args, **kwargs):
        blocks.clear()
        assert isinstance(args[1], tuple)
        return function(*args, **kwargs)

    index = await publish_bundle(
        BundleStore(tmp_path),
        permit,
        blocks,
        SimpleNamespace(call=call),
    )
    assert "body" in index.blocks


async def test_cancel_drains_write_then_reports_unknown(tmp_path):
    store = BundleStore(tmp_path)
    permit = ticket()
    entered = threading.Event()
    release = threading.Event()
    original = store.publish

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    store.publish = delayed
    async with MigrationIO(1) as io:
        task = asyncio.create_task(
            publish_bundle(
                store,
                permit,
                [BundleBlock("body", "message", b"body")],
                io,
            ),
        )
        while not entered.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(UnknownCommit) as error:
            await task
        assert error.value.operation_key == permit.request.operation_key
    assert store.inspect(permit.relative_path).metadata == {
        "operation_key": permit.request.operation_key,
    }


async def test_existing_bundle_is_never_replaced(tmp_path):
    store = BundleStore(tmp_path)
    permit = ticket()
    async with MigrationIO(1) as io:
        original = await publish_bundle(
            store,
            permit,
            [BundleBlock("body", "message", b"original")],
            io,
        )
        with pytest.raises(FileExistsError):
            await publish_bundle(
                store,
                permit,
                [BundleBlock("body", "message", b"replacement")],
                io,
            )
    extent = original.blocks["body"]
    assert (
        store.read_extent(
            original.relative_path,
            extent.offset,
            extent.length,
        )
        == b"original"
    )


@pytest.mark.parametrize("field", ["relative_path", "metadata"])
async def test_inconsistent_nas_result_is_rejected(tmp_path, field):
    store = BundleStore(tmp_path)
    permit = ticket()

    async def call(function, *args, **kwargs):
        index = function(*args, **kwargs)
        value = "foreign.bundle" if field == "relative_path" else {}
        return replace(index, **{field: value})

    with pytest.raises(IntegrityError, match="operation or path"):
        await publish_bundle(
            store,
            permit,
            [BundleBlock("body", "message", b"body")],
            SimpleNamespace(call=call),
        )

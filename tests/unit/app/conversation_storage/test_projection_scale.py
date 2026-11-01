# -*- coding: utf-8 -*-
"""Long lived task history must not turn the per-batch pin bound into a limit."""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.projection_publication import _existing
from swe.app.conversation_storage.projection_references import (
    snapshot_projection,
    ref_key,
)
from swe.app.conversation_storage.retention import RetentionStore
from swe.app.conversation_storage.session_projection import (
    project_state,
    hydrate_state,
)
from tests.unit.app.conversation_storage.test_retention import (
    RetentionDatabase,
)


class HistoryDatabase(RetentionDatabase):
    def __init__(self, count):
        super().__init__()
        template = self.objects[1]
        self.objects.clear()
        self.objects.update(
            {
                i: {
                    **template,
                    "id": i,
                    "session_id": 1,
                    "relative_path": str(i).encode(),
                    "byte_length": 100,
                }
                for i in range(1, count + 1)
            },
        )
        self.messages = {
            i: {
                "id": i,
                "publication_id": i,
                "session_id": 1,
                "byte_offset": 0,
                "byte_length": 10,
                "deleted_at": None,
                "layer": "task",
                "source_msg_key": str(i).encode(),
                "role_key": b"assistant",
            }
            for i in self.objects
        }

    async def fetch_all(self, query, params):
        if "swe_conversation_messages" in query:
            return list(self.messages.values())
        return await super().fetch_all(query, params)

    async def fetch_one(self, query, params):
        if "swe_conversation_messages" in query:
            return self.messages.get(params[0])
        return await super().fetch_one(query, params)


def history(count):
    db = HistoryDatabase(count)
    state = {
        "task_messages": [
            {"id": str(i), "role": "assistant", "content": str(i)}
            for i in db.objects
        ],
        "future": {"keep": True},
    }

    def read(path, *_args, **_kwargs):
        return json.dumps(state["task_messages"][int(path) - 1]).encode()

    service = SimpleNamespace(db=db, bundles=SimpleNamespace(read_extent=read))
    row = {
        "tenant_key": b"t",
        "source_key": b"s",
        "agent_key": b"a",
        "session_id": 1,
        "base_state_revision": 0,
    }
    envelope = project_state(
        state,
        operation_key="current",
        resolve_message=lambda layer, body: {
            "publication_id": int(body["id"]),
            "message_id": int(body["id"]),
            "offset": 0,
            "length": 10,
        },
    ).envelope
    request = SimpleNamespace(
        scope=WorkspaceScope("t", "s", "a"),
        session_id=1,
    )
    return db, service, row, request, state, envelope


@pytest.mark.parametrize("count", [4096, 4097])
@pytest.mark.parametrize("path", ["publication", "recovery"])
async def test_long_task_history_across_fresh_requests(count, path):
    db, service, row, request, state, envelope = history(count)
    for _ in range(2):
        if path == "publication":
            values = await _existing(service, row)
            assert sum(map(len, values.values())) == count
        else:
            refs = await snapshot_projection(
                service,
                request,
                envelope,
                None,
                current_id=count + 1,
            )
            try:
                assert (
                    hydrate_state(
                        envelope,
                        lambda ref, payloads=refs.payloads: payloads[
                            ref_key(ref)
                        ],
                    )
                    == state
                )
            finally:
                await refs.finish(RetentionStore(db, request.scope))
        assert all(pin["state"] == "released" for pin in db.pins)


@pytest.mark.parametrize("path", ["publication", "recovery"])
@pytest.mark.parametrize("failure", ["pin", "read", "cancel"])
async def test_long_history_failure_releases_every_batch(
    monkeypatch,
    path,
    failure,
):
    db, service, row, request, _, envelope = history(4097)
    original_pin = RetentionStore.pin
    calls = 0

    async def pin(store, ids, *, kind, **kwargs):
        nonlocal calls
        calls += 1
        if failure == "pin" and calls == 2:
            raise RuntimeError("second pin failed")
        return await original_pin(store, ids, kind=kind, **kwargs)

    monkeypatch.setattr(RetentionStore, "pin", pin)
    original = service.bundles.read_extent

    def read(path, *args, **kwargs):
        if int(path) == 4097:
            if failure == "cancel":
                raise asyncio.CancelledError
            if failure == "read":
                raise RuntimeError("last read failed")
        return original(path, *args, **kwargs)

    service.bundles.read_extent = read
    with pytest.raises(
        asyncio.CancelledError if failure == "cancel" else RuntimeError,
    ):
        if path == "publication":
            await _existing(service, row)
        else:
            await snapshot_projection(
                service,
                request,
                envelope,
                None,
                current_id=4098,
            )
    assert not [pin for pin in db.pins if pin["state"] == "active"]


@pytest.mark.parametrize("count", [4095, 4096, 4097])
async def test_projection_commit_installs_all_dependencies(count):
    from swe.app.conversation_storage.source_messages import SourceMessages

    db, service, _, request, _, _ = history(count + 1)
    sources = SourceMessages(
        dependency_publications={
            i: db.objects[i] for i in range(1, count + 1)
        },
    )
    await sources.install_dependencies(service, request, db.objects[count + 1])
    assert [pin["publication_id"] for pin in db.pins] == list(
        range(1, count + 1),
    )
    assert all(
        pin["kind"] == "dependency" and pin["state"] == "active"
        for pin in db.pins
    )


@pytest.mark.parametrize("path", ["publication", "recovery"])
async def test_external_cancel_drains_io_before_releasing_all_batches(path):
    db, service, row, request, _, envelope = history(4097)
    entered, release = threading.Event(), threading.Event()
    original = service.bundles.read_extent

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    service.bundles.read_extent = blocked
    task = asyncio.create_task(
        (
            _existing(service, row)
            if path == "publication"
            else snapshot_projection(
                service,
                request,
                envelope,
                None,
                current_id=4098,
            )
        ),
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert (
            len([pin for pin in db.pins if pin["state"] == "active"]) == 4097
        )
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(pin["state"] == "released" for pin in db.pins)


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_commit_source_cleanup_failure_still_releases_projection_batches(
    tmp_path,
    monkeypatch,
    error,
):
    from unittest.mock import AsyncMock
    from swe.app.conversation_storage import publications
    from swe.app.conversation_storage.bundle import BundleBlock, BundleStore
    from swe.app.conversation_storage.projection_references import (
        ProjectionReferences,
    )
    from swe.app.conversation_storage.retention import ReaderPins
    from tests.unit.app.conversation_storage.test_publications import (
        PublicationDatabase,
        request as publication_request,
    )

    history_db, _, _, request, _, _ = history(4097)
    pins = ReaderPins(RetentionStore(history_db, request.scope))
    await pins.pin(list(history_db.objects))
    refs = ProjectionReferences(batch=pins)
    monkeypatch.setattr(
        publications,
        "snapshot_projection",
        AsyncMock(return_value=refs),
    )

    async def failed_source_cleanup(*_args):
        raise error("source cleanup failed")

    monkeypatch.setattr(
        publications.SourceMessages,
        "finish",
        failed_source_cleanup,
    )
    db = PublicationDatabase()
    db.session["recovery_publication_id"] = None
    store = publications.PublicationStore(db, BundleStore(tmp_path))
    operation = publication_request()
    await store.reserve(
        operation,
        publications.CommitPlan(recovery_block="root"),
    )
    await store.write_bundle(
        operation.operation_key,
        [BundleBlock("root", "recovery", b'{"future":true}')],
    )
    with pytest.raises(error):
        await store.commit(operation.operation_key)
    assert all(pin["state"] == "released" for pin in history_db.pins)

# -*- coding: utf-8 -*-
"""Share phase isolation, owner SQL routing and bounded NAS concurrency."""

import asyncio
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swe.app.conversation_storage.identity import WorkspaceScope
from swe.app.conversation_storage.migration.direct_import import DirectTarget
from swe.app.conversation_storage.migration.importer import (
    ConversationImporter,
    ParallelWorkspaceImporter,
)
from swe.app.conversation_storage.migration.parallel import MigrationIO
from swe.app.conversation_storage.migration.parallel_shares import (
    run_parallel_shares,
)
from tests.unit.app.conversation_storage.test_parallel_import import Store


def share(index, *, linked=True):
    return {
        "position": "share:" + str(index),
        "service_unit_id": "svc" + str(index % 2),
        "chat_position": "chat" if linked else None,
        "scope_kind": "workspace" if linked else "legacy_share",
        "record": {
            "service_unit_id": "svc" + str(index % 2),
            "token": "token" + str(index),
            "chat_id": "legacy-chat",
            "creator_id": "creator",
            "tenant_id": "tenant",
            "snapshot_key": "snapshot",
            "created_at": "2026-11-01T00:00:00Z",
            "access_count": 17,
            "last_access_at": "2026-11-02T00:00:00Z",
        },
        "snapshot": {"fixture": index},
    }


def harness(owner):
    calls, stores = [], []

    class TrackedStore(Store):
        async def get(self, value):
            assert asyncio.current_task() is owner
            calls.append(value)

        async def close_migration_stages(self):
            assert asyncio.current_task() is owner
            self.closed = True

    def factory(workspace):
        assert asyncio.current_task() is owner
        store = TrackedStore()
        store.closed = False
        store.scope = workspace["scope"]
        stores.append(store)
        return store

    async def resolve(item):
        assert asyncio.current_task() is owner
        linked = item["scope_kind"] == "workspace"
        return (
            WorkspaceScope(
                "tenant",
                "source" if linked else "",
                "agent" if linked else "",
                service_unit_id=item["service_unit_id"],
            ),
            11 if linked else None,
            12 if linked else None,
        )

    importer = SimpleNamespace(
        journal=SimpleNamespace(),
        target=DirectTarget("schema", "schema", "migration"),
        _resolve_share=resolve,
    )
    return importer, factory, stores, calls


async def test_shares_use_private_stores_shared_bounded_io_and_owner_sql(
    monkeypatch,
):
    owner = asyncio.current_task()
    importer, factory, stores, calls = harness(owner)
    barrier, lock = Barrier(2), Lock()
    active = peak = 0
    publications = []
    health = []

    def body_io():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=5)
        with lock:
            active -= 1

    async def publish(self, position, scope, rows, blocks, *, spec):
        assert asyncio.current_task() is not owner
        assert self.context.io is io
        await self.context.store("get", position)
        await self.context.io.call(body_io)
        publications.append((position, scope, rows, blocks, spec))

    async def check():
        assert asyncio.current_task() is owner
        health.append(True)

    monkeypatch.setattr(ParallelWorkspaceImporter, "_publish", publish)
    inputs = [share(index, linked=index % 2 == 0) for index in range(8)]
    async with MigrationIO(2) as io:
        await run_parallel_shares(
            importer,
            inputs,
            factory,
            io=io,
            workers=4,
            health_check=check,
        )
        assert await io.call(lambda: "still open") == "still open"
    assert peak == 2
    assert health and len(stores) == 8 and all(s.closed for s in stores)
    assert sorted(calls) == sorted(item["position"] for item in inputs)
    by_position = {item[0]: item for item in publications}
    for item in inputs:
        position, scope, rows, blocks, spec = by_position[item["position"]]
        assert position == item["position"] and rows == []
        assert scope.service_unit_id == item["service_unit_id"]
        assert spec.scope_kind == item["scope_kind"]
        assert spec.kind == "share_import"
        for key, value in item["record"].items():
            if key != "service_unit_id":
                assert getattr(spec.commit_plan.share, key) == value
        assert blocks[0].name == "share"
        assert spec.session_id == (11 if item["chat_position"] else None)
        assert spec.chat_id == (12 if item["chat_position"] else None)


@pytest.mark.parametrize("stop_mode", ["cancel", "health"])
async def test_share_failure_drains_io_and_closes_private_stores(
    monkeypatch,
    stop_mode,
):
    started, release, finished = Event(), Event(), Event()
    stores = []

    def slow_io():
        started.set()
        assert release.wait(timeout=5)
        finished.set()

    async def publish(self, *_args, **_kwargs):
        await self.context.io.call(slow_io)

    monkeypatch.setattr(ParallelWorkspaceImporter, "_publish", publish)
    monkeypatch.setattr(
        "swe.app.conversation_storage.migration.parallel.HEALTH_CHECK_INTERVAL",
        0.001,
    )

    async def run():
        importer, factory, actual, _calls = harness(asyncio.current_task())
        stores.append(actual)

        async def health():
            if started.is_set() and stop_mode == "health":
                raise RuntimeError("lost migration lock")

        async with MigrationIO(1) as io:
            await run_parallel_shares(
                importer,
                [share(0)],
                factory,
                io=io,
                workers=1,
                health_check=health,
            )

    running = asyncio.create_task(run())
    while not started.is_set():
        await asyncio.sleep(0)
    if stop_mode == "cancel":
        running.cancel()
        await asyncio.sleep(0)
        running.cancel()
    else:
        await asyncio.sleep(0.01)
    assert not running.done()
    release.set()
    error = asyncio.CancelledError if stop_mode == "cancel" else RuntimeError
    with pytest.raises(error):
        await asyncio.wait_for(running, 2)
    assert finished.is_set()
    assert stores[0] and all(store.closed for store in stores[0])


async def test_share_resolution_preserves_linked_and_orphan_scopes():
    importer = object.__new__(ConversationImporter)
    importer.chat_ids = {"chat": 12}
    owner = asyncio.current_task()

    async def fetch(_query, params):
        assert asyncio.current_task() is owner and params == (12,)
        return {
            "session_id": 11,
            "service_unit_key": b"svc0",
            "tenant_key": b"tenant",
            "source_key": b"source",
            "agent_key": b"agent",
        }

    importer.db = SimpleNamespace(fetch_one=fetch)
    scope, sid, cid = await importer._resolve_share(share(0))
    assert scope.values == ("svc0", "tenant", "source", "agent")
    assert (sid, cid) == (11, 12)
    scope, sid, cid = await importer._resolve_share(share(1, linked=False))
    assert scope.values == ("svc1", "tenant", "", "")
    assert sid is cid is None
    with pytest.raises(ValueError, match="share service differs"):
        await importer._resolve_share(share(1))


@pytest.mark.parametrize("failed", [False, True])
async def test_direct_import_runs_shares_after_workspaces_in_same_io(
    monkeypatch,
    failed,
):
    from swe.app.conversation_storage.migration.direct_import import (
        DirectImporter,
    )
    from swe.app.conversation_storage.migration.freshness import tenant_key
    from swe.app.conversation_storage.migration.parallel_import import (
        ParallelImportPrototype,
    )

    importer = object.__new__(DirectImporter)
    importer.journal = SimpleNamespace(
        prepare=AsyncMock(),
        mark_state=AsyncMock(),
        capture_retentions=AsyncMock(),
        persist_updates=True,
    )
    importer.manifest = SimpleNamespace(
        data={
            "tenant_sync_started_at_ns": {tenant_key("svc0", "tenant"): 1},
            "batches": {},
        },
        migration_id="migration",
        _persist=lambda: None,
    )
    importer.target = DirectTarget("schema", "schema", "migration")
    importer.batch_size = 128
    importer.stores = {"svc0": SimpleNamespace(validate_target=AsyncMock())}
    for method in (
        "_validate_source_binding",
        "_assert_source_fresh",
        "_validate_runtime_sources",
    ):
        setattr(importer, method, lambda _report: None)
    events, pools = [], []
    health = AsyncMock()

    async def workspace_run(_self, _inputs, _factory, *, io):
        pools.append(io)
        events.append("workspaces")
        assert await io.call(lambda: True)
        return {"chat": 12}

    async def shares_run(actual, inputs, _factory, *, io, **options):
        assert actual is importer and actual.chat_ids == {"chat": 12}
        assert inputs == [share(0)] and io is pools[0]
        assert options == {"workers": 2, "health_check": health}
        events.append("shares")
        assert await io.call(lambda: True)
        if failed:
            raise RuntimeError("share failed")

    monkeypatch.setattr(ParallelImportPrototype, "run", workspace_run)
    monkeypatch.setattr(
        "swe.app.conversation_storage.migration.parallel_shares.run_parallel_shares",
        shares_run,
    )
    report = {
        "version": 2,
        "ready": True,
        "blockers": [],
        "workspaces": [],
        "shares": [share(0)],
        "files": [],
    }
    if failed:
        with pytest.raises(RuntimeError, match="share failed"):
            await importer.run_parallel(report, workers=2, health_check=health)
        importer.journal.capture_retentions.assert_not_awaited()
        assert importer.journal.mark_state.await_args_list == [
            (("importing",),),
        ]
    else:
        await importer.run_parallel(report, workers=2, health_check=health)
        importer.journal.mark_state.assert_awaited_with("imported")
    assert events == ["workspaces", "shares"]
    assert pools[0]._closed
    assert importer.journal.persist_updates is True

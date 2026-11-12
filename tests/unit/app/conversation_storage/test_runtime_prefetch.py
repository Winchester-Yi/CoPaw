# -*- coding: utf-8 -*-
"""Fresh runtime-body evidence preserves owner SQL and strict locators."""

import asyncio
import threading
from io import BytesIO
from types import SimpleNamespace

import pytest

from swe.app.conversation_storage.migration import runtime_prefetch, readonly
from swe.app.conversation_storage.migration.parallel import MigrationIO
from swe.app.conversation_storage.errors import IntegrityError


@pytest.mark.parametrize("first_id", [0, 1])
async def test_runtime_prefetch_overlaps_nas_and_keeps_owner_and_pins(
    monkeypatch,
    first_id,
):
    owner = asyncio.current_task()
    barrier = threading.Barrier(2)
    pinned = set()
    calls = []

    class Retention:
        def __init__(self, _db, scope):
            self.scope = scope

        async def pin(self, ids, *, kind):
            assert asyncio.current_task() is owner
            assert kind == "reader"
            pinned.update(ids)
            return ids

        async def finish_reader(self, ids):
            assert asyncio.current_task() is owner
            pinned.difference_update(ids)

    monkeypatch.setattr(readonly, "ReadOnlyRetention", Retention)

    class Database:
        async def fetch_all(self, _query, params):
            assert asyncio.current_task() is owner
            identity = int(params[3]) + first_id
            assert "id>%s" not in _query
            return [
                {
                    "id": identity,
                    "relative_path": str(identity),
                    "byte_length": 20,
                },
            ]

    class Bundles:
        root = "/cache"

        @staticmethod
        def _open(_path):
            return BytesIO(
                runtime_prefetch.HEADER.pack(b"fakehead", 1, 1, 1, 4),
            )

        @staticmethod
        def validate_publication_locator(row):
            return row["relative_path"]

        @staticmethod
        def inspect(path, *, expected_length):
            assert int(path) in pinned
            barrier.wait(timeout=5)
            return SimpleNamespace(
                byte_length=expected_length,
                blocks={"body": SimpleNamespace(offset=10, length=4)},
            )

        @staticmethod
        def read_extent(path, _offset, _length, **_kwargs):
            assert int(path) in pinned
            calls.append(path)
            return b"body"

    async def runtime(workspace, cache):
        assert asyncio.current_task() is owner
        path = str(int(workspace["scope"][3]) + first_id)
        assert cache.read_extent(path, 10, 4, expected_length=20) == b"body"
        with pytest.raises(IntegrityError, match="length differs"):
            cache.read_extent(path, 10, 4, expected_length=21)
        with pytest.raises(IntegrityError, match="absent"):
            cache.read_extent(path, 0, 1)

    inventory = {
        "workspaces": [
            {"scope": ["svc", "tenant", "source", str(i)]} for i in range(2)
        ],
    }
    async with MigrationIO(2) as io:
        await runtime_prefetch.verify_prefetched_workspaces(
            Database(),
            {"svc": Bundles()},
            inventory,
            runtime,
            io=io,
            workers=2,
            allowance=65536,
        )
    assert sorted(calls) == [str(first_id), str(first_id + 1)]
    assert not pinned


async def test_runtime_prefetch_budget_fails_before_body_read():
    class Bundles:
        root = "/unused"

        @staticmethod
        def _open(_path):
            return BytesIO(
                runtime_prefetch.HEADER.pack(b"fakehead", 1, 1, 1, 100),
            )

        @staticmethod
        def validate_publication_locator(row):
            return row["relative_path"]

        @staticmethod
        def inspect(_path, **_kwargs):
            return SimpleNamespace(
                blocks={"body": SimpleNamespace(offset=0, length=100)},
            )

        @staticmethod
        def read_extent(*_args, **_kwargs):
            pytest.fail("budget must fail before allocation")

    async with MigrationIO(1) as io:
        with pytest.raises(ValueError, match="memory allowance"):
            await runtime_prefetch._prefetch(
                Bundles(),
                [{"relative_path": "one", "byte_length": 100}],
                io,
                10,
            )


def test_runtime_index_budget_precedes_json_decoder():
    class Bundles:
        @staticmethod
        def _open(_path):
            return BytesIO(
                runtime_prefetch.HEADER.pack(b"fakehead", 1, 4096, 4096, 0),
            )

        @staticmethod
        def inspect(*_args, **_kwargs):
            pytest.fail("JSON decoder must not run before budget check")

    with pytest.raises(ValueError, match="index exceeds.*memory allowance"):
        runtime_prefetch._inspect_bounded(Bundles(), "one", 8192, 65536)


@pytest.mark.parametrize("kind", ["workspace", "share"])
@pytest.mark.parametrize("duplicate", [False, True])
async def test_runtime_prefetch_rejects_missing_or_duplicate_coverage(
    monkeypatch,
    kind,
    duplicate,
):
    class Coordinator:
        def __init__(self, handlers, **_kwargs):
            self.handlers = handlers

        async def run(self, _items, _worker):
            if duplicate:
                check = self.handlers["runtime_" + kind + "_check"]
                await check(0, None)
                await check(0, None)

    monkeypatch.setattr(runtime_prefetch, "MigrationCoordinator", Coordinator)

    async def runtime(_item, _cache):
        pass

    async with MigrationIO(1) as io:
        with pytest.raises(IntegrityError, match="duplicate|incomplete"):
            if kind == "workspace":
                await runtime_prefetch.verify_prefetched_workspaces(
                    None,
                    {},
                    {"workspaces": [{}]},
                    runtime,
                    io=io,
                    workers=1,
                    allowance=65536,
                )
            else:
                await runtime_prefetch.verify_prefetched_shares(
                    None,
                    {},
                    [{}],
                    runtime,
                    io=io,
                    workers=1,
                    allowance=65536,
                )


async def test_runtime_release_all_attempts_every_index_after_errors():
    visited = []
    pins = {0: None, 1: None, 2: None}

    async def release(index):
        visited.append(index)
        del pins[index]
        if index == 0:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await runtime_prefetch._release_all(pins, release)
    assert visited == [0, 1, 2]
    assert pins == {}


async def test_runtime_release_all_attempts_every_batch(monkeypatch):
    released = []

    class Retention:
        def __init__(self, *_args):
            pass

        async def pin(self, ids, **_kwargs):
            return ids

        async def finish_reader(self, ids):
            released.append(ids)
            if ids[0] == 0:
                raise RuntimeError("first release failed")

    class Database:
        async def fetch_all(self, _query, params):
            start = 0 if len(params) == 4 else params[-1] + 1
            return [{"id": i} for i in range(start, min(129, start + 128))]

    class Coordinator:
        def __init__(self, handlers, **_kwargs):
            self.handlers = handlers

        async def run(self, _items, _worker):
            await self.handlers["runtime_workspace_locators"](0)
            raise ValueError("force cleanup")

    monkeypatch.setattr(readonly, "ReadOnlyRetention", Retention)
    monkeypatch.setattr(runtime_prefetch, "MigrationCoordinator", Coordinator)
    async with MigrationIO(1) as io:
        with pytest.raises(RuntimeError, match="first release failed"):
            await runtime_prefetch.verify_prefetched_workspaces(
                Database(),
                {},
                {"workspaces": [{"scope": ["s", "t", "x", "a"]}]},
                None,
                io=io,
                workers=1,
                allowance=4 * 1024 * 1024,
            )
    assert len(released) == 2
    assert released[1] == [128]


async def test_runtime_prefetch_uses_fresh_real_bundle_and_fails_cache_misses(
    tmp_path,
):
    from swe.app.conversation_storage.bundle import BundleBlock, BundleStore
    from swe.app.conversation_storage.identity import WorkspaceScope

    store = BundleStore(tmp_path)
    scope = WorkspaceScope("t", "s", "a", service_unit_id="svc")
    operation = "ab" + "0" * 62
    index = store.publish(
        operation,
        [BundleBlock("body", "message", b"fresh body")],
        scope=scope,
    )
    row = {
        **scope.fields,
        "scope_kind": "workspace",
        "operation_key": operation.encode(),
        "relative_path": index.relative_path.encode(),
        "byte_length": index.byte_length,
    }
    async with MigrationIO(1) as io:
        cache = await runtime_prefetch._prefetch(store, [row], io, 65536)
    block = index.blocks["body"]
    # Once prepared the runtime reader performs no additional filesystem IO.
    (tmp_path / index.relative_path).unlink()
    assert (
        cache.read_extent(
            index.relative_path,
            block.offset,
            block.length,
            expected_length=index.byte_length,
        )
        == b"fresh body"
    )
    with pytest.raises(IntegrityError, match="absent"):
        cache.read_extent(
            index.relative_path + ".other",
            block.offset,
            block.length,
        )
    with pytest.raises(IntegrityError, match="absent"):
        cache.read_extent(index.relative_path, block.offset + 1, block.length)
    with pytest.raises(IntegrityError, match="length differs"):
        cache.inspect(
            index.relative_path,
            expected_length=index.byte_length + 1,
        )


@pytest.mark.parametrize("kind", ["workspace", "share"])
async def test_runtime_prefetch_merges_reports_in_inventory_order(
    monkeypatch,
    kind,
):
    class Coordinator:
        def __init__(self, handlers, **_kwargs):
            self.handlers = handlers

        async def run(self, _items, _worker):
            check = self.handlers["runtime_" + kind + "_check"]
            await check(1, None)
            await check(0, None)

    monkeypatch.setattr(runtime_prefetch, "MigrationCoordinator", Coordinator)

    async def runtime(item, _cache):
        return {"checked": [item["position"]], "blockers": []}

    merged = []
    items = [{"position": "first"}, {"position": "second"}]
    async with MigrationIO(1) as io:
        options = {
            "io": io,
            "workers": 1,
            "allowance": 65536,
            "merge_result": merged.append,
        }
        if kind == "workspace":
            await runtime_prefetch.verify_prefetched_workspaces(
                None,
                {},
                {"workspaces": items},
                runtime,
                **options,
            )
        else:
            await runtime_prefetch.verify_prefetched_shares(
                None,
                {},
                items,
                runtime,
                **options,
            )
    assert [report["checked"] for report in merged] == [["first"], ["second"]]


async def test_runtime_prefetch_repeated_cancel_drains_before_release(
    monkeypatch,
):
    reading, unblock, finished = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    released = []

    class Retention:
        def __init__(self, *_args):
            pass

        async def pin(self, ids, **_kwargs):
            return ids

        async def finish_reader(self, ids):
            assert (
                finished.is_set()
            ), "offline reader lifetime ended before IO drained"
            released.extend(ids)

    class Database:
        async def fetch_all(self, *_args):
            return [{"id": 1, "relative_path": "one", "byte_length": 20}]

    class Bundles:
        root = "/unused"

        @staticmethod
        def _open(_path):
            return BytesIO(
                runtime_prefetch.HEADER.pack(b"fakehead", 1, 1, 1, 4),
            )

        @staticmethod
        def validate_publication_locator(row):
            return row["relative_path"]

        @staticmethod
        def inspect(_path, **_kwargs):
            return SimpleNamespace(
                byte_length=20,
                blocks={"body": SimpleNamespace(offset=10, length=4)},
            )

        @staticmethod
        def read_extent(*_args, **_kwargs):
            reading.set()
            assert unblock.wait(5)
            finished.set()
            return b"body"

    monkeypatch.setattr(readonly, "ReadOnlyRetention", Retention)
    async with MigrationIO(1) as io:
        task = asyncio.create_task(
            runtime_prefetch.verify_prefetched_workspaces(
                Database(),
                {"svc": Bundles()},
                {"workspaces": [{"scope": ["svc", "t", "s", "a"]}]},
                None,
                io=io,
                workers=1,
                allowance=65536,
            ),
        )
        try:
            assert await asyncio.to_thread(reading.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert released == []
        finally:
            unblock.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert released == [1]

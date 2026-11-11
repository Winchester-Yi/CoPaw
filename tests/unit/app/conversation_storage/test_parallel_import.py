# -*- coding: utf-8 -*-
import asyncio
from pathlib import Path
from types import SimpleNamespace
from threading import Barrier, Event

import pytest

from swe.app.conversation_storage.migration.freshness import tenant_key
from swe.app.conversation_storage.migration.parallel_import import (
    ParallelImportPrototype,
)


class Journal:
    def __init__(self):
        self.calls = []

    async def allocate(self, *args, **kwargs):
        self.calls.append(asyncio.current_task())
        return len(self.calls)


class Store:
    bundles = SimpleNamespace(root=Path("/unused-migration-test"))

    async def close_migration_stages(self):
        pass

    async def read_write_recovery(self, *_args):
        raise AssertionError("not used")

    async def read_verify_evidence(self, *_args):
        raise AssertionError("not used")

    async def compare_verify_recovery(self, *_args):
        raise AssertionError("not used")

    async def confirm_bundle_nas(self, *_args):
        raise AssertionError("not used")

    async def read_commit_evidence(self, *_args):
        raise AssertionError("not used")

    async def publish_prepared_bundle(self, *_args):
        raise AssertionError("not used")


def runner(workers=2):
    journal = Journal()
    return (
        ParallelImportPrototype(
            lambda _workspace: Store(),
            journal,
            {"tenant_sync_started_at_ns": {tenant_key("svc", "tenant"): 123}},
            workers=workers,
        ),
        journal,
    )


def workspaces(count=2):
    return [
        {"scope": ["svc", "tenant", "source", str(index)]}
        for index in range(count)
    ]


async def test_workspace_io_runs_concurrently_and_contexts_are_private():
    prototype, journal = runner()
    barrier = Barrier(2)
    contexts = []

    class Importer:
        chat_ids = {}

        def __init__(self, context):
            contexts.append(context)

        async def run_workspace(self, workspace):
            await self.context.io.call(barrier.wait, timeout=3)
            await self.context.journal("allocate", workspace["scope"])

    def make_importer(context):
        importer = Importer(context)
        importer.context = context
        return importer

    await prototype.run(workspaces(), make_importer)

    assert len({id(context) for context in contexts}) == 2
    assert len(set(journal.calls)) == 1
    assert prototype._router._contexts == {}


async def test_workspace_context_is_closed_after_import_failure():
    prototype, _journal = runner(workers=1)

    class Importer:
        chat_ids = {}

        async def run_workspace(self, _workspace):
            raise RuntimeError("import failed")

    with pytest.raises(RuntimeError, match="import failed"):
        await prototype.run(workspaces(1), lambda _context: Importer())

    assert prototype._router._contexts == {}


async def test_router_close_attempts_every_store_and_keeps_failed_context():
    from swe.app.conversation_storage.migration.parallel_import import (
        MigrationDomainRouter,
    )

    calls = []
    fail_first = True

    class TrackedStore:
        def __init__(self, name):
            self.name = name

        async def close_migration_stages(self):
            nonlocal fail_first
            calls.append(self.name)
            if self.name == "first" and fail_first:
                raise OSError("temporary cleanup failure")

    router = MigrationDomainRouter(lambda _workspace: None, Journal())
    router._contexts = {
        "first": TrackedStore("first"),
        "second": TrackedStore("second"),
    }

    with pytest.raises(OSError, match="cleanup failure"):
        await router.close()

    assert calls == ["first", "second"]
    assert set(router._contexts) == {"first"}

    fail_first = False
    await router.close()
    assert calls == ["first", "second", "first"]
    assert router._contexts == {}


async def test_cancel_waits_for_running_io_and_clears_contexts():
    prototype, _journal = runner(workers=1)
    started, release, finished = Event(), Event(), Event()

    def slow_io():
        started.set()
        release.wait(timeout=3)
        finished.set()

    class Importer:
        chat_ids = {}

        def __init__(self, context):
            self.context = context

        async def run_workspace(self, _workspace):
            await self.context.io.call(slow_io)

    task = asyncio.create_task(
        prototype.run(workspaces(1), Importer),
    )
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished.is_set()
    assert prototype._router._contexts == {}


async def test_parallel_import_requires_service_tenant_scope():
    prototype, _journal = runner(workers=1)
    with pytest.raises(ValueError, match="service and tenant"):
        await prototype.run([{"scope": ["svc"]}], lambda _context: None)


async def test_parallel_import_requires_fixed_tenant_cutoff():
    prototype, _journal = runner(workers=1)
    prototype._manifest_data = {"tenant_sync_started_at_ns": {}}
    with pytest.raises(ValueError, match="cutoff"):
        await prototype.run(workspaces(1), lambda _context: None)


async def test_parallel_workspace_import_keeps_legacy_stage_order():
    from swe.app.conversation_storage.migration.importer import (
        ParallelWorkspaceImporter,
    )
    from swe.app.conversation_storage.migration.direct_import import (
        DirectTarget,
    )

    scope = ["svc", "tenant", "source", "agent"]
    context = type("Context", (), {"manifest_data": {}})()
    importer = ParallelWorkspaceImporter(
        context,
        DirectTarget("schema", "schema", "migration"),
    )
    calls = []

    async def record(name, item, *_args):
        calls.append((name, item["position"]))

    async def identities(workspace, _scope):
        for session in workspace["sessions"]:
            await record("identities", session)

    async def session(item, _workspace, _scope):
        await record("session", item)

    importer._identities = identities
    importer._session = session
    importer._archive = lambda item, _scope: record("archive", item)
    await importer.run_workspace(
        {
            "scope": scope,
            "sessions": [{"position": "s1"}, {"position": "s2"}],
            "chats": [{"position": "c1"}, {"position": "c2"}],
        },
    )

    assert calls == [
        ("identities", "s1"),
        ("identities", "s2"),
        ("session", "s1"),
        ("session", "s2"),
        ("archive", "c1"),
        ("archive", "c2"),
    ]


async def test_router_verifies_already_committed_batch_with_real_stages(
    tmp_path,
):
    from swe.app.conversation_storage.migration.parallel_import import (
        MigrationDomainRouter,
        ParallelWorkspaceContext,
    )
    from swe.app.conversation_storage.migration.parallel import (
        MigrationCoordinator,
        MigrationIO,
    )
    from swe.app.conversation_storage.migration.install_records import (
        plan_block,
    )
    from tests.unit.app.conversation_storage.test_offline_verify_stages import (
        committed,
    )

    store, request, plan, expected, blocks = await committed(tmp_path)
    store.chat_session_id = None
    router = MigrationDomainRouter(lambda _workspace: store, Journal())
    owner = asyncio.current_task()
    get = store.get

    async def owner_get(*args, **kwargs):
        assert asyncio.current_task() is owner
        return await get(*args, **kwargs)

    store.get = owner_get
    body = plan_block(
        store.target,
        expected["rows"],
        expected["updates"],
        expected["locks"],
    )
    async with MigrationIO(2) as io:

        async def verify(workspace, send):
            key, nas = await send("workspace.open", workspace)
            context = ParallelWorkspaceContext({}, key, send, nas, io)
            result = await context.commit(
                request.operation_key,
                verification=(request, 1, plan, expected, [body, *blocks]),
            )
            assert result["state"] == "committed"
            await send("workspace.close", key)

        await MigrationCoordinator(router.handlers(), workers=1).run(
            [{}],
            verify,
        )
    assert not router._contexts

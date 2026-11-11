# -*- coding: utf-8 -*-
import asyncio
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
        async def run_workspace(self, _workspace):
            raise RuntimeError("import failed")

    with pytest.raises(RuntimeError, match="import failed"):
        await prototype.run(workspaces(1), lambda _context: Importer())

    assert prototype._router._contexts == {}


async def test_cancel_waits_for_running_io_and_clears_contexts():
    prototype, _journal = runner(workers=1)
    started, release, finished = Event(), Event(), Event()

    def slow_io():
        started.set()
        release.wait(timeout=3)
        finished.set()

    class Importer:
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

# -*- coding: utf-8 -*-
"""Private owner-task routing prototype; not connected to the CLI importer."""

import asyncio
from copy import deepcopy
from functools import partial
from pathlib import Path
from types import MappingProxyType

from ..bundle import BundleStore
from .freshness import tenant_key
from .parallel import MigrationCoordinator, MigrationIO


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze(item) for key, item in value.items()},
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return deepcopy(value)


def workspace_source_copy(value):
    """Restore worker-private JSON containers for legacy extraction helpers."""
    if isinstance(value, (dict, MappingProxyType)):
        return {
            key: workspace_source_copy(item) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [workspace_source_copy(item) for item in value]
    return deepcopy(value)


class MigrationDomainRouter:
    """Fixed domain handlers execute in the coordinator's SQL-owner task."""

    JOURNAL = frozenset({"allocate", "set_batch_state", "refresh_batch"})
    STORE = frozenset(
        {
            "reserve_batch",
            "prepare_confirm_bundle",
            "commit_confirm_bundle",
            "prepare_commit",
            "commit_evidence",
            "get",
            "prepare_write_bundle",
            "commit_written_bundle",
            "chat_session_id",
            "prepare_verify_batch",
            "prepare_verify_blocks",
            "capture_verify_recovery",
            "commit_verify_evidence",
            "release_verify_recovery",
            "finish_write_recovery",
        },
    )

    def __init__(self, store_factory, journal):
        self._store_factory = store_factory
        self._journal = journal
        self._contexts = {}
        self.chat_ids = {}

    def handlers(self):
        return {
            "workspace.open": self._open,
            "workspace.close": self._close,
            "workspace.complete": self._complete,
            **{
                "journal." + name: self._journal_handler(name)
                for name in self.JOURNAL
            },
            **{
                "store." + name: self._store_handler(name)
                for name in self.STORE
            },
        }

    async def _open(self, workspace):
        # Factory is trusted composition code and runs only in the owner task.
        # It must construct a fresh store with private reservation/stage maps.
        store = self._store_factory(workspace)
        if any(existing is store for existing in self._contexts.values()):
            raise ValueError("workspace store must be private")
        key = object()
        self._contexts[key] = store
        return key, _NASStages(store)

    async def _close(self, key):
        store = self._contexts[key]
        await store.close_migration_stages()
        self._contexts.pop(key)

    async def _complete(self, key, chat_ids):
        if key not in self._contexts:
            raise KeyError("closed or foreign workspace context")
        if self.chat_ids.keys() & chat_ids.keys():
            raise ValueError("duplicate workspace Chat position")
        self.chat_ids.update(deepcopy(chat_ids))

    async def close(self):
        failure = None
        for key, store in tuple(self._contexts.items()):
            try:
                await store.close_migration_stages()
            except (Exception, asyncio.CancelledError) as error:
                if failure is None:
                    failure = error
            else:
                self._contexts.pop(key, None)
        if failure is not None:
            raise failure

    def _journal_handler(self, name):
        async def handle(key, args, kwargs):
            if key not in self._contexts:
                raise KeyError("closed or foreign workspace context")
            return deepcopy(
                await getattr(self._journal, name)(*args, **kwargs),
            )

        return handle

    def _store_handler(self, name):
        async def handle(key, args, kwargs):
            result = await getattr(self._contexts[key], name)(*args, **kwargs)
            if name in ("get", "reserve_batch"):
                return deepcopy(result)
            return result

        return handle


class _NASStages:
    """Narrow NAS capability; no worker-visible database or manifest."""

    __slots__ = (
        "_confirm",
        "_read_commit",
        "_publish_write",
        "_bundle_root",
        "_read_verify",
        "_compare_recovery",
        "_read_write_recovery",
    )

    def __init__(self, store):
        self._confirm = store.confirm_bundle_nas
        self._read_commit = store.read_commit_evidence
        self._publish_write = store.publish_prepared_bundle
        self._bundle_root = store.bundles.root
        self._read_verify = store.read_verify_evidence
        self._compare_recovery = store.compare_verify_recovery
        self._read_write_recovery = store.read_write_recovery

    async def confirm(self, ticket, io):
        return await self._confirm(ticket, io)

    async def read_commit(self, ticket, io):
        return await self._read_commit(ticket, io)

    async def publish_write(self, ticket, blocks, io):
        return await self._publish_write(ticket, blocks, io)

    async def bundle_exists(self, row, io):
        relative_path = BundleStore.validate_publication_locator(row)
        return await io.call(
            partial(Path.exists, self._bundle_root / relative_path),
        )

    async def read_verify(self, ticket, io):
        return await self._read_verify(ticket, io)

    async def compare_recovery(self, capture, io):
        return await self._compare_recovery(capture, io)

    async def read_write_recovery(self, ticket, io):
        return await self._read_write_recovery(ticket, io)


class ParallelWorkspaceContext:
    """One worker's private identity maps, frozen inputs and domain calls."""

    def __init__(self, manifest_data, key, request, nas, io):
        self.manifest_data = manifest_data
        self.session_ids, self.chat_ids, self.turn_ids = {}, {}, {}
        self._key, self._request, self._nas = key, request, nas
        self.io = io

    async def journal(self, command, *args, **kwargs):
        if command not in MigrationDomainRouter.JOURNAL:
            raise ValueError("unknown journal command")
        return await self._request(
            "journal." + command,
            self._key,
            args,
            kwargs,
        )

    async def store(self, command, *args, **kwargs):
        if command not in MigrationDomainRouter.STORE:
            raise ValueError("unknown store command")
        return await self._request("store." + command, self._key, args, kwargs)

    async def confirm_bundle(self, operation_key):
        ticket = await self.store("prepare_confirm_bundle", operation_key)
        receipt = await self._nas.confirm(ticket, self.io)
        return await self.store("commit_confirm_bundle", receipt)

    async def commit(self, operation_key, *, verification):
        ticket = await self.store("prepare_commit", operation_key)
        if isinstance(ticket, dict):
            return await self.verify_batch(*verification)
        evidence = await self._nas.read_commit(ticket, self.io)
        return await self.store("commit_evidence", evidence)

    async def write_bundle(self, operation_key, blocks):
        from .offline_write import TurnRecoveryTicket

        preparation = await self.store(
            "prepare_write_bundle",
            operation_key,
            blocks,
        )
        if isinstance(preparation, TurnRecoveryTicket):
            evidence = await self._nas.read_write_recovery(
                preparation,
                self.io,
            )
            preparation = await self.store(
                "finish_write_recovery",
                preparation,
                evidence,
            )
        ticket, prepared = preparation
        receipt = await self._nas.publish_write(ticket, prepared, self.io)
        return await self.store("commit_written_bundle", receipt)

    async def bundle_exists(self, row):
        return await self._nas.bundle_exists(row, self.io)

    async def _verify(self, ticket):
        evidence = await self._nas.read_verify(ticket, self.io)
        capture = await self.store("capture_verify_recovery", evidence)
        if capture is not None:
            await self._nas.compare_recovery(capture, self.io)
        return await self.store("commit_verify_evidence", evidence)

    async def verify_batch(
        self,
        request,
        publication_id,
        plan,
        expected,
        blocks,
    ):
        ticket = await self.store(
            "prepare_verify_batch",
            request,
            publication_id,
            plan,
            expected,
            [block for block in blocks if block.name != "import_plan"],
        )
        return await self._verify(ticket)

    async def verify_blocks(self, row, blocks):
        operation_key = row["operation_key"].decode()
        ticket = await self.store(
            "prepare_verify_blocks",
            operation_key,
            blocks,
        )
        return await self._verify(ticket)


class ParallelImportPrototype:
    """Run private workspace operations; never call DirectImporter.run.

    Callers must prepare/hydrate the journal and fix tenant cutoffs first.
    Factory creates fresh stores on the owner task. Importer factory receives
    only a context, runs on the worker and must not close over a live DB.
    Source freshness, shared artifacts and shares remain outer orchestration.
    """

    def __init__(
        self,
        store_factory,
        journal,
        manifest_data,
        *,
        workers=4,
        io_workers=None,
        health_check=None,
    ):
        self._manifest_data = _freeze(
            {
                key: manifest_data[key]
                for key in (
                    "tenant_sync_started_at_ns",
                    "source_boundary",
                )
                if key in manifest_data
            },
        )
        self._router = MigrationDomainRouter(store_factory, journal)
        self._workers, self._health_check = workers, health_check
        self._io_workers = workers if io_workers is None else io_workers

    async def run(self, workspaces, importer_factory, *, io=None):
        if io is not None:
            return await self._run(workspaces, importer_factory, io)
        async with MigrationIO(self._io_workers) as owned_io:
            return await self._run(workspaces, importer_factory, owned_io)

    async def _run(self, workspaces, importer_factory, io):
        coordinator = MigrationCoordinator(
            self._router.handlers(),
            workers=self._workers,
            health_check=self._health_check,
        )

        async def run_workspace(workspace, request):
            scope = workspace["scope"]
            if not isinstance(scope, (list, tuple)) or len(scope) < 2:
                raise ValueError(
                    "workspace requires service and tenant scope",
                )
            cutoff = self._manifest_data.get(
                "tenant_sync_started_at_ns",
                {},
            ).get(tenant_key(scope[0], scope[1]))
            if type(cutoff) is not int or cutoff <= 0:
                raise ValueError(
                    "tenant cutoff must be fixed before parallel import",
                )
            key, nas = await request("workspace.open", workspace)
            context = ParallelWorkspaceContext(
                self._manifest_data,
                key,
                request,
                nas,
                io,
            )
            try:
                importer = importer_factory(context)
                await importer.run_workspace(_freeze(workspace))
                await request("workspace.complete", key, importer.chat_ids)
            finally:
                await request("workspace.close", key)

        try:
            await coordinator.run(workspaces, run_workspace)
        finally:
            await self._router.close()
        return deepcopy(self._router.chat_ids)

# -*- coding: utf-8 -*-
"""Private owner-task routing prototype; not connected to the CLI importer."""

from copy import deepcopy
from types import MappingProxyType

from .freshness import tenant_key
from .parallel import MigrationCoordinator, MigrationIO


class ParallelImportNotReady(RuntimeError):
    """An import phase still needs an explicit split SQL/NAS adapter."""


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze(item) for key, item in value.items()},
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
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
        },
    )

    def __init__(self, store_factory, journal):
        self._store_factory = store_factory
        self._journal = journal
        self._contexts = {}

    def handlers(self):
        return {
            "workspace.open": self._open,
            "workspace.close": self._close,
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
        self._contexts.pop(key)

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

    __slots__ = ("_confirm", "_read_commit", "_publish_write")

    def __init__(self, store):
        self._confirm = store.confirm_bundle_nas
        self._read_commit = store.read_commit_evidence
        self._publish_write = store.publish_prepared_bundle

    async def confirm(self, ticket, io):
        return await self._confirm(ticket, io)

    async def read_commit(self, ticket, io):
        return await self._read_commit(ticket, io)

    async def publish_write(self, ticket, blocks, io):
        return await self._publish_write(ticket, blocks, io)


class ParallelWorkspaceContext:
    """One worker's private identity maps, frozen inputs and domain calls."""

    def __init__(self, manifest_data, key, request, nas, io, adapters):
        self.manifest_data = manifest_data
        self.session_ids, self.chat_ids, self.turn_ids = {}, {}, {}
        self._key, self._request, self._nas = key, request, nas
        self.io, self._adapters = io, adapters

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

    async def commit(self, operation_key):
        ticket = await self.store("prepare_commit", operation_key)
        if isinstance(ticket, dict):
            # prepare_commit's already-committed return still requires the
            # separate original-bundle verification adapter.
            return await self._adapt("verify_committed", ticket)
        evidence = await self._nas.read_commit(ticket, self.io)
        return await self.store("commit_evidence", evidence)

    async def _adapt(self, name, *args):
        adapter = self._adapters.get(name)
        if adapter is None:
            raise ParallelImportNotReady(
                "parallel import adapter required: " + name,
            )
        return await adapter(self, *args)

    async def write_bundle(self, operation_key, blocks):
        ticket, prepared = await self.store(
            "prepare_write_bundle",
            operation_key,
            blocks,
        )
        receipt = await self._nas.publish_write(ticket, prepared, self.io)
        return await self.store("commit_written_bundle", receipt)

    async def verify_batch(self, *args):
        return await self._adapt("verify_batch", *args)

    async def verify_blocks(self, *args):
        return await self._adapt("verify_blocks", *args)


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
        health_check=None,
        adapters=None,
    ):
        self._manifest_data = _freeze(manifest_data)
        self._router = MigrationDomainRouter(store_factory, journal)
        self._workers, self._health_check = workers, health_check
        self._adapters = dict(adapters or {})

    async def run(self, workspaces, importer_factory):
        async with MigrationIO(self._workers) as io:
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
                    self._adapters,
                )
                try:
                    importer = importer_factory(context)
                    await importer.run_workspace(_freeze(workspace))
                finally:
                    await request("workspace.close", key)

            try:
                await coordinator.run(workspaces, run_workspace)
            finally:
                self._router._contexts.clear()

# -*- coding: utf-8 -*-
"""Publish shares after workspaces, using private stores and owner-task SQL."""

from .importer import ParallelWorkspaceImporter
from .parallel import MigrationCoordinator
from .parallel_import import MigrationDomainRouter, ParallelWorkspaceContext


async def run_parallel_shares(
    importer,
    shares,
    store_factory,
    *,
    io,
    workers=4,
    health_check=None,
):
    router = MigrationDomainRouter(store_factory, importer.journal)
    handlers = router.handlers()
    handlers["share.resolve"] = importer._resolve_share
    target = importer.target

    async def publish(share, request):
        scope, sid, cid = await request("share.resolve", share)
        key, nas = await request("workspace.open", {"scope": scope.values})
        context = ParallelWorkspaceContext({}, key, request, nas, io)
        try:
            publisher = ParallelWorkspaceImporter(context, target)
            await publisher._publish_share(share, scope, sid, cid)
        finally:
            await request("workspace.close", key)

    coordinator = MigrationCoordinator(
        handlers,
        workers=workers,
        health_check=health_check,
    )
    try:
        await coordinator.run(shares, publish)
    finally:
        await router.close()

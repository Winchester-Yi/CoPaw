# -*- coding: utf-8 -*-
"""Bounded independent workspace parsing into disposable inventory records."""

from collections import Counter
from dataclasses import dataclass

from .inventory_v3 import inventory_workspace
from .parallel import MigrationCoordinator, MigrationIO
from .spool import ByteBudget


@dataclass(frozen=True)
class WorkspaceInventory:
    index: int
    record: object
    scope: tuple
    position: str
    sessions: int
    chats: int
    files: int
    blockers: tuple


async def spool_workspaces(
    data,
    spool,
    *,
    workers=4,
    io_workers=4,
    memory_bytes=512 * 1024 * 1024,
):
    """Parse and spool with fixed fan-out and no retained conversation bodies.

    Each parser gets one equal share of the memory budget. The old reader still
    needs a separate preflight size check before this API is used by the CLI.
    """
    if not 1 <= workers <= 32:
        raise ValueError("workers must be between 1 and 32")
    budget = ByteBudget(memory_bytes)
    allowance = memory_bytes // workers
    if allowance < 1:
        raise ValueError("memory budget is smaller than worker count")
    summaries = [None] * len(data["workspaces"])
    async with MigrationIO(io_workers) as io:

        async def parse(index, _request):
            async with budget.reserve(allowance):
                result = await io.call(inventory_workspace, data, index)
                record = await io.call(spool.write, index, result)
                workspace = result["workspaces"][0]
                summaries[index] = WorkspaceInventory(
                    index=index,
                    record=record,
                    scope=tuple(workspace["scope"]),
                    position=workspace["position"],
                    sessions=len(workspace["sessions"]),
                    chats=len(workspace["chats"]),
                    files=len(result["files"]),
                    blockers=tuple(result["blockers"]),
                )

        await MigrationCoordinator({}, workers=workers).run(
            range(len(summaries)),
            parse,
        )
    positions = Counter(item.position for item in summaries)
    if any(count != 1 for count in positions.values()):
        raise ValueError("duplicate workspace inventory position")
    return summaries

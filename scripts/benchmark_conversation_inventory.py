# -*- coding: utf-8 -*-
"""Measure local spooled inventory at 1/4/8/16 workspace workers.

This is an inventory-stage benchmark, not a MySQL migration or NAS acceptance
test. It generates a frozen local tree with sparse Chat/Session content.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import tracemalloc

import psutil

from swe.app.conversation_storage.migration.discovery import source_key
from swe.app.conversation_storage.migration import inventory_parallel
from swe.app.conversation_storage.migration.inventory_parallel import (
    spool_workspaces,
)
from swe.app.conversation_storage.migration.spool import InventorySpool


def _workspace(root, index, populated):
    workspace = root / ("workspace-" + str(index).zfill(5))
    workspace.mkdir()
    if populated:
        (workspace / "sessions").mkdir()
        (workspace / "agent.json").write_text(
            json.dumps({"id": "agent-" + str(index)}),
        )
        (workspace / "chats.json").write_text(
            json.dumps(
                {
                    "chats": [
                        {
                            "id": "chat-" + str(index),
                            "session_id": "session-" + str(index),
                            "user_id": "benchmark",
                            "channel": "console",
                            "created_at": "2026-01-01T00:00:00Z",
                            "updated_at": "2026-01-01T00:00:00Z",
                        },
                    ],
                },
            ),
        )
        (
            workspace
            / "sessions"
            / ("benchmark_session-" + str(index) + ".json")
        ).write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "revision": 1,
                    "agent": {
                        "memory": {
                            "content": [
                                {
                                    "name": "user",
                                    "role": "user",
                                    "content": "benchmark question",
                                },
                                {
                                    "name": "assistant",
                                    "role": "assistant",
                                    "content": "benchmark answer",
                                },
                            ],
                        },
                    },
                },
            ),
        )
    else:
        (workspace / "chats.json").write_text('{"chats": []}')
    for path in workspace.rglob("*"):
        if path.is_file():
            path.chmod(0o444)
        else:
            path.chmod(0o555)
    workspace.chmod(0o555)
    return {
        "service_unit_id": "BENCHMARK-SERVICE",
        "tenant_id": "benchmark",
        "source_id": "snapshot",
        "agent_id": "agent-" + str(index),
        "root": str(workspace),
        "position": source_key("workspace", str(index)),
    }


def _make_writable(root):
    for path in sorted(
        root.rglob("*"),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        if path.is_dir():
            path.chmod(0o755)
    root.chmod(0o755)


async def _measure(data, workspace_root, work_root, workers, io_delay):
    original = inventory_parallel.inventory_workspace

    def delayed_inventory(source, index):
        if io_delay:
            time.sleep(io_delay)
        return original(source, index)

    inventory_parallel.inventory_workspace = delayed_inventory
    process = psutil.Process()
    rss_peak = [process.memory_info().rss]
    stop_sampler = threading.Event()

    def sample_rss():
        while not stop_sampler.wait(0.01):
            rss_peak[0] = max(rss_peak[0], process.memory_info().rss)

    sampler = threading.Thread(target=sample_rss, daemon=True)
    sampler.start()
    tracemalloc.start()
    started = time.perf_counter()
    try:
        with InventorySpool(
            work_root,
            max_bytes=8 * 1024 * 1024 * 1024,
            record_limit=128 * 1024 * 1024,
            protected=(workspace_root,),
        ) as spool:
            summaries = await spool_workspaces(
                data,
                spool,
                workers=workers,
                io_workers=workers,
                memory_bytes=512 * 1024 * 1024,
            )
            elapsed = time.perf_counter() - started
            _current, peak = tracemalloc.get_traced_memory()
            rss_peak[0] = max(rss_peak[0], process.memory_info().rss)
            return {
                "workers": workers,
                "workspaces": len(summaries),
                "sessions": sum(item.sessions for item in summaries),
                "chats": sum(item.chats for item in summaries),
                "elapsed_seconds": round(elapsed, 3),
                "workspaces_per_second": round(len(summaries) / elapsed, 1),
                "spool_bytes": spool.used_bytes,
                "python_peak_bytes": peak,
                "sampled_process_peak_rss_bytes": rss_peak[0],
            }
    finally:
        stop_sampler.set()
        sampler.join()
        inventory_parallel.inventory_workspace = original
        tracemalloc.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspaces", type=int, default=10000)
    parser.add_argument("--populated-every", type=int, default=20)
    parser.add_argument("--io-delay-ms", type=float, default=1.0)
    args = parser.parse_args()
    if args.workspaces <= 0 or args.populated_every <= 0:
        parser.error("workspaces and populated-every must be positive")

    with tempfile.TemporaryDirectory(
        prefix="conversation-inventory-bench-",
    ) as temp:
        parent = Path(temp).resolve()
        source = parent / "source"
        work = parent / "work"
        source.mkdir()
        work.mkdir()
        try:
            mappings = [
                _workspace(
                    source,
                    index,
                    index % args.populated_every == 0,
                )
                for index in range(args.workspaces)
            ]
            source.chmod(0o555)
            data = {
                "workspaces": mappings,
                "shares": [],
                "fingerprints": [],
            }
            results = [
                asyncio.run(
                    _measure(
                        data,
                        source,
                        work,
                        workers,
                        args.io_delay_ms / 1000,
                    ),
                )
                for workers in (1, 4, 8, 16)
            ]
            print(
                json.dumps(
                    {
                        "scope": "local spooled inventory only; excludes MySQL/NAS publication and verify/finalize",
                        "configured_io_delay_ms": args.io_delay_ms,
                        "results": results,
                    },
                    sort_keys=True,
                ),
            )
        finally:
            _make_writable(source)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Measure local semantic batch bounds against isolated MySQL and NAS bundles.

This is a local-only baseline. It creates one generated test schema, writes to
one temporary BundleStore root, and drops both in a ``finally`` block. It does
not alter production batch defaults or inspect the user's existing workspace.
"""

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import platform
import subprocess
import tempfile
import time
from uuid import uuid4

import psutil
from agentscope.agent import ReActAgent
from agentscope.formatter import OpenAIChatFormatter
from agentscope.memory import InMemoryMemory
from agentscope.message import Msg
from agentscope.model import ChatModelBase

from swe.app.conversation_storage.bundle import BundleStore
from swe.app.conversation_storage.identity import (
    SessionIdentity,
    WorkspaceScope,
)
from swe.app.conversation_storage.semantic_runtime import (
    SemanticRuntimePublisher,
)
from swe.app.conversation_storage.session_adapter import (
    DatabaseSessionExecution,
    MySQLSessionAdapter,
    SemanticBatch,
)
from swe.database.config import DatabaseConfig
from swe.database.connection import DatabaseConnection

DEFAULT_GROUP = {
    "name": "default",
    "max_bytes": 64 * 1024,
    "max_events": 64,
    "max_seconds": 1.0,
}
GROUPS = [
    {
        "name": "small",
        "max_bytes": 16 * 1024,
        "max_events": 8,
        "max_seconds": 0.1,
    },
    DEFAULT_GROUP,
    {
        "name": "large",
        "max_bytes": 256 * 1024,
        "max_events": 256,
        "max_seconds": 2.0,
    },
]
EVENTS = 128
PAYLOAD_BYTES = 2048
EVENT_INTERVAL_SECONDS = 0.005
MIDPOINT_IDLE_SECONDS = 2.05
SAMPLE_INTERVAL_SECONDS = 0.01
SCOPE = WorkspaceScope(
    "tenant",
    "semantic-benchmark",
    "agent",
    service_unit_id="semantic-benchmark",
)


def latency_summary(values):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {
            "sample_count": 0,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "max_ms": None,
        }

    def nearest_rank(percentile):
        index = max(0, math.ceil(len(ordered) * percentile / 100) - 1)
        return round(ordered[index], 3)

    return {
        "sample_count": len(ordered),
        "p50_ms": nearest_rank(50),
        "p95_ms": nearest_rank(95),
        "p99_ms": nearest_rank(99),
        "max_ms": round(max(ordered), 3),
    }


def _load_dev_database_config(path):
    """Read explicit dev config without changing the caller's environment."""
    values = json.loads(path.read_text(encoding="utf-8"))
    access = values["SWE_DB_ACCESS"]
    return DatabaseConfig(
        host=values["SWE_DB_HOST"],
        port=int(values["SWE_DB_PORT"]),
        user=values["SWE_DB_USER"],
        password=access[4:] if len(access) > 4 else access,
        database=values["SWE_DB_NAME"],
        min_connections=1,
        max_connections=2,
        time_zone="+00:00",
        read_timeout=30,
        write_timeout=30,
    )


async def install_schema(database):
    root = (
        Path(__file__).resolve().parent
        / "sql"
        / "conversation_metadata_tables.sql"
    )
    source = "\n".join(
        line
        for line in root.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("--")
    )
    for statement in source.split(";"):
        if statement.strip():
            await database.execute(statement.strip())


@asynccontextmanager
async def isolated_database(config: DatabaseConfig):
    control = DatabaseConnection(config)
    child = None
    schema = "test_semantic_bench_" + uuid4().hex
    created = False
    try:
        await control.connect()
        await control.execute(
            f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4",
        )
        created = True
        child = DatabaseConnection(
            config.model_copy(update={"database": schema}),
        )
        await child.connect()
        if await child.fetch_all("SHOW FULL TABLES"):
            raise RuntimeError("generated benchmark schema is not empty")
        await install_schema(child)
        yield child, schema
    finally:
        try:
            if child is not None:
                await child.close()
        finally:
            try:
                if created:
                    await control.execute(f"DROP DATABASE `{schema}`")
            finally:
                await control.close()


class _UnusedModel(ChatModelBase):
    async def __call__(self, *args, **kwargs):
        raise AssertionError("the print-only benchmark must not call a model")


class RunMetrics:
    def __init__(self):
        self.pending_events = []
        self.commit_latencies = []
        self.durable_latencies = []
        self.print_latencies = []

    def record_commit(self, root, *, started, finished):
        content = root["agent"]["memory"]["content"]
        watermark = max(
            message.get("metadata", {}).get("benchmark_sequence", 0)
            for message, _ in content
        )
        self.commit_latencies.append((finished - started) * 1000)
        self.durable_latencies.extend(
            (finished - event_started) * 1000
            for sequence, event_started in self.pending_events
            if sequence <= watermark
        )
        self.pending_events = [
            event for event in self.pending_events if event[0] > watermark
        ]


async def sample_resources(database, process, stop, samples):
    while not stop.is_set():
        samples["rss_peak_bytes"] = max(
            samples["rss_peak_bytes"],
            process.memory_info().rss,
        )
        samples["db_pool_sampled_peak_entries"] = max(
            samples["db_pool_sampled_peak_entries"],
            len(database._pool._entries),
        )
        try:
            await asyncio.wait_for(stop.wait(), SAMPLE_INTERVAL_SECONDS)
        except TimeoutError:
            pass


async def emit_events(agent, message, metrics):
    for sequence in range(1, EVENTS + 2):
        event_started = time.perf_counter()
        metrics.pending_events.append((sequence, event_started))
        message.content = "x" * (min(sequence, EVENTS) * PAYLOAD_BYTES)
        message.metadata = {"benchmark_sequence": sequence}
        await agent.print(message, last=sequence == EVENTS + 1)
        metrics.print_latencies.append(
            (time.perf_counter() - event_started) * 1000,
        )
        if sequence == EVENTS // 2 - 1:
            await asyncio.sleep(MIDPOINT_IDLE_SECONDS)
        if sequence <= EVENTS:
            await asyncio.sleep(EVENT_INTERVAL_SECONDS)


def _tree_bytes(root):
    return sum(
        path.stat().st_size for path in root.rglob("*") if path.is_file()
    )


async def run_group(database, nas_root, group, index):
    nas_root.mkdir()
    session_id = "benchmark-" + group["name"]
    identity = SessionIdentity(SCOPE, "alice", "console", session_id)
    bundles = BundleStore(nas_root)
    adapter = MySQLSessionAdapter(database, SCOPE, bundles, channel="console")
    execution = DatabaseSessionExecution(adapter, identity)
    await execution.__aenter__()
    agent = ReActAgent(
        "assistant",
        "Benchmark",
        _UnusedModel("unused", stream=True),
        OpenAIChatFormatter(),
        memory=InMemoryMemory(),
    )
    agent.set_console_output_enabled(False)
    agent.set_msg_queue_enabled(False)
    preserved = {
        "unregistered_module": {"benchmark": "preserve-complete-root"},
    }

    def root_provider():
        return {**execution.state, **preserved, "agent": agent.state_dict()}

    publisher = SemanticRuntimePublisher(
        agent,
        execution,
        root_provider,
        batch=SemanticBatch(
            group["max_bytes"],
            group["max_events"],
            group["max_seconds"],
        ),
    ).install()
    metrics = RunMetrics()
    original_commit = execution.commit_state

    async def timed_commit(state):
        started = time.perf_counter()
        result = await original_commit(state)
        metrics.record_commit(
            state,
            started=started,
            finished=time.perf_counter(),
        )
        return result

    execution.commit_state = timed_commit
    before = await database.fetch_one(
        "SELECT COUNT(*) AS n FROM swe_conversation_publications",
    )
    process = psutil.Process()
    rss_start = process.memory_info().rss
    samples = {"rss_peak_bytes": rss_start, "db_pool_sampled_peak_entries": 0}
    stop = asyncio.Event()
    sampler = asyncio.create_task(
        sample_resources(database, process, stop, samples),
    )
    message = Msg("assistant", "", "assistant")
    started = time.perf_counter()
    try:
        await emit_events(agent, message, metrics)
        publisher.check()
    finally:
        try:
            await publisher.close()
        finally:
            stop.set()
            await sampler
            await execution.__aexit__(None, None, None)
    elapsed = time.perf_counter() - started
    after = await database.fetch_one(
        "SELECT COUNT(*) AS n FROM swe_conversation_publications",
    )
    committed = await database.fetch_one(
        "SELECT COUNT(*) AS n FROM swe_conversation_publications WHERE state='committed'",
    )
    state = await adapter.get_session_state_dict(session_id, "alice")
    final_message = state["agent"]["memory"]["content"][-1][0]
    if (
        metrics.pending_events
        or final_message != message.to_dict()
        or state["unregistered_module"] != preserved["unregistered_module"]
        or int(committed["n"]) != int(after["n"])
    ):
        raise AssertionError(
            "benchmark final root or publication is incomplete",
        )
    return {
        "name": group["name"],
        "batch": group,
        "events": EVENTS + 1,
        "payload_bytes_per_event": PAYLOAD_BYTES,
        "publication_count": int(after["n"] - before["n"]),
        "nas_bytes": _tree_bytes(nas_root),
        "event_print_latency": latency_summary(metrics.print_latencies),
        "event_to_durable_latency": latency_summary(metrics.durable_latencies),
        "flush_commit_latency": latency_summary(metrics.commit_latencies),
        "elapsed_seconds": round(elapsed, 6),
        "rss_start_bytes": rss_start,
        **samples,
        "db_lock_wait_ms": None,
        "db_diagnostics_note": "lock wait instrumentation unavailable in this local run",
        "final_root_verified": True,
        "index": index,
    }


async def benchmark(output, config_path):
    config = _load_dev_database_config(config_path)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(
        prefix="swe-semantic-benchmark-",
    ) as directory:
        root = Path(directory)
        async with isolated_database(config) as (database, schema):
            results = [
                await run_group(database, root / group["name"], group, index)
                for index, group in enumerate(GROUPS)
            ]
            report = {
                "status": "passed",
                "schema": schema,
                "nas_root": str(root),
                "scope": "local-only-isolated-sql-and-temp-bundlestore",
                "event_source": "actual agentscope.ReActAgent.print installed hooks",
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "git_head": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    text=True,
                    cwd=Path(__file__).resolve().parents[1],
                ).strip(),
                "python": platform.python_version(),
                "platform": platform.platform(),
                "events_per_group": EVENTS + 1,
                "workload": {
                    "cumulative_same_message_id": True,
                    "delta_bytes": PAYLOAD_BYTES,
                    "event_interval_seconds": EVENT_INTERVAL_SECONDS,
                    "midpoint_idle_seconds": MIDPOINT_IDLE_SECONDS,
                    "idle_after_sequence": EVENTS // 2 - 1,
                    "resource_sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
                    "group_order": [group["name"] for group in GROUPS],
                },
                "metric_definitions": {
                    "event_print_latency": "before print to print return; includes awaited publication",
                    "event_to_durable_latency": "before print to first successful commit containing its sequence",
                    "flush_commit_latency": "execution.commit_state call duration, including SQL and local bundle IO",
                    "nas_bytes": "logical file bytes retained before temporary directory cleanup",
                    "elapsed_seconds": "producer, fixed pacing/idle, publisher close; excludes setup and final verification",
                    "percentiles": "nearest rank; few flush samples make p99 effectively max",
                },
                "groups": results,
                "limitations": [
                    "single producer and one DatabaseSessionExecution per group",
                    "state-only Session publication; no Turn/model/tool/approval throughput claim",
                    "one measured run per group, sequential order, no statistical significance claim",
                    "5ms is a pacing delay after awaited print, not an independent arrival rate",
                    "RSS and pool occupancy sampled at 10ms; short peaks can be missed",
                    "temporary local filesystem, not a production NAS mount",
                    "database lock wait metrics unavailable and reported null",
                    "results do not tune or validate production batch defaults",
                ],
            }
    report["schema_dropped"] = True
    report["temporary_bundle_root_removed"] = not root.exists()
    report["total_elapsed_seconds"] = round(time.perf_counter() - started, 6)
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="main-tree dev.json (credentials are never included in output)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/private/tmp/conversation-semantic-benchmark.json"),
    )
    args = parser.parse_args()
    report = asyncio.run(benchmark(args.output, args.config))
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

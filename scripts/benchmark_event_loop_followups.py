# -*- coding: utf-8 -*-
"""Reproducible local kernel load; no external model, DB or S3 required."""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import psutil

from swe.agents.memory.chat_checkpoint import CheckpointEvent
from swe.agents.memory.conversation_archive import ConversationArchiveStore
from swe.agents.tools import shell
from swe.agents.tools.shell_output_spool import ShellOutputSpool
from swe.agents.utils.tool_output_compaction import compact_text_output
from swe.app.backup.io_worker import run_backup_io
from swe.app.backup.s3_client import S3BackupClient
from swe.config.context import current_tenant_id
from swe.output_workers import run_output_work
from swe.performance_metrics import METRICS, timed_work
from swe.providers.provider_runtime_cache import ProviderRuntimeCache
from swe.runtime_workers import run_runtime_state_work

_CHAT_ID = "11111111-1111-4111-8111-111111111111"


def percentile(values, fraction):
    import math

    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


async def default_executor_probe(baseline):
    """Compare runtime-state wait with two unrelated default-thread tasks."""
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
    started = [threading.Event(), threading.Event()]

    def occupy(event):
        event.set()
        time.sleep(0.25)

    tasks = [
        asyncio.create_task(asyncio.to_thread(occupy, event))
        for event in started
    ]
    while not all(event.is_set() for event in started):
        await asyncio.sleep(0.001)
    before = time.monotonic()
    if baseline:
        await asyncio.to_thread(
            timed_work,
            "runtime_state",
            before,
            lambda: None,
        )
    else:
        await run_runtime_state_work(lambda: None)
    elapsed_ms = (time.monotonic() - before) * 1000
    await asyncio.gather(*tasks)
    return elapsed_ms


async def shell_load(root, tenant, size_mb, budget, baseline):
    """Run real Unix pipes, with per-request ContextVar and workspace."""
    token = current_tenant_id.set(tenant)
    workspace = root / tenant
    workspace.mkdir()
    code = (
        "import sys; "
        f"chunk=b'line '+b'x'*1018+b'\\n'; "
        f"[sys.stdout.buffer.write(chunk) for _ in range({size_mb * 1024})]"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    spool = (
        None
        if baseline
        else ShellOutputSpool(
            workspace,
            budget,
            shell.smart_decode,
            shell._format_shell_response,
        )
    )
    try:
        if spool is None:
            code, stdout, stderr = await shell._execute_unix_subprocess(
                command,
                workspace,
                60,
                os.environ.copy(),
            )
            response = await run_output_work(
                shell._format_shell_response,
                code,
                stdout,
                stderr,
            )
            display = await run_output_work(
                compact_text_output,
                response,
                max_bytes=budget,
                workspace_dir=workspace,
                artifact_dir=workspace / "tool_result",
            )
            buffered = len(stdout.encode()) + len(stderr.encode())
            display_bytes = len(display.text.encode())
        else:
            with spool.activate():
                code, stdout, stderr = await shell._execute_unix_subprocess(
                    command,
                    workspace,
                    60,
                    os.environ.copy(),
                )
                buffered = spool.buffered_bytes
                text, _ = await run_output_work(
                    spool.prepare_response,
                    code,
                    stdout,
                    stderr,
                    "",
                )
            display_bytes = len(text.encode())
        propagated = await run_runtime_state_work(current_tenant_id.get)
        assert propagated == tenant
        artifacts = list((workspace / "tool_result").glob("*.txt"))
        assert len(artifacts) == 1
        assert artifacts[0].stat().st_size == size_mb * 1024 * 1024 - 1
        assert display_bytes <= budget
        return {
            "tenant": tenant,
            "buffered_at_finish_bytes": buffered,
            "display_bytes": display_bytes,
        }
    finally:
        if spool is not None:
            await spool.aclose()
        current_tenant_id.reset(token)


def checkpoint_fixture(root, event_count):
    store = ConversationArchiveStore(root, cursor_secret=b"benchmark-only")
    directory = store.path_for(_CHAT_ID)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "epochs.json").write_text(
        '{"current_epoch":1}',
        encoding="utf-8",
    )
    with (directory / "events.jsonl").open("w", encoding="utf-8") as target:
        for sequence in range(1, event_count + 1):
            event = CheckpointEvent.new(
                sequence=sequence,
                epoch=1,
                type="message_added",
                facts={
                    "message_id": f"message-{sequence}",
                    "role": "assistant",
                },
            )
            target.write(json.dumps(event.to_dict()) + "\n")
    return store


def mock_s3_pages():
    client = object.__new__(S3BackupClient)
    client.bucket, client.prefix = "benchmark", "backup"

    def page(**params):
        number = int(params.get("ContinuationToken", "0"))
        return {
            "IsTruncated": number < 2,
            "NextContinuationToken": str(number + 1),
        }

    client._s3 = SimpleNamespace(list_objects_v2=page)
    client.list_backups()


async def benchmark(options):
    process = psutil.Process()
    rss_start = process.memory_info().rss
    rss_samples, lag_samples = [rss_start], []
    stop = asyncio.Event()

    async def sample():
        while not stop.is_set():
            planned = time.monotonic() + 0.01
            await asyncio.sleep(0.01)
            lag_samples.append(max(0, (time.monotonic() - planned) * 1000))
            rss_samples.append(process.memory_info().rss)

    with tempfile.TemporaryDirectory(prefix="swe-performance-") as directory:
        root = Path(directory)
        # Fixture creation is excluded from the timed/RSS sampling window.
        store = checkpoint_fixture(root / "archive", options.events)
        rss_start = process.memory_info().rss
        rss_samples[:] = [rss_start]
        sampler = asyncio.create_task(sample())
        started = time.monotonic()
        cache = ProviderRuntimeCache()
        try:
            default_wait = await default_executor_probe(options.baseline)
            read_epoch = (
                store._read_checkpoint_state
                if options.baseline
                else store._read_checkpoint_epoch
            )
            await run_runtime_state_work(read_epoch, _CHAT_ID)
            results = await asyncio.gather(
                *(
                    shell_load(
                        root,
                        f"tenant-{i}",
                        options.output_mb,
                        options.budget,
                        options.baseline,
                    )
                    for i in range(options.tenants)
                ),
                *(run_backup_io(mock_s3_pages) for _ in range(4)),
                *(
                    asyncio.wrap_future(cache.submit(time.sleep, 0.02))
                    for _ in range(12)
                ),
            )
            elapsed = time.monotonic() - started
        finally:
            stop.set()
            await sampler
            cache.init_executor.shutdown()
        report = {
            "mode": (
                "legacy-kernel-simulation"
                if options.baseline
                else "optimized-kernels"
            ),
            "platform": sys.platform,
            "tenant_count": options.tenants,
            "stdout_mb_per_tenant": options.output_mb,
            "checkpoint_events": options.events,
            "shell_results": results[: options.tenants],
            "elapsed_seconds": elapsed,
            "sample_interval_ms": 10,
            "lag_sample_count": len(lag_samples),
            "event_loop_lag_p95_ms": percentile(lag_samples, 0.95),
            "event_loop_lag_p99_ms": percentile(lag_samples, 0.99),
            "rss_start_bytes": rss_start,
            "rss_finish_bytes": process.memory_info().rss,
            "rss_sampled_peak_bytes": max(rss_samples),
            "default_saturation_runtime_wait_ms": default_wait,
            "performance_metrics": METRICS.snapshot(),
            "scope": "local kernels, simulated tenant contexts, mock S3; no HTTP/model/DB",
        }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--tenants", type=int, default=4)
    parser.add_argument("--output-mb", type=int, default=16)
    parser.add_argument("--events", type=int, default=10000)
    parser.add_argument("--budget", type=int, default=4096)
    args = parser.parse_args()
    if min(args.tenants, args.output_mb, args.events, args.budget) < 1:
        parser.error("load arguments must be positive")
    asyncio.run(benchmark(args))

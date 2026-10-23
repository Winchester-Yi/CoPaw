# -*- coding: utf-8 -*-
"""Compare local SSE kernels with independent producers/consumers; no HTTP."""

import argparse
import asyncio
import json
import math
import platform
import sys
import time

import psutil

from swe.app.runner.sse_replay import ReplayStream, _BUDGET
from swe.performance_metrics import METRICS


def summary(values):
    ordered = sorted(values)
    result = {"sample_count": len(ordered)}
    for percentile in (50, 95, 99):
        result[f"p{percentile}_ms"] = (
            ordered[max(0, math.ceil(len(ordered) * percentile / 100) - 1)]
            if ordered
            else None
        )
    result["max_ms"] = max(ordered) if ordered else None
    return result


def frame(tenant, index):
    return (
        "data: "
        + json.dumps({"tenant": tenant, "index": index, "text": "x" * 1000})
        + "\n\n"
    )


async def immediate_append(stream, event):
    """Model the old per-event commit path using the current log/read kernel.

    This deliberately bypasses offer/flush: using the compatibility append
    wrapper would measure the new writer's scheduling overhead as the baseline.
    It is not an exact execution of the old binary.
    """
    # The harness runs exactly one producer per stream.
    stream.committed = await stream.log.append(event)
    stream.accepted = stream.committed
    for subscriber in stream.subscribers:
        subscriber.changed.set()


# Keep resource ownership and probe lifetimes together in this standalone harness.
async def benchmark(options):  # pylint: disable=too-many-statements
    process = psutil.Process()
    rss_start = process.memory_info().rss
    peak = rss_start
    pending_peak = 0
    stream_pending_peak = 0
    lag = []
    delivery = []
    admission = []
    stop = asyncio.Event()
    streams = []
    retained = []
    slow = []
    baseline = _BUDGET.snapshot()
    worker_start = METRICS.snapshot()["workers"]["stream"]["completed_count"]

    async def sample():
        nonlocal peak, pending_peak
        while not stop.is_set():
            before = time.monotonic()
            await asyncio.sleep(0.005)
            lag.append(max(0, time.monotonic() - before - 0.005) * 1000)
            peak = max(peak, process.memory_info().rss)
            pending_peak = max(
                pending_peak,
                _BUDGET.snapshot()["pending_memory_bytes"],
            )

    async def run(tenant):  # pylint: disable=too-many-statements
        nonlocal stream_pending_peak
        if options.legacy:
            history, stalled, fast = [], asyncio.Queue(), asyncio.Queue()
            retained.append(history)
            slow.append(stalled)
            stream = None
        else:
            stream = ReplayStream()
            streams.append(stream)
            slow.append(stream.subscribe())
            fast = stream.subscribe()
        published_at = [None] * options.events
        origin = time.monotonic()

        async def consume(index):
            event = await fast.get()
            assert event == frame(tenant, index), (tenant, index, event)
            delivery.append((time.monotonic() - published_at[index]) * 1000)

        async def producer():
            nonlocal stream_pending_peak
            for index in range(options.events):
                if options.rate:
                    await asyncio.sleep(
                        max(
                            0,
                            origin + index / options.rate - time.monotonic(),
                        ),
                    )
                event = frame(tenant, index)
                before = published_at[index] = time.monotonic()
                if options.legacy:
                    history.append(event)
                    stalled.put_nowait(event)
                    fast.put_nowait(event)
                    if index % 64 == 0:
                        await asyncio.sleep(0)
                elif options.immediate:
                    await immediate_append(stream, event)
                elif options.serial:
                    await stream.append(event)
                else:
                    sequence = await stream.offer(event)
                    assert sequence is not None, stream.error
                admission.append((time.monotonic() - before) * 1000)
                if stream is not None:
                    stream_pending_peak = max(
                        stream_pending_peak,
                        stream.pending_bytes,
                    )
                if options.serial:
                    await consume(index)
            if stream is not None:
                await stream.end()
            else:
                fast.put_nowait(None)

        async def consumer():
            for index in range(options.events):
                await consume(index)
            assert await fast.get() is None

        if options.serial:
            await producer()
            assert await fast.get() is None
        else:
            await asyncio.gather(producer(), consumer())

    sampler = asyncio.create_task(sample())
    started = time.monotonic()
    try:
        await asyncio.gather(
            *(run(tenant) for tenant in range(options.tenants)),
        )
        elapsed = time.monotonic() - started
        await asyncio.sleep(0.01)
        budget = _BUDGET.snapshot()
        worker = METRICS.snapshot()["workers"]["stream"]
        result = {
            "mode": (
                "legacy-kernel-simulation"
                if options.legacy
                else (
                    "immediate-current-kernel"
                    if options.immediate
                    else "batched-cursor-replay"
                )
            ),
            "consumer_mode": (
                "serial-handshake" if options.serial else "independent"
            ),
            "tenant_count": options.tenants,
            "events_per_tenant": options.events,
            "target_events_per_second_per_tenant": options.rate or None,
            "elapsed_seconds": elapsed,
            "rss_start_bytes": rss_start,
            "rss_finish_bytes": process.memory_info().rss,
            "rss_sampled_peak_bytes": peak,
            "rss_sample_interval_seconds": 0.005,
            "event_loop_lag": summary(lag),
            "publish_start_to_consume": summary(delivery),
            "publish_call_duration": summary(admission),
            "pending_memory_sampled_peak_bytes": pending_peak,
            "per_stream_pending_observed_peak_bytes": stream_pending_peak,
            "slow_subscriber_buffered_payload_bytes": (
                None
                if options.legacy
                else sum(sub.buffered_bytes for sub in slow)
            ),
            "legacy_slow_queue_entries": (
                sum(queue.qsize() for queue in slow) if options.legacy else 0
            ),
            "replay_budget": budget,
            "replay_counter_deltas": {
                key: budget[key] - baseline[key]
                for key in (
                    "written_records",
                    "write_batches",
                    "written_bytes",
                    "read_bytes",
                    "quota_rejections",
                    "replay_errors",
                )
            },
            "stream_worker": worker,
            "stream_worker_completed_delta": worker["completed_count"]
            - worker_start,
            "environment": {
                "platform": platform.platform(),
                "python": sys.version,
            },
            "scope": (
                "Local kernels, one fast and one stalled subscriber per simulated "
                "tenant; no HTTP/model/DB. Immediate mode models old per-event "
                "publication on the current log/read implementation, not the old binary."
            ),
        }
    finally:
        stop.set()
        await sampler
        for stream in streams:
            await stream.aclose()
    result["replay_budget_after_close"] = _BUDGET.snapshot()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--legacy",
        action="store_true",
        help="in-memory history/queue simulation",
    )
    mode.add_argument(
        "--immediate",
        action="store_true",
        help="per-event commit on current kernel; not old binary",
    )
    parser.add_argument(
        "--serial",
        action="store_true",
        help="original publish/get handshake instead of independent consumer",
    )
    parser.add_argument("--tenants", type=int, default=4)
    parser.add_argument("--events", type=int, default=5000)
    parser.add_argument(
        "--rate",
        type=float,
        default=0,
        help="target events/second per tenant; 0 means unpaced burst",
    )
    options = parser.parse_args()
    if options.tenants < 1 or options.events < 1 or options.rate < 0:
        parser.error(
            "tenants/events must be positive; rate must be nonnegative",
        )
    asyncio.run(benchmark(options))

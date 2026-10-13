# -*- coding: utf-8 -*-
"""Capture and storage faults preserve the actual model caller's result."""

import asyncio
import copy
from types import SimpleNamespace

import pytest

from swe.token_diagnostics.collector import Collector
from swe.token_diagnostics.config import DiagnosticConfig
from swe.token_diagnostics.context import accounting_run
from swe.token_diagnostics.model_observer import (
    AttemptObserver,
    LogicalObserver,
)


class Store:
    async def write_batch(self, records):
        self.records = copy.deepcopy(records)

    async def close(self):
        pass


def pending(collector, kind):
    return [
        record["data"]
        for record, _, _ in collector.writer.pending.values()
        if record["kind"] == kind
    ]


def assert_bounded(collector):
    writer = collector.writer
    assert writer.active_count == writer.active_bytes == 0
    assert (
        writer.pending_count + writer.inflight_count
        <= collector.config.max_records
    )
    assert (
        writer.buffer_bytes + writer.active_bytes + writer.inflight_bytes
        <= collector.config.max_bytes
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing_key", "byte_limit", "time_budget"])
async def test_capture_fault_preserves_response_and_independent_usage(
    fault,
    monkeypatch,
):
    checks = 0

    def clock():
        nonlocal checks
        checks += 1
        return 0.006 if fault == "time_budget" and checks > 7 else 0.0

    monkeypatch.setattr(
        "swe.token_diagnostics.context_capture.monotonic",
        clock,
    )
    collector = Collector(
        DiagnosticConfig(
            enabled=True,
            context_enabled=True,
            fingerprint_key="" if fault == "missing_key" else "test-key",
            flush_interval=60,
        ),
        Store(),
    )
    response = SimpleNamespace(usage={"input_tokens": 7, "output_tokens": 2})

    class Model:
        async def __call__(self, messages, **kwargs):
            return response

    try:
        with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
            actual = await LogicalObserver(AttemptObserver(Model(), "p", {}))(
                [{"role": "user", "content": "SECRET" + "x" * 1_000_000}],
            )
        assert actual is response
        call = pending(collector, "call")[0]
        context = pending(collector, "context")[0]
        assert (call["input_tokens"], call["output_tokens"]) == (7, 2)
        assert call["context_id"] == context["context_id"]
        assert context["capture_reason"] == fault
        assert context["capture_state"] == (
            "unavailable" if fault == "missing_key" else "partial"
        )
        assert context["expected_item_count"] is None
        if fault == "missing_key":
            assert context["text_bytes"] is None
        else:
            assert 0 < context["text_bytes"] < 1_000_006
            assert context["occurrences"][0]["content_digest"] is None
        assert "SECRET" not in repr(context)
        assert len(context["blocks"]) <= 1024
        assert (
            context["encoded_bytes"] is None
            or context["encoded_bytes"] <= 256 * 1024
        )
        assert_bounded(collector)
    finally:
        await collector.close()


@pytest.mark.asyncio
async def test_full_buffer_preserves_response_and_marks_lost_evidence():
    collector = Collector(
        DiagnosticConfig(enabled=True, context_enabled=True, max_bytes=1),
        Store(),
    )
    response = SimpleNamespace(usage={"input_tokens": 7, "output_tokens": 2})

    class Model:
        async def __call__(self, messages, **kwargs):
            return response

    try:
        with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
            actual = await LogicalObserver(AttemptObserver(Model(), "p", {}))(
                [],
            )
        assert actual is response
        assert collector.writer.pending_count == 0
        assert pending(collector, "call") == []
        assert sum(collector.writer.gaps.values()) > 0
        assert_bounded(collector)
    finally:
        await collector.close()


@pytest.mark.asyncio
async def test_database_timeout_does_not_delay_caller_or_fill_unknown_usage():
    started = asyncio.Event()

    class HangingStore(Store):
        async def write_batch(self, records):
            started.set()
            await asyncio.Event().wait()

    collector = Collector(
        DiagnosticConfig(
            enabled=True,
            context_enabled=True,
            fingerprint_key="test-key",
            flush_interval=60,
            write_timeout=0.01,
            shutdown_timeout=0.02,
        ),
        HangingStore(),
    )
    response = SimpleNamespace(usage={"input_tokens": 7})
    flush = None

    class Model:
        async def __call__(self, messages, **kwargs):
            nonlocal flush
            flush = asyncio.create_task(collector.writer.flush_once())
            await asyncio.wait_for(started.wait(), timeout=0.5)
            assert collector.writer.inflight_count > 0
            assert not flush.done()
            return response

    try:
        with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
            actual = await LogicalObserver(AttemptObserver(Model(), "p", {}))(
                [],
            )
        assert actual is response
        assert await asyncio.wait_for(flush, timeout=0.5) is False
        call = pending(collector, "call")[0]
        assert call["revision"] == 2
        assert call["input_tokens"] == 7
        assert call["output_tokens"] is None
        assert call["usage_status"] == "partial"
        assert (
            collector.writer.inflight_count
            == collector.writer.inflight_bytes
            == 0
        )
        assert_bounded(collector)
    finally:
        await asyncio.wait_for(collector.close(), timeout=0.5)

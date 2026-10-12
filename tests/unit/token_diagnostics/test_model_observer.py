# -*- coding: utf-8 -*-
"""Observable requests preserve output and close numeric attempt records."""

import asyncio
from types import SimpleNamespace

import pytest


def implementation():
    try:
        from swe.token_diagnostics.context import accounting_run
        from swe.token_diagnostics.collector import Collector
        from swe.token_diagnostics.config import DiagnosticConfig
        from swe.token_diagnostics.model_observer import (
            AttemptObserver,
            LogicalObserver,
        )
    except ModuleNotFoundError:
        pytest.fail("Token attempt observation is missing")
    return (
        accounting_run,
        Collector,
        DiagnosticConfig,
        AttemptObserver,
        LogicalObserver,
    )


class MemoryStore:
    async def write_batch(self, records):
        pass

    async def close(self):
        pass


def calls(collector):
    return [
        entry[0]["data"]
        for entry in collector.writer.pending.values()
        if entry[0]["kind"] == "call"
    ]


@pytest.mark.asyncio
async def test_zero_usage_and_provider_output_are_preserved():
    run, collector_cls, config_cls, attempt_cls, logical_cls = implementation()
    collector = collector_cls(config_cls(enabled=True), MemoryStore())
    response = SimpleNamespace(
        usage={"input_tokens": 0, "output_tokens": 0}, content="unchanged"
    )

    class Model:
        async def __call__(self, messages, **kwargs):
            return response

    model = logical_cls(attempt_cls(Model(), "p", {}))
    with run({"source_id": "s", "tenant_id": "t"}, collector=collector):
        assert await model([{"role": "user", "content": "hi"}]) is response
    data = calls(collector)[0]
    assert data["usage_status"] == "reported"
    assert data["execution_state"] == "completed"
    assert data["input_tokens"] == 0
    await collector.close()


@pytest.mark.asyncio
async def test_retry_attempts_share_logical_id_and_keep_distinct_call_ids():
    run, collector_cls, config_cls, attempt_cls, logical_cls = implementation()
    collector = collector_cls(config_cls(enabled=True), MemoryStore())

    class Model:
        count = 0

        async def __call__(self, messages, **kwargs):
            self.count += 1
            if self.count == 1:
                raise ValueError("private error text")
            return SimpleNamespace(
                usage={"input_tokens": 10, "output_tokens": 2}
            )

    attempt = attempt_cls(Model(), "p", {})

    class Retry:
        async def __call__(self, messages, **kwargs):
            try:
                return await attempt(messages, **kwargs)
            except ValueError:
                return await attempt(messages, **kwargs)

    with run({"source_id": "s", "tenant_id": "t"}, collector=collector):
        await logical_cls(Retry())([{"role": "user", "content": "hi"}])
    data = calls(collector)
    assert len(data) == 2
    assert len({x["call_id"] for x in data}) == 2
    assert len({x["logical_call_id"] for x in data}) == 1
    assert [x["attempt_no"] for x in data] == [1, 2]
    assert "private error text" not in str(data)
    await collector.close()


@pytest.mark.asyncio
async def test_explicit_stream_close_keeps_partial_usage_and_closes_provider():
    run, collector_cls, config_cls, attempt_cls, logical_cls = implementation()
    collector = collector_cls(config_cls(enabled=True), MemoryStore())
    closed = []

    class Model:
        async def __call__(self, messages, **kwargs):
            async def stream():
                try:
                    yield SimpleNamespace(
                        usage={"input_tokens": 10, "output_tokens": 1}
                    )
                    await asyncio.Event().wait()
                finally:
                    closed.append(True)

            return stream()

    with run({"source_id": "s", "tenant_id": "t"}, collector=collector):
        result = await logical_cls(attempt_cls(Model(), "p", {}))(
            [{"role": "user", "content": "hi"}]
        )
        await anext(result)
        await result.aclose()
    data = calls(collector)[0]
    assert data["execution_state"] == "cancelled"
    assert data["usage_status"] == "partial"
    assert closed == [True]
    await collector.close()


@pytest.mark.asyncio
async def test_stream_cumulative_usage_is_not_summed():
    run, collector_cls, config_cls, attempt_cls, logical_cls = implementation()
    collector = collector_cls(config_cls(enabled=True), MemoryStore())

    class Model:
        async def __call__(self, messages, **kwargs):
            async def stream():
                for count in [1, 2, 3]:
                    yield SimpleNamespace(
                        usage={"input_tokens": 10, "output_tokens": count}
                    )

            return stream()

    with run({"source_id": "s", "tenant_id": "t"}, collector=collector):
        stream = await logical_cls(attempt_cls(Model(), "p", {}))([])
        async for _ in stream:
            pass
    assert calls(collector)[0]["output_tokens"] == 3
    await collector.close()


@pytest.mark.asyncio
async def test_numeric_capture_failure_does_not_replace_model_response(
    monkeypatch,
):
    run, collector_cls, config_cls, attempt_cls, logical_cls = implementation()
    collector = collector_cls(config_cls(enabled=True), MemoryStore())
    response = SimpleNamespace(content="original")

    class Model:
        async def __call__(self, messages, **kwargs):
            return response

    def broken(*args):
        raise RuntimeError("capture failure")

    monkeypatch.setattr(
        "swe.token_diagnostics.model_observer.extract_usage", broken
    )
    with run({"source_id": "s", "tenant_id": "t"}, collector=collector):
        assert await logical_cls(attempt_cls(Model(), "p", {}))([]) is response
    await collector.close()


@pytest.mark.asyncio
async def test_model_stream_does_not_leak_scope_and_can_close_in_another_task():
    run, collector_cls, config_cls, attempt_cls, logical_cls = implementation()
    from swe.token_diagnostics.context import current_logical

    collector = collector_cls(config_cls(enabled=True), MemoryStore())

    class Model:
        async def __call__(self, messages, **kwargs):
            async def stream():
                yield SimpleNamespace(usage=None)
                yield SimpleNamespace(usage=None)

            return stream()

    with run({"source_id": "s", "tenant_id": "t"}, collector=collector):
        result = await logical_cls(attempt_cls(Model(), "p", {}))([])
        await anext(result)
        assert current_logical() is None
        await asyncio.create_task(result.aclose())
        assert current_logical() is None
    await collector.close()


@pytest.mark.asyncio
async def test_stream_closed_before_first_chunk_finishes_attempt_and_releases_budget():
    run, collector_cls, config_cls, attempt_cls, logical_cls = implementation()
    collector = collector_cls(config_cls(enabled=True), MemoryStore())

    class Model:
        async def __call__(self, messages, **kwargs):
            async def stream():
                yield SimpleNamespace(usage=None)

            return stream()

    with run({"source_id": "s", "tenant_id": "t"}, collector=collector):
        result = await logical_cls(attempt_cls(Model(), "p", {}))([])
        await result.aclose()
    assert calls(collector)[0]["execution_state"] == "cancelled"
    assert collector.writer.active_bytes == 0
    await collector.close()


@pytest.mark.asyncio
async def test_stream_refinements_keep_known_input_and_late_finish_reason():
    run, collector_cls, config_cls, attempt_cls, logical_cls = implementation()
    collector = collector_cls(config_cls(enabled=True), MemoryStore())

    class Model:
        async def __call__(self, messages, **kwargs):
            async def stream():
                yield SimpleNamespace(usage={"input_tokens": 10})
                yield SimpleNamespace(usage={"output_tokens": 20})
                yield SimpleNamespace(usage=None, finish_reason="length")

            return stream()

    with run({"source_id": "s", "tenant_id": "t"}, collector=collector):
        result = await logical_cls(attempt_cls(Model(), "p", {}))([])
        async for _ in result:
            pass
    data = calls(collector)[0]
    assert (data["input_tokens"], data["output_tokens"]) == (10, 20)
    assert data["finish_reason"] == "length"
    await collector.close()

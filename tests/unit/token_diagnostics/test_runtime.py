# -*- coding: utf-8 -*-
"""Accounting must survive absent Trace records and preserve query output."""

from types import SimpleNamespace

import pytest


def implementation():
    try:
        from swe.token_diagnostics.runtime import (
            accounting_query,
            accounting_subagent,
        )
        from swe.token_diagnostics.context import (
            accounting_run,
            child_snapshot,
        )
        from swe.token_diagnostics.collector import Collector
        from swe.token_diagnostics.config import DiagnosticConfig
    except ModuleNotFoundError:
        pytest.fail("Runtime accounting integration is missing")
    return (
        accounting_query,
        accounting_subagent,
        accounting_run,
        child_snapshot,
        Collector,
        DiagnosticConfig,
    )


class Store:
    async def write_batch(self, records):
        pass

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_query_without_trace_preserves_frames_and_records_cancelled_close(
    monkeypatch,
):
    decorate, _, _, _, collector_cls, config_cls = implementation()
    collector = collector_cls(config_cls(enabled=True), Store())
    monkeypatch.setattr(
        "swe.token_diagnostics.context.get_collector", lambda: collector
    )

    class Runner:
        agent_id = "agent"

        @decorate
        async def query_handler(self, msgs, request=None):
            yield "first", False
            yield "last", True

    request = SimpleNamespace(
        user_id="tenant", source_id="source", trace_id=None, channel_meta={}
    )
    stream = Runner().query_handler([], request)
    assert await anext(stream) == ("first", False)
    await stream.aclose()
    run = next(
        x[0]["data"]
        for x in collector.writer.pending.values()
        if x[0]["kind"] == "run"
    )
    assert run["tenant_id"] == "tenant"
    assert run["trace_id"] is None
    assert run["execution_state"] == "cancelled"
    await collector.close()


@pytest.mark.asyncio
async def test_child_scope_has_own_identity_and_declared_parent(monkeypatch):
    _, decorate, run, child_snapshot, collector_cls, config_cls = (
        implementation()
    )
    collector = collector_cls(config_cls(enabled=True), Store())
    monkeypatch.setattr(
        "swe.token_diagnostics.context.get_collector", lambda: collector
    )

    class Runtime:
        @decorate
        async def run(self, **kwargs):
            from swe.token_diagnostics.context import current_branch

            return current_branch().fields

    with run(
        {"source_id": "s", "tenant_id": "t"}, collector=collector
    ) as parent:
        snapshot = child_snapshot()
        result = await Runtime().run(
            request_context={
                "source_id": "s",
                "tenant_id": "t",
                "_token_scope": snapshot,
            }
        )
        assert result["task_run_id"] == parent.fields["task_run_id"]
        assert result["branch_run_id"] != parent.fields["branch_run_id"]
        assert result["parent_branch_run_id"] == parent.fields["branch_run_id"]
    await collector.close()


@pytest.mark.asyncio
async def test_input_provenance_survives_logical_model_scope():
    _, _, run, _, collector_cls, config_cls = implementation()
    from swe.token_diagnostics.provenance import register_origin
    from swe.token_diagnostics.model_observer import (
        AttemptObserver,
        LogicalObserver,
    )

    collector = collector_cls(
        config_cls(enabled=True, fingerprint_key="key"), Store()
    )

    class Model:
        async def __call__(self, messages, **kwargs):
            return SimpleNamespace(
                usage={"input_tokens": 10, "output_tokens": 2}
            )

    with run(
        {"source_id": "s", "tenant_id": "t"}, collector=collector
    ) as branch:
        register_origin(
            "hook input",
            branch.scope_key,
            collector.config,
            "inject",
            "hook-1",
        )
        await LogicalObserver(AttemptObserver(Model(), "p", {}))(
            [{"role": "system", "content": "hook input"}]
        )
    call = next(
        x[0]["data"]
        for x in collector.writer.pending.values()
        if x[0]["kind"] == "call"
    )
    assert call["items"][0]["category"] == "inject"
    await collector.close()


@pytest.mark.asyncio
async def test_external_auxiliary_call_has_unknown_usage_not_zero():
    _, _, run, _, collector_cls, config_cls = implementation()
    from swe.token_diagnostics.runtime import external_call

    collector = collector_cls(config_cls(enabled=True), Store())
    with run({"source_id": "s", "tenant_id": "t"}, collector=collector):
        with external_call("title"):
            pass
    records = [
        x[0]["data"]
        for x in collector.writer.pending.values()
        if x[0]["kind"] == "call"
    ]
    assert len(records) == 1
    assert records[0]["purpose"] == "title"
    assert records[0]["input_tokens"] is None
    assert records[0]["usage_status"] == "unavailable"
    await collector.close()


@pytest.mark.asyncio
async def test_query_stream_can_be_closed_from_another_task(monkeypatch):
    import asyncio

    decorate, _, _, _, collector_cls, config_cls = implementation()
    from swe.token_diagnostics.context import current_branch

    collector = collector_cls(config_cls(enabled=True), Store())
    monkeypatch.setattr(
        "swe.token_diagnostics.context.get_collector", lambda: collector
    )

    class Runner:
        @decorate
        async def query_handler(self, msgs, request=None):
            yield "first"
            yield "last"

    stream = Runner().query_handler(
        [], SimpleNamespace(user_id="t", source_id="s", channel_meta={})
    )
    assert await anext(stream) == "first"
    assert current_branch() is None
    await asyncio.create_task(stream.aclose())
    assert current_branch() is None
    await collector.close()

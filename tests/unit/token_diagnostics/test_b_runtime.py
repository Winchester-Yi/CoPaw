# -*- coding: utf-8 -*-
"""Runtime B capture observes actual mutations without changing execution."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from swe.token_diagnostics.collector import Collector
from swe.token_diagnostics.config import DiagnosticConfig
from swe.token_diagnostics.context import accounting_run
from swe.token_diagnostics.model_observer import (
    AttemptObserver,
    LogicalObserver,
)
from swe.token_diagnostics.writer import DiagnosticWriter


class Store:
    def __init__(self):
        self.records = []

    async def write_batch(self, records):
        self.records.extend(copy.deepcopy(records))

    async def close(self):
        pass


def records(collector, kind):
    return [
        v[0]["data"]
        for v in collector.writer.pending.values()
        if v[0]["kind"] == kind
    ]


@pytest.fixture
async def collector():
    value = Collector(
        DiagnosticConfig(
            enabled=True,
            context_enabled=True,
            fingerprint_key="test-key",
        ),
        Store(),
    )
    yield value
    await value.close()


@pytest.mark.asyncio
async def test_prepared_context_uses_attempt_id_and_final_request_times(
    collector,
):
    response = SimpleNamespace(usage={"input_tokens": 7, "output_tokens": 2})

    class Model:
        model_name = "model"

        async def __call__(self, messages, **kwargs):
            context = records(collector, "context")[0]
            call = records(collector, "call")[0]
            assert context["context_id"] == call["context_id"]
            assert context["context_revision"] == 1
            assert context["request_started_at"] == call["started_at"]
            assert context["request_ended_at"] is None
            assert collector.writer.active_bytes >= len(
                json.dumps(call).encode(),
            )
            return response

    with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
        assert (
            await LogicalObserver(AttemptObserver(Model(), "p", {}))(
                [{"role": "user", "content": "hello"}],
            )
            is response
        )
    call = records(collector, "call")[0]
    context = records(collector, "context")[0]
    assert context["context_revision"] == 2
    assert context["request_ended_at"] == call["ended_at"]
    assert context["capture_state"] == "complete"
    assert context["agent_id"] == call["agent_id"]
    assert call["input_tokens"] == 7
    assert call["items"] == []
    assert call["capture_state"] == "unavailable"
    assert call["estimated_input_tokens"] is None
    assert call["fingerprint_available"] is False
    assert "business_step_id" not in context
    assert collector.writer.active_bytes == 0


@pytest.mark.asyncio
async def test_b_does_not_run_unbounded_legacy_capture(collector, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("legacy full serialization invoked")

    monkeypatch.setattr(
        "swe.token_diagnostics.model_observer.capture_inputs",
        forbidden,
    )

    class Model:
        async def __call__(self, messages, **kwargs):
            return SimpleNamespace(
                usage={"input_tokens": 1, "output_tokens": 1},
            )

    with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
        await LogicalObserver(AttemptObserver(Model(), "p", {}))([])
    assert records(collector, "context")[0]["capture_state"] == "complete"


@pytest.mark.asyncio
async def test_flag_off_preserves_a_capture():
    collector = Collector(
        DiagnosticConfig(enabled=True, fingerprint_key="k"),
        Store(),
    )

    class Model:
        async def __call__(self, messages, **kwargs):
            return SimpleNamespace(usage=None)

    with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
        await LogicalObserver(AttemptObserver(Model(), "p", {}))(
            [{"role": "user", "content": "hello"}],
        )
    assert records(collector, "context") == []
    assert records(collector, "call")[0]["items"][0]["fingerprint"]
    assert records(collector, "call")[0]["schema_version"] == "1.0"
    await collector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,identity,revision_key",
    [
        ("context", "context_id", "context_revision"),
        ("context_operation", "operation_id", "revision"),
    ],
)
async def test_writer_b_records_are_atomic_and_revision_aware(
    kind,
    identity,
    revision_key,
):
    store = Store()
    writer = DiagnosticWriter(store, DiagnosticConfig(enabled=True))
    data = {
        identity: "id",
        "branch_run_id": "b",
        revision_key: 2,
        "occurrences": [{"occurrence_index": 0}],
        "blocks": [{"block_index": 0}],
    }
    assert writer.add({"kind": kind, "data": data})
    assert writer.add({"kind": kind, "data": {**data, revision_key: 1}})
    data["blocks"].clear()
    await writer.flush_once()
    assert len(store.records) == 1
    assert store.records[0]["data"][revision_key] == 2
    assert len(store.records[0]["data"]["blocks"]) == 1


@pytest.mark.asyncio
async def test_writer_context_final_revision_replaces_initial():
    store = Store()
    writer = DiagnosticWriter(store, DiagnosticConfig(enabled=True))
    data = {"context_id": "c", "branch_run_id": "b", "context_revision": 1}
    assert writer.add({"kind": "context", "data": data})
    assert writer.add(
        {"kind": "context", "data": {**data, "context_revision": 2}},
    )
    await writer.flush_once()
    assert store.records[0]["data"]["context_revision"] == 2


@pytest.mark.asyncio
async def test_summary_binding_does_not_cross_scope_or_copied_finished_context(
    collector,
):
    from swe.token_diagnostics.context import create_branch, bind_branch
    from swe.token_diagnostics.context_operations import (
        bind_context_operation,
        associate_summary_attempt,
    )
    from swe.token_diagnostics.runtime import (
        begin_context_operation,
        finish_context_operation,
    )

    with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
        operation = begin_context_operation([], "compact")
        with bind_context_operation(operation):
            copied = __import__("contextvars").copy_context()
            other = create_branch(
                {"source_id": "s", "tenant_id": "other"},
                collector,
            )
            with bind_branch(other):
                assert associate_summary_attempt(other, "foreign-call") is None
            finish_context_operation(operation, None, "skipped")
            assert (
                copied.run(
                    associate_summary_attempt,
                    operation.branch,
                    "late-call",
                )
                is None
            )


@pytest.mark.asyncio
async def test_capture_adapter_failures_preserve_mutation_and_cancel(
    collector,
    monkeypatch,
):
    from swe.token_diagnostics.runtime import begin_context_operation
    from swe.agents.hooks.memory_compaction import MemoryCompactionHook
    from swe.config.config import ToolResultCompactConfig

    def broken(*args, **kwargs):
        raise ValueError("telemetry failed")

    monkeypatch.setattr(
        "swe.token_diagnostics.context_operations.begin_context_operation",
        broken,
    )
    changed = []

    class Manager:
        async def compact_tool_result(self, **kwargs):
            changed.append(True)

    running = SimpleNamespace(tool_result_compact=ToolResultCompactConfig())
    with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
        await MemoryCompactionHook(Manager())._compact_tool_results_if_enabled(
            [],
            running,
        )
        assert changed == [True]
        monkeypatch.setattr(
            "swe.token_diagnostics.context_operations.begin_context_operation",
            lambda *a, **kw: (_ for _ in ()).throw(asyncio.CancelledError()),
        )
        with pytest.raises(asyncio.CancelledError):
            begin_context_operation([], "compact")


@pytest.mark.asyncio
async def test_b_stream_closed_without_first_chunk_finishes_context(collector):
    class Model:
        async def __call__(self, messages, **kwargs):
            async def stream():
                yield SimpleNamespace(usage=None)

            return stream()

    with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
        stream = await LogicalObserver(AttemptObserver(Model(), "p", {}))([])
        await stream.aclose()
    context = records(collector, "context")[0]
    assert context["context_revision"] == 2
    assert context["execution_state"] == "cancelled"
    assert context["request_ended_at"]
    assert collector.writer.active_count == collector.writer.active_bytes == 0


@pytest.mark.asyncio
async def test_writer_context_budget_counts_active_inflight_and_retry_metadata():
    class SlowStore(Store):
        started = asyncio.Event()

        async def write_batch(self, records):
            self.started.set()
            await asyncio.Event().wait()

    store = SlowStore()
    collector = Collector(DiagnosticConfig(enabled=True, max_records=2), store)
    writer = collector.writer
    record = {
        "kind": "context",
        "data": {
            "context_id": "c",
            "branch_run_id": "b",
            "context_revision": 1,
        },
    }
    assert writer.add(record)
    assert collector.reserve(100)
    task = asyncio.create_task(writer.flush_once())
    await store.started.wait()
    assert not writer.add(
        {"kind": "context", "data": {**record["data"], "context_id": "d"}},
    )
    assert writer.inflight_count == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.pending_count == 1
    assert writer.inflight_bytes == 0
    collector.release(100)
    writer.store = Store()
    await collector.close()


def test_writer_rejects_whole_oversized_context():
    writer = DiagnosticWriter(
        Store(),
        DiagnosticConfig(enabled=True, max_record_bytes=100),
    )
    assert not writer.add(
        {
            "kind": "context",
            "data": {
                "context_id": "id",
                "branch_run_id": "b",
                "context_revision": 1,
                "blocks": [{"chained_digest": "x" * 256}],
            },
        },
    )
    assert writer.pending_count == 0
    assert writer.gaps["b"] == 1


def test_writer_budget_includes_gap_metadata():
    writer = DiagnosticWriter(Store(), DiagnosticConfig(enabled=True))
    assert writer.add(
        {
            "kind": "context",
            "data": {
                "context_id": "c",
                "branch_run_id": "b",
                "context_revision": 1,
            },
        },
    )
    row = next(iter(writer.pending.values()))[0]
    assert "gap_count" in row["data"] and "gap_tracking_limited" in row["data"]
    assert writer.buffer_bytes == len(
        json.dumps(row, ensure_ascii=False).encode(),
    )


@pytest.mark.asyncio
async def test_operation_metadata_reservation_released_at_finish(collector):
    from swe.token_diagnostics.runtime import (
        begin_context_operation,
        finish_context_operation,
    )

    with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
        operation = begin_context_operation([], "compact")
        assert collector.writer.active_count == 1
        assert collector.writer.active_bytes > 0
        finish_context_operation(operation, [], "completed")
        assert (
            collector.writer.active_count == collector.writer.active_bytes == 0
        )


@pytest.mark.asyncio
async def test_operations_record_missing_sides_and_do_not_invent_relations(
    collector,
):
    from swe.token_diagnostics.runtime import (
        begin_context_operation,
        finish_context_operation,
    )

    with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
        operation = begin_context_operation(None, "compact")
        assert operation.operation_id
        finish_context_operation(operation, None, "skipped")
    row = records(collector, "context_operation")[0]
    assert row["operation_state"] == "skipped"
    assert row["missing_before"] is True and row["missing_after"] is True
    assert row["segments"] == []
    assert records(collector, "context") == []


@pytest.mark.asyncio
async def test_operation_summary_binding_is_explicit_and_expires(collector):
    from swe.token_diagnostics.runtime import (
        begin_context_operation,
        finish_context_operation,
    )
    from swe.token_diagnostics.context_operations import bind_context_operation

    class Model:
        async def __call__(self, messages, **kwargs):
            return SimpleNamespace(usage=None)

    model = LogicalObserver(AttemptObserver(Model(), "p", {}))
    with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
        operation = begin_context_operation([], "compact")
        with bind_context_operation(operation):
            await model([])
            await model([])
            finish_context_operation(operation, [], "completed")
            await model([])
    calls = records(collector, "call")
    contexts = {
        row["call_id"]: row
        for row in records(collector, "context")
        if row.get("call_id")
    }
    assert (
        contexts[calls[0]["call_id"]]["operation_id"] == operation.operation_id
    )
    assert (
        contexts[calls[1]["call_id"]]["operation_id"] == operation.operation_id
    )
    assert contexts[calls[2]["call_id"]]["operation_id"] is None
    assert (
        records(collector, "context_operation")[0]["summary_call_id"]
        == calls[0]["call_id"]
    )


@pytest.mark.asyncio
async def test_tool_compaction_captures_real_mutation_and_propagates_cancel(
    collector,
):
    from agentscope.message import Msg
    from swe.agents.hooks.memory_compaction import MemoryCompactionHook
    from swe.config.config import ToolResultCompactConfig

    message = Msg(
        "tool",
        [{"type": "tool_result", "id": "c", "output": "old"}],
        "assistant",
    )

    class Manager:
        async def compact_tool_result(self, messages, **kwargs):
            messages[0].content[0]["output"] = "new"

    running = SimpleNamespace(tool_result_compact=ToolResultCompactConfig())
    with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
        await MemoryCompactionHook(Manager())._compact_tool_results_if_enabled(
            [message],
            running,
        )
    contexts = records(collector, "context")
    assert {c["context_role"] for c in contexts} == {"before", "after"}
    assert (
        contexts[0]["occurrences"][0]["content_digest"]
        != contexts[1]["occurrences"][0]["content_digest"]
    )
    assert records(collector, "context_operation")[0]["segments"] == []

    class CancelManager:
        async def compact_tool_result(self, **kwargs):
            raise asyncio.CancelledError

    with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
        with pytest.raises(asyncio.CancelledError):
            await MemoryCompactionHook(
                CancelManager(),
            )._compact_tool_results_if_enabled([message], running)
    assert (
        records(collector, "context_operation")[-1]["operation_state"]
        == "cancelled"
    )


@pytest.mark.asyncio
async def test_capture_error_keeps_provider_usage_and_same_context_id(
    collector,
    monkeypatch,
):
    def broken(*args, **kwargs):
        raise ValueError("private body")

    monkeypatch.setattr(
        "swe.token_diagnostics.model_observer.capture_context",
        broken,
    )
    response = SimpleNamespace(usage={"input_tokens": 4, "output_tokens": 5})

    class Model:
        async def __call__(self, messages, **kwargs):
            return response

    with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
        assert (
            await LogicalObserver(AttemptObserver(Model(), "p", {}))([])
            is response
        )
    call = records(collector, "call")[0]
    context = records(collector, "context")[0]
    assert call["input_tokens"] == 4
    assert context["context_id"] == call["context_id"]
    assert context["capture_reason"] == "capture_error"
    assert "private body" not in str(context)


@pytest.mark.asyncio
async def test_snapshot_adapter_error_does_not_replace_response(
    collector,
    monkeypatch,
):
    def broken(*args, **kwargs):
        raise ValueError("adapter failed")

    monkeypatch.setattr(
        "swe.token_diagnostics.model_observer.prepared_snapshot",
        broken,
    )
    response = SimpleNamespace(usage={"input_tokens": 4, "output_tokens": 5})

    class Model:
        async def __call__(self, messages, **kwargs):
            return response

    with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
        assert (
            await LogicalObserver(AttemptObserver(Model(), "p", {}))([])
            is response
        )
    assert records(collector, "call")[0]["input_tokens"] == 4


@pytest.mark.asyncio
async def test_queue_overflow_does_not_keep_unreserved_active_attempt():
    collector = Collector(
        DiagnosticConfig(
            enabled=True,
            context_enabled=True,
            fingerprint_key="k",
            max_bytes=1,
        ),
        Store(),
    )

    class Model:
        async def __call__(self, messages, **kwargs):
            from swe.token_diagnostics.context import current_logical

            assert current_logical().active == {}
            return SimpleNamespace(
                usage={"input_tokens": 4, "output_tokens": 5},
            )

    with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
        await LogicalObserver(AttemptObserver(Model(), "p", {}))([])
    assert collector.writer.active_count == collector.writer.active_bytes == 0
    await collector.close()


@pytest.mark.asyncio
async def test_checkpoint_snapshot_uses_full_online_layer_from_remeasurement(
    collector,
):
    from swe.agents.hooks.memory_compaction import (
        MemoryCompactionHook,
        ContextBudgetDecision,
    )
    from swe.config.config import ContextCompactConfig

    before = [
        {"role": "user", "content": "old"},
        {"role": "user", "content": "recent"},
    ]
    current = before

    class Manager:
        async def install_ready_precompaction(self, **kwargs):
            return True

    async def remeasure():
        nonlocal current
        current = [{"role": "system", "content": "summary"}, before[-1]]
        return 10

    remeasure.diagnostic_snapshot = lambda: current
    with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
        result = await MemoryCompactionHook(
            Manager(),
        )._install_checkpoint_stage(
            SimpleNamespace(memory=object()),
            SimpleNamespace(
                max_input_length=100,
                context_compact=ContextCompactConfig(),
            ),
            before[:1],
            "chat",
            ContextBudgetDecision(80, 0.8, "active", None),
            remeasure,
        )
    assert result is True
    contexts = records(collector, "context")
    assert len(contexts) == 2
    assert all(len(c["occurrences"]) == 2 for c in contexts)
    assert (
        contexts[0]["representation_version"]
        == contexts[1]["representation_version"]
    )
    assert contexts[0]["estimator_version"] == contexts[1]["estimator_version"]
    assert records(collector, "context_operation")[0]["missing_after"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["summary", "", "error", "cancel"])
async def test_legacy_operation_spans_generation_and_persist_without_extra_reads(
    collector,
    monkeypatch,
    result,
):
    from unittest.mock import AsyncMock
    from agentscope.message import Msg
    from swe.agents.hooks.memory_compaction import MemoryCompactionHook
    from swe.config.config import ToolResultCompactConfig

    message = Msg("user", "history", "user")
    memory = SimpleNamespace(
        get_memory=AsyncMock(return_value=[message]),
        get_compressed_summary=lambda: "old summary",
        mark_messages_compressed=AsyncMock(return_value=1),
        update_compressed_summary=AsyncMock(),
    )

    class SummaryModel:
        async def __call__(self, messages, **kwargs):
            return SimpleNamespace(
                usage={"input_tokens": 10, "output_tokens": 2},
            )

    model = LogicalObserver(AttemptObserver(SummaryModel(), "p", {}))

    class Manager:
        agent_id = "agent"
        tenant_id = "t"
        check_context = AsyncMock(return_value=([message], [], True))

        async def compact_memory(self, **kwargs):
            await kwargs["_bound_chat_model"](
                [{"role": "user", "content": "summary input"}],
            )
            if result == "error":
                raise ValueError("failure")
            if result == "cancel":
                raise asyncio.CancelledError
            return result

    running = SimpleNamespace(
        memory_compact_threshold=100,
        memory_compact_reserve=0,
        tool_result_compact=ToolResultCompactConfig(enabled=False),
        memory_summary=SimpleNamespace(memory_summary_enabled=False),
        context_compact=SimpleNamespace(context_compact_enabled=True),
    )
    monkeypatch.setattr(
        "swe.agents.hooks.memory_compaction.load_agent_config",
        lambda *a, **kw: SimpleNamespace(running=running),
    )
    monkeypatch.setattr(
        "swe.agents.hooks.memory_compaction.get_swe_token_counter",
        lambda *a: SimpleNamespace(count=AsyncMock(return_value=0)),
    )
    agent = SimpleNamespace(
        name="agent",
        sys_prompt="system",
        memory=memory,
        model=model,
        formatter=object(),
        print=AsyncMock(),
    )
    with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
        if result == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await MemoryCompactionHook(Manager())(agent, {})
        else:
            await MemoryCompactionHook(Manager())(agent, {})
    assert memory.get_memory.await_count == 2
    row = records(collector, "context_operation")[0]
    expected = {
        "summary": "completed",
        "": "skipped",
        "error": "error",
        "cancel": "cancelled",
    }
    assert row["operation_state"] == expected[result]
    assert row["missing_after"] is True
    assert row["summary_call_id"] == records(collector, "call")[0]["call_id"]
    before_context = next(
        c
        for c in records(collector, "context")
        if c["context_role"] == "before"
    )
    assert len(before_context["occurrences"]) == 3
    assert memory.update_compressed_summary.await_count == int(
        result == "summary",
    )


@pytest.mark.asyncio
async def test_explicit_sources_change_versions_and_only_explicit_relations(
    collector,
):
    from swe.token_diagnostics.provenance import register_source
    from swe.token_diagnostics.runtime import (
        begin_context_operation,
        finish_context_operation,
    )

    message = {"role": "tool", "content": "raw"}
    scope = ("s", "t")
    with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
        with register_source(
            message,
            scope,
            collector.config,
            source_object_id="tool-output-1",
            source_version="raw-v1",
            category="tool_result",
        ):
            operation = begin_context_operation(
                [message],
                "tool_result_compaction",
            )
        message["content"] = "projection"
        with register_source(
            message,
            scope,
            collector.config,
            source_object_id="tool-output-1",
            source_version="projection-v2",
            category="tool_result",
        ):
            finish_context_operation(
                operation,
                [message],
                "completed",
                segments=[
                    {
                        "relation_type": "replaced",
                        "before_occurrence_index": 0,
                        "after_occurrence_index": 0,
                        "source_object_id": "tool-output-1",
                        "source_version": "raw-v1",
                    },
                ],
            )
    contexts = records(collector, "context")
    assert [c["occurrences"][0]["source_version"] for c in contexts] == [
        "raw-v1",
        "projection-v2",
    ]
    assert (
        records(collector, "context_operation")[0]["segments"][0][
            "relation_type"
        ]
        == "replaced"
    )


@pytest.mark.asyncio
async def test_removed_created_facts_need_explicit_source_versions(collector):
    from swe.token_diagnostics.provenance import register_source
    from swe.token_diagnostics.runtime import (
        begin_context_operation,
        finish_context_operation,
    )

    old = {"role": "user", "content": "old"}
    new = {"role": "assistant", "content": "summary"}
    with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
        with register_source(
            old,
            ("s", "t"),
            collector.config,
            source_object_id="source",
            source_version="v1",
            category="user",
        ):
            operation = begin_context_operation([old], "compact")
        with register_source(
            new,
            ("s", "t"),
            collector.config,
            source_object_id="summary",
            source_version="v2",
            category="assistant",
        ):
            finish_context_operation(
                operation,
                [new],
                segments=[
                    {
                        "relation_type": "removed",
                        "before_occurrence_index": 0,
                        "after_occurrence_index": None,
                        "source_object_id": "source",
                        "source_version": "v1",
                    },
                    {
                        "relation_type": "created",
                        "before_occurrence_index": None,
                        "after_occurrence_index": 0,
                        "source_object_id": "summary",
                        "source_version": "v2",
                    },
                    {
                        "relation_type": "created",
                        "before_occurrence_index": None,
                        "after_occurrence_index": 0,
                        "source_object_id": "guessed",
                        "source_version": "v2",
                    },
                ],
            )
    assert [
        s["relation_type"]
        for s in records(collector, "context_operation")[0]["segments"]
    ] == ["removed", "created"]


@pytest.mark.asyncio
async def test_background_summary_does_not_claim_online_change(collector):
    from swe.agents.hooks.memory_compaction import (
        MemoryCompactionHook,
        ContextBudgetDecision,
    )

    class Manager:
        async def schedule_precompaction(self, **kwargs):
            return True

    hook = MemoryCompactionHook(Manager())
    with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
        hook._schedule_governance_precompaction(
            SimpleNamespace(
                memory=object(),
                model=object(),
                formatter=object(),
            ),
            [],
            "chat",
            ContextBudgetDecision(65, 0.65, "governance", 0),
            ("chat", 1),
        )
        await asyncio.gather(*hook._precompaction_tasks.values())
    assert records(collector, "context_operation") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "cancelled"])
async def test_checkpoint_install_error_and_cancellation_finalize_operation(
    collector,
    failure,
):
    from swe.agents.hooks.memory_compaction import (
        MemoryCompactionHook,
        ContextBudgetDecision,
    )

    class Manager:
        async def install_ready_precompaction(self, **kwargs):
            if failure == "cancelled":
                raise asyncio.CancelledError
            raise ValueError("business error")

    with accounting_run({"source_id": "s", "tenant_id": "t"}, collector):
        with pytest.raises(
            asyncio.CancelledError if failure == "cancelled" else ValueError,
        ):
            await MemoryCompactionHook(Manager())._install_checkpoint_stage(
                SimpleNamespace(memory=object()),
                object(),
                [],
                "chat",
                ContextBudgetDecision(80, 0.8, "active", None),
                None,
            )
    row = records(collector, "context_operation")[0]
    assert row["operation_state"] == failure
    assert row["missing_before"] is True and row["missing_after"] is True


@pytest.mark.parametrize("count", [2000, 2001])
def test_explicit_operation_relation_count_has_storage_cap(count):
    from swe.token_diagnostics.context_operations import (
        ContextOperation,
        _relations,
    )

    origins = tuple((index, f"s{index}", "v") for index in range(2000))
    operation = ContextOperation(
        None,
        {
            "missing_before": False,
            "before_context_id": "before",
        },
        origins,
    )
    captured = {
        "context_id": "after",
        "capture_state": "complete",
        "occurrences": [
            {
                "occurrence_index": index,
                "source_object_id": f"s{index}",
                "source_version": "v",
            }
            for index in range(2000)
        ],
    }
    facts = [
        {
            "relation_type": "replaced",
            "before_occurrence_index": index % 2000,
            "after_occurrence_index": index % 2000,
            "source_object_id": f"s{index % 2000}",
            "source_version": "v",
        }
        for index in range(count)
    ]
    rows = _relations(operation, captured, facts)
    assert len(rows) == 2000
    assert rows[-1]["relation_index"] == 1999
    assert all(type(row["source_version"]) is str for row in rows)


@pytest.mark.asyncio
async def test_a_queue_budget_degrades_items_and_keeps_final_usage():
    value = Collector(
        DiagnosticConfig(
            enabled=True,
            context_enabled=False,
            fingerprint_key="k",
            max_bytes=6000,
        ),
        Store(),
    )
    response = SimpleNamespace(usage={"input_tokens": 7, "output_tokens": 2})

    class Model:
        async def __call__(self, messages, **kwargs):
            return response

    with accounting_run({"source_id": "s", "tenant_id": "t"}, value):
        assert (
            await LogicalObserver(AttemptObserver(Model(), "p", {}))(
                [{"role": "user", "content": "hello"} for _ in range(50)],
            )
            is response
        )
    rows = records(value, "call")
    assert len(rows) == 1
    assert rows[0]["capture_state"] == "partial"
    assert rows[0]["items"] == []
    assert rows[0]["input_tokens"] == 7 and rows[0]["output_tokens"] == 2
    assert value.writer.buffer_bytes <= value.config.max_bytes
    assert value.writer.active_count == value.writer.active_bytes == 0
    await value.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("b_enabled", [False, True])
@pytest.mark.parametrize("fault", ["factory", "getter"])
async def test_legacy_observation_factory_and_summary_accessor_fail_open(
    monkeypatch,
    b_enabled,
    fault,
):
    from unittest.mock import AsyncMock
    from agentscope.message import Msg
    from swe.agents.hooks.memory_compaction import MemoryCompactionHook
    from swe.config.config import ToolResultCompactConfig

    value = Collector(
        DiagnosticConfig(
            enabled=True,
            context_enabled=b_enabled,
            fingerprint_key="k",
        ),
        Store(),
    )
    messages = [Msg("user", "history", "user")]
    summary_calls = []

    def summary():
        import inspect

        summary_calls.append(True)
        caller = inspect.currentframe().f_back.f_code.co_name
        if (
            fault == "getter"
            and len(summary_calls) >= 3
            and caller
            not in {"_run_context_compaction", "_get_left_compact_threshold"}
        ):
            raise ValueError("observation-only getter failure")
        return "old summary"

    memory = SimpleNamespace(
        get_memory=AsyncMock(return_value=messages),
        get_compressed_summary=summary,
        mark_messages_compressed=AsyncMock(return_value=1),
        update_compressed_summary=AsyncMock(),
    )
    manager = SimpleNamespace(
        agent_id="agent",
        tenant_id="t",
        check_context=AsyncMock(return_value=(messages, [], True)),
        compact_memory=AsyncMock(return_value="summary"),
    )
    running = SimpleNamespace(
        memory_compact_threshold=100,
        memory_compact_reserve=0,
        tool_result_compact=ToolResultCompactConfig(enabled=False),
        memory_summary=SimpleNamespace(memory_summary_enabled=False),
        context_compact=SimpleNamespace(context_compact_enabled=True),
    )
    monkeypatch.setattr(
        "swe.agents.hooks.memory_compaction.load_agent_config",
        lambda *a, **kw: SimpleNamespace(running=running),
    )
    monkeypatch.setattr(
        "swe.agents.hooks.memory_compaction.get_swe_token_counter",
        lambda *a: SimpleNamespace(count=AsyncMock(return_value=0)),
    )
    if fault == "factory":

        def broken(*args, **kwargs):
            raise ValueError("snapshot factory failure")

        monkeypatch.setattr(
            "swe.token_diagnostics.context_operations.snapshot_messages",
            broken,
        )
    agent = SimpleNamespace(
        name="agent",
        sys_prompt="system",
        memory=memory,
        model=object(),
        formatter=object(),
        print=AsyncMock(),
    )
    with accounting_run({"source_id": "s", "tenant_id": "t"}, value):
        await MemoryCompactionHook(manager)(agent, {})
    manager.compact_memory.assert_awaited_once()
    memory.update_compressed_summary.assert_awaited_once_with("summary")
    if not b_enabled:
        assert len(summary_calls) == 3
    await value.close()

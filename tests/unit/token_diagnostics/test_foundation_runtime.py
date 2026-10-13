# -*- coding: utf-8 -*-
"""Request identity and versioned metadata without live Provider requests."""

import json
from types import SimpleNamespace

import pytest


class Store:
    async def write_batch(self, records):
        pass

    async def close(self):
        pass


def collector():
    from swe.token_diagnostics.collector import Collector
    from swe.token_diagnostics.config import DiagnosticConfig

    return Collector(
        DiagnosticConfig(enabled=True, fingerprint_key="test-key"),
        Store(),
    )


def records(capture, kind):
    return [
        entry[0]["data"]
        for entry in capture.writer.pending.values()
        if entry[0]["kind"] == kind
    ]


@pytest.mark.asyncio
async def test_root_identity_is_scoped_to_execution_not_session_or_trace():
    from swe.token_diagnostics.context import create_branch

    capture = collector()
    metadata = {
        "source_id": "source",
        "tenant_id": "alice",
        "execution_id": "turn-1",
        "identity_source": "answer_turn",
        "session_id": "shared-session",
        "trace_id": "shared-trace",
    }
    first = create_branch(metadata, capture)
    attached = create_branch(metadata, capture)
    later = create_branch({**metadata, "execution_id": "turn-2"}, capture)
    other_tenant = create_branch({**metadata, "tenant_id": "bob"}, capture)
    other_source = create_branch({**metadata, "source_id": "other"}, capture)
    other_origin = create_branch(
        {**metadata, "execution_origin": "scheduled"},
        capture,
    )
    assert attached.fields["task_run_id"] == first.fields["task_run_id"]
    assert attached.fields["branch_run_id"] == first.fields["branch_run_id"]
    assert (
        len(
            {
                run.fields["task_run_id"]
                for run in (
                    first,
                    later,
                    other_tenant,
                    other_source,
                    other_origin,
                )
            },
        )
        == 5
    )
    assert first.fields["schema_version"] == "1.0"
    assert first.fields["identity_source"] == "answer_turn"
    await capture.close()


@pytest.mark.asyncio
async def test_fallback_does_not_join_requests_by_session():
    from swe.token_diagnostics.context import create_branch

    capture = collector()
    metadata = {"source_id": "s", "tenant_id": "a", "session_id": "same"}
    first = create_branch(metadata, capture)
    second = create_branch(metadata, capture)
    assert first.fields["task_run_id"] != second.fields["task_run_id"]
    assert first.fields["identity_source"] == "token_generated"
    await capture.close()


@pytest.mark.asyncio
async def test_query_uses_actual_turn_and_scheduled_execution_claims(
    monkeypatch,
):
    from swe.token_diagnostics.runtime import accounting_query
    from swe.token_diagnostics.context import current_branch

    capture = collector()
    monkeypatch.setattr(
        "swe.token_diagnostics.context.get_collector",
        lambda: capture,
    )

    class Runner:
        agent_id = "agent"

        @accounting_query
        async def query(self, msgs, request=None):
            yield dict(current_branch().fields)

    request = SimpleNamespace(
        user_id="alice",
        source_id="s",
        session_id="chat",
        channel_meta={
            "answer_turn_identity": SimpleNamespace(
                chat_id="chat",
                msgid="message",
                turn_id="turn-1",
            ),
        },
    )
    first = [data async for data in Runner().query([], request)][0]
    resumed = [data async for data in Runner().query([], request)][0]
    assert first["task_run_id"] == resumed["task_run_id"]
    assert first["identity_source"] == "answer_turn"
    scheduled = SimpleNamespace(
        user_id="alice",
        source_id="s",
        channel_meta={},
        execution_origin="scheduled",
        cron_persistence_key="fire-1",
    )
    cron = [data async for data in Runner().query([], scheduled)][0]
    assert cron["execution_origin"] == "scheduled"
    assert cron["execution_id"] == "fire-1"
    assert cron["identity_source"] == "cron_execution"
    await capture.close()


@pytest.mark.asyncio
async def test_context_and_tool_steps_are_versioned_without_original_content():
    from swe.token_diagnostics.context import (
        accounting_run,
        child_snapshot,
        tool_started,
        tool_completed,
        compaction_completed,
    )
    from swe.token_diagnostics.model_observer import (
        AttemptObserver,
        LogicalObserver,
    )

    capture = collector()

    class Model:
        async def __call__(self, messages, **kwargs):
            return SimpleNamespace(
                usage={"input_tokens": 10, "output_tokens": 2},
            )

    with accounting_run(
        {"source_id": "s", "tenant_id": "a"},
        capture,
    ) as branch:
        model = LogicalObserver(AttemptObserver(Model(), "p", {}))
        for _ in range(2):
            await model([{"role": "user", "content": "PRIVATE query"}])
        tool = tool_started(
            {"id": "op-1", "name": "query", "input": "PRIVATE args"},
        )
        tool_completed(tool, "PRIVATE result")
        branch.fields["started_at"] = "2000-01-01T00:00:00Z"
        compaction_completed()
        child = child_snapshot()
        assert child["schema_version"] == "1.0"
        assert child["task_run_id"] == branch.fields["task_run_id"]
    calls = records(capture, "call")
    assert len({call["context_id"] for call in calls}) == 2
    assert len({call["step_id"] for call in calls}) == 2
    for call in calls:
        assert call["parent_step_id"] == branch.fields["branch_run_id"]
        assert call["assembly_stage"] == "messages_only"
        assert call["prompt_format_confidence"] == "message_approx"
        assert call["prompt_modality"] == "text"
        assert call["assembled_at"]
        assert call["usage_origin"] == "provider"
        assert call["usage_accuracy"] == "reported"
    events = records(capture, "event")
    started = next(e for e in events if e["event_type"] == "tool_started")
    ended = next(e for e in events if e["event_type"] == "tool_completed")
    assert started["step_id"] == ended["step_id"]
    assert started["started_at"] == ended["started_at"]
    assert ended["ended_at"]
    assert started["tool_kind"] == "unknown"
    assert started["idempotency_class"] == "unknown"
    assert started["is_cacheable"] is False
    compact = next(e for e in events if e["event_type"] == "compaction")
    assert compact["started_at"] is None
    assert compact["ended_at"] and compact["ended_at"].startswith(
        compact["created_at"][:10],
    )
    assert all(e["schema_version"] == "1.0" for e in events)
    assert "PRIVATE" not in json.dumps(calls + events)
    await capture.close()


def test_media_and_unavailable_usage_have_explicit_accuracy():
    from swe.token_diagnostics.config import DiagnosticConfig
    from swe.token_diagnostics.input_capture import capture_inputs
    from swe.token_diagnostics.models import UsageSnapshot

    captured = capture_inputs(
        [
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": "PRIVATE"}],
            },
        ],
        [],
        "s:a",
        DiagnosticConfig(enabled=True),
    )
    assert captured["assembly_stage"] == "messages_only"
    assert captured["prompt_modality"] == "multimodal"
    assert captured["capture_state"] == "partial"
    assert captured["items"][0]["estimated_tokens"] is None
    assert UsageSnapshot().as_dict()["usage_origin"] == "unknown"
    assert UsageSnapshot().as_dict()["usage_accuracy"] == "unavailable"


def test_execution_identity_requires_complete_turn_and_unambiguous_scope():
    from swe.token_diagnostics.contract import (
        execution_identity,
        scoped_run_id,
    )

    assert (
        execution_identity({"answer_turn_identity": {"chat_id": "chat"}})[1]
        == "token_generated"
    )
    assert scoped_run_id("a\x1fb", "c", "request", "e") != scoped_run_id(
        "a",
        "b\x1fc",
        "request",
        "e",
    )


@pytest.mark.asyncio
async def test_capture_fault_and_external_call_keep_explicit_unknowns(
    monkeypatch,
):
    from swe.token_diagnostics.context import accounting_run
    from swe.token_diagnostics.runtime import external_call
    from swe.token_diagnostics.model_observer import (
        AttemptObserver,
        LogicalObserver,
    )

    capture = collector()
    response = SimpleNamespace(usage={"input_tokens": 0})

    class Model:
        async def __call__(self, messages, **kwargs):
            return response

    def broken(*args):
        raise RuntimeError("PRIVATE capture failure")

    monkeypatch.setattr(
        "swe.token_diagnostics.model_observer.capture_inputs",
        broken,
    )
    with accounting_run({"source_id": "s", "tenant_id": "a"}, capture):
        assert (
            await LogicalObserver(AttemptObserver(Model(), "p", {}))([])
            is response
        )
        with external_call("title"):
            pass
    internal, external = records(capture, "call")
    assert internal["capture_state"] == "unavailable"
    assert internal["assembly_stage"] == "messages_only"
    assert internal["prompt_modality"] == "unknown"
    assert internal["usage_status"] == "partial"
    assert internal["usage_accuracy"] == "reported"
    assert internal["input_tokens"] == 0
    assert external["usage_origin"] == "unknown"
    assert external["usage_accuracy"] == "unavailable"
    assert external["step_id"] and external["parent_step_id"]
    assert external["context_id"] is None
    assert "PRIVATE" not in json.dumps([internal, external])
    await capture.close()

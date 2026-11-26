# -*- coding: utf-8 -*-
import io
import hashlib
import json
import threading
import asyncio
from types import SimpleNamespace

import pytest

from swe.observability import (
    Observability,
    ObservabilityContext,
    Scope,
    bind_context,
    current_context,
)
from swe.observability.config import EmitterConfig
from swe.observability.emitter import JsonlEmitter
from swe.observability.instrumentation import (
    ObservabilityModelWrapper,
    Observability as ObservabilityFacade,
    _system_prompt_digest,
    record_model_request_attempt,
)
from swe.observability.models import MetadataEvent
from swe.observability.projector import _validate_payload
from swe.providers.retry_chat_model import (
    RateLimitConfig,
    RetryChatModel,
    RetryConfig,
)


class BlockingStream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def write(self, value):
        self.entered.set()
        self.release.wait(1)
        return super().write(value)


def test_system_prompt_digest_preserves_scope_order_and_boundaries():
    digest = _system_prompt_digest(
        [
            {"role": "user", "content": "ignored"},
            {"role": "system", "content": "ab"},
            {"role": "assistant", "content": "ignored too"},
            {"role": "system", "content": "c"},
        ],
    )
    expected = hashlib.sha256(
        len(b"ab").to_bytes(8, "big")
        + b"ab"
        + len(b"c").to_bytes(8, "big")
        + b"c",
    ).hexdigest()

    assert digest == f"sha256:{expected}"
    assert digest != _system_prompt_digest(
        [
            {"role": "system", "content": "c"},
            {"role": "system", "content": "ab"},
        ],
    )
    assert digest != _system_prompt_digest(
        [{"role": "system", "content": "abc"}],
    )
    assert (
        _system_prompt_digest([{"role": "user", "content": "ignored"}]) is None
    )
    assert (
        _system_prompt_digest(
            [{"role": "system", "content": "invalid surrogate: \ud800"}],
        )
        is None
    )
    assert (
        _system_prompt_digest(
            [
                {"role": "system", "content": "text"},
                {
                    "role": "system",
                    "content": [{"type": "text", "text": "block"}],
                },
            ],
        )
        is None
    )


def test_context_carrier_and_nested_binding_are_isolated():
    original = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("svc", "src", "tenant", "agent"),
        business_ref={"chat_id": "chat"},
    )
    restored = ObservabilityContext.from_carrier(original.to_carrier())
    assert restored == original
    assert "body" not in original.to_carrier()
    with bind_context(original):
        assert current_context() == original
        with bind_context(None):
            assert current_context() is None
        assert current_context() == original
    assert current_context() is None
    invalid_carrier = original.to_carrier()
    invalid_carrier["capture_enabled"] = "false"
    with pytest.raises(ValueError, match="boolean"):
        ObservabilityContext.from_carrier(invalid_carrier)


@pytest.mark.asyncio
async def test_concurrent_context_bindings_do_not_cross_tasks():
    first = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("svc", "one", "tenant", "agent"),
    )
    second = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("svc", "two", "tenant", "agent"),
    )

    async def read(context):
        with bind_context(context):
            await asyncio.sleep(0)
            return current_context().scope.source_id

    assert await asyncio.gather(read(first), read(second)) == ["one", "two"]


def test_span_emits_self_contained_end_and_preserves_exception():
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    obs = Observability(emitter)
    context = obs.admit_run(
        scope=Scope("svc", "src", "tenant", "agent"),
        enabled=True,
    )
    attempt = obs.start_attempt(context)
    next_attempt = obs.start_attempt(attempt)
    assert next_attempt.attempt_index == 2
    assert next_attempt.predecessor_attempt_id == attempt.obs_attempt_id
    assert next_attempt.attempt_started_at
    assert (
        ObservabilityContext.from_carrier(
            next_attempt.to_carrier(),
        ).attempt_started_at
        == next_attempt.attempt_started_at
    )
    with pytest.raises(RuntimeError, match="business failure"):
        with bind_context(attempt):
            with obs.span("Tool", "execute", name="registered.tool") as span:
                assert span.event("retry")
                assert span.event("model.retry_scheduled")
                raise RuntimeError("business failure")
    obs.end_run(context, status="failed")
    assert emitter.close() == 0
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    ended = next(
        record for record in records if record["event_name"] == "span.ended"
    )
    assert ended["obs_span_id"] == ended["entity_id"]
    assert ended["started_at"]
    assert ended["ended_at"]
    assert ended["status"] == "failed"
    assert ended["error_type"] == "RuntimeError"
    assert "business failure" not in stream.getvalue()
    assert records[-1]["event_name"] == "run.ended"
    incremental = [
        record for record in records if record["event_name"] == "span.event"
    ]
    assert [record["phase_key"] for record in incremental] == [
        "event",
        "event",
    ]
    assert [record["local_seq"] for record in incremental] == [0, 1]
    assert records[-1]["producer_id"]


def test_run_asset_fact_contains_only_safe_identity_and_digest():
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    observer = Observability(emitter)
    context = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("svc", "source", "tenant", "agent"),
    )

    with bind_context(context):
        assert observer.asset_used(
            "skill",
            "research",
            source="runtime_snapshot",
            digest="sha256:abc123",
        )
    emitter.close()

    event = json.loads(stream.getvalue().splitlines()[0])
    assert event["event_name"] == "asset.used"
    assert "parent_obs_span_id" not in event
    assert event["asset"] == {
        "asset_type": "skill",
        "asset_id": "research",
        "asset_source": "runtime_snapshot",
        "asset_digest": "sha256:abc123",
    }
    assert "content" not in event["asset"]


def test_run_end_uses_local_monotonic_duration(monkeypatch):
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    obs = Observability(emitter)
    context = obs.admit_run(
        scope=Scope("svc", "src", "tenant", "agent"),
        enabled=True,
    )
    started_ns = context.run_started_monotonic_ns
    monkeypatch.setattr(
        "swe.observability.instrumentation.time.monotonic_ns",
        lambda: started_ns + 2_500_000,
    )

    assert obs.end_run(context, status="succeeded")
    emitter.close()
    ended = json.loads(stream.getvalue().splitlines()[-1])
    assert ended["metrics"]["duration_us"] == 2500


def test_run_end_omits_uncomparable_cross_process_duration():
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    obs = Observability(emitter)
    context = obs.admit_run(
        scope=Scope("svc", "src", "tenant", "agent"),
        enabled=True,
    )
    carrier = context.to_carrier()
    carrier["run_clock_producer_id"] = "another-process"
    restored = ObservabilityContext.from_carrier(carrier)

    assert restored.run_started_monotonic_ns is None
    assert obs.end_run(restored, status="succeeded")
    emitter.close()
    ended = json.loads(stream.getvalue().splitlines()[-1])
    assert "metrics" not in ended


def _assert_not_saved_model_input(payload, *, chat_id, span_id):
    model_input = next(
        json.loads(line)
        for line in payload.splitlines()
        if '"content_type":"model_input"' in line
    )
    assert model_input["event_type"] == "evidence.linked"
    assert model_input["chat_id"] == chat_id
    assert model_input["saved_state"] == "not_saved"
    assert model_input["reference_id"] == span_id
    assert "message_id" not in model_input
    assert "private system instructions" not in json.dumps(model_input)
    from swe.observability.projector import _validate_payload

    assert _validate_payload(json.dumps(model_input))["event_type"] == (
        "evidence.linked"
    )


@pytest.mark.asyncio
async def test_model_wrapper_records_usage_without_payload(monkeypatch):
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )

    class Model:
        model_name = "retry-model"
        stream = False

        async def __call__(self, *_args, **_kwargs):
            from swe.observability.instrumentation import record_model_retry

            record_model_retry(1, "TimeoutError", 0.01)
            return SimpleNamespace(
                content=[{"type": "text", "text": "private answer"}],
                usage=SimpleNamespace(input_tokens=3, output_tokens=4),
                metadata={
                    "model": "actual-response-model",
                    "finish_reason": "stop",
                },
            )

    context = Observability().admit_run(
        scope=Scope("svc", "src", "tenant", "agent"),
        business_ref={
            "chat_id": "chat-1",
            "root_question_msgid": "question-1",
        },
        enabled=True,
    )
    with bind_context(context):
        await ObservabilityModelWrapper(
            Model(),
            provider_key="provider",
            model_key="model",
        )([{"role": "system", "content": "private system instructions"}])
    emitter.close()
    payload = stream.getvalue()
    assert "private answer" not in payload
    ended = next(
        json.loads(line)
        for line in payload.splitlines()
        if '"event_name":"span.ended"' in line
    )
    assert ended["span_type"] == "LLM"
    assert ended["metrics"]["input_tokens"] == 3
    assert ended["metrics"]["output_tokens"] == 4
    assert "response_model_key" not in ended
    assert "finish_reason" not in ended
    assets = [
        json.loads(line)
        for line in payload.splitlines()
        if '"event_name":"asset.used"' in line
    ]
    model_asset = next(
        record for record in assets if record["asset"]["asset_type"] == "model"
    )
    prompt_asset = next(
        record
        for record in assets
        if record["asset"]["asset_type"] == "prompt"
    )
    agent_asset = next(
        record for record in assets if record["asset"]["asset_type"] == "agent"
    )
    assert model_asset["asset"]["asset_id"] == "provider/model"
    assert model_asset["parent_obs_span_id"] == ended["obs_span_id"]
    assert model_asset["asset"]["asset_version"] == "unknown"
    prompt_bytes = b"private system instructions"
    prompt_digest = hashlib.sha256(
        len(prompt_bytes).to_bytes(8, "big") + prompt_bytes,
    ).hexdigest()
    assert prompt_asset["parent_obs_span_id"] == ended["obs_span_id"]
    assert prompt_asset["asset"] == {
        "asset_type": "prompt",
        "asset_id": "system_prompt",
        "asset_source": "runtime_model_request",
        "asset_version": "unknown",
        "asset_digest": f"sha256:{prompt_digest}",
    }
    from swe.observability.projector import _validate_payload

    assert _validate_payload(json.dumps(prompt_asset))["event_name"] == (
        "asset.used"
    )
    assert agent_asset["asset"]["asset_id"] == "agent"
    assert agent_asset["asset"]["asset_version"] == "unknown"
    assert "private answer" not in json.dumps(assets)
    assert "private system instructions" not in json.dumps(assets)
    retry = next(
        json.loads(line)
        for line in payload.splitlines()
        if '"event_type":"model.retry_scheduled"' in line
    )
    assert retry["event_type"] == "model.retry_scheduled"
    assert retry["error_type"] == "TimeoutError"
    evidence = next(
        json.loads(line)
        for line in payload.splitlines()
        if '"event_type":"evidence.linked"' in line
    )
    assert evidence["chat_id"] == "chat-1"
    assert evidence["message_id"] == "question-1"
    assert evidence["content_type"] == "user_question"
    assert evidence["saved_state"] == "available"
    assert evidence["obs_span_id"] == ended["obs_span_id"]
    assert "private answer" not in json.dumps(evidence)
    _assert_not_saved_model_input(
        payload,
        chat_id="chat-1",
        span_id=ended["obs_span_id"],
    )


@pytest.mark.asyncio
async def test_assistant_evidence_does_not_guess_reused_response_id(
    monkeypatch,
):
    from swe.observability.instrumentation import (
        emit_assistant_answer_evidence,
    )

    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )

    class Model:
        model_name = "test-model"
        stream = False

        async def __call__(self, *_args, **_kwargs):
            return SimpleNamespace(
                id="provider-reused-response-id",
                content=[{"type": "text", "text": "private answer"}],
            )

    context = Observability().admit_run(
        scope=Scope("swe", "source-a", "tenant-a", "agent-a"),
        business_ref={"chat_id": "chat-a"},
        enabled=True,
    )
    with bind_context(context):
        wrapper = ObservabilityModelWrapper(
            Model(),
            provider_key="provider",
            model_key="test-model",
        )
        first = await wrapper([])
        second = await wrapper([])
        assert first.id == second.id
        assert not emit_assistant_answer_evidence(
            chat_id="chat-a",
            message_id="message-a",
            invocation_id=first.id,
            has_tool_use=False,
            has_visible_answer=True,
        )
    emitter.close()

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert not any(
        event.get("event_type") == "evidence.linked"
        and event.get("content_type") == "tool_output"
        for event in events
    )
    assert "private answer" not in stream.getvalue()


def test_pending_tool_evidence_binds_the_concrete_message(monkeypatch):
    from agentscope.message import Msg
    from swe.observability.instrumentation import (
        bind_tool_span,
        mark_pending_message_evidence,
    )
    from swe.observability.outbox import verify_event

    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_KEY_ID", "key1")
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_SECRET", "x" * 32)
    monkeypatch.setattr(
        "swe.observability.outbox_recovery.scope_registered",
        lambda *_: True,
    )
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    context = Observability().prepare_run(
        scope=Scope("swe", "source-a", "tenant-a", "agent-a"),
        business_ref={"chat_id": "chat-a"},
        enabled=True,
    )
    message = Msg(
        "system",
        [{"type": "tool_result", "id": "call", "name": "tool", "output": []}],
        "system",
    )
    with bind_context(context):
        span = Observability(emitter).span("tool", "call")
        with bind_tool_span(span):
            mark_pending_message_evidence(message)
            mark_pending_message_evidence(message)
    emitter.close()
    record = message.metadata["agent_observability_outbox"]
    event = verify_event(record)
    assert (
        event["reference_id"]
        == message.metadata["agent_observability_evidence_ref"]
    )
    assert event["saved_state"] == "available"
    assert event["event_type"] == "evidence.linked"
    assert (
        sum(
            '"event_type":"evidence.pending"' in line
            for line in stream.getvalue().splitlines()
        )
        == 1
    )


@pytest.mark.parametrize("registered", [False, True])
def test_pending_evidence_reports_not_saved_when_recovery_unavailable(
    monkeypatch,
    registered,
):
    from agentscope.message import Msg
    from swe.observability.instrumentation import (
        bind_tool_span,
        mark_pending_message_evidence,
    )

    monkeypatch.setattr(
        "swe.observability.outbox_recovery.scope_registered",
        lambda *args: registered,
    )
    monkeypatch.delenv("SWE_AGENTOBS_OUTBOX_KEY_ID", raising=False)
    monkeypatch.delenv("SWE_AGENTOBS_OUTBOX_SECRET", raising=False)
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    context = Observability().prepare_run(
        scope=Scope("swe", "source-a", "tenant-a", "agent-a"),
        business_ref={"chat_id": "chat-a"},
        enabled=True,
    )
    message = Msg(
        "system",
        [
            {
                "type": "tool_result",
                "id": "call",
                "name": "tool",
                "output": ["private-output"],
            },
        ],
        "system",
    )
    with bind_context(context):
        span = Observability(emitter).span("TOOL", "tool.execute")
        with bind_tool_span(span):
            mark_pending_message_evidence(message)
    emitter.close()
    assert not message.metadata
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    evidence = [
        event
        for event in events
        if event.get("event_type") == "evidence.linked"
        and event.get("content_type") == "tool_output"
    ]
    assert len(evidence) == 1
    assert evidence[0]["saved_state"] == "not_saved"
    assert evidence[0]["content_type"] == "tool_output"
    assert "private-output" not in stream.getvalue()


@pytest.mark.asyncio
async def test_pending_answer_uses_model_invocation_span(monkeypatch):
    from agentscope.message import Msg
    from swe.observability.instrumentation import mark_pending_message_evidence

    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_KEY_ID", "key1")
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_SECRET", "x" * 32)
    monkeypatch.setattr(
        "swe.observability.outbox_recovery.scope_registered",
        lambda *_: True,
    )

    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )

    class Model:
        stream = False

        async def __call__(self, *_args, **_kwargs):
            return SimpleNamespace(
                id="invocation",
                content=[{"type": "text", "text": "answer"}],
            )

    context = Observability().prepare_run(
        scope=Scope("swe", "source-a", "tenant-a", "agent-a"),
        business_ref={"chat_id": "chat-a"},
        enabled=True,
    )
    with bind_context(context):
        response = await ObservabilityModelWrapper(
            Model(),
            provider_key="p",
            model_key="m",
        )([])
        message = Msg("assistant", response.content, "assistant")
        message.invocation_id = response.id
        mark_pending_message_evidence(message)
    emitter.close()
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    pending = [
        event
        for event in events
        if event.get("event_type") == "evidence.pending"
    ]
    assert len(pending) == 1
    assert (
        pending[0]["reference_id"]
        == message.metadata["agent_observability_evidence_ref"]
    )
    assert pending[0]["content_type"] == "assistant_answer"
    assert "answer" not in json.dumps(
        pending[0]["metadata"] if "metadata" in pending[0] else {},
    )


@pytest.mark.asyncio
async def test_unregistered_answer_reports_not_saved(monkeypatch):
    from agentscope.message import Msg
    from swe.observability.instrumentation import mark_pending_message_evidence

    monkeypatch.setattr(
        "swe.observability.outbox_recovery.scope_registered",
        lambda *_: False,
    )
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )

    class Model:
        stream = False

        async def __call__(self, *_args, **_kwargs):
            return SimpleNamespace(
                id="invocation",
                content=[{"type": "text", "text": "private-answer"}],
            )

    context = Observability().prepare_run(
        scope=Scope("swe", "source-a", "tenant-a", "agent-a"),
        business_ref={"chat_id": "chat-a"},
        enabled=True,
    )
    with bind_context(context):
        response = await ObservabilityModelWrapper(
            Model(),
            provider_key="p",
            model_key="m",
        )([])
        message = Msg("assistant", response.content, "assistant")
        message.invocation_id = response.id
        mark_pending_message_evidence(message)
    emitter.close()
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    evidence = [
        event
        for event in events
        if event.get("event_type") == "evidence.linked"
        and event.get("content_type") == "assistant_answer"
    ]
    assert len(evidence) == 1
    assert evidence[0]["saved_state"] == "not_saved"
    assert evidence[0]["content_type"] == "assistant_answer"
    assert not message.metadata
    assert "private-answer" not in stream.getvalue()


@pytest.mark.asyncio
async def test_streamed_answer_retains_its_model_span(monkeypatch):
    from agentscope.message import Msg
    from swe.observability.instrumentation import mark_pending_message_evidence
    from swe.observability.outbox import verify_event

    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_KEY_ID", "key1")
    monkeypatch.setenv("SWE_AGENTOBS_OUTBOX_SECRET", "x" * 32)
    monkeypatch.setattr(
        "swe.observability.outbox_recovery.scope_registered",
        lambda *_: True,
    )
    emitter = JsonlEmitter(EmitterConfig(), stream=io.StringIO())
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )

    class Model:
        stream = True

        async def __call__(self, *_args, **_kwargs):
            async def chunks():
                yield SimpleNamespace(
                    id="stream-id",
                    content=[{"type": "text", "text": "answer"}],
                )

            return chunks()

    context = Observability().prepare_run(
        scope=Scope("swe", "source-a", "tenant-a", "agent-a"),
        business_ref={"chat_id": "chat-a"},
        enabled=True,
    )
    with bind_context(context):
        response = await ObservabilityModelWrapper(
            Model(),
            provider_key="p",
            model_key="m",
        )([])
        async for chunk in response:
            message = Msg("assistant", chunk.content, "assistant")
            message.invocation_id = chunk.id
        mark_pending_message_evidence(message)
    event = verify_event(message.metadata["agent_observability_outbox"])
    assert event["content_type"] == "assistant_answer"
    emitter.close()


@pytest.mark.asyncio
async def test_model_span_uses_bound_react_step_index(monkeypatch):
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )

    class Model:
        model_name = "step-model"
        stream = False

        async def __call__(self, *_args, **_kwargs):
            return SimpleNamespace(
                content=[{"type": "text", "text": "private answer"}],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                metadata={},
            )

    context = Observability().admit_run(
        scope=Scope("svc", "src", "tenant", "agent"),
        enabled=True,
    )
    from swe.observability.instrumentation import bind_agent_step_index

    with bind_context(context), bind_agent_step_index(3):
        await ObservabilityModelWrapper(
            Model(),
            provider_key="provider",
            model_key="step-model",
        )([])
    emitter.close()
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    started = next(
        record
        for record in records
        if record["event_name"] == "span.started"
        and record.get("span_type") == "LLM"
    )
    assert started["step_index"] == 3
    assert "private answer" not in stream.getvalue()


@pytest.mark.asyncio
async def test_model_wrapper_records_model_call_exception_without_details(
    monkeypatch,
):
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )

    class Model:
        model_name = "retry-model"
        stream = False

        async def __call__(self, *_args, **_kwargs):
            raise RuntimeError("private provider detail")

    async def on_attempt(index, streaming):
        record_model_request_attempt(index, streaming=streaming)

    retried = RetryChatModel(
        Model(),
        retry_config=RetryConfig(enabled=False, max_retries=1),
        rate_limit_config=RateLimitConfig(max_qpm=0),
        on_request_attempt=on_attempt,
    )
    context = Observability().admit_run(
        scope=Scope("svc", "src", "tenant", "agent"),
        enabled=True,
    )
    with bind_context(Observability().start_attempt(context)):
        with pytest.raises(RuntimeError, match="private provider detail"):
            await ObservabilityModelWrapper(
                retried,
                provider_key="provider",
                model_key="retry-model",
            )([])
    assert emitter.close() == 0
    payload = stream.getvalue()
    records = [json.loads(line) for line in payload.splitlines()]
    ended = [
        record for record in records if record["event_name"] == "span.ended"
    ]
    assert {
        (
            record["operation"],
            record["status"],
            record["technical_status"],
            record["error_type"],
        )
        for record in ended
    } == {
        ("chat", "failed", "ERROR", "RuntimeError"),
        ("request_attempt", "failed", "ERROR", "RuntimeError"),
    }
    assert "private provider detail" not in payload


@pytest.mark.asyncio
async def test_model_wrapper_records_stream_iteration_exception_and_closes_source(
    monkeypatch,
):
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )
    source_closed = asyncio.Event()

    class Model:
        model_name = "retry-model"
        stream = True

        async def __call__(self, *_args, **_kwargs):
            async def chunks():
                try:
                    yield SimpleNamespace(
                        content=[
                            {"type": "text", "text": "private visible text"},
                        ],
                    )
                    raise RuntimeError("private stream detail")
                finally:
                    source_closed.set()

            return chunks()

    async def on_attempt(index, streaming):
        record_model_request_attempt(index, streaming=streaming)

    retried = RetryChatModel(
        Model(),
        retry_config=RetryConfig(enabled=False, max_retries=1),
        rate_limit_config=RateLimitConfig(max_qpm=0),
        on_request_attempt=on_attempt,
    )
    context = Observability().admit_run(
        scope=Scope("svc", "src", "tenant", "agent"),
        enabled=True,
    )
    with bind_context(Observability().start_attempt(context)):
        chunks = await ObservabilityModelWrapper(
            retried,
            provider_key="provider",
            model_key="retry-model",
        )([])
        with pytest.raises(RuntimeError, match="private stream detail"):
            async for _chunk in chunks:
                pass
    assert emitter.close() == 0
    payload = stream.getvalue()
    records = [json.loads(line) for line in payload.splitlines()]
    ended = [
        record for record in records if record["event_name"] == "span.ended"
    ]
    assert {
        (
            record["operation"],
            record["status"],
            record["technical_status"],
            record["error_type"],
        )
        for record in ended
    } == {
        ("chat", "failed", "ERROR", "RuntimeError"),
        ("request_attempt", "failed", "ERROR", "RuntimeError"),
    }
    assert source_closed.is_set()
    assert "private stream detail" not in payload
    assert "private visible text" not in payload


@pytest.mark.asyncio
async def test_model_retry_projects_request_attempt_spans_without_duplicate_usage(
    monkeypatch,
):
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )

    class Model:
        model_name = "retry-model"
        stream = False

        def __init__(self):
            self.calls = 0

        async def __call__(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(content=[], usage={"input_tokens": 10})
            return SimpleNamespace(
                content=[{"type": "text", "text": "private answer"}],
                usage={"input_tokens": 20, "output_tokens": 0},
            )

    async def on_attempt(index, streaming):
        record_model_request_attempt(index, streaming=streaming)

    model = Model()
    retried = RetryChatModel(
        model,
        retry_config=RetryConfig(enabled=False, max_retries=1),
        rate_limit_config=RateLimitConfig(max_qpm=0),
        on_request_attempt=on_attempt,
    )
    context = Observability().admit_run(
        scope=Scope("svc", "src", "tenant", "agent"),
        enabled=True,
    )
    with bind_context(Observability().start_attempt(context)):
        await ObservabilityModelWrapper(
            retried,
            provider_key="provider",
            model_key="retry-model",
        )([])
    assert emitter.close() == 0
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    attempt_starts = [
        record
        for record in records
        if record.get("span_type") == "LLM"
        and record.get("operation") == "request_attempt"
        and record["event_name"] == "span.started"
    ]
    attempt_ends = [
        record
        for record in records
        if record.get("span_type") == "LLM"
        and record.get("operation") == "request_attempt"
        and record["event_name"] == "span.ended"
    ]
    assert model.calls == 2
    assert [record["request_attempt_index"] for record in attempt_starts] == [
        1,
        2,
    ]
    assert [record["status"] for record in attempt_ends] == [
        "failed",
        "succeeded",
    ]
    assert attempt_starts[1]["links"] == [
        {
            "relation_type": "retry_of",
            "obs_span_id": attempt_starts[0]["obs_span_id"],
        },
    ]
    logical_end = next(
        record
        for record in records
        if record.get("span_type") == "LLM"
        and record.get("operation") == "chat"
        and record["event_name"] == "span.ended"
    )
    assert [record["metrics"]["input_tokens"] for record in attempt_ends] == [
        10,
        20,
    ]
    assert "output_tokens" not in attempt_ends[0]["metrics"]
    assert attempt_ends[1]["metrics"]["output_tokens"] == 0
    assert logical_end["metrics"]["input_tokens"] == 20
    assert "private answer" not in stream.getvalue()


@pytest.mark.asyncio
async def test_model_wrapper_records_first_nonempty_stream_chunk(monkeypatch):
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )

    class Model:
        stream = True

        async def __call__(self, *_args, **_kwargs):
            async def chunks():
                yield SimpleNamespace(content=[])
                yield SimpleNamespace(
                    content=[{"type": "text", "text": "visible"}],
                    usage=SimpleNamespace(input_tokens=2, output_tokens=1),
                    metadata={
                        "model": "stream-response-model",
                        "finish_reason": "stop",
                    },
                )

            return chunks()

    context = Observability().admit_run(
        scope=Scope("svc", "src", "tenant", "agent"),
        enabled=True,
    )
    with bind_context(context):
        chunks = await ObservabilityModelWrapper(
            Model(),
            provider_key="provider",
            model_key="model",
        )([])
        async for _chunk in chunks:
            pass
    emitter.close()
    ended = next(
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if '"event_name":"span.ended"' in line
    )
    assert ended["metrics"]["first_chunk_us"] >= 0
    assert ended["metrics"]["first_visible_text_us"] >= 0
    assert ended["metrics"]["usage_known"] is True
    assert "response_model_key" not in ended
    assert "finish_reason" not in ended


@pytest.mark.asyncio
async def test_model_wrapper_preserves_latest_stream_usage_snapshot(
    monkeypatch,
):
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )

    class Model:
        stream = True

        async def __call__(self, *_args, **_kwargs):
            async def chunks():
                yield SimpleNamespace(
                    content=[],
                    usage=SimpleNamespace(input_tokens=2, output_tokens=1),
                )
                yield SimpleNamespace(
                    content=[],
                    usage=SimpleNamespace(input_tokens=3, output_tokens=4),
                    metadata={"model": "actual-model"},
                )

            return chunks()

    context = Observability().admit_run(
        scope=Scope("svc", "src", "tenant", "agent"),
        enabled=True,
    )
    with bind_context(context):
        chunks = await ObservabilityModelWrapper(
            Model(),
            provider_key="provider",
            model_key="requested-model",
        )([])
        async for _chunk in chunks:
            pass
    emitter.close()
    ended = next(
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if '"event_name":"span.ended"' in line
    )
    assert ended["metrics"]["input_tokens"] == 3
    assert ended["metrics"]["output_tokens"] == 4
    assert "first_chunk_us" not in ended["metrics"]
    assert "first_visible_text_us" not in ended["metrics"]
    assert "response_model_key" not in ended


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stream_chunks",
    [[], [SimpleNamespace(content=[])]],
)
async def test_model_wrapper_keeps_empty_stream_usage_unknown(
    monkeypatch,
    stream_chunks,
):
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )

    class Model:
        stream = True

        async def __call__(self, *_args, **_kwargs):
            async def chunks():
                for chunk in stream_chunks:
                    yield chunk

            return chunks()

    context = Observability().admit_run(
        scope=Scope("svc", "src", "tenant", "agent"),
        enabled=True,
    )
    with bind_context(context):
        chunks = await ObservabilityModelWrapper(
            Model(),
            provider_key="provider",
            model_key="requested-model",
        )([])
        async for _chunk in chunks:
            pass
    emitter.close()
    ended = next(
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if '"event_name":"span.ended"' in line
    )
    assert ended["status"] == "succeeded"
    assert "input_tokens" not in ended["metrics"]
    assert "output_tokens" not in ended["metrics"]
    assert "usage_known" not in ended["metrics"]
    assert "first_chunk_us" not in ended["metrics"]
    assert "first_visible_text_us" not in ended["metrics"]


@pytest.mark.asyncio
async def test_model_wrapper_records_nonstreaming_request_cancellation(
    monkeypatch,
):
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )
    request_started = asyncio.Event()

    class Model:
        stream = False

        async def __call__(self, *_args, **_kwargs):
            request_started.set()
            await asyncio.Event().wait()

    context = Observability().admit_run(
        scope=Scope("svc", "src", "tenant", "agent"),
        enabled=True,
    )
    with bind_context(context):
        task = asyncio.create_task(
            ObservabilityModelWrapper(
                Model(),
                provider_key="provider",
                model_key="model",
            )([{"text": "private request body"}]),
        )
    await asyncio.wait_for(request_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert emitter.close() == 0
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    ended = [
        record for record in records if record["event_name"] == "span.ended"
    ]
    assert len(ended) == 1
    assert ended[0]["operation"] == "chat"
    assert ended[0]["status"] == "cancelled"
    assert "private request body" not in stream.getvalue()


@pytest.mark.asyncio
async def test_model_wrapper_records_stream_cancellation_and_closes_source(
    monkeypatch,
):
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )
    source_closed = asyncio.Event()
    stream_waiting = asyncio.Event()

    class Model:
        stream = True

        async def __call__(self, *_args, **_kwargs):
            async def chunks():
                try:
                    stream_waiting.set()
                    yield SimpleNamespace(
                        content=[{"type": "text", "text": "private output"}],
                    )
                    await asyncio.Event().wait()
                finally:
                    source_closed.set()

            return chunks()

    context = Observability().admit_run(
        scope=Scope("svc", "src", "tenant", "agent"),
        enabled=True,
    )
    with bind_context(context):
        chunks = await ObservabilityModelWrapper(
            Model(),
            provider_key="provider",
            model_key="model",
        )([])

    async def consume():
        async for _chunk in chunks:
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(stream_waiting.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    emitter.close()
    ended = next(
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if '"event_name":"span.ended"' in line
    )
    assert ended["status"] == "cancelled"
    assert source_closed.is_set()
    assert "private output" not in stream.getvalue()


@pytest.mark.asyncio
async def test_model_wrapper_records_aclose_and_closes_source(monkeypatch):
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )
    source_closed = asyncio.Event()

    class Model:
        stream = True

        async def __call__(self, *_args, **_kwargs):
            async def chunks():
                try:
                    yield SimpleNamespace(
                        content=[{"type": "text", "text": "private output"}],
                    )
                    await asyncio.Event().wait()
                finally:
                    source_closed.set()

            return chunks()

    context = Observability().admit_run(
        scope=Scope("svc", "src", "tenant", "agent"),
        enabled=True,
    )
    with bind_context(context):
        chunks = await ObservabilityModelWrapper(
            Model(),
            provider_key="provider",
            model_key="model",
        )([])
        iterator = chunks.__aiter__()
        assert (await anext(iterator)).content
        await iterator.aclose()
    emitter.close()
    ended = next(
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if '"event_name":"span.ended"' in line
    )
    assert ended["status"] == "cancelled"
    assert source_closed.is_set()
    assert "private output" not in stream.getvalue()


@pytest.mark.asyncio
async def test_model_wrapper_keeps_concurrent_run_spans_isolated(monkeypatch):
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )

    class Model:
        stream = False

        async def __call__(self, messages, *_args, **_kwargs):
            await asyncio.sleep(0)
            call_id = messages[0]["call_id"]
            return SimpleNamespace(
                content=[{"type": "text", "text": f"private-{call_id}"}],
                usage={"input_tokens": 1 if call_id == "first" else 2},
            )

    observer = Observability()
    contexts = {
        call_id: observer.admit_run(
            scope=Scope("svc", "src", call_id, "agent"),
            enabled=True,
        )
        for call_id in ("first", "second")
    }
    wrapper = ObservabilityModelWrapper(
        Model(),
        provider_key="provider",
        model_key="requested-model",
    )

    async def invoke(call_id):
        with bind_context(contexts[call_id]):
            return await wrapper([{"call_id": call_id}])

    await asyncio.gather(invoke("first"), invoke("second"))
    emitter.close()
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    ended = [
        record for record in records if record["event_name"] == "span.ended"
    ]
    assert {
        record["obs_run_id"]: record["metrics"]["input_tokens"]
        for record in ended
    } == {
        contexts["first"].obs_run_id: 1,
        contexts["second"].obs_run_id: 2,
    }
    assert "private-first" not in stream.getvalue()
    assert "private-second" not in stream.getvalue()


def test_cancellation_is_recorded_and_propagated():
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    obs = Observability(emitter)
    context = obs.admit_run(
        scope=Scope("svc", "src", "tenant", "agent"),
        enabled=True,
    )
    attempt = obs.start_attempt(context)
    with pytest.raises(asyncio.CancelledError):
        with bind_context(attempt):
            with obs.span("Agent", "execute"):
                raise asyncio.CancelledError()
    emitter.close()
    ended = next(
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if '"event_name":"span.ended"' in line
    )
    assert ended["status"] == "cancelled"


def test_projection_drops_body_fields_and_bounds_values():
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    assert emitter.emit(
        {
            "schema_version": 1,
            "event_id": "event",
            "event_name": "span.event",
            "occurred_at": "now",
            "producer_id": "test",
            "entity_kind": "span",
            "entity_id": "span",
            "phase_key": "step",
            "local_seq": 0,
            "obs_run_id": "run",
            "run_started_at": "now",
            "capture_enabled": True,
            "scope": {
                "service_unit_id": "service",
                "source_id": "source",
                "tenant_id": "tenant",
                "agent_id": "agent",
            },
            "operation": "tool",
            "arguments": {"secret": "private"},
            "prompt": "private prompt",
            "name": "singlewordprompt",
        },
    )
    emitter.close()
    record = json.loads(stream.getvalue())
    assert "arguments" not in record
    assert "prompt" not in record
    assert record["name"] is None


def test_nested_metrics_survive_projection_and_optional_metadata_truncates():
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    event = {
        "schema_version": 1,
        "event_id": "event",
        "event_name": "span.ended",
        "occurred_at": "now",
        "producer_id": "test-producer",
        "entity_kind": "span",
        "entity_id": "span",
        "phase_key": "ended",
        "local_seq": 0,
        "obs_run_id": "run",
        "run_started_at": "now",
        "capture_enabled": True,
        "scope": {
            "service_unit_id": "service",
            "source_id": "source",
            "tenant_id": "tenant",
            "agent_id": "agent",
        },
        "metrics": {"duration_us": 123, "usage_known": False},
        "quality": {"start_observed": False, "end_observed": True},
        "model": {"provider_key": "provider", "request_model_key": "model"},
        "business_ref": {"chat_id": "chat"},
    }
    assert emitter.emit(event)
    emitter.close()
    record = json.loads(stream.getvalue())
    assert record["metrics"] == {"duration_us": 123, "usage_known": False}
    assert record["quality"]["end_observed"] is True

    truncated_stream = io.StringIO()
    truncated_emitter = JsonlEmitter(
        EmitterConfig(max_event_bytes=600),
        stream=truncated_stream,
    )
    assert truncated_emitter.emit(
        {
            **event,
            "events": [{"event_name": "event" * 50} for _ in range(32)],
        },
    )
    truncated_emitter.close()
    truncated = json.loads(truncated_stream.getvalue())
    assert truncated["metadata_truncated"] is True
    assert "events" not in truncated


def test_event_attributes_cannot_replace_envelope_identity():
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    obs = Observability(emitter)
    context = obs.admit_run(
        scope=Scope("svc", "src", "tenant", "agent"),
        enabled=True,
    )
    with bind_context(context):
        span = obs.span(
            "Agent",
            "execute",
            obs_run_id="attacker",
            event_name="run.ended",
        )
        span.end(
            status="succeeded",
            started_at="forged",
            metrics={"duration_us": 0},
            quality={"start_observed": False},
        )
    emitter.close()
    start, end = [
        record
        for record in map(json.loads, stream.getvalue().splitlines())
        if record["event_name"] in {"span.started", "span.ended"}
    ]
    assert start["obs_run_id"] == context.obs_run_id
    assert start["event_name"] == "span.started"
    assert start["span_type"] == "Agent"
    assert end["obs_run_id"] == context.obs_run_id
    assert end["started_at"] != "forged"
    assert end["metrics"]["duration_us"] > 0
    assert end["quality"]["start_observed"] is True


def test_stdout_write_failure_is_contained():
    class BrokenStream:
        def write(self, _value):
            raise OSError("private failure detail")

        def flush(self):
            raise OSError("private failure detail")

    emitter = JsonlEmitter(EmitterConfig(), stream=BrokenStream())
    assert emitter.emit(
        {
            "schema_version": 1,
            "event_id": "event",
            "event_name": "observer.health",
            "occurred_at": "now",
            "producer_id": "test",
            "entity_kind": "observer",
            "entity_id": "observer",
            "phase_key": "health",
            "local_seq": 0,
        },
    )
    assert emitter.close() == 0
    assert emitter.dropped_count == 1


def test_shutdown_budget_is_clamped_to_one_second():
    config = EmitterConfig(shutdown_timeout=50)
    assert config.shutdown_timeout == 1


def test_emitter_initialization_failure_degrades_to_noop(monkeypatch):
    def fail_creation():
        raise RuntimeError("writer unavailable")

    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        fail_creation,
    )
    obs = Observability()
    context = obs.admit_run(
        scope=Scope("svc", "src", "tenant", "agent"),
        enabled=True,
    )
    assert context.capture_enabled
    assert obs.end_run(context, status="succeeded") is False


def test_plain_text_array_values_and_unregistered_enums_are_removed():
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    assert emitter.emit(
        {
            "schema_version": 1,
            "event_id": "event",
            "event_name": "observer.health",
            "occurred_at": "now",
            "producer_id": "test",
            "entity_kind": "observer",
            "entity_id": "observer",
            "phase_key": "health",
            "local_seq": 0,
            "span_type": "user supplied sentence",
            "events": ["private body"],
        },
    )
    emitter.close()
    record = json.loads(stream.getvalue())
    assert record["span_type"] is None
    assert record["events"] is None


def test_required_protocol_overflow_is_dropped_without_invalid_json():
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(max_event_bytes=20), stream=stream)
    event = {
        "schema_version": 1,
        "event_id": "event",
        "event_name": "observer.health",
        "occurred_at": "now",
        "producer_id": "test",
        "entity_kind": "observer",
        "entity_id": "observer",
        "phase_key": "health",
        "local_seq": 0,
    }
    assert not emitter.emit(event)
    assert emitter.close() == 0
    assert stream.getvalue() == ""


def test_disabled_run_does_not_construct_default_emitter(monkeypatch):
    def fail_if_created():
        raise AssertionError("disabled instrumentation created an emitter")

    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        fail_if_created,
    )
    obs = ObservabilityFacade()
    context = obs.admit_run(
        scope=Scope("svc", "src", "tenant", "agent"),
        enabled=False,
    )
    with bind_context(context):
        assert obs.span("LLM", "call", messages=["private"]) is None
    assert not obs.end_run(context, status="succeeded")


def test_missing_required_protocol_fields_are_dropped():
    emitter = JsonlEmitter(EmitterConfig(), stream=io.StringIO())
    assert not emitter.emit({"event_name": "observer.health"})
    assert emitter.dropped_count == 1
    assert emitter.close() == 0


def test_queue_budget_counts_in_flight_event_and_shutdown_is_bounded():
    stream = BlockingStream()
    emitter = JsonlEmitter(
        EmitterConfig(max_queue_events=1, max_queue_bytes=2048),
        stream=stream,
    )
    event = {
        "schema_version": 1,
        "event_id": "event",
        "event_name": "observer.health",
        "occurred_at": "now",
        "producer_id": "test",
        "entity_kind": "observer",
        "entity_id": "observer",
        "phase_key": "health",
        "local_seq": 0,
    }
    assert emitter.emit(event)
    assert stream.entered.wait(1)
    assert not emitter.emit({**event, "event_id": "event-2"})
    assert emitter.dropped_count == 1
    remaining = emitter.close(timeout=0.01)
    assert remaining == 1
    stream.release.set()
    emitter.close()


def test_stdout_prefix_covers_run_and_health_events(capsys):
    emitter = JsonlEmitter(EmitterConfig())
    try:
        Observability(emitter=emitter).admit_run(
            scope=Scope("svc", "src", "tenant", "agent"),
            enabled=True,
        )
        assert not emitter.emit({"event_name": "observer.health"})
    finally:
        assert emitter.close() == 0

    lines = capsys.readouterr().out.splitlines()
    events = []
    for line in lines:
        assert line.startswith("AGENTOBS ")
        events.append(_validate_payload(line.removeprefix("AGENTOBS ")))
    assert {event["event_name"] for event in events} == {
        "run.accepted",
        "asset.used",
        "observer.health",
    }


def test_health_reports_accumulated_drops_after_writer_unblocks(monkeypatch):
    monkeypatch.setattr(
        "swe.observability.emitter._HEALTH_INTERVAL_SECONDS",
        600,
    )
    health_written = threading.Event()

    class HealthStream(BlockingStream):
        def write(self, value):
            written = super().write(value)
            if json.loads(value)["event_name"] == "observer.health":
                health_written.set()
            return written

    stream = HealthStream()
    emitter = JsonlEmitter(EmitterConfig(max_queue_events=2), stream=stream)
    try:
        context = ObservabilityContext.new_run(
            scope=Scope("unit", "src", "tenant", "agent"),
            capture_enabled=True,
        )
        event = MetadataEvent(
            "run.accepted",
            context,
            "run",
            context.obs_run_id,
            "accepted",
            0,
            {"started_at": context.run_started_at},
        )
        assert emitter.emit(event)
        assert stream.entered.wait(1)
        assert emitter.emit(event)
        assert not emitter.emit({"event_name": "observer.health"})
        assert not emitter.emit(event)
        assert emitter.dropped_count == 2
        stream.release.set()
        assert health_written.wait(1)
        assert not emitter.emit_raw(b"invalid")
        assert emitter.dropped_count == 3
    finally:
        stream.release.set()
        assert emitter.close() == 0

    accepted, first, queued, final = map(
        _validate_payload,
        stream.getvalue().splitlines(),
    )
    assert accepted["event_name"] == "run.accepted"
    assert queued == accepted
    assert [first["local_seq"], final["local_seq"]] == [0, 1]
    assert first["entity_id"] == final["entity_id"]
    assert (
        first["producer_id"] == final["producer_id"] == accepted["producer_id"]
    )
    assert first["event_name"] == final["event_name"] == "observer.health"
    assert first["metrics"] == {"dropped_events": 2}
    assert final["metrics"] == {"dropped_events": 3}
    assert "obs_run_id" not in first
    assert "scope" not in first


def test_close_counts_health_frame_reserved_during_shutdown(monkeypatch):
    stream = BlockingStream()
    emitter = JsonlEmitter(stream=stream)
    join = emitter._thread.join

    def join_while_health_is_in_flight(_timeout):
        # Let the writer reserve the final frame after close sets _closed.
        emitter._condition.release()
        try:
            assert stream.entered.wait(1)
        finally:
            emitter._condition.acquire()

    try:
        with emitter._condition:
            assert not emitter.emit({"event_name": "observer.health"})
            monkeypatch.setattr(
                emitter._thread,
                "join",
                join_while_health_is_in_flight,
            )
            assert emitter.close(timeout=0.01) == 1
    finally:
        monkeypatch.setattr(emitter._thread, "join", join)
        stream.release.set()
        assert emitter.close() == 0


def test_nested_metrics_preserve_delay_ms_but_drop_unknown_keys():
    """白名单只放开 delay_ms，其他未知嵌套键仍应被投影剔除。"""
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    assert emitter.emit(
        {
            "schema_version": 1,
            "event_id": "event",
            "event_name": "span.event",
            "occurred_at": "now",
            "producer_id": "test",
            "entity_kind": "span",
            "entity_id": "span",
            "phase_key": "event",
            "local_seq": 0,
            "obs_run_id": "run",
            "run_started_at": "now",
            "capture_enabled": True,
            "scope": {
                "service_unit_id": "service",
                "source_id": "source",
                "tenant_id": "tenant",
                "agent_id": "agent",
            },
            "event_type": "model.retry_scheduled",
            "metrics": {"delay_ms": 250, "bogus_key": 1},
        },
    )
    emitter.close()
    record = json.loads(stream.getvalue())
    assert record["metrics"] == {"delay_ms": 250}


def test_emit_raw_forwards_child_lines_and_rejects_bad_payloads():
    """worker 转发验证正式协议，并再次应用元数据白名单。"""
    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    original = io.StringIO()
    child = JsonlEmitter(EmitterConfig(), stream=original)
    Observability(child).admit_run(
        scope=Scope("unit", "source", "tenant", "agent"),
        enabled=True,
    )
    assert child.close() == 0
    expected = json.loads(original.getvalue().splitlines()[0])
    line = (
        json.dumps({**expected, "content": "internal body"}).encode() + b"\n"
    )

    assert emitter.emit_raw(line) is True
    assert emitter.emit_raw(b'{"content":"internal body"}\n') is False
    assert emitter.emit_raw(b"not-json") is False
    assert emitter.emit_raw(b"") is False
    assert emitter.emit_raw(b'{"ok":1}' + b"x" * (24 * 1024)) is False
    emitter.close()

    forwarded, *health = map(json.loads, stream.getvalue().splitlines())
    assert forwarded == expected
    assert health[-1]["metrics"] == {"dropped_events": 4}
    assert emitter.dropped_count == 4


def test_service_unit_prefers_business_env_then_legacy(monkeypatch):
    """写入与查询共用同一 service unit 来源。"""
    from swe.observability import identity

    monkeypatch.setenv("CMB_CAAS_SERVICEUNITID", "business-unit")
    monkeypatch.setenv("SWE_SERVICE_UNIT_ID", "legacy-unit")
    assert identity.service_unit_id() == "business-unit"

    monkeypatch.delenv("CMB_CAAS_SERVICEUNITID")
    assert identity.service_unit_id() == "legacy-unit"

    monkeypatch.delenv("SWE_SERVICE_UNIT_ID")
    assert identity.service_unit_id() == "swe"


def test_run_state_events_get_distinct_slots(monkeypatch):
    """同一 Run 重复上报同一状态时不能命中同一槽位。"""
    from swe.observability import instrumentation
    from swe.observability.models import ObservabilityContext, Scope

    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(instrumentation, "default_emitter", lambda: emitter)
    context = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("svc", "source", "tenant", "agent"),
    )
    observability = instrumentation.Observability()

    assert observability.run_state(context, status="running")
    assert observability.run_state(context, status="running")
    emitter.close()

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["local_seq"] for event in events] == [1, 2]
    assert all(event["phase_key"] == "state:running" for event in events)


def test_end_run_clears_state_sequence(monkeypatch):
    """Run 终结后必须释放进程内的状态槽位计数。"""
    from swe.observability import instrumentation
    from swe.observability.models import ObservabilityContext, Scope

    stream = io.StringIO()
    emitter = JsonlEmitter(EmitterConfig(), stream=stream)
    monkeypatch.setattr(instrumentation, "default_emitter", lambda: emitter)
    context = ObservabilityContext.new_run(
        capture_enabled=True,
        scope=Scope("svc", "source", "tenant", "agent"),
    )
    observability = instrumentation.Observability()

    assert observability.run_state(context, status="running")
    assert context.obs_run_id in instrumentation._run_state_sequences
    assert observability.end_run(context, status="succeeded")
    assert context.obs_run_id not in instrumentation._run_state_sequences
    emitter.close()


def test_events_example_matches_ingest_contract():
    """样例事件必须能通过当前投影校验，避免契约与实现再次漂移。"""
    from pathlib import Path

    from swe.observability.projector import _validate_payload

    path = (
        Path(__file__).resolve().parents[3]
        / ".scratch"
        / "agent-observability"
        / "events.example.jsonl"
    )
    if not path.exists():
        pytest.skip("events example not present")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines
    for line in lines:
        if line.strip():
            _validate_payload(line)


@pytest.mark.asyncio
async def test_legacy_nas_evidence_reads_committed_session_and_archive(
    tmp_path,
):
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from agentscope.message import Msg

    from swe.agents.memory.conversation_archive import ConversationArchiveStore
    from swe.app.runner.models import ChatSpec
    from swe.app.runner.session import SafeJSONSession
    from swe.observability.nas_evidence import nas_reference
    from swe.observability.query import _load_evidence_item

    chat = ChatSpec(
        id=str(uuid4()),
        user_id="user",
        session_id="session",
        channel="console",
    )
    answer = Msg(
        "assistant",
        [
            {"type": "thinking", "thinking": "private probe"},
            {"type": "text", "text": "visible answer"},
        ],
        "assistant",
        invocation_id="response",
    )
    tool = Msg(
        "tool",
        [
            {
                "type": "tool_result",
                "id": "call-1",
                "name": "echo",
                "output": [{"type": "text", "text": "saved output"}],
            },
        ],
        "system",
    )
    session = SafeJSONSession(str(tmp_path / "sessions"))
    archive = ConversationArchiveStore(tmp_path / "dialog")
    async with session.execution(chat.session_id, chat.user_id) as execution:
        await execution.commit_state(
            {"agent": {"memory": {"content": [[answer.to_dict(), []]]}}},
        )
    await archive.commit(chat.id, [tool])
    await archive.commit(
        chat.id,
        [Msg("user", f"later {index}", "user") for index in range(501)],
    )

    workspace = SimpleNamespace(
        agent_id="agent",
        source_id="source",
        tenant_id="tenant",
        workspace_dir=tmp_path,
        runner=SimpleNamespace(session=session),
        chat_manager=SimpleNamespace(get_chat=AsyncMock(return_value=chat)),
    )
    request = SimpleNamespace(state=SimpleNamespace(source_id="source"))
    run = {
        "service_unit_key": b"svc",
        "tenant_key": b"tenant",
        "agent_key": b"agent",
        "source_key": b"source",
        "chat_key": chat.id,
        "session_key": chat.session_id,
        "root_question_key": "question",
    }

    async def load(reference, run_row):
        result, _, _ = await _load_evidence_item(
            reference,
            request=request,
            run=run_row,
            history=object(),
            workspace=workspace,
            max_bytes=32 * 1024,
            authority={},
        )
        return result

    for message, kind in ((tool, "tool_output"), (answer, "assistant_answer")):
        reference = {
            "chat_id": chat.id,
            "message_id": message.id,
            "reference_id": nas_reference(message.to_dict()),
            "content_type": kind,
            "saved_state": "available",
        }
        result = await load(reference, run)
        assert result["status"] == "available"
        payload = json.dumps(result["message"])
        if kind == "assistant_answer":
            assert (
                "visible answer" in payload and "private probe" not in payload
            )
        else:
            assert "saved output" in payload
        for wrong_scope in (
            {"source_key": b"other"},
            {"tenant_key": b"other"},
        ):
            denied = await load(reference, {**run, **wrong_scope})
            assert denied["status"] == "unavailable"
        changed = await load({**reference, "reference_id": "nas:invalid"}, run)
        assert changed["status"] == "unavailable"

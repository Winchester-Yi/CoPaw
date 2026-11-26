# -*- coding: utf-8 -*-
import asyncio
import io
import json

import httpx
import pytest
from openai import LengthFinishReasonError
from pydantic import BaseModel
from types import SimpleNamespace
from unittest.mock import AsyncMock

from swe.observability import Observability, Scope, bind_context
from swe.observability.emitter import JsonlEmitter
from swe.observability.instrumentation import (
    ObservabilityModelWrapper,
    record_model_retry,
)
from swe.providers.kimi_chat_model import KimiChatModel
from swe.providers.openai_chat_model_compat import OpenAIChatModelCompat
from swe.providers.retry_chat_model import (
    RateLimitConfig,
    RetryChatModel,
    RetryConfig,
)
from swe.token_usage.model_wrapper import TokenRecordingModelWrapper
from swe.tracing.model_wrapper import TracingModelWrapper
from swe.tracing.agent_trace_model_wrapper import AgentTraceModelWrapper

MODEL = "deepseek-v4-flash"
MESSAGES = [{"role": "user", "content": "private user text"}]


def _sse(*, choices=(), usage=None, model=MODEL):
    return (
        "data: "
        + json.dumps(
            {
                "id": "chatcmpl-338dcace-b8de-4335-9a0e-18b2a5851675",
                "object": "chat.completion.chunk",
                "created": 1795597418,
                "model": model,
                "choices": list(choices),
                "usage": usage,
            },
        )
        + "\n\n"
    ).encode()


@pytest.fixture
def capture(monkeypatch):
    sink = io.StringIO()
    emitter = JsonlEmitter(stream=sink)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )
    observer = Observability(emitter)
    run = observer.admit_run(
        scope=Scope("svc", "src", "tenant", "model-request-tests"),
        enabled=True,
    )

    def read():
        assert emitter.close() == 0
        raw = sink.getvalue()
        assert "private user text" not in raw
        assert "private output" not in raw
        assert "private-provider-error" not in raw
        return [json.loads(line) for line in raw.splitlines()]

    yield observer.start_attempt(run), read
    emitter.close()


class _SSEStream(httpx.AsyncByteStream):
    def __init__(self, *frames, after_frames=None):
        self.frames = frames
        self.after_frames = after_frames
        self.closed = False

    async def __aiter__(self):
        for frame in self.frames:
            yield frame
        if self.after_frames:
            await self.after_frames()

    async def aclose(self):
        self.closed = True


def _adapter(
    transport,
    *,
    adapter_class=OpenAIChatModelCompat,
    stream=True,
    retry_config=None,
):
    adapter = adapter_class(
        "requested-alias",
        api_key="fake-key",
        stream=stream,
        client_kwargs={
            "http_client": httpx.AsyncClient(
                transport=httpx.MockTransport(transport),
            ),
            "max_retries": 0,
        },
    )

    async def on_retry(index, _maximum, error, delay):
        record_model_retry(index, type(error).__name__, delay)

    model = ObservabilityModelWrapper(
        RetryChatModel(
            adapter,
            retry_config=retry_config or RetryConfig(enabled=False),
            rate_limit_config=RateLimitConfig(max_qpm=0),
            on_retry=on_retry,
        ),
        provider_key="openai",
        model_key="requested-alias",
        count_requests=True,
    )
    return adapter, model


def _ends(records, operation):
    return [
        record
        for record in records
        if record.get("operation") == operation
        and record["event_name"] == "span.ended"
    ]


@pytest.mark.parametrize(
    "adapter_class",
    [OpenAIChatModelCompat, KimiChatModel],
)
async def test_actual_sdk_sse_merges_finish_and_usage(capture, adapter_class):
    async def respond(request):
        assert (
            json.loads(request.content)["stream_options"]["include_usage"]
            is True
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_SSEStream(
                _sse(choices=[{"index": 0, "delta": {"role": "assistant"}}]),
                _sse(choices=[{"index": 0, "delta": {"content": "<think>"}}]),
                _sse(
                    choices=[
                        {
                            "index": 0,
                            "delta": {
                                "content": "private output</think>first text",
                            },
                        },
                    ],
                ),
                _sse(
                    choices=[
                        {
                            "index": 0,
                            "delta": {"content": "。"},
                            "finish_reason": "stop",
                        },
                    ],
                ),
                _sse(
                    usage={
                        "prompt_tokens": 333,
                        "completion_tokens": 11,
                        "total_tokens": 344,
                    },
                ),
                b"data: [DONE]\n\n",
            ),
        )

    adapter, model = _adapter(respond, adapter_class=adapter_class)
    context, read = capture
    async with adapter.client:
        with bind_context(context):
            stream = await model(MESSAGES)
            chunks = [chunk async for chunk in stream]
    assert chunks[-1].usage.input_tokens == 333
    assert chunks[-1].usage.output_tokens == 11
    [request] = _ends(read(), "request_attempt")
    assert request["response_model_key"] == MODEL
    assert request["request_model_key"] == "requested-alias"
    assert request["finish_reason"] == "stop"
    assert request["metrics"]["input_tokens"] == 333
    assert request["metrics"]["output_tokens"] == 11
    assert request["metrics"]["first_chunk_us"] <= (
        request["metrics"]["first_visible_text_us"]
    )


async def test_actual_sdk_nonstreaming_response_preserves_zero(capture):
    async def respond(request):
        assert json.loads(request.content)["stream"] is False
        return httpx.Response(
            200,
            json={
                "id": "completion",
                "object": "chat.completion",
                "created": 0,
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "private output",
                        },
                        "finish_reason": "length",
                    },
                ],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 1,
                    "total_tokens": 1,
                },
            },
        )

    adapter, model = _adapter(respond)
    context, read = capture
    model.stream = False
    async with adapter.client:
        with bind_context(context):
            response = await model(MESSAGES)
    assert response.content[0]["text"] == "private output"
    [request] = _ends(read(), "request_attempt")
    assert request["response_model_key"] == MODEL
    assert request["finish_reason"] == "length"
    assert request["streaming"] is False
    assert request["metrics"]["input_tokens"] == 0
    assert request["metrics"]["output_tokens"] == 1
    assert "first_chunk_us" not in request["metrics"]


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
async def test_stream_terminal_retains_acquired_facts(capture, outcome):
    waiting = asyncio.Event()

    async def terminal():
        if outcome == "failed":
            raise RuntimeError("private-provider-error")
        waiting.set()
        await asyncio.Event().wait()

    source = _SSEStream(
        _sse(choices=[{"index": 0, "delta": {"content": "private output"}}]),
        _sse(
            usage={
                "prompt_tokens": 0,
                "completion_tokens": 2,
                "total_tokens": 2,
            },
        ),
        after_frames=terminal,
    )

    async def respond(_request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=source,
        )

    adapter, model = _adapter(respond)
    context, read = capture

    async def consume():
        async for _ in stream:
            pass

    async with adapter.client:
        task_started = asyncio.Event()

        async def deferred_consume():
            await task_started.wait()
            await consume()

        task = asyncio.create_task(deferred_consume())
        with bind_context(context):
            stream = await model(MESSAGES)
        task_started.set()
        if outcome == "cancelled":
            await asyncio.wait_for(waiting.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(RuntimeError, match="private-provider-error"):
                await task
    [request] = _ends(read(), "request_attempt")
    assert source.closed
    assert request["status"] == outcome
    assert request["response_model_key"] == MODEL
    assert request["metrics"]["input_tokens"] == 0
    assert request["metrics"]["output_tokens"] == 2
    assert request["metrics"]["first_chunk_us"] >= 0


async def test_cancelled_model_retry_keeps_retry_links_without_recovery(
    capture,
    monkeypatch,
):
    sent = asyncio.Event()
    calls = 0

    async def transport(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                503,
                json={"error": {"message": "unavailable"}},
            )
        sent.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "swe.providers.retry_chat_model._compute_backoff",
        lambda *_: 0,
    )
    adapter, model = _adapter(
        transport,
        stream=False,
        retry_config=RetryConfig(enabled=True, max_retries=1),
    )
    context, read = capture
    async with adapter.client:
        with bind_context(context):
            task = asyncio.create_task(model(MESSAGES))
        await asyncio.wait_for(sent.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    records = read()
    first, second = _ends(records, "request_attempt")
    assert (first["status"], second["status"]) == ("failed", "cancelled")
    assert {
        (link["relation_type"], link["obs_span_id"])
        for event in records
        if event.get("event_type") == "link.added"
        for link in event.get("links", [])
    } == {("retry_next", second["obs_span_id"])}


async def test_interleaved_calls_close_and_finish_their_own_request(capture):
    sources = []

    async def respond(_request):
        source = _SSEStream(
            _sse(
                choices=[{"index": 0, "delta": {"content": "private output"}}],
                model=f"model-{len(sources)}",
            ),
            b"data: [DONE]\n\n",
        )
        sources.append(source)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=source,
        )

    adapter, model = _adapter(respond)
    context, read = capture
    async with adapter.client:
        with bind_context(context):
            first = await model(MESSAGES)
            second = await model(MESSAGES)
        await first.aclose()
        assert sources[0].closed
        assert not sources[1].closed
        assert [chunk async for chunk in second]
    records = read()
    requests = _ends(records, "request_attempt")
    logical = _ends(records, "chat")
    assert [record["status"] for record in requests] == [
        "cancelled",
        "succeeded",
    ]
    assert [record["request_attempt_index"] for record in requests] == [1, 1]
    assert [record["parent_obs_span_id"] for record in requests] == [
        record["obs_span_id"] for record in logical
    ]
    assert "response_model_key" not in requests[0]
    assert requests[1]["response_model_key"] == "model-1"
    assert all("links" not in request for request in requests)


async def test_unstarted_structured_stream_records_zero_requests(capture):
    class Output(BaseModel):
        answer: str

    async def unexpected_request(_request):
        pytest.fail("structured stream contacted provider before iteration")

    adapter, model = _adapter(unexpected_request)
    context, read = capture
    async with adapter.client:
        with bind_context(context):
            stream = await model(MESSAGES, structured_model=Output)
        await stream.aclose()
    records = read()
    assert all(
        record.get("operation") != "request_attempt" for record in records
    )
    [logical] = _ends(records, "chat")
    assert logical["status"] == "cancelled"
    assert logical["no_request_attempts"] is True


async def test_usage_only_response_keeps_usage_with_no_output_latency(capture):
    async def respond(_request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_SSEStream(
                _sse(choices=[{"index": 0, "delta": {"content": ""}}]),
                _sse(
                    usage={
                        "prompt_tokens": 1,
                        "completion_tokens": 0,
                        "total_tokens": 1,
                    },
                ),
                b"data: [DONE]\n\n",
            ),
        )

    adapter, model = _adapter(respond)
    context, read = capture
    async with adapter.client:
        with bind_context(context):
            stream = await model(MESSAGES)
            with pytest.raises(RuntimeError, match="empty model output"):
                assert [chunk async for chunk in stream] == []
    requests = _ends(read(), "request_attempt")
    assert len(requests) == 2
    for request in requests:
        assert request["status"] == "failed"
        assert request["metrics"]["input_tokens"] == 1
        assert request["metrics"]["output_tokens"] == 0
        assert "first_chunk_us" not in request["metrics"]
        assert "first_visible_text_us" not in request["metrics"]


@pytest.mark.parametrize("consume_first", [False, True])
async def test_close_propagates_through_trace_wrappers_without_capture(
    monkeypatch,
    consume_first,
):
    source = _SSEStream(
        _sse(choices=[{"index": 0, "delta": {"content": "output"}}]),
        b"data: [DONE]\n\n",
    )

    async def respond(_request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=source,
        )

    trace_manager = SimpleNamespace(
        enabled=True,
        emit_llm_input=AsyncMock(return_value="span"),
    )
    monkeypatch.setattr(
        "swe.tracing.model_wrapper.get_trace_manager",
        lambda: trace_manager,
    )
    monkeypatch.setattr(
        "swe.tracing.agent_trace_model_wrapper.current_capture",
        lambda: SimpleNamespace(span=None),
    )
    adapter, _ = _adapter(respond)
    model = RetryChatModel(
        TracingModelWrapper(
            "openai",
            TokenRecordingModelWrapper(
                "openai",
                AgentTraceModelWrapper(adapter),
            ),
            trace_context={
                "trace_id": "trace",
                "session_id": "chat",
                "source_id": "source",
                "user_id": "tenant",
            },
        ),
        rate_limit_config=RateLimitConfig(max_qpm=0),
    )
    async with adapter.client:
        stream = await model(MESSAGES)
        if consume_first:
            assert (await anext(stream)).content
        await stream.aclose()
        assert source.closed
    trace_manager.emit_llm_input.assert_awaited_once()


async def test_structured_sdk_length_error_preserves_response_facts(capture):
    class Output(BaseModel):
        answer: str

    async def respond(_request):
        return httpx.Response(
            200,
            json={
                "id": "completion",
                "object": "chat.completion",
                "created": 0,
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": "{"},
                    },
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 2,
                    "total_tokens": 14,
                },
            },
        )

    adapter, model = _adapter(respond, stream=False)
    context, read = capture
    async with adapter.client:
        with bind_context(context), pytest.raises(LengthFinishReasonError):
            await model(MESSAGES, structured_model=Output)
    [request] = _ends(read(), "request_attempt")
    assert request["status"] == "failed"
    assert request["finish_reason"] == "length"
    assert request["response_model_key"] == MODEL
    assert request["metrics"]["input_tokens"] == 12
    assert request["metrics"]["output_tokens"] == 2

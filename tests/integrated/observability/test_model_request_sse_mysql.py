# -*- coding: utf-8 -*-
"""Project the documented production SSE through the model factory and API."""

import json
from io import StringIO
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from openai import AsyncOpenAI

from swe.agents.model_factory import (
    PreparedModelConfig,
    create_model_and_formatter,
)
from swe.database import DatabaseConnection
from swe.observability import Observability, Scope, bind_context
from swe.observability.emitter import JsonlEmitter
from swe.observability.identity import service_unit_id
from swe.observability.projector import InboxProjector
from swe.observability.query import router
from swe.observability.store import ObservabilityStore
from swe.providers.models import ModelSlotConfig
from swe.providers.openai_chat_model_compat import OpenAIChatModelCompat
from swe.providers.openai_provider import OpenAIProvider
from swe.providers.retry_chat_model import RateLimitConfig, RetryConfig

from tests.integrated.observability.test_mysql_pipeline import (
    _test_db_config,
    pytestmark as mysql_required,
)

pytestmark = mysql_required


def _production_sse() -> bytes:
    issue = (
        Path(__file__).resolve().parents[3]
        / ".scratch/agent-observability/issues/05-model-call-observability.md"
    )
    sample = issue.read_text(encoding="utf-8").split(
        "## 生产 OpenAI 兼容流式响应样例",
        1,
    )[1]
    return (sample.split("```text\n", 1)[1].split("```", 1)[0] + "\n").encode()


async def _capture_sse(monkeypatch, *, retry=False, cancel=False):
    provider_requests = 0

    def respond(request):
        nonlocal provider_requests
        provider_requests += 1
        payload = json.loads(request.content)
        assert payload["stream_options"]["include_usage"] is True
        if retry and provider_requests == 1:
            return httpx.Response(500, json={"error": {"message": "retry"}})
        sample = _production_sse()
        if retry:
            sample = sample.replace(
                b'"prompt_tokens": 333',
                b'"prompt_tokens": 20',
            ).replace(b'"total_tokens": 344', b'"total_tokens": 31')
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=sample,
        )

    sink = StringIO()
    emitter = JsonlEmitter(stream=sink)
    observer = Observability(emitter)
    monkeypatch.setattr(
        "swe.observability.instrumentation.default_emitter",
        lambda: emitter,
    )
    scope = Scope(service_unit_id(), "agentobs-sse", "sse-user", "sse-agent")
    run = observer.admit_run(scope=scope, enabled=True)
    attempt = observer.start_attempt(run)
    client = AsyncOpenAI(
        api_key="test-key",
        base_url="https://model.test/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    adapter = OpenAIChatModelCompat(
        model_name="requested-alias",
        stream=True,
        api_key="test-key",
    )
    await adapter.client.close()
    adapter.client = client
    provider = OpenAIProvider(id="openai", name="OpenAI")
    monkeypatch.setattr(
        OpenAIProvider,
        "get_chat_model_instance",
        lambda *_args, **_kwargs: adapter,
    )
    try:
        model, _ = create_model_and_formatter(
            prepared_model_config=PreparedModelConfig(
                tenant_id=scope.tenant_id,
                agent_id=scope.agent_id,
                model_slot=ModelSlotConfig(
                    provider_id=provider.id,
                    model="requested-alias",
                ),
                provider=provider,
                retry_config=RetryConfig(
                    enabled=retry,
                    max_retries=1,
                    backoff_base=0,
                    backoff_cap=0,
                ),
                rate_limit_config=RateLimitConfig(max_qpm=0),
            ),
        )
        with bind_context(attempt):
            stream = await model(
                [{"role": "user", "content": "private input"}]
            )
            if cancel:
                output = [await anext(stream)]
                await stream.aclose()
            else:
                output = [chunk async for chunk in stream]
        assert output[-1].content == [{"type": "text", "text": "。"}]
        status = "cancelled" if cancel else "succeeded"
        observer.end_attempt(attempt, status=status)
        observer.end_run(run, status=status)
    finally:
        await client.close()
        assert emitter.close() == 0
    raw = sink.getvalue()
    assert "private input" not in raw and "。" not in raw
    return run, raw.splitlines()


def _test_api(store):
    app = FastAPI()
    app.state.agent_observability_store = store

    @app.middleware("http")
    async def bind_test_scope(request: Request, call_next):
        request.state.source_id = request.headers.get("X-Source-Id")
        request.state.effective_tenant_id = "sse-user"
        return await call_next(request)

    app.include_router(router, prefix="/api")
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["production", "retry", "cancel"])
async def test_production_sse_projects_to_authorized_request_detail(
    monkeypatch,
    scenario,
):
    run, lines = await _capture_sse(
        monkeypatch,
        retry=scenario == "retry",
        cancel=scenario == "cancel",
    )
    connection = DatabaseConnection(_test_db_config())
    await connection.connect()
    try:
        store = ObservabilityStore(connection)
        async with connection.transaction() as session:
            for line in reversed(lines):
                await session.insert(
                    "INSERT INTO swe_agentobs_inbox (payload_text) VALUES (%s)",
                    (line,),
                )
        projector = InboxProjector(store)
        while await projector.project_batch():
            pass
        url = f"/api/agent-observability/runs/{run.obs_run_id}"
        headers = {
            "X-User-Role": "manager",
            "X-Source-Id": run.scope.source_id,
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_test_api(store)),
            base_url="http://agentobs.test",
        ) as client:
            response = await client.get(url, headers=headers)
            assert response.status_code == 200, response.text
            denied = await client.get(
                url,
                headers={**headers, "X-Source-Id": "different-source"},
            )
            assert denied.status_code == 404
        detail = response.json()
        if scenario == "production":
            _assert_production_request(detail)
        else:
            _assert_retry_or_cancel(detail, scenario)
    finally:
        await connection.close()


def _assert_production_request(detail):
    assert detail["run"]["llm_calls"] == 1
    assert detail["run"]["request_attempt_count"] == 1
    assert detail["run"]["input_tokens"] == 333
    assert detail["run"]["output_tokens"] == 11
    (request,) = [
        span
        for span in detail["spans"]
        if span["operation"] == "request_attempt"
    ]
    assert request["status"] == "succeeded"
    assert request["streaming"] is True
    assert request["request_attempt_index"] == 1
    assert request["request_model_key"] == "requested-alias"
    assert request["response_model_key"] == "deepseek-v4-flash"
    assert request["finish_reason"] == "stop"
    assert (request["input_tokens"], request["output_tokens"]) == (333, 11)
    assert request["total_tokens"] == 344


def _assert_retry_or_cancel(detail, scenario):
    run = detail["run"]
    requests = [
        span
        for span in detail["spans"]
        if span["operation"] == "request_attempt"
    ]
    assert run["llm_calls"] == 1
    if scenario == "retry":
        assert run["request_attempt_count"] == 2
        assert run["input_tokens"] == 20
        assert run["input_tokens_known_requests"] == 1
        assert [row["status"] for row in requests] == ["failed", "succeeded"]
        assert [row["input_tokens"] for row in requests] == [None, 20]
    else:
        assert run["request_attempt_count"] == 1
        assert run["input_tokens"] is None
        assert requests[0]["status"] == "cancelled"
        assert requests[0]["input_tokens"] is None

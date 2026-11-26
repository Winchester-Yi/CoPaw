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


async def _capture_sse(monkeypatch):
    def respond(request):
        payload = json.loads(request.content)
        assert payload["stream_options"]["include_usage"] is True
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_production_sse(),
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
                retry_config=RetryConfig(enabled=False),
                rate_limit_config=RateLimitConfig(max_qpm=0),
            ),
        )
        with bind_context(attempt):
            stream = await model(
                [{"role": "user", "content": "private input"}]
            )
            output = [chunk async for chunk in stream]
        assert output[-1].content == [{"type": "text", "text": "。"}]
        observer.end_attempt(attempt, status="succeeded")
        observer.end_run(run, status="succeeded")
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
async def test_production_sse_projects_to_authorized_request_detail(
    monkeypatch,
):
    run, lines = await _capture_sse(monkeypatch)
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
        _assert_production_request(response.json())
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

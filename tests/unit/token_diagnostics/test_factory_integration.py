"""Exercise the real factory/legacy/retry chain with Trace unavailable."""

from types import SimpleNamespace
import sys

import pytest
from agentscope.model import ChatResponse
from agentscope.model._model_usage import ChatUsage


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="The existing Agent dependencies require fcntl; run in Linux",
)
async def test_enabled_factory_without_trace_preserves_stream_and_final_usage(
    monkeypatch,
):
    from swe.agents import model_factory
    from swe.token_diagnostics import collector as collector_module
    from swe.token_diagnostics.collector import Collector
    from swe.token_diagnostics.config import DiagnosticConfig
    from swe.token_diagnostics.context import accounting_run
    from swe.providers.provider import ModelRuntimeConfig

    RetryConfig = model_factory.RetryConfig

    class Store:
        async def write_batch(self, records):
            pass

        async def close(self):
            pass

    closed = []

    class Model:
        model_name = "test"

        async def __call__(self, messages, **kwargs):
            async def stream():
                try:
                    yield ChatResponse(content=[], usage=ChatUsage(0, 0, 0))
                    yield ChatResponse(content=[], usage=ChatUsage(0, 0, 0))
                finally:
                    closed.append(True)

            return stream()

    collector = Collector(DiagnosticConfig(enabled=True), Store())
    monkeypatch.setattr(collector_module, "_collector", collector)
    monkeypatch.setattr(model_factory, "has_trace_manager", lambda: False)
    monkeypatch.setattr(
        model_factory,
        "_get_model_runtime_configs",
        lambda *args: (RetryConfig(enabled=False), None),
    )
    monkeypatch.setattr(
        model_factory, "_create_formatter_instance", lambda *args: object()
    )
    provider = SimpleNamespace(
        get_model_config=lambda model: ModelRuntimeConfig(
            max_output_length=1024
        ),
        build_generation_kwargs=lambda *args, **kwargs: {},
        get_chat_model_instance=lambda *args, **kwargs: Model(),
    )
    slot = SimpleNamespace(provider_id="test", model="test")
    with accounting_run(
        {"source_id": "s", "tenant_id": "t"}, collector=collector
    ):
        model, _ = model_factory.create_model_and_formatter(
            model_slot_override=slot, model_provider_override=provider
        )
        stream = await model([{"role": "user", "content": "hello"}])
        await anext(stream)
        await stream.aclose()
    calls = [
        x[0]["data"]
        for x in collector.writer.pending.values()
        if x[0]["kind"] == "call"
    ]
    assert len(calls) == 1
    assert calls[0]["execution_state"] == "cancelled"
    assert calls[0]["usage_status"] == "partial"
    assert calls[0]["max_output_tokens"] == 1024
    assert closed == [True]
    await collector.close()

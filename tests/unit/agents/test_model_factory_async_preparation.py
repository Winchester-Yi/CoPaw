# -*- coding: utf-8 -*-
"""Request model preparation keeps disk work outside the event loop."""

from __future__ import annotations

import threading
from contextvars import ContextVar
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from swe.agents import model_factory
from swe.providers.models import ModelSlotConfig
from swe.providers.provider import ModelRuntimeConfig
from swe.providers.provider_runtime_cache import ProviderRuntimeCache
from swe.providers.retry_chat_model import RateLimitConfig, RetryConfig


@dataclass
class _Provider:
    id: str
    config: ModelRuntimeConfig
    build: object

    def get_model_config(self, _model):
        return self.config

    def build_generation_kwargs(self, config, **_kwargs):
        return config.generation_kwargs("max_tokens")

    def get_chat_model_instance(self, model, **kwargs):
        return self.build(model, kwargs)


@pytest.fixture
def model_boundary(monkeypatch):
    monkeypatch.setattr(
        model_factory,
        "_create_formatter_instance",
        lambda _: object(),
    )
    monkeypatch.setattr(
        model_factory,
        "_wrap_model_with_tracing",
        lambda _, model, **kwargs: model,
    )
    retry_wrapper = MagicMock(side_effect=lambda model, **kwargs: model)
    monkeypatch.setattr(model_factory, "RetryChatModel", retry_wrapper)
    monkeypatch.setattr(model_factory, "_get_tenant_id", lambda: "tenant-a")
    return retry_wrapper


@pytest.mark.asyncio
async def test_implicit_agent_id_resolution_runs_in_provider_worker(
    monkeypatch,
):
    from swe.app import agent_context

    loop_thread = threading.get_ident()
    marker = ContextVar("implicit_agent_scope", default=None)
    token = marker.set("source-a")
    seen = []

    def resolve(_tenant_id):
        seen.append((threading.get_ident(), marker.get()))
        return "agent-a"

    monkeypatch.setattr(agent_context, "get_current_agent_id", resolve)
    monkeypatch.setattr(
        model_factory,
        "_get_model_runtime_configs",
        lambda *_: (None, None),
    )
    provider = _Provider("custom", ModelRuntimeConfig(), lambda *_: object())
    cache = ProviderRuntimeCache()
    monkeypatch.setattr(model_factory.ProviderManager, "_runtime_cache", cache)
    try:
        prepared = await model_factory.prepare_model_config_async(
            manager=SimpleNamespace(
                get_active_model=lambda: ModelSlotConfig(
                    provider_id="custom",
                    model="model-a",
                ),
                get_provider=lambda _: provider,
            ),
        )
    finally:
        marker.reset(token)
        cache.init_executor.shutdown(wait=True)
    assert prepared.agent_id == "agent-a"
    assert seen and all(thread != loop_thread for thread, _ in seen)
    assert all(scope == "source-a" for _, scope in seen)


@pytest.mark.asyncio
async def test_preparation_uses_provider_executor_and_constructs_client_on_loop(
    monkeypatch,
    model_boundary,
):
    main_thread = threading.get_ident()
    marker = ContextVar("request_marker", default=None)
    marker.set("source-a")
    seen = []

    def build(model, kwargs):
        assert threading.get_ident() == main_thread
        seen.append((model, kwargs))
        return object()

    provider = _Provider("openai", ModelRuntimeConfig(temperature=0.2), build)
    slot = ModelSlotConfig(provider_id="openai", model="gpt-5")
    manager = SimpleNamespace(
        get_active_model=lambda: slot,
        get_provider=lambda provider_id: provider,
    )
    retry = RetryConfig(max_retries=4)
    rate = RateLimitConfig(max_qpm=17)

    def read_configs(agent_id, tenant_id):
        assert threading.get_ident() != main_thread
        assert marker.get() == "source-a"
        assert (agent_id, tenant_id) == ("agent-a", "tenant-a")
        return retry, rate

    monkeypatch.setattr(
        model_factory,
        "_get_model_runtime_configs",
        read_configs,
    )
    storage = MagicMock(
        side_effect=AssertionError("storage lock on model construction"),
    )
    monkeypatch.setattr(
        model_factory.ProviderManager,
        "ensure_tenant_provider_storage",
        storage,
    )
    cache = ProviderRuntimeCache()
    monkeypatch.setattr(model_factory.ProviderManager, "_runtime_cache", cache)
    try:
        prepared = await model_factory.prepare_model_config_async(
            agent_id="agent-a",
            manager=manager,
        )
        provider.config = ModelRuntimeConfig(temperature=0.9)
        slot.model = "changed-model"
        monkeypatch.setattr(
            model_factory,
            "_get_model_runtime_configs",
            lambda *_: pytest.fail("configuration reread on loop"),
        )
        model_factory.create_model_and_formatter(
            prepared_model_config=prepared,
        )
    finally:
        cache.init_executor.shutdown(wait=True)

    storage.assert_not_called()
    assert seen == [("gpt-5", {"generation_kwargs": {"temperature": 0.2}})]
    assert model_boundary.call_args.kwargs["retry_config"] == retry
    assert model_boundary.call_args.kwargs["rate_limit_config"] == rate
    assert model_boundary.call_args.kwargs["tenant_id"] == "tenant-a"
    assert model_boundary.call_args.kwargs["agent_id"] == "agent-a"


@pytest.mark.asyncio
async def test_prepared_private_model_retains_frozen_fallback(
    monkeypatch,
    model_boundary,
):
    builds = []

    def unavailable(_model, _kwargs):
        raise ValueError("selected unavailable")

    def build(model, kwargs):
        builds.append((model, kwargs))
        return object()

    selected = _Provider("selected", ModelRuntimeConfig(), unavailable)
    fallback = _Provider("parent", ModelRuntimeConfig(temperature=0.3), build)
    monkeypatch.setattr(
        model_factory,
        "_get_model_runtime_configs",
        lambda *_: (None, None),
    )
    cache = ProviderRuntimeCache()
    monkeypatch.setattr(model_factory.ProviderManager, "_runtime_cache", cache)
    manager_lookup = MagicMock(
        side_effect=AssertionError("private provider consulted manager"),
    )
    monkeypatch.setattr(
        model_factory.ProviderManager,
        "get_or_create_instance",
        manager_lookup,
    )
    try:
        prepared = await model_factory.prepare_model_config_async(
            agent_id="agent-a",
            model_slot_override=ModelSlotConfig(
                provider_id="selected",
                model="selected-model",
            ),
            model_provider_override=selected,
            fallback_model_slot=ModelSlotConfig(
                provider_id="parent",
                model="parent-model",
            ),
            fallback_model_provider=fallback,
        )
        fallback.config = ModelRuntimeConfig(temperature=0.9)
        resolved = {}
        model_factory.create_model_and_formatter(
            prepared_model_config=prepared,
            resolved_model_info=resolved,
        )
    finally:
        cache.init_executor.shutdown(wait=True)

    manager_lookup.assert_not_called()
    assert builds == [
        ("parent-model", {"generation_kwargs": {"temperature": 0.3}}),
    ]
    assert resolved == {"provider_id": "parent", "model": "parent-model"}


@pytest.mark.asyncio
async def test_preparation_preserves_source_rate_override_and_scoped_model(
    monkeypatch,
    model_boundary,
):
    from swe.app.crons.model_slot_context import bind_model_slot_override
    from swe.app.source_system_config.models import (
        EffectiveSourceSystemConfig,
        SourceSystemConfig,
    )
    from swe.app.source_system_config.runtime import bind_source_system_config

    builds = []
    provider = _Provider(
        "cron-provider",
        ModelRuntimeConfig(),
        lambda model, _: builds.append(model) or object(),
    )
    manager = SimpleNamespace(
        get_active_model=lambda: pytest.fail("cron override lost"),
        get_provider=lambda provider_id: (
            provider if provider_id == "cron-provider" else None
        ),
    )
    config = SimpleNamespace(
        running=SimpleNamespace(
            llm_retry_enabled=True,
            llm_max_retries=6,
            llm_backoff_base=1.0,
            llm_backoff_cap=20.0,
            llm_max_concurrent=7,
            llm_max_qpm=80,
            llm_rate_limit_pause=4.0,
            llm_rate_limit_jitter=0.5,
            llm_acquire_timeout=30.0,
        ),
    )
    reads = []

    def load_agent_config(agent_id, **kwargs):
        reads.append((agent_id, kwargs, threading.get_ident()))
        return config

    monkeypatch.setattr(
        "swe.config.config.load_agent_config",
        load_agent_config,
    )
    cache = ProviderRuntimeCache()
    monkeypatch.setattr(model_factory.ProviderManager, "_runtime_cache", cache)
    source = EffectiveSourceSystemConfig(
        source_id="portal",
        config=SourceSystemConfig.model_validate({}),
        raw_config=SourceSystemConfig.model_validate(
            {"llm_rate_limiter": {"llm_max_qpm": 12}},
        ),
        version=3,
    )
    try:
        with (
            bind_source_system_config(source),
            bind_model_slot_override(
                ModelSlotConfig(
                    provider_id="cron-provider",
                    model="cron-model",
                ),
            ),
        ):
            prepared = await model_factory.prepare_model_config_async(
                agent_id="agent-a",
                manager=manager,
            )
        model_factory.create_model_and_formatter(
            prepared_model_config=prepared,
        )
    finally:
        cache.init_executor.shutdown(wait=True)

    assert builds == ["cron-model"]
    assert len(reads) == 1
    assert reads[0][:2] == ("agent-a", {"tenant_id": "tenant-a"})
    assert reads[0][2] != threading.get_ident()
    assert model_boundary.call_args.kwargs["retry_config"].max_retries == 6
    assert model_boundary.call_args.kwargs["rate_limit_config"].max_qpm == 12


@pytest.mark.asyncio
async def test_query_runtime_prepares_model_before_agent_construction(
    monkeypatch,
):
    from unittest.mock import AsyncMock
    from swe.app.runner import query_runtime

    events = []
    snapshot = object()
    manager = SimpleNamespace(refresh_if_due=AsyncMock())
    inputs = SimpleNamespace(agent_config=SimpleNamespace(id="agent-a"))

    async def prepare(**kwargs):
        assert kwargs == {
            "agent_id": "agent-a",
            "tenant_id": "tenant-a",
            "manager": manager,
        }
        events.append("prepare")
        return snapshot

    async def finalize(**kwargs):
        assert kwargs["inputs"].prepared_model_config is snapshot
        events.append("construct")
        return object()

    monkeypatch.setattr(
        query_runtime.ProviderManager,
        "get_or_create_instance",
        AsyncMock(return_value=manager),
    )
    monkeypatch.setattr(
        model_factory,
        "prepare_model_config_async",
        prepare,
        raising=False,
    )
    owner = SimpleNamespace(
        tenant_id="tenant-a",
        _build_query_runtime_inputs=AsyncMock(return_value=inputs),
        _start_query_runtime_resources=AsyncMock(
            return_value=(object(), None),
        ),
        _finalize_query_runtime=finalize,
        _cleanup_query_runtime_mcp_clients=AsyncMock(),
    )
    await query_runtime.prepare_query_runtime(
        owner,
        request=object(),
        msgs=[],
        query=None,
        preflight=object(),
    )
    assert events == ["prepare", "construct"]


@pytest.mark.asyncio
async def test_standalone_preparation_initializes_scope_and_preserves_context(
    monkeypatch,
):
    from unittest.mock import AsyncMock
    from swe.config.context import (
        tenant_context,
        get_current_effective_tenant_id,
    )

    threads = []
    provider = _Provider("openai", ModelRuntimeConfig(), lambda *_: object())
    manager = SimpleNamespace(
        refresh_if_due=AsyncMock(),
        get_active_model=lambda: ModelSlotConfig(
            provider_id="openai",
            model="gpt-5",
        ),
        get_provider=lambda _: provider,
    )
    lookup = AsyncMock(return_value=manager)
    monkeypatch.setattr(
        model_factory.ProviderManager,
        "get_or_create_instance",
        lookup,
    )

    def read_configs(agent_id, tenant_id):
        assert agent_id == "agent-a"
        assert tenant_id == get_current_effective_tenant_id()
        threads.append(threading.get_ident())
        return None, None

    monkeypatch.setattr(
        model_factory,
        "_get_model_runtime_configs",
        read_configs,
    )
    cache = ProviderRuntimeCache()
    monkeypatch.setattr(model_factory.ProviderManager, "_runtime_cache", cache)
    try:
        with tenant_context(tenant_id="default", source_id="portal"):
            expected_scope = get_current_effective_tenant_id()
            prepared = await model_factory.prepare_model_config_async(
                agent_id="agent-a",
                tenant_id="default",
            )
    finally:
        cache.init_executor.shutdown(wait=True)

    lookup.assert_awaited_once_with("default")
    manager.refresh_if_due.assert_awaited_once()
    assert prepared.tenant_id == expected_scope
    assert threads and threads[0] != threading.get_ident()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slot,expected_error",
    [
        (None, "No tenant model configuration found."),
        (
            ModelSlotConfig(provider_id="", model=""),
            "No tenant model configuration found.",
        ),
        (
            ModelSlotConfig(provider_id="missing", model="gpt-5"),
            "Provider 'missing' not found.",
        ),
    ],
)
async def test_preparation_preserves_missing_model_errors(
    monkeypatch,
    slot,
    expected_error,
):
    manager = SimpleNamespace(
        get_active_model=lambda: slot,
        get_provider=lambda _: None,
    )
    cache = ProviderRuntimeCache()
    monkeypatch.setattr(model_factory.ProviderManager, "_runtime_cache", cache)
    try:
        with pytest.raises(ValueError, match=expected_error):
            await model_factory.prepare_model_config_async(
                agent_id="agent-a",
                manager=manager,
            )
    finally:
        cache.init_executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_cancelled_preparation_consumes_late_worker_failure(monkeypatch):
    import asyncio
    import gc

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    errors = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    def get_active_model():
        started.set()
        try:
            assert release.wait(timeout=5)
            raise ValueError("provider read failed after cancellation")
        finally:
            finished.set()

    manager = SimpleNamespace(get_active_model=get_active_model)
    cache = ProviderRuntimeCache()
    monkeypatch.setattr(model_factory.ProviderManager, "_runtime_cache", cache)
    try:
        task = asyncio.create_task(
            model_factory.prepare_model_config_async(manager=manager),
        )
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        await asyncio.sleep(0)
        del task
        gc.collect()
        await asyncio.sleep(0)
        assert errors == []
    finally:
        release.set()
        cache.init_executor.shutdown(wait=True)
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_name",
    ["openai", "anthropic", "ollama"],
)
async def test_real_sdk_tls_preparation_runs_on_worker(
    monkeypatch,
    model_boundary,
    provider_name,
):
    import ssl
    from swe.providers.openai_provider import OpenAIProvider
    from swe.providers.anthropic_provider import AnthropicProvider
    from swe.providers.ollama_provider import OllamaProvider

    classes = {
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
        "ollama": OllamaProvider,
    }
    provider = classes[provider_name](
        id=provider_name,
        name=provider_name,
        api_key="test-key",
        base_url="https://example.invalid/v1",
    )
    monkeypatch.setattr(
        model_factory,
        "_get_model_runtime_configs",
        lambda *_: (None, None),
    )
    main_thread = threading.get_ident()
    tls_threads = []
    import anthropic
    import openai

    sdk = anthropic if provider_name == "anthropic" else openai
    sdk_class = (
        sdk.AsyncAnthropic if provider_name == "anthropic" else sdk.AsyncClient
    )
    original_init = sdk_class.__init__
    constructions = []

    def construct(self, *args, **kwargs):
        constructions.append(threading.get_ident())
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(sdk_class, "__init__", construct)
    original = ssl.create_default_context

    def create_context(*args, **kwargs):
        tls_threads.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", create_context)
    cache = ProviderRuntimeCache()
    monkeypatch.setattr(model_factory.ProviderManager, "_runtime_cache", cache)
    try:
        prepared = await model_factory.prepare_model_config_async(
            agent_id="agent-a",
            model_slot_override=ModelSlotConfig(
                provider_id=provider_name,
                model="test-model",
            ),
            model_provider_override=provider,
        )
        model, _ = model_factory.create_model_and_formatter(
            prepared_model_config=prepared,
        )
        try:
            assert tls_threads and main_thread not in tls_threads
            assert constructions == [main_thread]
            assert model.client.timeout.read == 600
            assert model.client._client.follow_redirects is True
            context = model.client._client._transport._pool._ssl_context
            assert context.verify_mode == ssl.CERT_REQUIRED
            assert context.check_hostname
            pool = model.client._client._transport._pool
            assert (
                pool._max_connections
                == sdk.DEFAULT_CONNECTION_LIMITS.max_connections
            )
            assert (
                pool._max_keepalive_connections
                == sdk.DEFAULT_CONNECTION_LIMITS.max_keepalive_connections
            )
        finally:
            await model.client.close()
    finally:
        cache.init_executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_prepared_tls_respects_cert_environment_and_binding_lifecycle(
    tmp_path,
    monkeypatch,
    model_boundary,
):
    import asyncio
    import certifi
    import ssl
    from pathlib import Path
    from swe.providers.client_tls import (
        bind_prepared_client_tls,
        prepared_http_client_kwargs,
    )
    from swe.providers.openai_provider import OpenAIProvider

    cert_file = tmp_path / "ca.pem"
    cert_file.write_bytes(Path(certifi.where()).read_bytes())
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_file))
    observed = []
    original = ssl.create_default_context

    def create_context(*args, **kwargs):
        observed.append((kwargs.get("cafile"), threading.get_ident()))
        return original(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", create_context)
    monkeypatch.setattr(
        model_factory,
        "_get_model_runtime_configs",
        lambda *_: (None, None),
    )
    cache = ProviderRuntimeCache()
    monkeypatch.setattr(model_factory.ProviderManager, "_runtime_cache", cache)
    try:
        prepared = await model_factory.prepare_model_config_async(
            agent_id="agent-a",
            model_slot_override=ModelSlotConfig(
                provider_id="openai",
                model="test",
            ),
            model_provider_override=OpenAIProvider(id="openai", name="openai"),
        )
        assert observed == [(str(cert_file), observed[0][1])]
        assert observed[0][1] != threading.get_ident()
        assert prepared_http_client_kwargs("openai") == {}
        with (
            pytest.raises(ValueError),
            bind_prepared_client_tls(prepared.tls_context),
        ):
            http_client = prepared_http_client_kwargs("openai")["http_client"]
            raise ValueError("construction failed")
        assert prepared_http_client_kwargs("openai") == {}
        http_client.__del__()
        await asyncio.sleep(0)
        assert http_client.is_closed
    finally:
        cache.init_executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_async_model_factory_prepares_then_constructs_on_loop(
    monkeypatch,
):
    from unittest.mock import AsyncMock

    snapshot = object()
    preparation = AsyncMock(return_value=snapshot)
    construction = MagicMock(return_value=(object(), object()))
    monkeypatch.setattr(
        model_factory,
        "prepare_model_config_async",
        preparation,
    )
    monkeypatch.setattr(
        model_factory,
        "create_model_and_formatter",
        construction,
    )
    trace_context = {"trace_id": "trace-a"}
    result = await model_factory.create_model_and_formatter_async(
        agent_id="agent-a",
        trace_context=trace_context,
    )
    preparation.assert_awaited_once_with(agent_id="agent-a")
    construction.assert_called_once_with(
        agent_id="agent-a",
        trace_context=trace_context,
        prepared_model_config=snapshot,
    )
    assert result == construction.return_value


@pytest.mark.asyncio
async def test_tls_failure_preserves_model_fallback(
    monkeypatch,
    model_boundary,
):
    from swe.providers.openai_provider import OpenAIProvider

    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/swe-test-ca.pem")
    monkeypatch.setattr(
        model_factory,
        "_get_model_runtime_configs",
        lambda *_: (None, None),
    )
    fallback_model = object()
    fallback = _Provider(
        "fallback",
        ModelRuntimeConfig(),
        lambda *_: fallback_model,
    )
    cache = ProviderRuntimeCache()
    monkeypatch.setattr(model_factory.ProviderManager, "_runtime_cache", cache)
    try:
        prepared = await model_factory.prepare_model_config_async(
            agent_id="agent-a",
            model_slot_override=ModelSlotConfig(
                provider_id="openai",
                model="test",
            ),
            model_provider_override=OpenAIProvider(
                id="openai",
                name="openai",
                api_key="test-key",
            ),
            fallback_model_slot=ModelSlotConfig(
                provider_id="fallback",
                model="test",
            ),
            fallback_model_provider=fallback,
        )
        model, _ = model_factory.create_model_and_formatter(
            prepared_model_config=prepared,
        )
        assert model is fallback_model
    finally:
        cache.init_executor.shutdown(wait=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_kind", ["none", "custom", "openai"])
async def test_failed_tls_preparation_never_reloads_certificates_on_loop(
    monkeypatch,
    model_boundary,
    fallback_kind,
):
    import ssl
    from swe.providers.openai_provider import OpenAIProvider

    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/swe-test-ca.pem")
    monkeypatch.setattr(
        model_factory,
        "_get_model_runtime_configs",
        lambda *_: (None, None),
    )
    threads = []
    original = ssl.create_default_context

    def certificate_read(*args, **kwargs):
        threads.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", certificate_read)
    primary = OpenAIProvider(id="openai", name="openai", api_key="test-key")
    slot = ModelSlotConfig(provider_id="openai", model="test")
    fallback_model = object()
    fallback = None
    if fallback_kind == "openai":
        fallback = primary.model_copy(deep=True)
    elif fallback_kind == "custom":
        fallback = _Provider(
            "fallback",
            ModelRuntimeConfig(),
            lambda *_: fallback_model,
        )
    cache = ProviderRuntimeCache()
    monkeypatch.setattr(model_factory.ProviderManager, "_runtime_cache", cache)
    try:
        prepared = await model_factory.prepare_model_config_async(
            agent_id="agent-a",
            model_slot_override=slot,
            model_provider_override=primary,
            fallback_model_slot=slot if fallback is not None else None,
            fallback_model_provider=fallback,
        )
        if fallback_kind == "custom":
            model, _ = model_factory.create_model_and_formatter(
                prepared_model_config=prepared,
            )
            assert model is fallback_model
        else:
            with pytest.raises(OSError):
                model_factory.create_model_and_formatter(
                    prepared_model_config=prepared,
                )
        assert threads and threading.get_ident() not in threads
    finally:
        cache.init_executor.shutdown(wait=True)

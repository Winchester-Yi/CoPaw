# -*- coding: utf-8 -*-
"""OpenAI administration clients prepare TLS before loop-owned installation."""

import asyncio
import ssl
import threading
import time
from types import SimpleNamespace

import pytest

from swe.providers.openai_provider import OpenAIProvider


@pytest.mark.asyncio
async def test_real_admin_client_loads_certificates_only_on_worker(
    monkeypatch,
):
    from swe.providers import openai_provider

    loop_thread = threading.get_ident()
    threads, installs = [], []
    original_tls = ssl.create_default_context
    original_client = openai_provider.AsyncOpenAI

    def create_tls(*args, **kwargs):
        threads.append(threading.get_ident())
        return original_tls(*args, **kwargs)

    def install(**kwargs):
        installs.append(threading.get_ident())
        return original_client(**kwargs)

    monkeypatch.setattr(ssl, "create_default_context", create_tls)
    monkeypatch.setattr(openai_provider, "AsyncOpenAI", install)
    provider = OpenAIProvider(id="openai", name="openai", api_key="test-key")
    client = await provider._client_async(timeout=7)
    try:
        assert threads and loop_thread not in threads
        assert installs == [loop_thread]
        assert client.timeout == 7
        context = client._client._transport._pool._ssl_context
        assert context.verify_mode == ssl.CERT_REQUIRED
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cancelled_preparation_does_not_install_late_client(monkeypatch):
    import httpx

    provider = OpenAIProvider(id="openai", name="openai", api_key="test-key")
    started, release, finished = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    installs = []

    def prepare():
        started.set()
        try:
            assert release.wait(5)
            return object()
        finally:
            finished.set()

    monkeypatch.setattr(httpx, "create_ssl_context", prepare)
    monkeypatch.setattr(provider, "_client", lambda **_: installs.append(True))
    task = asyncio.create_task(provider._client_async())
    try:
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        async with asyncio.timeout(2):
            while not finished.is_set():
                await asyncio.sleep(0.001)
    assert installs == []


@pytest.mark.asyncio
async def test_failed_preparation_does_not_poison_later_client(monkeypatch):
    from swe.providers.client_tls import prepared_http_client_kwargs

    provider = OpenAIProvider(id="openai", name="openai", api_key="test-key")
    with monkeypatch.context() as environment:
        environment.setenv("SSL_CERT_FILE", "/nonexistent/swe-test-ca.pem")
        with pytest.raises(OSError):
            await provider._client_async()
    assert prepared_http_client_kwargs("openai") == {}
    client = await provider._client_async()
    try:
        assert not client.is_closed()
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["connection", "models", "model", "image", "video"],
)
async def test_admin_operations_use_prepared_client(monkeypatch, operation):
    from swe.providers.client_tls import prepared_http_client_kwargs

    provider = OpenAIProvider(id="openai", name="openai", api_key="test-key")
    clients, prepared = [], []

    async def rows(**_kwargs):
        return SimpleNamespace(data=[])

    async def empty_stream():
        yield SimpleNamespace()

    async def complete(**kwargs):
        if kwargs.get("stream"):
            return empty_stream()
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="red blue")),
            ],
        )

    def install(timeout=5):
        kwargs = prepared_http_client_kwargs("openai")
        prepared.append(bool(kwargs))
        if kwargs:
            clients.append(kwargs["http_client"])
        return SimpleNamespace(
            models=SimpleNamespace(list=rows),
            chat=SimpleNamespace(completions=SimpleNamespace(create=complete)),
        )

    monkeypatch.setattr(provider, "_client", install)
    try:
        if operation == "connection":
            await provider.check_connection()
        elif operation == "models":
            await provider.fetch_models()
        elif operation == "model":
            await provider.check_model_connection("test")
        elif operation == "image":
            await provider._probe_image_support("test")
        else:
            await provider._try_video_url(
                "test",
                "https://example.test/video",
                5,
                start_time=time.monotonic(),
            )
        assert prepared == [True]
        assert prepared_http_client_kwargs("openai") == {}
    finally:
        for client in clients:
            await client.aclose()

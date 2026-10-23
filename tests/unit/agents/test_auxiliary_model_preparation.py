# -*- coding: utf-8 -*-
"""Async model cache misses preserve refresh and cancellation boundaries."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from swe.agents.utils import tool_summary
from swe.app.suggestions import service


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["suggestion", "summary"])
async def test_reset_during_model_preparation_does_not_cache_old_snapshot(
    monkeypatch,
    kind,
):
    module = service if kind == "suggestion" else tool_summary
    if kind == "suggestion":
        reset = service.SuggestionService.reset_model
        get_model = service.SuggestionService.get_model
        cache = service.SuggestionService._models_by_trace
    else:
        reset = tool_summary.reset_summary_caches
        get_model = tool_summary._get_summary_model
        cache = tool_summary._summary_models_by_trace
    reset()
    started, release = asyncio.Event(), asyncio.Event()

    async def prepare():
        started.set()
        await release.wait()
        return object()

    model = object()
    monkeypatch.setattr(module, "prepare_model_config_async", prepare)
    monkeypatch.setattr(module, "capture_current_trace_context", lambda: None)
    monkeypatch.setattr(
        module,
        "create_model_and_formatter",
        lambda **_: (model, object()),
    )
    task = asyncio.create_task(get_model())
    await started.wait()
    reset()
    release.set()
    assert await task is model
    assert not cache
    reset()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["suggestion", "summary"])
async def test_cancelled_model_preparation_does_not_construct_client(
    monkeypatch,
    kind,
):
    module = service if kind == "suggestion" else tool_summary
    reset = (
        service.SuggestionService.reset_model
        if kind == "suggestion"
        else tool_summary.reset_summary_caches
    )
    get_model = (
        service.SuggestionService.get_model
        if kind == "suggestion"
        else tool_summary._get_summary_model
    )
    reset()
    started = asyncio.Event()

    async def prepare():
        started.set()
        await asyncio.Event().wait()

    constructor = AsyncMock()
    monkeypatch.setattr(module, "prepare_model_config_async", prepare)
    monkeypatch.setattr(module, "create_model_and_formatter", constructor)
    task = asyncio.create_task(get_model())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    constructor.assert_not_called()
    reset()

# -*- coding: utf-8 -*-
"""Suggestion service trace binding regression tests."""

import pytest

from swe.app.suggestions import service as suggestion_service


@pytest.mark.asyncio
async def test_get_model_binds_current_trace_context(monkeypatch) -> None:
    observed = {}

    async def fake_model(_messages):
        return "[]"

    def fake_create_model_and_formatter(
        *,
        trace_context=None,
        prepared_model_config=None,
    ):
        observed["trace_context"] = trace_context
        return fake_model, object()

    monkeypatch.setattr(
        suggestion_service,
        "create_model_and_formatter",
        fake_create_model_and_formatter,
    )
    monkeypatch.setattr(
        suggestion_service,
        "capture_current_trace_context",
        lambda: {
            "trace_id": "trace-suggestion",
            "user_id": "user-1",
            "session_id": "session-1",
            "channel": "console",
            "source_id": "source-1",
        },
    )

    model = await suggestion_service.SuggestionService.get_model()

    assert model is fake_model
    assert observed["trace_context"]["trace_id"] == "trace-suggestion"


@pytest.mark.asyncio
async def test_get_model_reuses_cached_model_within_same_trace(
    monkeypatch,
) -> None:
    created = []

    async def fake_model(_messages):
        return "[]"

    def fake_create_model_and_formatter(
        *,
        trace_context=None,
        prepared_model_config=None,
    ):
        created.append(trace_context)
        return fake_model, object()

    suggestion_service.SuggestionService.reset_model()
    monkeypatch.setattr(
        suggestion_service,
        "create_model_and_formatter",
        fake_create_model_and_formatter,
    )
    monkeypatch.setattr(
        suggestion_service,
        "capture_current_trace_context",
        lambda: {"trace_id": "trace-suggestion"},
    )

    first = await suggestion_service.SuggestionService.get_model()
    second = await suggestion_service.SuggestionService.get_model()

    assert first is second
    assert len(created) == 1


@pytest.mark.asyncio
async def test_reset_model_clears_cached_models(monkeypatch) -> None:
    created = []

    async def fake_model(_messages):
        return "[]"

    def fake_create_model_and_formatter(
        *,
        trace_context=None,
        prepared_model_config=None,
    ):
        created.append(trace_context)
        return fake_model, object()

    suggestion_service.SuggestionService.reset_model()
    monkeypatch.setattr(
        suggestion_service,
        "create_model_and_formatter",
        fake_create_model_and_formatter,
    )
    monkeypatch.setattr(
        suggestion_service,
        "capture_current_trace_context",
        lambda: {"trace_id": "trace-suggestion"},
    )

    await suggestion_service.SuggestionService.get_model()
    suggestion_service.SuggestionService.reset_model()
    await suggestion_service.SuggestionService.get_model()

    assert len(created) == 2


def test_reset_model_disposes_cached_models() -> None:
    closed = []

    class FakeModel:
        def close(self) -> None:
            closed.append("closed")

    suggestion_service.SuggestionService._models_by_trace = (
        suggestion_service.OrderedDict(
            [(("trace-a", ("tenant-a", "agent-a")), FakeModel())],
        )
    )

    suggestion_service.SuggestionService.reset_model()

    assert closed == ["closed"]
    assert suggestion_service.SuggestionService._models_by_trace == (
        suggestion_service.OrderedDict()
    )


@pytest.mark.asyncio
async def test_get_model_reuses_cached_model_without_trace_within_scope(
    monkeypatch,
) -> None:
    created = []

    async def fake_model(_messages):
        return "[]"

    def fake_create_model_and_formatter(
        *,
        trace_context=None,
        prepared_model_config=None,
    ):
        created.append(trace_context)
        return fake_model, object()

    suggestion_service.SuggestionService.reset_model()
    monkeypatch.setattr(
        suggestion_service,
        "create_model_and_formatter",
        fake_create_model_and_formatter,
    )
    monkeypatch.setattr(
        suggestion_service,
        "capture_current_trace_context",
        lambda: None,
    )

    first = await suggestion_service.SuggestionService.get_model()
    second = await suggestion_service.SuggestionService.get_model()

    assert first is fake_model
    assert second is fake_model
    assert len(created) == 1


@pytest.mark.asyncio
async def test_get_model_cache_key_includes_runtime_scope(monkeypatch) -> None:
    created = []
    trace_context = {"trace_id": "trace-suggestion"}
    states = [("tenant-a", "agent-a"), ("tenant-b", "agent-a")]

    async def fake_model(_messages):
        return "[]"

    def fake_create_model_and_formatter(
        *,
        trace_context=None,
        prepared_model_config=None,
    ):
        created.append(trace_context)
        return fake_model, object()

    suggestion_service.SuggestionService.reset_model()
    monkeypatch.setattr(
        suggestion_service,
        "create_model_and_formatter",
        fake_create_model_and_formatter,
    )
    monkeypatch.setattr(
        suggestion_service,
        "capture_current_trace_context",
        lambda: trace_context,
    )
    monkeypatch.setattr(
        suggestion_service.SuggestionService,
        "_runtime_scope_key",
        lambda: states.pop(0),
    )

    await suggestion_service.SuggestionService.get_model()
    await suggestion_service.SuggestionService.get_model()

    assert len(created) == 2


@pytest.mark.asyncio
async def test_get_model_evicts_oldest_cached_trace(monkeypatch) -> None:
    created = []
    trace_ids = iter(
        [
            {"trace_id": "trace-1"},
            {"trace_id": "trace-2"},
            {"trace_id": "trace-3"},
            {"trace_id": "trace-1"},
        ],
    )

    async def fake_model(_messages):
        return "[]"

    def fake_create_model_and_formatter(
        *,
        trace_context=None,
        prepared_model_config=None,
    ):
        created.append(trace_context)
        return fake_model, object()

    suggestion_service.SuggestionService.reset_model()
    monkeypatch.setattr(
        suggestion_service,
        "create_model_and_formatter",
        fake_create_model_and_formatter,
    )
    monkeypatch.setattr(
        suggestion_service,
        "capture_current_trace_context",
        lambda: next(trace_ids),
    )
    monkeypatch.setattr(
        suggestion_service.SuggestionService,
        "_runtime_scope_key",
        lambda: ("tenant-a", "agent-a"),
    )
    monkeypatch.setattr(suggestion_service, "_MODEL_CACHE_LIMIT", 2)

    await suggestion_service.SuggestionService.get_model()
    await suggestion_service.SuggestionService.get_model()
    await suggestion_service.SuggestionService.get_model()
    await suggestion_service.SuggestionService.get_model()

    assert [item["trace_id"] for item in created] == [
        "trace-1",
        "trace-2",
        "trace-3",
        "trace-1",
    ]


@pytest.mark.asyncio
async def test_get_model_eviction_disposes_oldest_cached_model(
    monkeypatch,
) -> None:
    closed = []
    trace_ids = iter(
        [
            {"trace_id": "trace-1"},
            {"trace_id": "trace-2"},
            {"trace_id": "trace-3"},
        ],
    )

    class FakeModel:
        def __init__(self, label: str) -> None:
            self.label = label

        async def aclose(self) -> None:
            closed.append(self.label)

    def fake_create_model_and_formatter(
        *,
        trace_context=None,
        prepared_model_config=None,
    ):
        return FakeModel(trace_context["trace_id"]), object()

    suggestion_service.SuggestionService.reset_model()
    monkeypatch.setattr(
        suggestion_service,
        "create_model_and_formatter",
        fake_create_model_and_formatter,
    )
    monkeypatch.setattr(
        suggestion_service,
        "capture_current_trace_context",
        lambda: next(trace_ids),
    )
    monkeypatch.setattr(
        suggestion_service.SuggestionService,
        "_runtime_scope_key",
        lambda: ("tenant-a", "agent-a"),
    )
    monkeypatch.setattr(suggestion_service, "_MODEL_CACHE_LIMIT", 2)

    await suggestion_service.SuggestionService.get_model()
    await suggestion_service.SuggestionService.get_model()
    await suggestion_service.SuggestionService.get_model()

    assert closed == ["trace-1"]


@pytest.fixture(autouse=True)
def prepared_model_boundary(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        suggestion_service,
        "prepare_model_config_async",
        AsyncMock(return_value=object()),
    )

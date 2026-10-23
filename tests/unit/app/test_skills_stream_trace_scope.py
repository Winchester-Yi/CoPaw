# -*- coding: utf-8 -*-
"""skills_stream trace 绑定回归测试。"""

from __future__ import annotations

from types import SimpleNamespace

from swe.app.routers import skills_stream


def test_get_model_binds_current_trace_context(monkeypatch) -> None:
    observed = {}

    def fake_create_model_and_formatter(*, trace_context=None):
        observed["trace_context"] = trace_context
        return object(), object()

    monkeypatch.setattr(
        skills_stream,
        "create_model_and_formatter",
        fake_create_model_and_formatter,
    )

    model = skills_stream.get_model(
        trace_context={
            "trace_id": "trace-skill-stream",
            "user_id": "user-1",
            "session_id": "session-1",
            "channel": "console",
            "source_id": "source-1",
        },
    )

    assert model is not None
    assert observed["trace_context"]["trace_id"] == "trace-skill-stream"


async def test_ai_optimize_skill_stream_uses_trace_snapshot(
    monkeypatch,
) -> None:
    observed = {}

    async def fake_model(_messages):
        return SimpleNamespace(text="optimized")

    async def fake_get_model(trace_context=None):
        observed["trace_context"] = trace_context
        return fake_model

    monkeypatch.setattr(
        skills_stream,
        "get_model_async",
        fake_get_model,
    )
    monkeypatch.setattr(
        skills_stream,
        "capture_current_trace_context",
        lambda: {
            "trace_id": "trace-skill-stream",
            "user_id": "user-1",
            "session_id": "session-1",
            "channel": "console",
            "source_id": "source-1",
        },
    )

    response = await skills_stream.ai_optimize_skill_stream(
        skills_stream.AIOptimizeSkillRequest(
            content="content",
            language="zh",
        ),
        SimpleNamespace(),
    )

    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)

    assert chunks
    assert observed["trace_context"]["trace_id"] == "trace-skill-stream"

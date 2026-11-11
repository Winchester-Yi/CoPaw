# -*- coding: utf-8 -*-
"""验证 Console chat 请求会透传静态文件 URL 网络字段。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from agentscope_runtime.engine.schemas.agent_schemas import AgentRequest

from swe.app.routers import console as console_router
from swe.app.routers.console import _extract_session_and_payload


def test_console_forwards_explicit_subagent_intent():
    from types import SimpleNamespace
    from swe.app.runner.runner import AgentRunner

    payload = _extract_session_and_payload(
        {
            "session_id": "s",
            "user_id": "u",
            "input": [],
            "enable_subagents": True,
        },
    )
    assert payload["meta"]["enable_subagents"] is True
    request = SimpleNamespace(channel_meta=payload["meta"])
    assert AgentRunner._request_allows_subagents(request) is True


def test_console_forwards_intent_from_parsed_agent_request():
    from agentscope_runtime.engine.schemas.agent_schemas import AgentRequest

    request = AgentRequest.model_validate(
        {
            "session_id": "s",
            "user_id": "u",
            "input": [],
            "enable_subagents": True,
        },
    )
    assert (
        _extract_session_and_payload(request)["meta"]["enable_subagents"]
        is True
    )


def test_extract_session_and_payload_keeps_file_url_network():
    payload = _extract_session_and_payload(
        {
            "channel": "console",
            "user_id": "alice",
            "session_id": "chat-1",
            "input": [],
            "file_url_network": "business",
        },
    )

    assert payload["meta"]["file_url_network"] == "business"


def test_extract_session_and_payload_keeps_plan_mode():
    payload = _extract_session_and_payload(
        {
            "channel": "console",
            "user_id": "alice",
            "session_id": "chat-1",
            "input": [],
            "mode": "plan",
        },
    )

    assert payload["meta"]["mode"] == "plan"


def test_extract_session_and_payload_keeps_normal_mode():
    payload = _extract_session_and_payload(
        {
            "channel": "console",
            "user_id": "alice",
            "session_id": "chat-1",
            "input": [],
            "mode": "normal",
        },
    )

    assert payload["meta"]["mode"] == "normal"


def test_extract_session_and_payload_keeps_agent_request_plan_mode():
    request = AgentRequest.model_validate(
        {
            "input": [
                {
                    "role": "user",
                    "type": "message",
                    "content": [{"type": "text", "text": "plan this"}],
                },
            ],
            "session_id": "chat-1",
            "user_id": "alice",
            "mode": "plan",
        },
    )

    payload = _extract_session_and_payload(request)

    assert payload["meta"]["mode"] == "plan"


def test_extract_session_and_payload_drops_unsupported_mode():
    payload = _extract_session_and_payload(
        {
            "channel": "console",
            "user_id": "alice",
            "session_id": "chat-1",
            "input": [],
            "mode": "unsupported",
        },
    )

    assert "mode" not in payload["meta"]


def test_extract_session_and_payload_keeps_identity_fields():
    """验证 Console chat 请求会透传 tracing 依赖的身份字段。"""
    payload = _extract_session_and_payload(
        {
            "channel": "console",
            "user_id": "alice",
            "session_id": "chat-1",
            "input": [],
            "user_name": "Alice",
            "bbk_id": "3301",
        },
    )

    assert payload["meta"]["user_name"] == "Alice"
    assert payload["meta"]["bbk_id"] == "3301"


def test_extract_session_and_payload_reads_identity_from_agent_request_meta():
    """验证 AgentRequest 路径会从 channel_meta 兜底读取身份字段。"""
    fake_request = SimpleNamespace(
        channel="console",
        user_id="alice",
        session_id="chat-1",
        input=[],
        channel_meta={
            "user_name": "Alice",
            "bbk_id": "3301",
        },
    )
    with patch.object(console_router, "AgentRequest", SimpleNamespace):
        payload = _extract_session_and_payload(fake_request)

    assert payload["meta"]["user_name"] == "Alice"
    assert payload["meta"]["bbk_id"] == "3301"

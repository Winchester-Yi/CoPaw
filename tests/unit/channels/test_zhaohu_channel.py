# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Unit tests for Zhaohu channel case2 non-streaming handling."""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock, MagicMock

import pytest
from agentscope_runtime.engine.schemas.agent_schemas import (
    AgentRequest,
    ContentType,
    Message,
    Role,
    RunStatus,
    TextContent,
)

import swe.app.channels.zhaohu.channel as zhaohu_channel_module
from swe.app.channels.zhaohu.channel import ZhaohuChannel

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_channel(**overrides: Any) -> ZhaohuChannel:
    """Create a ZhaohuChannel with dummy process handler."""

    async def _noop_process(_request):
        yield  # pragma: no cover

    defaults = {
        "process": _noop_process,
        "enabled": True,
        "push_url": "https://test.push.url",
        "sys_id": "test_sys_id",
        "robot_open_id": "test_robot_open_id",
        "channel_code": "ZH",
        "net": "DMZ",
        "request_timeout": 15.0,
        "bot_prefix": "",
        "custom_card_url": "https://test.card.url",
        "oauth_url": "https://test.oauth.url",
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
    }
    defaults.update(overrides)
    ch = ZhaohuChannel(**defaults)
    ch._http = MagicMock()
    return ch


def _decode_action_tag(url: str) -> dict[str, Any]:
    params = parse_qs(urlparse(url).query)
    encoded = params["actionParams"][0]
    payload = json.loads(base64.b64decode(encoded).decode("utf-8"))
    return payload["tag"]


def _make_request(
    session_id: str = "test_session",
    user_id: str = "test_user",
    text: str = "test content",
) -> AgentRequest:
    """Create a minimal AgentRequest for testing."""
    msg = Message(
        type="message",
        role=Role.USER,
        content=[TextContent(type=ContentType.TEXT, text=text)],
    )
    return AgentRequest(
        session_id=session_id,
        user_id=user_id,
        input=[msg],
        channel="zhaohu",
    )


def _make_completed_event(text: str) -> MagicMock:
    """Create a mock completed message event."""
    event = MagicMock()
    event.object = "message"
    event.status = RunStatus.Completed
    # Mock _message_to_content_parts behavior
    event.content = [TextContent(type=ContentType.TEXT, text=text)]
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"enabled": False},
        {"push_url": ""},
        {"sys_id": ""},
        {"robot_open_id": ""},
    ],
)
async def test_send_rejects_unavailable_delivery_configuration(
    overrides,
) -> None:
    channel = _make_channel(**overrides)

    with pytest.raises(RuntimeError, match="zhaohu delivery unavailable"):
        await channel.send(
            "user-1",
            "scheduled output",
            {"cron_delivery_key": "cron:execution-1:output"},
        )


@pytest.mark.asyncio
async def test_non_cron_send_keeps_unavailable_configuration_as_noop() -> None:
    channel = _make_channel(push_url="")

    await channel.send("user-1", "ordinary output")


@pytest.mark.asyncio
async def test_send_event_reports_unrenderable_message_as_unconfirmed() -> (
    None
):
    channel = _make_channel()
    event = _make_completed_event("output")
    channel._message_to_content_parts = MagicMock(return_value=[])

    delivered = await channel.send_event(
        user_id="user-1",
        session_id="session-1",
        event=event,
    )

    assert delivered is False


@pytest.mark.asyncio
async def test_send_event_reports_blank_text_as_unconfirmed() -> None:
    channel = _make_channel()
    event = _make_completed_event("output")
    channel._message_to_content_parts = MagicMock(
        return_value=[TextContent(type=ContentType.TEXT, text="   ")],
    )

    delivered = await channel.send_event(
        user_id="user-1",
        session_id="session-1",
        event=event,
    )

    assert delivered is False


@pytest.mark.asyncio
async def test_send_event_requires_explicit_channel_delivery_ack() -> None:
    channel = _make_channel()
    event = _make_completed_event("output")
    channel.send_content_parts = AsyncMock(return_value=None)

    delivered = await channel.send_event(
        user_id="user-1",
        session_id="session-1",
        event=event,
    )

    assert delivered is False


@pytest.mark.asyncio
async def test_send_rejects_non_successful_push_response(monkeypatch) -> None:
    class _Response:
        content = b'{"returnCode":"FAIL"}'

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"returnCode": "FAIL"}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, *_args, **_kwargs) -> _Response:
            return _Response()

    monkeypatch.setattr(
        zhaohu_channel_module.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(),
    )
    channel = _make_channel()

    with pytest.raises(RuntimeError, match="returnCode=FAIL"):
        await channel.send(
            "user-1",
            "scheduled output",
            {"cron_delivery_key": "cron:execution-1:output"},
        )


@pytest.mark.asyncio
async def test_cron_approval_card_sends_information_without_buttons():
    ch = _make_channel()
    ch.send_custom_card = AsyncMock()
    ch.send = AsyncMock()

    code, msg = await ch.send_cron_approval_card(
        request_id="approval-1",
        session_id="cron-task:job-1",
        user_id="user-1",
        agent_id="agent-a",
        tenant_id="tenant-a",
        source_id="source-a",
        tool_name="execute_shell_command",
        result_summary="发现 shell 风险",
        findings_count=1,
        tool_input={"cmd": "echo hi"},
        approve_command="/approve approval-1",
        deny_command="/deny approval-1",
    )

    assert (code, msg) == (0, "sent")
    ch.send_custom_card.assert_awaited_once()
    open_id, content = ch.send_custom_card.await_args.args
    assert open_id == "user-1"
    assert "execute_shell_command" in content[0]["list"][0]["content"]
    assert "发现 shell 风险" in content[0]["list"][0]["content"]
    assert "echo hi" in content[0]["list"][0]["content"]
    assert "approval-1" in content[0]["list"][0]["content"]
    assert "/approve" not in content[0]["list"][0]["content"]
    assert "/deny" not in content[0]["list"][0]["content"]

    approve_tag = _decode_action_tag(
        content[1]["list"][0]["actionLink"]["url"],
    )
    reject_tag = _decode_action_tag(
        content[1]["list"][1]["actionLink"]["url"],
    )
    for tag, action_type in (
        (approve_tag, "approve"),
        (reject_tag, "reject"),
    ):
        assert tag["request_id"] == "approval-1"
        assert tag["type"] == action_type
        assert tag["agent_id"] == "agent-a"
        assert tag["agentId"] == "agent-a"
        assert tag["tenant_id"] == "tenant-a"
        assert tag["source_id"] == "source-a"
    ch.send.assert_not_called()


@pytest.mark.asyncio
async def test_cron_approval_result_sends_plain_notification():
    ch = _make_channel()
    ch.send_custom_card = AsyncMock()
    ch.send = AsyncMock()

    code, msg = await ch.send_cron_approval_result(
        request_id="approval-1",
        session_id="cron-task:job-1",
        user_id="user-1",
        tool_name="execute_shell_command",
        decision="approved",
    )

    assert (code, msg) == (0, "sent")
    ch.send.assert_awaited_once()
    to_handle, text, meta = ch.send.await_args.args
    assert to_handle == "user-1"
    assert "工具审批已通过" in text
    assert "execute_shell_command" in text
    assert "approval-1" in text
    assert meta["session_id"] == "cron-task:job-1"
    assert meta["notification_summary"] == "工具审批结果"
    ch.send_custom_card.assert_not_called()


# ---------------------------------------------------------------------------
# Tests for _run_task_llm_and_notify
# ---------------------------------------------------------------------------


class TestRunTaskLlmAndNotify:
    """Tests for _run_task_llm_and_notify method."""

    @pytest.mark.asyncio
    async def test_collects_complete_result(self):
        """验证 _run_task_llm_and_notify 正确收集完整结果."""
        ch = _make_channel()

        # Mock process to yield completed events
        async def _mock_process(_request):
            # Yield a completed message event
            event = _make_completed_event("Final result text here")
            yield event

        ch._process = _mock_process

        # Mock send_custom_card
        ch.send_custom_card = AsyncMock(return_value=(0, "msg123"))

        # Mock send
        ch.send = AsyncMock()

        request = _make_request()
        meta = {"send_addr": "yst_id_123"}

        await ch._run_task_llm_and_notify(
            request=request,
            session_id="test_session",
            task_content="Do something",
            from_id="open_id_123",
            meta=meta,
            user_id="sap_id_123",
        )

        # Verify card was sent
        ch.send_custom_card.assert_called_once()

        # Verify final result was sent via push_url
        ch.send.assert_called_once()
        call_args = ch.send.call_args
        assert call_args[0][0] == "yst_id_123"
        assert "Final result text here" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_card_sent_immediately(self):
        """验证卡片通知立即发送."""
        ch = _make_channel()

        async def _mock_process(_request):
            yield _make_completed_event("Result")

        ch._process = _mock_process
        ch.send_custom_card = AsyncMock(return_value=(0, "msg123"))
        ch.send = AsyncMock()

        request = _make_request()
        meta = {"send_addr": "yst_id_123"}

        await ch._run_task_llm_and_notify(
            request=request,
            session_id="test_session",
            task_content="Do something",
            from_id="open_id_123",
            meta=meta,
            user_id="sap_id_123",
        )

        # Card should be called first (before send)
        ch.send_custom_card.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_result_no_send(self):
        """验证无结果时不发送消息."""
        ch = _make_channel()

        async def _mock_process(_request):
            # Yield nothing (no completed message)
            yield MagicMock(object="other", status=None)

        ch._process = _mock_process
        ch.send_custom_card = AsyncMock(return_value=(0, "msg123"))
        ch.send = AsyncMock()

        request = _make_request()
        meta = {"send_addr": "yst_id_123"}

        await ch._run_task_llm_and_notify(
            request=request,
            session_id="test_session",
            task_content="Do something",
            from_id="open_id_123",
            meta=meta,
            user_id="sap_id_123",
        )

        # Card should still be sent
        ch.send_custom_card.assert_called_once()
        # But no result send
        ch.send.assert_not_called()


class TestErrorHandling:
    """Tests for error handling in _run_task_llm_and_notify."""

    @pytest.mark.asyncio
    async def test_error_notification_sent(self):
        """验证处理失败时发送错误通知."""
        ch = _make_channel()

        async def _mock_process(_request):
            raise RuntimeError("Processing failed")

        ch._process = _mock_process
        ch.send_custom_card = AsyncMock(return_value=(0, "msg123"))
        ch.send = AsyncMock()

        request = _make_request()
        meta = {"send_addr": "yst_id_123"}

        await ch._run_task_llm_and_notify(
            request=request,
            session_id="test_session",
            task_content="Do something",
            from_id="open_id_123",
            meta=meta,
            user_id="sap_id_123",
        )

        # Card should still be sent
        ch.send_custom_card.assert_called_once()

        # Error notification should be sent
        ch.send.assert_called_once()
        call_args = ch.send.call_args
        assert call_args[0][0] == "yst_id_123"
        assert "错误" in call_args[0][1] or "error" in call_args[0][1].lower()

    @pytest.mark.asyncio
    async def test_no_send_addr_no_error_send(self):
        """验证无 send_addr 时不发送错误通知."""
        ch = _make_channel()

        async def _mock_process(_request):
            raise RuntimeError("Processing failed")

        ch._process = _mock_process
        ch.send_custom_card = AsyncMock(return_value=(0, "msg123"))
        ch.send = AsyncMock()

        request = _make_request()
        meta = {}  # No send_addr

        await ch._run_task_llm_and_notify(
            request=request,
            session_id="test_session",
            task_content="Do something",
            from_id="open_id_123",
            meta=meta,
            user_id="sap_id_123",
        )

        # Card should still be sent
        ch.send_custom_card.assert_called_once()
        # No error send since no send_addr
        ch.send.assert_not_called()


class TestHandleTaskAssignment:
    """Tests for _handle_task_assignment using current direct flow."""

    @pytest.mark.asyncio
    async def test_calls_get_llm_response_direct_without_workspace(self):
        """验证无 workspace 时走 direct 响应分支."""
        ch = _make_channel()

        ch._get_llm_response_direct = AsyncMock()
        ch.send_custom_card = AsyncMock(return_value=(0, "msg123"))

        await ch._handle_task_assignment(
            sap_id="sap123",
            from_id="open123",
            task_content="This is a long task description",
            meta={"send_addr": "yst123"},
            yst_id="yst123",
            msg_content="This is a long task description",
        )

        ch._get_llm_response_direct.assert_called_once()
        call_args = ch._get_llm_response_direct.call_args[0]
        assert call_args[0]["send_addr"] == "yst123"
        assert call_args[4] == "yst123"

    @pytest.mark.asyncio
    async def test_no_consume_with_tracker_call(self):
        """验证不调用 _consume_with_tracker."""
        ch = _make_channel()

        ch._get_llm_response_direct = AsyncMock()
        ch._consume_with_tracker = AsyncMock()
        ch.send_custom_card = AsyncMock(return_value=(0, "msg123"))

        await ch._handle_task_assignment(
            sap_id="sap123",
            from_id="open123",
            task_content="This is a long task description",
            meta={"send_addr": "yst123"},
            yst_id="yst123",
            msg_content="This is a long task description",
        )

        # Should NOT call _consume_with_tracker
        ch._consume_with_tracker.assert_not_called()
        ch._get_llm_response_direct.assert_called_once()


class TestCase1AndCase3Unaffected:
    """Tests for Case 1 and Case 3 flow not affected."""

    @pytest.mark.asyncio
    async def test_case1_task_progress_query_unchanged(self):
        """验证 Case 1（任务进度查询）流程不变."""
        ch = _make_channel()

        # Mock _query_task_progress
        ch._query_task_progress = AsyncMock(return_value=True)

        # Case 1: message is exactly one of the keywords
        await ch._route_message(
            "msg123",
            "open123",
            "sap123",
            "yst123",
            "我的任务进度",
            {"send_addr": "yst123"},
        )

        # Should call _query_task_progress
        ch._query_task_progress.assert_called_once()

    @pytest.mark.asyncio
    async def test_case3_casual_chat_unchanged(self):
        """验证 Case 3（闲聊）流程不变."""
        ch = _make_channel()

        # Mock _handle_casual_chat
        ch._handle_casual_chat = AsyncMock()

        # Case 3: short message (< 10 chars)
        await ch._route_message(
            "msg123",
            "open123",
            "sap123",
            "yst123",
            "你好",  # Short message
            {"send_addr": "yst123"},
        )

        # Should call _handle_casual_chat
        ch._handle_casual_chat.assert_called_once()

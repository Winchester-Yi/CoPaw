# -*- coding: utf-8 -*-
"""会话标题异步生成与前端刷新事件的回归测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from swe.app.channels.console.channel import ConsoleChannel
from swe.app.runner import runner as runner_module


def _should_generate_session_title(chat, fallback_name: str) -> bool:
    """读取 runner 的标题生成判定函数，缺失时给出清晰失败。"""
    should_generate = getattr(
        runner_module,
        "_should_generate_session_title",
        None,
    )
    assert should_generate is not None
    return should_generate(chat, fallback_name=fallback_name)


@pytest.mark.asyncio
async def test_console_stream_waits_for_session_title_task():
    """主回答结束后，SSE 应等待标题任务写入并推送刷新事件。"""

    async def process(request):
        async def update_title():
            await asyncio.sleep(0)
            request.channel_meta = {
                **request.channel_meta,
                "session_title": "费用分析",
            }

        setattr(
            request,
            "_session_title_task",
            asyncio.create_task(update_title()),
        )
        yield SimpleNamespace(object="message", status=None, type="message")

    channel = ConsoleChannel(
        process=process,
        enabled=True,
        bot_prefix="Friday",
    )
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        input=None,
        channel_meta={},
    )

    events = [event async for event in channel.stream_one(request)]

    assert any(
        '"object": "session_title_updated"' in event
        and '"session_title": "费用分析"' in event
        for event in events
    )


@pytest.mark.asyncio
async def test_console_stream_does_not_wait_for_title_before_first_event():
    """A slow title task must not delay the first business SSE event."""

    async def process(request):
        async def update_title():
            await asyncio.sleep(0)
            request.channel_meta = {
                **request.channel_meta,
                "session_title": "费用分析",
            }

        setattr(
            request,
            "_session_title_task",
            asyncio.create_task(update_title()),
        )
        yield SimpleNamespace(object="message", status=None, type="message")

    channel = ConsoleChannel(
        process=process,
        enabled=True,
        bot_prefix="Friday",
    )
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        input=None,
        channel_meta={},
    )

    events = [event async for event in channel.stream_one(request)]

    assert '"object": "session_title_updated"' not in events[0]
    assert any(
        '"object": "session_title_updated"' in event for event in events
    )


@pytest.mark.asyncio
async def test_console_stream_reads_session_title_written_during_process():
    """同步 hook 写入标题时，SSE 结束阶段也应重新读取 channel_meta。"""

    async def process(request):
        request.channel_meta = {
            **request.channel_meta,
            "session_title": "Hook 标题",
        }
        yield SimpleNamespace(object="message", status=None, type="message")

    channel = ConsoleChannel(
        process=process,
        enabled=True,
        bot_prefix="Friday",
    )
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        input=None,
        channel_meta={},
    )

    events = [event async for event in channel.stream_one(request)]

    assert any(
        '"object": "session_title_updated"' in event
        and '"session_title": "Hook 标题"' in event
        for event in events
    )


@pytest.mark.asyncio
async def test_console_stream_emits_compaction_boundary_metadata_only():
    """归档完成后，前端收到独立的分割线事件而不是空助手消息。"""

    async def process(_request):
        yield SimpleNamespace(
            object="message",
            status=None,
            type="message",
            output=[],
            metadata={
                "conversation_compaction_boundary": {
                    "id": "boundary-1",
                    "archived_message_count": 3,
                },
            },
        )

    channel = ConsoleChannel(
        process=process,
        enabled=True,
        bot_prefix="Friday",
    )
    request = SimpleNamespace(
        session_id="session-1",
        user_id="user-1",
        input=None,
        channel_meta={"chat_id": "chat-from-runtime-metadata"},
    )

    events = [event async for event in channel.stream_one(request)]

    assert any(
        '"object": "conversation_compacted"' in event
        and '"chat_id": "chat-from-runtime-metadata"' in event
        and '"archived_message_count": 3' in event
        for event in events
    )
    assert not any('"object": "message"' in event for event in events)


@pytest.mark.asyncio
async def test_console_compaction_uses_chat_id_from_reassigned_metadata():
    """Boundary frames must not reuse metadata captured before title emit."""

    async def process(request):
        request.channel_meta = {"chat_id": "chat-after-title"}
        yield SimpleNamespace(
            object="message",
            status=None,
            type="message",
            output=[],
            metadata={
                "conversation_compaction_boundary": {
                    "id": "boundary-1",
                    "archived_message_count": 3,
                },
            },
        )

    channel = ConsoleChannel(
        process=process,
        enabled=True,
        bot_prefix="Friday",
    )
    request = SimpleNamespace(
        chat_id="legacy-chat-id",
        session_id="session-1",
        user_id="user-1",
        input=None,
        channel_meta={
            "chat_id": "stale-chat-id",
            "session_title": "Already emitted",
        },
    )

    events = [event async for event in channel.stream_one(request)]

    assert any('"chat_id": "chat-after-title"' in event for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "channel_meta",
    [{}, {"chat_id": ""}, {"chat_id": 42}],
)
async def test_console_compaction_uses_legacy_chat_id_for_invalid_metadata(
    channel_meta,
):
    """SSE must preserve the legacy request field when metadata is unusable."""

    async def process(_request):
        yield SimpleNamespace(
            object="message",
            status=None,
            type="message",
            output=[],
            metadata={
                "conversation_compaction_boundary": {
                    "id": "boundary-1",
                    "archived_message_count": 3,
                },
            },
        )

    channel = ConsoleChannel(
        process=process,
        enabled=True,
        bot_prefix="Friday",
    )
    request = SimpleNamespace(
        chat_id="legacy-chat-id",
        session_id="session-1",
        user_id="user-1",
        input=None,
        channel_meta=channel_meta,
    )

    events = [event async for event in channel.stream_one(request)]

    assert any('"chat_id": "legacy-chat-id"' in event for event in events)


def test_should_generate_session_title_for_legacy_auto_name():
    """尚未生成过标题且仍是历史自动名称时，允许生成标题。"""
    chat = SimpleNamespace(name="帮我分析销售数", meta={})

    assert _should_generate_session_title(
        chat,
        fallback_name="帮我分析销售数",
    )


def test_should_not_generate_session_title_after_generated():
    """已经生成过标题的会话不应在后续轮次再次覆盖。"""
    chat = SimpleNamespace(
        name="销售复盘",
        meta={"session_title_generated": True},
    )

    assert not _should_generate_session_title(
        chat,
        fallback_name="帮我分析销售数",
    )


def test_should_not_generate_session_title_for_custom_name():
    """用户或外部系统已改名的会话不应被自动标题覆盖。"""
    chat = SimpleNamespace(name="人工命名", meta={})

    assert not _should_generate_session_title(
        chat,
        fallback_name="帮我分析销售数",
    )

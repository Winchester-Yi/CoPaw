# -*- coding: utf-8 -*-

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from swe.app.channels.console.channel import ConsoleChannel
from swe.app.channels.manager import ChannelManager
from swe.app.channels.base import ContentType, TextContent
from swe.config.config import ChannelConfig, Config, ConsoleConfig


class _FakeConsoleChannel:
    channel = "console"

    @classmethod
    def from_env(cls, process, on_reply_sent=None):
        return SimpleNamespace(
            channel=cls.channel,
            process=process,
            on_reply_sent=on_reply_sent,
        )

    @classmethod
    def from_config(cls, **kwargs):
        return SimpleNamespace(
            channel=cls.channel,
            config=kwargs["config"],
        )


class _FakeZhaohuChannel:
    channel = "zhaohu"

    @classmethod
    def from_env(cls, process, on_reply_sent=None):
        return SimpleNamespace(
            channel=cls.channel,
            process=process,
            on_reply_sent=on_reply_sent,
        )

    @classmethod
    def from_config(cls, **kwargs):
        return SimpleNamespace(
            channel=cls.channel,
            config=kwargs["config"],
        )


def test_channel_manager_from_env_keeps_console_when_env_filter_excludes_it():
    with (
        patch(
            "swe.app.channels.manager.get_available_channels",
            return_value=("zhaohu",),
        ),
        patch(
            "swe.app.channels.manager.get_channel_registry",
            return_value={
                "console": _FakeConsoleChannel,
                "zhaohu": _FakeZhaohuChannel,
            },
        ),
    ):
        manager = ChannelManager.from_env(process=Mock())

    assert [channel.channel for channel in manager.channels] == [
        "console",
        "zhaohu",
    ]


def test_channel_manager_from_config_keeps_console_when_env_filter_excludes_it():
    config = Config(channels=ChannelConfig())

    with (
        patch(
            "swe.app.channels.manager.get_available_channels",
            return_value=("zhaohu",),
        ),
        patch(
            "swe.app.channels.manager.get_channel_registry",
            return_value={
                "console": _FakeConsoleChannel,
                "zhaohu": _FakeZhaohuChannel,
            },
        ),
    ):
        manager = ChannelManager.from_config(
            process=Mock(),
            config=config,
        )

    assert [channel.channel for channel in manager.channels] == [
        "console",
        "zhaohu",
    ]
    assert manager.channels[0].config.enabled is True


def test_console_channel_from_env_ignores_disable_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CONSOLE_CHANNEL_ENABLED", "0")
    monkeypatch.setenv("CONSOLE_MEDIA_DIR", str(tmp_path))

    channel = ConsoleChannel.from_env(process=Mock())

    assert channel.enabled is True


def test_console_channel_from_config_ignores_disabled_config(tmp_path):
    channel = ConsoleChannel.from_config(
        process=Mock(),
        config=ConsoleConfig(enabled=False, media_dir=str(tmp_path)),
    )

    assert channel.enabled is True


async def test_console_channel_does_not_confirm_parts_when_disabled(tmp_path):
    channel = ConsoleChannel(
        process=Mock(),
        enabled=False,
        bot_prefix="",
        media_dir=str(tmp_path),
    )

    delivered = await channel.send_content_parts(
        "user-1",
        [TextContent(type=ContentType.TEXT, text="output")],
    )

    assert delivered is False


def test_console_channel_copies_b3_trace_id_from_meta_to_request(tmp_path):
    channel = ConsoleChannel(
        process=Mock(),
        enabled=True,
        bot_prefix="",
        media_dir=str(tmp_path),
    )

    request = channel.build_agent_request_from_native(
        {
            "channel_id": "console",
            "sender_id": "user-1",
            "content_parts": [
                TextContent(type=ContentType.TEXT, text="hello"),
            ],
            "meta": {
                "session_id": "session-1",
                "b3_trace_id": "8267fd70bacf497704fec30eaa353979",
            },
        },
    )

    assert request.b3_trace_id == "8267fd70bacf497704fec30eaa353979"


class _ReloadableFakeChannel:
    uses_manager_queue = True

    def __init__(self, channel: str):
        self.channel = channel
        self.start = AsyncMock()
        self.stop = AsyncMock()
        self.set_enqueue = Mock()
        self._workspace = None
        self._command_registry = None

    def set_workspace(self, workspace, command_registry=None):
        self._workspace = workspace
        self._command_registry = command_registry


class _QueueAwareFakeChannel:
    uses_manager_queue = True

    def __init__(
        self,
        channel: str,
        label: str,
        events: list[str],
        *,
        block_on_consume: bool = False,
    ):
        self.channel = channel
        self.label = label
        self.events = events
        self.block_on_consume = block_on_consume
        self.start = AsyncMock()
        self.stop = AsyncMock()
        self.set_enqueue = Mock()
        self.consume_started = asyncio.Event()
        self.release_consume = asyncio.Event()

    def _extract_query_from_payload(self, payload):
        return payload.get("text", "")

    def get_debounce_key(self, payload):
        return payload["session_id"]

    def _is_native_payload(self, payload):
        return False

    def merge_requests(self, requests):
        return requests[0] if requests else None

    async def consume_one(self, payload):
        del payload
        self.consume_started.set()
        if self.block_on_consume:
            await self.release_consume.wait()
        self.events.append(self.label)


async def test_replace_channel_preserves_workspace_binding():
    old_channel = _ReloadableFakeChannel("console")
    manager = ChannelManager([old_channel])
    workspace = SimpleNamespace(tenant_id="tenant-a")
    manager.set_workspace(workspace)

    new_channel = _ReloadableFakeChannel("console")

    await manager.replace_channel(new_channel)

    assert new_channel._workspace is workspace
    assert new_channel._command_registry is not None


async def test_replace_channel_reuses_existing_session_queue_with_new_channel():
    events: list[str] = []
    old_channel = _QueueAwareFakeChannel("console", "old", events)
    manager = ChannelManager([old_channel])
    await manager.start_all()

    try:
        manager._enqueue_one(  # pylint: disable=protected-access
            "console",
            {"session_id": "console:user-1", "text": "first"},
        )
        await asyncio.sleep(0.05)

        new_channel = _QueueAwareFakeChannel("console", "new", events)
        await manager.replace_channel(new_channel)

        manager._enqueue_one(  # pylint: disable=protected-access
            "console",
            {"session_id": "console:user-1", "text": "second"},
        )
        await asyncio.sleep(0.05)
    finally:
        await manager.stop_all()

    assert events == ["old", "new"]


async def test_remove_channel_cancels_existing_session_consumers():
    events: list[str] = []
    channel = _QueueAwareFakeChannel(
        "console",
        "old",
        events,
        block_on_consume=True,
    )
    manager = ChannelManager([channel])
    await manager.start_all()
    queue_keys_after_remove = None

    try:
        manager._enqueue_one(  # pylint: disable=protected-access
            "console",
            {"session_id": "console:user-1", "text": "first"},
        )
        await asyncio.wait_for(channel.consume_started.wait(), timeout=1.0)

        manager._enqueue_one(  # pylint: disable=protected-access
            "console",
            {"session_id": "console:user-1", "text": "second"},
        )
        await asyncio.sleep(0.05)

        removed = await manager.remove_channel("console")
        channel.release_consume.set()
        await asyncio.sleep(0.05)
        queue_keys_after_remove = set(manager._queue_manager._queues)
    finally:
        await manager.stop_all()

    assert removed is True
    assert events == []
    assert queue_keys_after_remove == set()

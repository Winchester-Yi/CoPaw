# -*- coding: utf-8 -*-
"""Tests for ConsoleChannel terminal output controls."""

from unittest.mock import patch

import pytest
from agentscope_runtime.engine.schemas.agent_schemas import TextContent

from swe.app import console_push_store
from swe.app.channels.console.channel import ConsoleChannel


def test_console_output_can_be_disabled_with_environment(
    monkeypatch,
    tmp_path,
):
    """Disabling console output must not call stdout print."""
    monkeypatch.setenv("SWE_CONSOLE_OUTPUT_ENABLED", "false")
    channel = ConsoleChannel(
        process=None,
        enabled=True,
        bot_prefix="",
        media_dir=str(tmp_path),
    )

    with patch("builtins.print") as printer:
        channel._safe_print("🤖 [12:00:00] Bot reply")

    printer.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("content_parts", [False, True])
async def test_console_propagates_rejected_push_without_truncation(
    monkeypatch,
    tmp_path,
    content_parts,
):
    monkeypatch.setenv("SWE_CONSOLE_OUTPUT_ENABLED", "false")
    await console_push_store.reset()
    monkeypatch.setattr(console_push_store, "_MAX_ENTRY_BYTES", 100)
    channel = ConsoleChannel(
        process=None,
        enabled=True,
        bot_prefix="",
        media_dir=str(tmp_path),
    )
    text = "中" * 100
    if content_parts:
        delivered = await channel.send_content_parts(
            "user",
            [TextContent(type="text", text=text)],
            {"session_id": "s"},
        )
    else:
        delivered = await channel.send("user", text, {"session_id": "s"})
    assert delivered is False
    assert await console_push_store.take("s") == []
    assert await channel.send("user", text) is True
    await console_push_store.reset()

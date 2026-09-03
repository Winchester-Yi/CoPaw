# -*- coding: utf-8 -*-
"""Tests for ConsoleChannel terminal output controls."""

from unittest.mock import patch

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

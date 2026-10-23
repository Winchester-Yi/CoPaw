# -*- coding: utf-8 -*-
"""Immediately accepted SSE events must still cooperate with the event loop."""

import asyncio

import pytest

from swe.app.answer_turn.models import TurnIdentity
from swe.app.runner.sse_replay import ReplayStream
from swe.app.runner.task_tracker import TaskTracker
from swe.app.runner.tool_output_frames import (
    emit_tool_output_text,
    tool_output_invocation,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_frames", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_immediate_publication_yields_and_preserves_order(
    monkeypatch,
    tool_frames,
    cancel,
):
    monkeypatch.setenv("SWE_SSE_BATCH_WRITES", "1")
    tracker = TaskTracker()
    identity = TurnIdentity(chat_id="fair", msgid="msg", turn_id="turn-fair")
    frames, observed = [], []

    def observe():
        observed.append(len(frames))
        if cancel:
            tracker._runs[identity.chat_id].task.cancel()

    async def offer(_stream, text):
        frames.append(text)
        if len(frames) == 1:
            asyncio.get_running_loop().call_soon(observe)
        return len(frames)

    monkeypatch.setattr(ReplayStream, "offer", offer)

    async def producer(*_args):
        with tool_output_invocation(
            tool_call_id="call",
            tool_name="execute_shell_command",
        ):
            for index in range(130):
                if tool_frames:
                    await emit_tool_output_text("stdout", f"{index}\n")
                else:
                    yield f"data: {index}\n\n"

    subscriber, _ = await tracker.attach_or_start(identity, {}, producer)
    try:
        await tracker.wait_all_done(timeout=2)
        assert observed == [64]
        expected = 64 if cancel else 130
        assert len(frames) == expected
        if tool_frames:
            import json

            assert [
                json.loads(frame[6:])["sequence"] for frame in frames
            ] == list(
                range(1, expected + 1),
            )
        else:
            assert frames == [
                f"data: {index}\n\n" for index in range(expected)
            ]
        assert identity.chat_id not in tracker._runs
    finally:
        await subscriber.stream.aclose()

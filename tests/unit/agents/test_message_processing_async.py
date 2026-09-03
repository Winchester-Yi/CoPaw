# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from agentscope.message import Msg

from swe.agents.utils import message_processing


@pytest.mark.asyncio
async def test_media_blocks_are_processed_in_parallel_and_written_in_order(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        message_processing,
        "load_config",
        lambda: SimpleNamespace(agents=SimpleNamespace(language="en")),
    )

    async def fake_process(content, _index, block):
        await asyncio.sleep(0.03 if block["id"] == "first" else 0.0)
        content[0] = {"type": block["type"], "id": block["id"], "done": True}
        return block["id"]

    monkeypatch.setattr(
        message_processing,
        "_process_single_block",
        fake_process,
    )
    msg = Msg(
        name="user",
        role="user",
        content=[
            {"type": "image", "id": "first"},
            {"type": "image", "id": "second"},
        ],
    )

    await message_processing.process_file_and_media_blocks_in_message(msg)
    assert [
        block["id"]
        for block in msg.content
        if isinstance(block, dict) and "id" in block
    ] == [
        "first",
        "second",
    ]
    assert len(msg.content) == 4


@pytest.mark.asyncio
async def test_one_media_block_failure_does_not_abort_other_blocks(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        message_processing,
        "load_config",
        lambda: SimpleNamespace(agents=SimpleNamespace(language="en")),
    )

    async def fake_process(content, _index, block):
        if block["id"] == "bad":
            raise RuntimeError("broken")
        content[0] = {"type": block["type"], "id": block["id"], "done": True}
        return block["id"]

    monkeypatch.setattr(
        message_processing,
        "_process_single_block",
        fake_process,
    )
    msg = Msg(
        name="user",
        role="user",
        content=[
            {"type": "image", "id": "bad"},
            {"type": "image", "id": "good"},
        ],
    )

    await message_processing.process_file_and_media_blocks_in_message(msg)
    assert msg.content[0]["id"] == "bad"
    assert msg.content[1]["done"] is True

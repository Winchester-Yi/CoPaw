# -*- coding: utf-8 -*-
"""Request prompt preparation and cancellation preserve loop ownership."""

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from agentscope.message import Msg

from swe.agents import prompt
from swe.agents.react_agent import SWEAgent


@pytest.mark.asyncio
async def test_prepared_prompt_avoids_constructor_file_reads(
    tmp_path,
    monkeypatch,
):
    (tmp_path / "AGENTS.md").write_text("---\nname: test\n---\nhello\n")
    threads = []
    original = prompt.read_text_file_with_encoding_fallback

    def read(path):
        threads.append(threading.get_ident())
        return original(path)

    monkeypatch.setattr(prompt, "read_text_file_with_encoding_fallback", read)
    prepared = await prompt.prepare_system_prompt_async(
        tmp_path,
        ["AGENTS.md"],
        "agent",
        False,
    )
    assert threads and threading.get_ident() not in threads
    threads.clear()
    with prompt.bind_prepared_system_prompt(prepared):
        text = prompt.build_system_prompt_from_working_dir(
            tmp_path,
            ["AGENTS.md"],
            "agent",
            False,
        )
        agent = object.__new__(SWEAgent)
        agent._workspace_dir = tmp_path
        agent._agent_config = SimpleNamespace(
            system_prompt_files=["AGENTS.md"],
        )
        assert agent._system_prompt_file_snapshot() == prepared.file_snapshot
    assert threads == []
    assert "hello" in text and "Your agent id is `agent`" in text
    assert "name: test" not in text
    (tmp_path / "AGENTS.md").write_text("changed")
    assert "changed" in prompt.build_system_prompt_from_working_dir(
        tmp_path,
        ["AGENTS.md"],
        "agent",
        False,
    )


def _agent():
    agent = object.__new__(SWEAgent)
    agent._sys_prompt = "old"
    agent._sys_prompt_freshness_token = ("old",)
    agent.memory = SimpleNamespace(
        content=[(Msg("system", "old", "system"), [])],
    )
    return agent


@pytest.mark.asyncio
async def test_async_rebuild_computes_on_worker_applies_on_loop(monkeypatch):
    agent = _agent()
    observed = []

    def token(self):
        observed.append(threading.get_ident())
        return ("new",)

    def build(self):
        observed.append(threading.get_ident())
        return "new prompt"

    monkeypatch.setattr(
        SWEAgent,
        "_current_system_prompt_freshness_token",
        token,
    )
    monkeypatch.setattr(SWEAgent, "_build_sys_prompt", build)
    await agent.rebuild_sys_prompt_async()
    assert observed and threading.get_ident() not in observed
    assert agent._sys_prompt == "new prompt"
    assert agent.memory.content[0][0].content == "new prompt"


@pytest.mark.asyncio
async def test_cancelled_rebuild_does_not_apply_worker_result(monkeypatch):
    agent = _agent()
    started, release, finished = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    monkeypatch.setattr(
        SWEAgent,
        "_current_system_prompt_freshness_token",
        lambda self: ("new",),
    )

    def build(self):
        started.set()
        assert release.wait(5)
        finished.set()
        return "new"

    monkeypatch.setattr(SWEAgent, "_build_sys_prompt", build)
    task = asyncio.create_task(agent.rebuild_sys_prompt_async())
    try:
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    while not finished.is_set():
        await asyncio.sleep(0.001)
    assert agent._sys_prompt == "old"
    assert agent.memory.content[0][0].content == "old"


@pytest.mark.asyncio
async def test_prompt_preparation_is_request_scoped_and_binding_resets(
    tmp_path,
):
    other = tmp_path / "other"
    other.mkdir()
    (tmp_path / "AGENTS.md").write_text("tenant-a")
    (other / "AGENTS.md").write_text("tenant-b")
    first, second = await asyncio.gather(
        prompt.prepare_system_prompt_async(
            tmp_path,
            ["AGENTS.md"],
            "a",
            False,
        ),
        prompt.prepare_system_prompt_async(other, ["AGENTS.md"], "b", True),
    )
    with pytest.raises(ValueError), prompt.bind_prepared_system_prompt(first):
        assert "tenant-b" in prompt.build_system_prompt_from_working_dir(
            other,
            ["AGENTS.md"],
            "b",
            True,
        )
        with prompt.bind_prepared_system_prompt(second):
            assert (
                prompt.build_system_prompt_from_working_dir(
                    other,
                    ["AGENTS.md"],
                    "b",
                    True,
                )
                == second.text
            )
        assert prompt._prepared_prompt.get() is first
        raise ValueError("constructor failed")
    assert prompt._prepared_prompt.get() is None
    (tmp_path / "AGENTS.md").write_text("updated")
    assert "updated" in prompt.build_system_prompt_from_working_dir(
        tmp_path,
        ["AGENTS.md"],
        "a",
        False,
    )

# -*- coding: utf-8 -*-
"""Static schemas may be reused; tool instances and dynamic closures may not."""

import threading
from types import SimpleNamespace

import pytest
from agentscope.tool import Toolkit


def test_builtin_schema_matches_sdk_and_is_a_private_copy():
    from swe.agents.builtin_tool_schemas import get_builtin_tool_schema
    from swe.agents.tools import read_file

    toolkit = Toolkit()
    toolkit.register_tool_function(read_file)
    expected = toolkit.tools["read_file"].json_schema
    first = get_builtin_tool_schema(read_file)
    assert first == expected
    first["function"]["name"] = "changed"
    first["function"]["parameters"]["properties"].clear()
    assert get_builtin_tool_schema(read_file) == expected


def test_request_closures_are_not_cached():
    from swe.agents.builtin_tool_schemas import get_builtin_tool_schema

    def tool(path: str):
        return path

    assert get_builtin_tool_schema(tool) is None


@pytest.mark.asyncio
async def test_query_prewarms_schemas_in_existing_worker(monkeypatch):
    from swe.agents import builtin_tool_schemas
    from swe.app.runner.runner import AgentRunner
    from swe.runtime_workers import run_runtime_state_work

    seen = []
    monkeypatch.setattr(
        builtin_tool_schemas,
        "prepare_builtin_tool_schemas",
        lambda: seen.append(threading.get_ident()),
    )
    runner = object.__new__(AgentRunner)
    runner.session = SimpleNamespace()
    monkeypatch.setattr(runner, "_build_query_request_context", lambda **_: {})
    monkeypatch.setattr(runner, "_apply_query_mode_context", lambda **_: None)
    monkeypatch.setattr(
        runner,
        "_apply_selected_expert_context",
        lambda **_: None,
    )
    assert (
        await run_runtime_state_work(runner._prepare_query_agent_context, {})
        == {}
    )
    assert seen and threading.get_ident() not in seen


@pytest.mark.asyncio
async def test_prepared_builtin_registration_does_not_parse_on_loop(
    monkeypatch,
):
    from swe.agents import builtin_tool_schemas, react_agent
    from swe.agents.tools import read_file
    from swe.runtime_workers import run_runtime_state_work

    await run_runtime_state_work(
        builtin_tool_schemas.prepare_builtin_tool_schemas,
    )
    original = Toolkit.register_tool_function

    def register(toolkit, function, **kwargs):
        assert kwargs.get("json_schema") is not None
        return original(toolkit, function, **kwargs)

    monkeypatch.setattr(Toolkit, "register_tool_function", register)
    agent = object.__new__(react_agent.SWEAgent)
    monkeypatch.setattr(
        agent,
        "_normalize_registered_tool_functions",
        lambda *_: None,
    )
    first, second = Toolkit(), Toolkit()
    for toolkit in (first, second):
        agent._register_enabled_tools(
            toolkit,
            {"read_file": read_file},
            {},
            {},
            False,
            "skip",
        )
    assert first.tools["read_file"] is not second.tools["read_file"]
    first.tools["read_file"].json_schema["function"]["name"] = "changed"
    assert (
        second.tools["read_file"].json_schema["function"]["name"]
        == "read_file"
    )

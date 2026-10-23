# -*- coding: utf-8 -*-
"""Contract tests for the agent runtime construction boundary."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from swe.agents.agent_runtime_builder import (
    AgentRequestContext,
    AgentRuntimeComponents,
    AgentRuntimeBuilder,
    McpToolRegistrar,
)


def test_agent_constructor_logs_resolved_model_without_provider_lookup(
    monkeypatch,
):
    from swe.agents import react_agent
    from swe.providers.provider_manager import ProviderManager

    lookup = MagicMock(side_effect=AssertionError("provider lookup on loop"))
    monkeypatch.setattr(ProviderManager, "get_instance", lookup)
    config = SimpleNamespace(
        id="agent-a",
        language="zh",
        running=SimpleNamespace(max_iters=3),
    )
    config.model_copy = lambda **_: config

    def build(agent, **_kwargs):
        agent._resolved_model_slot.update(
            provider_id="custom",
            model="model-a",
        )
        return AgentRuntimeComponents(
            object(),
            "prompt",
            object(),
            object(),
            object(),
        )

    monkeypatch.setattr(AgentRuntimeBuilder, "build_for_swe_agent", build)
    monkeypatch.setattr(
        react_agent.ReActAgent,
        "__init__",
        lambda self, **_: None,
    )
    monkeypatch.setattr(react_agent, "CommandHandler", lambda **_: object())
    monkeypatch.setattr(
        react_agent.SWEAgent,
        "_init_agent_phase_state",
        lambda _: None,
    )
    monkeypatch.setattr(
        react_agent.SWEAgent,
        "_register_hooks",
        lambda _: None,
    )
    monkeypatch.setattr(
        react_agent.SWEAgent,
        "_current_system_prompt_freshness_token",
        lambda _: (),
    )

    def memory(agent, *_args, **_kwargs):
        agent.memory_manager = None
        agent._enable_memory_manager = False

    monkeypatch.setattr(react_agent.SWEAgent, "_setup_memory_manager", memory)
    agent = object.__new__(react_agent.SWEAgent)
    agent.name = "Friday"
    agent.memory = None
    react_agent.SWEAgent.__init__(agent, config)
    lookup.assert_not_called()


def test_request_context_round_trips_fixed_keys_and_extras() -> None:
    """Legacy request metadata retains every non-reserved value."""
    legacy_context = {
        "session_id": "session-1",
        "user_id": "user-1",
        "channel": "console",
        "agent_id": "agent-1",
        "tenant_id": "tenant-1",
        "chat_id": "chat-1",
        "turn_id": "turn-1",
        "trace_id": "trace-1",
        "source_id": "source-1",
    }

    context = AgentRequestContext.from_legacy_dict(legacy_context)

    assert context.session_id == "session-1"
    assert context.user_id == "user-1"
    assert context.channel == "console"
    assert context.agent_id == "agent-1"
    assert context.tenant_id == "tenant-1"
    assert context.chat_id == "chat-1"
    assert context.turn_id == "turn-1"
    assert context.extras == {"trace_id": "trace-1", "source_id": "source-1"}
    assert context.to_legacy_dict() == legacy_context


def test_request_context_preserves_none_value_presence() -> None:
    """Legacy None values remain present across the typed compatibility boundary."""
    legacy_context = {
        "session_id": None,
        "user_id": "user-1",
        "trace_id": None,
        "source_id": "source-1",
    }

    context = AgentRequestContext.from_legacy_dict(legacy_context)

    assert context.session_id is None
    assert context.extras == {"trace_id": None, "source_id": "source-1"}
    assert context.to_legacy_dict() == legacy_context


def test_request_context_extras_cannot_override_fixed_keys() -> None:
    """Reserved legacy keys always serialize from the typed context fields."""
    context = AgentRequestContext(
        session_id="session-1",
        user_id="user-1",
        channel="console",
        agent_id="agent-1",
        tenant_id="tenant-1",
        chat_id="chat-1",
        turn_id="turn-1",
        extras={
            "session_id": "forged-session",
            "tenant_id": "forged-tenant",
            "trace_id": "trace-1",
        },
    )

    assert context.to_legacy_dict() == {
        "session_id": "session-1",
        "user_id": "user-1",
        "channel": "console",
        "agent_id": "agent-1",
        "tenant_id": "tenant-1",
        "chat_id": "chat-1",
        "turn_id": "turn-1",
        "trace_id": "trace-1",
    }


def test_runtime_builder_exposes_constructed_components_without_model_call() -> (
    None
):
    """The builder assembles injected dependencies without invoking the model."""
    toolkit = object()
    model = object()
    formatter = object()
    memory = object()
    calls: list[str] = []

    def build_toolkit() -> object:
        calls.append("toolkit")
        return toolkit

    def build_system_prompt() -> str:
        calls.append("system_prompt")
        return "system prompt"

    def build_model_and_formatter() -> tuple[object, object]:
        calls.append("model_and_formatter")
        return model, formatter

    def build_memory() -> object:
        calls.append("memory")
        return memory

    components = AgentRuntimeBuilder(
        toolkit_factory=build_toolkit,
        system_prompt_factory=build_system_prompt,
        model_and_formatter_factory=build_model_and_formatter,
        memory_factory=build_memory,
    ).build()

    assert calls == [
        "toolkit",
        "system_prompt",
        "model_and_formatter",
        "memory",
    ]
    assert isinstance(components, AgentRuntimeComponents)
    assert components.toolkit is toolkit
    assert components.system_prompt == "system prompt"
    assert components.model is model
    assert components.formatter is formatter
    assert components.memory is memory


class _RecoverableListToolsError(ConnectionError):
    """Small fake transport failure for registrar recovery behavior."""


class _FakeStatefulClient:
    def __init__(self, name: str, failure: Exception | None = None) -> None:
        self.name = name
        self._failure = failure
        self.on_progress_callback: Callable[[], None] | None = None

    async def list_tools(self) -> list[object]:
        if self._failure is not None:
            raise self._failure
        return []


class _FakeToolkit:
    def __init__(self) -> None:
        self.registered_client_names: list[str] = []
        self.tools: dict[str, object] = {}

    async def register_mcp_client(
        self,
        client: _FakeStatefulClient,
        *,
        namesake_strategy: str,
    ) -> None:
        assert namesake_strategy == "skip"
        self.registered_client_names.append(client.name)
        self.tools[client.name] = object()


@pytest.mark.asyncio
async def test_registrar_from_agent_wires_watchdog_and_normalizes_new_tools() -> (
    None
):
    """The registrar uses the agent's policies at the registration boundary."""
    toolkit = _FakeToolkit()
    client = _FakeStatefulClient("available")
    reset_watchdog_calls: list[str] = []
    normalization_calls: list[tuple[object, list[str]]] = []

    class Agent:
        _mcp_clients = [client]
        _agent_config = type("AgentConfig", (), {"tools": None})()
        _source_tool_versions = ()

        def _reset_watchdog(self) -> None:
            reset_watchdog_calls.append("reset")

        def _normalize_registered_tool_functions(
            self,
            registered_toolkit: object,
            names: list[str],
        ) -> None:
            normalization_calls.append((registered_toolkit, names))

    agent = Agent()
    agent.toolkit = toolkit

    await McpToolRegistrar.from_agent(agent).register_clients()

    progress_callback = client.on_progress_callback
    assert progress_callback is not None
    progress_callback()
    assert reset_watchdog_calls == ["reset"]
    assert normalization_calls == [(toolkit, ["available"])]


@pytest.mark.asyncio
async def test_stateful_mcp_registration_continues_after_recoverable_failure() -> (
    None
):
    """A failed client does not prevent a later stateful client from registering."""
    toolkit = _FakeToolkit()
    registrar = McpToolRegistrar(toolkit=toolkit)

    await registrar.register_stateful_clients(
        [
            _FakeStatefulClient(
                "unavailable",
                _RecoverableListToolsError("temporary transport issue"),
            ),
            _FakeStatefulClient("available"),
        ],
    )

    assert toolkit.registered_client_names == ["available"]

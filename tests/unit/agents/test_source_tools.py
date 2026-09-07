# -*- coding: utf-8 -*-
from pathlib import Path
from types import SimpleNamespace

import pytest
from anyio import ClosedResourceError

from swe.app.source_tools.models import SourceToolVersion
from swe.agents.react_agent import SWEAgent
from swe.agents.source_tools import (
    SourceToolRuntime,
    _record_source_tool_invocation,
    _terminate_source_tool_process,
    build_source_tool_function,
    source_tool_runtime_env,
)


def _version(
    script: str,
    *,
    required_env: tuple[str, ...] = (),
) -> SourceToolVersion:
    return SourceToolVersion(
        name="source_echo",
        version=1,
        description="Echo the value.",
        json_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        required_env=required_env,
        content_digest="digest",
        script=script,
        created_at=1,
        created_by="manager",
    )


@pytest.mark.asyncio
async def test_source_tool_runs_in_tenant_workspace_and_returns_tool_response(
    tmp_path: Path,
):
    version = _version(
        """
TOOL_NAME = "source_echo"
async def execute(arguments, context):
    return {"value": arguments["value"], "workspace": context["workspace_dir"]}
""",
    )
    tool = build_source_tool_function(
        version,
        SourceToolRuntime(
            tenant_id="tenant-a",
            source_id="source-a",
            workspace_dir=tmp_path,
        ),
    )

    response = await tool(value="hello")

    assert response.content["value"] == "hello"
    assert response.content["workspace"] == str(tmp_path)


@pytest.mark.asyncio
async def test_source_tool_returns_structured_missing_environment_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "swe.agents.source_tools.load_tenant_runtime_env",
        lambda **_kwargs: {},
    )
    tool = build_source_tool_function(
        _version(
            """
TOOL_NAME = "source_echo"
async def execute(arguments, context):
    return {"ok": True}
""",
            required_env=("SERVICE_TOKEN",),
        ),
        SourceToolRuntime(
            tenant_id="tenant-a",
            source_id="source-a",
            workspace_dir=tmp_path,
        ),
    )

    response = await tool()

    assert response.content["error_type"] == "source_tool_configuration_error"


@pytest.mark.asyncio
async def test_source_tool_redacts_declared_credentials_from_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "swe.agents.source_tools.load_tenant_runtime_env",
        lambda **_kwargs: {"SERVICE_TOKEN": "real-secret"},
    )
    tool = build_source_tool_function(
        _version(
            """
TOOL_NAME = "source_echo"
async def execute(arguments, context):
    return {"token": "real-secret", "echo": "real-secret"}
""",
            required_env=("SERVICE_TOKEN",),
        ),
        SourceToolRuntime(
            tenant_id="tenant-a",
            source_id="source-a",
            workspace_dir=tmp_path,
        ),
    )

    response = await tool()

    assert response.content == {"token": "[REDACTED]", "echo": "[REDACTED]"}


def test_source_tool_env_contains_only_declared_tenant_values(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "swe.agents.source_tools.load_tenant_runtime_env",
        lambda **_kwargs: {"DECLARED": "value", "UNDECLARED": "secret"},
    )

    env = source_tool_runtime_env(
        required_env=("DECLARED",),
        tenant_id="tenant-a",
        source_id="source-a",
    )

    assert env["DECLARED"] == "value"
    assert "UNDECLARED" not in env


def test_agent_registers_source_snapshot_after_builtin_tools(tmp_path: Path):
    agent = object.__new__(SWEAgent)
    agent._source_tool_versions = (
        _version(
            """
TOOL_NAME = "source_echo"
async def execute(arguments, context):
    return {"ok": True}
""",
        ),
    )
    agent._request_context = {"tenant_id": "tenant-a", "source_id": "source-a"}
    agent._workspace_dir = tmp_path
    agent._agent_config = SimpleNamespace(
        tools=SimpleNamespace(builtin_tools={}),
    )

    toolkit = agent._create_toolkit()
    agent._register_source_tools(toolkit)

    assert "source_echo" in toolkit.tools


def test_builtin_override_validation_ignores_display_group_schema() -> None:
    parameters = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }
    registered_schema = {
        "type": "function",
        "function": {
            "name": "execute_shell_command",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
    registered = SimpleNamespace(
        json_schema=registered_schema,
        original_func=lambda command: command,
    )
    toolkit = SimpleNamespace(
        tools={"execute_shell_command": registered},
    )
    SWEAgent._normalize_registered_tool_functions(
        toolkit,
        ["execute_shell_command"],
    )
    version = SimpleNamespace(
        name="execute_shell_command",
        json_schema=parameters,
    )

    SWEAgent._validate_source_tool_registration(toolkit, version, True)


def test_source_tool_invocation_is_recorded_with_runtime_attribution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    recorded = []
    monkeypatch.setattr(
        "swe.app.source_tools.service.get_source_tool_service",
        lambda: SimpleNamespace(
            record_invocation=lambda **kwargs: recorded.append(kwargs),
        ),
    )

    runtime = SourceToolRuntime(
        tenant_id="tenant-a",
        source_id="source-a",
        workspace_dir=tmp_path,
        agent_id="agent-a",
    )
    _record_source_tool_invocation(runtime, _version(""), "succeeded")

    assert recorded == [
        {
            "source_id": "source-a",
            "tool": _version(""),
            "tenant_id": "tenant-a",
            "agent_id": "agent-a",
            "result": "succeeded",
        },
    ]


@pytest.mark.asyncio
async def test_timeout_termination_escalates_to_sigkill_after_grace_period(
    monkeypatch: pytest.MonkeyPatch,
):
    signals = []

    class Process:
        pid = 123
        returncode = None

        async def wait(self):
            await __import__("asyncio").sleep(60)

    monkeypatch.setattr("swe.agents.source_tools.os.name", "posix")
    monkeypatch.setattr(
        "swe.agents.source_tools.os.killpg",
        lambda _pid, signal: signals.append(signal),
    )
    monkeypatch.setattr(
        "swe.agents.source_tools.SOURCE_TOOL_TERMINATION_GRACE_SECONDS",
        0,
    )

    await _terminate_source_tool_process(Process())

    import signal

    assert signals == [signal.SIGTERM, signal.SIGKILL]


@pytest.mark.asyncio
async def test_mcp_registration_fails_when_it_collides_with_source_tool(
    tmp_path: Path,
):
    class Client:
        name = "test-mcp"

        async def list_tools(self):
            return [SimpleNamespace(name="source_echo")]

    agent = object.__new__(SWEAgent)
    agent._mcp_clients = (Client(),)
    agent._source_tool_versions = (_version(""),)
    agent._agent_config = SimpleNamespace(
        tools=SimpleNamespace(builtin_tools={}),
    )
    agent._workspace_dir = tmp_path

    with pytest.raises(RuntimeError, match="collides"):
        await agent.register_mcp_clients()


@pytest.mark.asyncio
async def test_mcp_registration_recovers_when_collision_preflight_is_interrupted(
    tmp_path: Path,
):
    class InterruptedClient:
        name = "interrupted-mcp"

        async def list_tools(self):
            raise ClosedResourceError()

    class RecoveredClient:
        name = "recovered-mcp"

    class Toolkit:
        tools: dict[str, object] = {}

        def __init__(self):
            self.registered_clients: list[object] = []

        async def register_mcp_client(
            self,
            client: object,
            *,
            namesake_strategy: str,
        ) -> None:
            assert namesake_strategy == "skip"
            self.registered_clients.append(client)

    interrupted = InterruptedClient()
    recovered = RecoveredClient()
    toolkit = Toolkit()
    agent = object.__new__(SWEAgent)
    agent._mcp_clients = [interrupted]
    agent._source_tool_versions = ()
    agent._agent_config = SimpleNamespace(
        tools=SimpleNamespace(builtin_tools={}),
    )
    agent._workspace_dir = tmp_path
    agent.toolkit = toolkit

    async def recover(client: object) -> object:
        assert client is interrupted
        return recovered

    agent._recover_mcp_client = recover
    agent._wire_mcp_progress_callbacks = lambda _client: None
    agent._normalize_registered_tool_functions = lambda _toolkit, _names: None

    await agent.register_mcp_clients()

    assert agent._mcp_clients == [recovered]
    assert toolkit.registered_clients == [recovered]

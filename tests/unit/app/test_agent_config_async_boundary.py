# -*- coding: utf-8 -*-
"""Request Agent configuration must not read storage on the event loop."""

import asyncio
import threading
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from swe.app import agent_context
from swe.app.routers import agent as agent_router
from swe.app.routers import agents as agents_router
from swe.app.routers import config as config_router
from swe.config.config import (
    AgentProfileConfig,
    AgentProfileRef,
    AgentsConfig,
    AgentsRunningConfig,
    Config,
    load_agent_config,
    save_agent_config,
)
from swe.config.utils import save_config
from swe.config import config


@pytest.mark.asyncio
async def test_request_agent_config_load_runs_in_worker(monkeypatch):
    loop_thread = threading.get_ident()
    scope = ContextVar("config_request_scope", default=None)
    token = scope.set("source-a")
    workspace = SimpleNamespace(agent_id="agent-a", tenant_id="tenant-a")
    expected = object()
    seen = []

    async def resolve(*_args, **_kwargs):
        return workspace

    def load(agent_id, *, tenant_id):
        seen.append((threading.get_ident(), scope.get(), agent_id, tenant_id))
        return expected

    monkeypatch.setattr(agent_context, "get_agent_for_request", resolve)
    monkeypatch.setattr(config, "load_agent_config", load)
    try:
        result = await agent_context.get_agent_and_config_for_request(object())
    finally:
        scope.reset(token)
    assert result == (workspace, expected)
    assert seen == [(seen[0][0], "source-a", "agent-a", "tenant-a")]
    assert seen[0][0] != loop_thread


@pytest.mark.asyncio
async def test_concurrent_agent_config_updates_preserve_both_fields(
    tmp_path,
    monkeypatch,
):
    """Different config fields must not overwrite each other on concurrent PUTs."""
    tenant_id = "tenant-a"
    agent_id = "agent-a"
    tenant_dir = tmp_path / tenant_id
    workspace_dir = tenant_dir / "workspaces" / agent_id
    workspace_dir.mkdir(parents=True)
    workspace = SimpleNamespace(
        agent_id=agent_id,
        tenant_id=tenant_id,
        cron_manager=None,
    )
    monkeypatch.setattr("swe.config.utils.WORKING_DIR", tmp_path)
    monkeypatch.setattr("swe.config.config.WORKING_DIR", tmp_path)
    save_config(
        Config(
            agents=AgentsConfig(
                active_agent=agent_id,
                profiles={
                    agent_id: AgentProfileRef(
                        id=agent_id,
                        workspace_dir=str(workspace_dir),
                    ),
                },
            ),
        ),
        tenant_dir / "config.json",
    )
    save_agent_config(
        agent_id,
        AgentProfileConfig(
            id=agent_id,
            name="Agent A",
            running=AgentsRunningConfig(),
            system_prompt_files=["AGENTS.md"],
        ),
        tenant_id=tenant_id,
    )
    load_barrier = asyncio.Barrier(3)

    async def stale_racing_load(_request):
        snapshot = await asyncio.to_thread(
            load_agent_config,
            agent_id,
            tenant_id=tenant_id,
        )
        await load_barrier.wait()
        return workspace, snapshot

    async def resolve_workspace(_request, agent_id=None):
        assert agent_id is None
        return workspace

    monkeypatch.setattr(
        agent_router,
        "get_agent_and_config_for_request",
        stale_racing_load,
    )
    monkeypatch.setattr(
        agent_context,
        "get_agent_and_config_for_request",
        stale_racing_load,
    )
    monkeypatch.setattr(
        agent_context,
        "get_agent_for_request",
        resolve_workspace,
    )
    monkeypatch.setattr(
        agent_router,
        "schedule_agent_reload",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        agents_router,
        "schedule_agent_reload",
        lambda *_args, **_kwargs: None,
    )
    request = SimpleNamespace(
        state=SimpleNamespace(
            tenant_id=tenant_id,
            source_id=None,
            scope_id=None,
            workspace=workspace,
            agent_id=agent_id,
        ),
        headers={},
    )

    updated_running = AgentsRunningConfig(llm_chat_max_concurrent=7)
    await asyncio.gather(
        agent_router.put_agents_running_config(updated_running, request),
        agent_router.put_system_prompt_files(["CUSTOM.md"], request),
        config_router.put_heartbeat(
            request,
            config_router.HeartbeatBody(enabled=True, every="1h"),
        ),
        agents_router.update_agent(
            agent_id,
            AgentProfileConfig.model_construct(
                id=agent_id,
                name="Renamed Agent",
            ),
            request,
        ),
    )

    stored_config = load_agent_config(agent_id, tenant_id=tenant_id)
    assert stored_config.running.llm_chat_max_concurrent == 7
    assert stored_config.system_prompt_files == ["CUSTOM.md"]
    assert stored_config.heartbeat is not None
    assert stored_config.heartbeat.enabled is True
    assert stored_config.name == "Renamed Agent"

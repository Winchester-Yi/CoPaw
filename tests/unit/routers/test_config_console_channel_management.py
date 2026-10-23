# -*- coding: utf-8 -*-

import sys
import types
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.modules.setdefault("segno", types.ModuleType("segno"))

from swe.config.config import (
    AgentProfileConfig,
    ChannelConfig,
    ConsoleConfig,
)
from swe.app.routers.config import (
    ChannelDistributionRequest,
    distribute_channel_config,
    get_channel,
    list_channels,
    list_channel_types,
    put_channel,
)


@asynccontextmanager
async def _agent_config_update_scope(agent, agent_config):
    yield agent, agent_config


async def test_list_channels_materializes_console_when_channels_missing():
    request = Mock()
    agent_config = SimpleNamespace(channels=None)

    with (
        patch(
            "swe.app.agent_context.get_agent_and_config_for_request",
            return_value=(SimpleNamespace(), agent_config),
        ),
        patch(
            "swe.app.routers.config.get_available_channels",
            return_value=["console"],
        ),
    ):
        result = await list_channels(request)

    assert result["console"]["enabled"] is True
    assert result["console"]["isBuiltin"] is True
    assert result["console"]["_constraints"]["enabled"]["readOnly"] is True


async def test_list_channels_keeps_console_visible_when_env_filter_excludes_it():
    request = Mock()
    agent_config = SimpleNamespace(channels=None)

    with (
        patch(
            "swe.app.agent_context.get_agent_and_config_for_request",
            return_value=(SimpleNamespace(), agent_config),
        ),
        patch(
            "swe.app.routers.config.get_available_channels",
            return_value=["zhaohu"],
        ),
    ):
        result = await list_channels(request)

    assert set(result) == {"console", "zhaohu"}
    assert result["console"]["enabled"] is True


async def test_list_channel_types_keeps_console_visible_when_env_filter_excludes_it():
    with patch(
        "swe.app.routers.config.get_available_channels",
        return_value=["zhaohu"],
    ):
        result = await list_channel_types()

    assert result == ["console", "zhaohu"]


async def test_get_channel_returns_synthesized_console_payload_when_missing():
    request = Mock()
    agent_config = SimpleNamespace(channels=None)

    with (
        patch(
            "swe.app.agent_context.get_agent_and_config_for_request",
            return_value=(SimpleNamespace(), agent_config),
        ),
        patch(
            "swe.app.routers.config.get_available_channels",
            return_value=["console"],
        ),
    ):
        result = await get_channel(request, "console")

    assert result["enabled"] is True
    assert result["_constraints"]["enabled"]["enforcedValue"] is True


async def test_get_channel_keeps_console_accessible_when_env_filter_excludes_it():
    request = Mock()
    agent_config = SimpleNamespace(channels=None)

    with (
        patch(
            "swe.app.agent_context.get_agent_and_config_for_request",
            return_value=(SimpleNamespace(), agent_config),
        ),
        patch(
            "swe.app.routers.config.get_available_channels",
            return_value=["zhaohu"],
        ),
    ):
        result = await get_channel(request, "console")

    assert result["enabled"] is True


async def test_put_channel_ignores_constraints_and_forces_console_enabled():
    request = Mock()
    agent = SimpleNamespace(agent_id="default", tenant_id="tenant-a")
    agent_config = SimpleNamespace(
        channels=ChannelConfig(console=ConsoleConfig(enabled=True)),
    )

    with (
        patch(
            "swe.app.agent_context.agent_config_update_scope",
            lambda _request: _agent_config_update_scope(agent, agent_config),
        ),
        patch(
            "swe.app.routers.config.get_available_channels",
            return_value=["console"],
        ),
        patch(
            "swe.app.routers.config.schedule_agent_reload",
        ),
        patch(
            "swe.config.config.save_agent_config",
        ),
    ):
        result = await put_channel(
            request,
            "console",
            {
                "enabled": False,
                "bot_prefix": "[BOT]",
                "_constraints": {
                    "enabled": {"readOnly": False, "enforcedValue": False},
                },
            },
        )

    assert agent_config.channels.console.enabled is True
    assert result["enabled"] is True
    assert result["bot_prefix"] == "[BOT]"
    assert result["_constraints"]["enabled"]["reasonKey"] == (
        "channels.constraints.console_enabled_mandatory"
    )


async def test_put_channel_keeps_console_accessible_when_env_filter_excludes_it():
    request = Mock()
    agent = SimpleNamespace(agent_id="default", tenant_id="tenant-a")
    agent_config = SimpleNamespace(channels=None)

    with (
        patch(
            "swe.app.agent_context.agent_config_update_scope",
            lambda _request: _agent_config_update_scope(agent, agent_config),
        ),
        patch(
            "swe.app.routers.config.get_available_channels",
            return_value=["zhaohu"],
        ),
        patch("swe.app.routers.config.schedule_agent_reload"),
        patch("swe.config.config.save_agent_config"),
    ):
        result = await put_channel(
            request,
            "console",
            {
                "enabled": False,
                "bot_prefix": "[CLI]",
            },
        )

    assert result["enabled"] is True
    assert result["bot_prefix"] == "[CLI]"
    assert agent_config.channels.zhaohu.enabled is False


async def test_distribute_channel_config_materializes_console_when_missing():
    request = Mock()
    source_agent = SimpleNamespace(
        agent_id="source-agent",
        tenant_id="tenant-source",
    )
    source_config = AgentProfileConfig(
        id="source-agent",
        name="Source Agent",
        channels=None,
    )
    target_config = AgentProfileConfig(
        id="default",
        name="Default Agent",
        channels=None,
    )

    with (
        patch(
            "swe.app.agent_context.get_agent_for_request",
            return_value=source_agent,
        ),
        patch(
            "swe.app.routers.config.get_available_channels",
            return_value=["console"],
        ),
        patch(
            "swe.config.config.load_agent_config",
            side_effect=[source_config, target_config],
        ),
        patch(
            "swe.app.routers.config._prepare_target_tenant",
            return_value=("tenant-target", "tenant-target", True),
        ),
        patch("swe.app.routers.config.schedule_agent_reload"),
        patch("swe.config.config.save_agent_config") as mock_save,
    ):
        result = await distribute_channel_config(
            request,
            "console",
            ChannelDistributionRequest(
                target_tenant_ids=["tenant-target"],
                overwrite=True,
            ),
        )

    assert [item.success for item in result.results] == [True]
    saved_config = mock_save.call_args.args[1]
    assert saved_config.channels.console.enabled is True


async def test_distribute_channel_config_keeps_console_accessible_when_env_filter_excludes_it():
    request = Mock()
    source_agent = SimpleNamespace(
        agent_id="source-agent",
        tenant_id="tenant-source",
    )
    source_config = AgentProfileConfig(
        id="source-agent",
        name="Source Agent",
        channels=None,
    )
    target_config = AgentProfileConfig(
        id="default",
        name="Default Agent",
        channels=None,
    )

    with (
        patch(
            "swe.app.agent_context.get_agent_for_request",
            return_value=source_agent,
        ),
        patch(
            "swe.app.routers.config.get_available_channels",
            return_value=["zhaohu"],
        ),
        patch(
            "swe.config.config.load_agent_config",
            side_effect=[source_config, target_config],
        ),
        patch(
            "swe.app.routers.config._prepare_target_tenant",
            return_value=("tenant-target", "tenant-target", True),
        ),
        patch("swe.app.routers.config.schedule_agent_reload"),
        patch("swe.config.config.save_agent_config") as mock_save,
    ):
        result = await distribute_channel_config(
            request,
            "console",
            ChannelDistributionRequest(
                target_tenant_ids=["tenant-target"],
                overwrite=True,
            ),
        )

    assert [item.success for item in result.results] == [True]
    saved_config = mock_save.call_args.args[1]
    assert saved_config.channels.console.enabled is True


async def test_distribute_channel_config_writes_default_target_to_source_template():
    request = Mock()
    source_agent = SimpleNamespace(
        agent_id="source-agent",
        tenant_id="default_ruice",
    )
    source_config = AgentProfileConfig(
        id="source-agent",
        name="Source Agent",
        channels=None,
    )
    target_config = AgentProfileConfig(
        id="default",
        name="Default Agent",
        channels=None,
    )

    with (
        patch(
            "swe.app.agent_context.get_agent_for_request",
            return_value=source_agent,
        ),
        patch(
            "swe.app.routers.config.get_available_channels",
            return_value=["console"],
        ),
        patch(
            "swe.config.config.load_agent_config",
            side_effect=[source_config, target_config],
        ),
        patch(
            "swe.app.routers.config._prepare_target_tenant",
            return_value=("default", "default_ruice", True),
        ),
        patch("swe.app.routers.config.schedule_agent_reload"),
        patch("swe.config.config.save_agent_config") as mock_save,
    ):
        result = await distribute_channel_config(
            request,
            "console",
            ChannelDistributionRequest(
                target_tenant_ids=["default"],
                overwrite=True,
            ),
        )

    assert [item.success for item in result.results] == [True]
    assert mock_save.call_args.kwargs["tenant_id"] == "default_ruice"
    saved_config = mock_save.call_args.args[1]
    assert saved_config.channels.zhaohu.enabled is False

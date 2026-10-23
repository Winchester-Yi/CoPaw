# -*- coding: utf-8 -*-
"""API routes for built-in tools management."""

# pylint: disable=no-name-in-module

from __future__ import annotations

from typing import List

from fastapi import (
    APIRouter,
    Body,
    HTTPException,
    Path,
    Request,
)
from pydantic import BaseModel, Field

from ..utils import schedule_agent_reload
from ...config import load_config

router = APIRouter(prefix="/tools", tags=["tools"])


class ToolInfo(BaseModel):
    """Tool information for API responses."""

    name: str = Field(..., description="Tool function name")
    enabled: bool = Field(..., description="Whether the tool is enabled")
    description: str = Field(default="", description="Tool description")
    async_execution: bool = Field(
        default=False,
        description="Whether to execute the tool asynchronously in background",
    )
    origin: str = Field(
        default="builtin",
        description="Whether this effective tool comes from code or source library",
    )
    source_version: int | None = Field(
        default=None,
        description="Active source-tool version when origin is source",
    )


@router.get("", response_model=List[ToolInfo])
async def list_tools(
    request: Request,
) -> List[ToolInfo]:
    """List all built-in tools and enabled status for active agent.

    Returns:
        List of tool information
    """
    from ..agent_context import get_agent_and_config_for_request

    _, agent_config = await get_agent_and_config_for_request(request)
    builtin_tools = _builtin_tools_for_agent(agent_config)
    if builtin_tools is None:
        return []

    source_tools = _active_source_tools(request)
    return list(_effective_tool_info(builtin_tools, source_tools).values())


def _builtin_tools_for_agent(agent_config):
    """Return agent tool settings, falling back to global defaults."""
    if agent_config.tools and agent_config.tools.builtin_tools:
        return agent_config.tools.builtin_tools

    config = load_config()
    tools_config = config.tools if hasattr(config, "tools") else None
    return tools_config.builtin_tools if tools_config else None


def _active_source_tools(request: Request):
    """Return active source-tool metadata for the request's source."""
    source_id = getattr(request.state, "source_id", None)
    source_tool_service = getattr(
        request.app.state,
        "source_tool_service",
        None,
    )
    if not source_id or source_tool_service is None:
        return ()
    return source_tool_service.list_metadata(source_id)


def _effective_tool_info(builtin_tools, source_tools) -> dict[str, ToolInfo]:
    """Merge configured builtins with active source-tool metadata."""
    active_source_names = {tool.name for tool in source_tools}
    tools_list: dict[str, ToolInfo] = {}
    for tool_config in builtin_tools.values():
        if (
            not _is_code_builtin(tool_config.name)
            and tool_config.name not in active_source_names
        ):
            continue
        tools_list[tool_config.name] = ToolInfo(
            name=tool_config.name,
            enabled=tool_config.enabled,
            description=tool_config.description,
            async_execution=tool_config.async_execution,
        )

    for source_tool in source_tools:
        configured = builtin_tools.get(source_tool.name)
        tools_list[source_tool.name] = ToolInfo(
            name=source_tool.name,
            enabled=configured.enabled if configured is not None else True,
            description=source_tool.description,
            async_execution=(
                configured.async_execution
                if configured is not None
                and source_tool.name == "execute_shell_command"
                else False
            ),
            origin="source",
            source_version=source_tool.version,
        )

    return tools_list


@router.patch("/{tool_name}/toggle", response_model=ToolInfo)
async def toggle_tool(
    tool_name: str = Path(...),
    request: Request = None,
) -> ToolInfo:
    """Toggle tool enabled status for active agent.

    Args:
        tool_name: Tool function name
        request: FastAPI request

    Returns:
        Updated tool information

    Raises:
        HTTPException: If tool not found
    """
    from ..agent_context import agent_config_update_scope
    from ...config.config import save_agent_config

    source_tool = _get_active_source_tool(request, tool_name)
    is_code_builtin = _is_code_builtin(tool_name)
    async with agent_config_update_scope(request) as (workspace, agent_config):
        if not agent_config.tools or (
            tool_name not in agent_config.tools.builtin_tools
            and source_tool is None
            and not is_code_builtin
        ):
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_name}' not found",
            )

        # New source tools are default-enabled and only acquire an Agent record
        # when the user explicitly changes that default.
        if tool_name not in agent_config.tools.builtin_tools:
            from ...config.config import BuiltinToolConfig

            agent_config.tools.builtin_tools[tool_name] = BuiltinToolConfig(
                name=tool_name,
                enabled=True,
                description=(
                    source_tool.description if source_tool is not None else ""
                ),
            )
        tool_config = agent_config.tools.builtin_tools[tool_name]
        tool_config.enabled = not tool_config.enabled
        save_agent_config(
            workspace.agent_id,
            agent_config,
            tenant_id=workspace.tenant_id,
        )

    # Hot reload config (async, non-blocking)
    schedule_agent_reload(
        request,
        workspace.agent_id,
        tenant_id=workspace.tenant_id,
    )

    # Return immediately (optimistic update)
    return ToolInfo(
        name=tool_config.name,
        enabled=tool_config.enabled,
        description=(
            source_tool.description
            if source_tool is not None
            else tool_config.description
        ),
        async_execution=tool_config.async_execution,
        origin="source" if source_tool is not None else "builtin",
        source_version=(
            source_tool.version if source_tool is not None else None
        ),
    )


@router.patch("/{tool_name}/async-execution", response_model=ToolInfo)
async def update_tool_async_execution(
    tool_name: str = Path(...),
    async_execution: bool = Body(..., embed=True),
    request: Request = None,
) -> ToolInfo:
    """Update tool async_execution setting for active agent.

    Args:
        tool_name: Tool function name
        async_execution: Whether to execute asynchronously
        request: FastAPI request

    Returns:
        Updated tool information

    Raises:
        HTTPException: If tool not found
    """
    from ..agent_context import agent_config_update_scope
    from ...config.config import save_agent_config

    source_tool = _get_active_source_tool(request, tool_name)
    if source_tool is not None and tool_name != "execute_shell_command":
        raise HTTPException(
            status_code=400,
            detail="Only execute_shell_command supports async execution",
        )
    async with agent_config_update_scope(request) as (workspace, agent_config):
        if (
            not agent_config.tools
            or tool_name not in agent_config.tools.builtin_tools
        ):
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_name}' not found",
            )

        tool_config = agent_config.tools.builtin_tools[tool_name]
        tool_config.async_execution = async_execution
        save_agent_config(
            workspace.agent_id,
            agent_config,
            tenant_id=workspace.tenant_id,
        )

    # Hot reload config (async, non-blocking)
    schedule_agent_reload(
        request,
        workspace.agent_id,
        tenant_id=workspace.tenant_id,
    )

    # Return immediately (optimistic update)
    return ToolInfo(
        name=tool_config.name,
        enabled=tool_config.enabled,
        description=tool_config.description,
        async_execution=tool_config.async_execution,
        origin="source" if source_tool is not None else "builtin",
        source_version=(
            source_tool.version if source_tool is not None else None
        ),
    )


def _get_active_source_tool(
    request: Request,
    tool_name: str,
):
    """Resolve current-source metadata without exposing script content."""
    source_id = getattr(request.state, "source_id", None)
    service = getattr(request.app.state, "source_tool_service", None)
    if not source_id or service is None:
        return None
    return next(
        (
            tool
            for tool in service.list_metadata(source_id)
            if tool.name == tool_name
        ),
        None,
    )


def _is_code_builtin(tool_name: str) -> bool:
    """Return whether a name remains available without an active source tool."""
    from ...config.config import _default_builtin_tools

    return tool_name in _default_builtin_tools()

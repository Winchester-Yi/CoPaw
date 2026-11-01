# -*- coding: utf-8 -*-
"""Command dispatch: run command path without creating SWEAgent.

Yields (Msg, last) compatible with query_handler stream.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import logging
from dataclasses import dataclass
from typing import AsyncIterator
from typing import TYPE_CHECKING

from agentscope.message import Msg, TextBlock

from . import control_commands
from ..conversation_storage.errors import StorageError
from .daemon_commands import (
    DaemonContext,
    DaemonCommandHandlerMixin,
    parse_daemon_query,
)
from ...agents.command_handler import CommandHandler
from ...agents.memory.expert_main_memory import ExpertMainMemoryManager
from ...config.config import load_agent_config

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .runner import AgentRunner


def _get_last_user_text(msgs) -> str | None:
    """Extract last user message text from msgs (runtime message list)."""
    if not msgs or len(msgs) == 0:
        return None
    last = msgs[-1]
    if hasattr(last, "get_text_content"):
        return last.get_text_content()
    if isinstance(last, dict):
        content = last.get("content") or last.get("text")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return block.get("text")
    return None


def _is_conversation_command(query: str | None) -> bool:
    """True if query is a conversation command (/compact, /new, etc.)."""
    if not query or not query.startswith("/"):
        return False
    cmd = query.strip().lstrip("/").split()[0] if query.strip() else ""
    return cmd in CommandHandler.SYSTEM_COMMANDS


def _is_control_command(query: str | None) -> bool:
    """True if query is a control command (/stop, etc.)."""
    return control_commands.is_control_command(query)


def _is_command(query: str | None) -> bool:
    """True if query is any known command.

    Priority order: daemon > control > conversation
    """
    if not query or not query.startswith("/"):
        return False
    if parse_daemon_query(query) is not None:
        return True
    if _is_control_command(query):
        return True
    return _is_conversation_command(query)


@dataclass(frozen=True)
class _CommandRequestContext:
    """Normalized request fields shared by command handlers."""

    session_id: str
    user_id: str
    chat_id: str
    channel: str


async def _resolve_command_context(
    request,
    runner: AgentRunner,
) -> _CommandRequestContext:
    """Resolve command identifiers, including the legacy chat lookup."""
    session_id = getattr(request, "session_id", "") or ""
    user_id = getattr(request, "user_id", "") or ""
    channel = getattr(request, "channel", "") or ""
    request_meta = getattr(request, "channel_meta", None) or {}
    chat_id = (
        getattr(request, "chat_id", "") or request_meta.get("chat_id") or ""
    )
    if not chat_id:
        chat_manager = getattr(runner, "_chat_manager", None)
        if chat_manager is not None and session_id:
            chat_id = (
                await chat_manager.get_chat_id_by_session(
                    session_id,
                    channel or "console",
                )
                or ""
            )
    return _CommandRequestContext(session_id, user_id, chat_id, channel)


def _restart_hint() -> Msg:
    """Build the status message yielded before an async daemon restart."""
    return Msg(
        name="Friday",
        role="assistant",
        content=[
            TextBlock(
                type="text",
                text=(
                    "**Restart in progress**\n\n"
                    "- Reloading agent with zero-downtime. Please wait."
                ),
            ),
        ],
    )


async def _run_daemon_command(
    query: str,
    parsed: tuple,
    context: _CommandRequestContext,
    runner: AgentRunner,
) -> AsyncIterator[tuple]:
    """Run a daemon command, yielding its optional restart hint first."""
    handler = DaemonCommandHandlerMixin()
    manager = getattr(runner, "_manager", None)
    if parsed[0] == "restart":
        logger.info(
            "run_command_path: daemon restart, manager=%s",
            "set" if manager is not None else "None",
        )
        yield _restart_hint(), True

    workspace = getattr(runner, "_workspace", None)
    tenant_id = getattr(workspace, "tenant_id", None)
    agent_id = runner.agent_id
    daemon_ctx = DaemonContext(
        load_config_fn=lambda: load_agent_config(
            agent_id,
            tenant_id=tenant_id,
        ),
        memory_manager=runner.memory_manager,
        manager=manager,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=context.session_id,
    )
    msg = await handler.handle_daemon_command(query, daemon_ctx)
    yield msg, True
    logger.info("handle_daemon_command %s completed", query)


async def _run_control_command(
    query: str,
    request,
    context: _CommandRequestContext,
    runner: AgentRunner,
) -> AsyncIterator[tuple]:
    """Run a control command with its resolved channel and workspace."""
    workspace = runner._workspace  # pylint: disable=protected-access
    if workspace is None:
        logger.error("run_command_path: control command but workspace not set")
        yield _command_error(
            "Control command unavailable (workspace not initialized)",
        ), True
        return

    channel = None
    channel_manager = workspace.channel_manager
    if channel_manager is not None:
        channel = await channel_manager.get_channel(context.channel)
    if channel is None:
        logger.error(
            "run_command_path: channel not found: %s",
            context.channel,
        )
        yield _command_error(f"Channel not found: {context.channel}"), True
        return

    control_ctx = control_commands.ControlContext(
        workspace=workspace,
        payload=request,
        channel=channel,
        session_id=context.session_id,
        user_id=context.user_id,
        args={},
    )
    try:
        response_text = await control_commands.handle_control_command(
            query,
            control_ctx,
        )
    except Exception as exc:
        logger.exception("Control command failed: %s", query)
        yield _command_error(
            str(exc),
            prefix="**Command Failed**\n\n",
        ), True
        return
    yield Msg(
        name="Friday",
        role="assistant",
        content=[TextBlock(type="text", text=response_text)],
    ), True
    logger.info("handle_control_command %s completed", query)


def _command_error(text: str, *, prefix: str = "**Error**\n\n") -> Msg:
    """Build the unchanged command-path error response shape."""
    return Msg(
        name="Friday",
        role="assistant",
        content=[TextBlock(type="text", text=prefix + text)],
    )


def _bind_command_memory(runner, memory, context, session_execution):
    workspace = getattr(runner, "_workspace", None)
    storage = getattr(workspace, "conversation_storage", None)
    if storage is None:
        return
    if session_execution is None or not context.chat_id:
        raise RuntimeError("Indexed command requires its Session and Chat")

    def root_provider():
        state = deepcopy(session_execution.state)
        state.setdefault("agent", {})["memory"] = memory.state_dict()
        return state

    storage.archive.bind_memory(
        memory,
        context.chat_id,
        session_execution,
        root_provider,
        None,
    )


async def _run_conversation_command(
    query: str,
    request,
    context: _CommandRequestContext,
    runner: AgentRunner,
    session_execution=None,
) -> AsyncIterator[tuple]:
    """Run a conversation command and persist its in-memory state."""
    memory = runner.memory_manager.get_in_memory_memory(
        chat_id=context.chat_id or None,
    )
    if session_execution is None:
        session_state = await runner.session.get_session_state_dict(
            session_id=context.session_id,
            user_id=context.user_id,
        )
    else:
        session_state = await session_execution.read_state()
    memory_state = session_state.get("agent", {}).get("memory", {})
    memory.load_state_dict(memory_state, strict=False)
    _bind_command_memory(runner, memory, context, session_execution)

    expert = bool(
        getattr(runner, "_get_expert_main_execution", lambda _: None)(request)
        or (getattr(request, "channel_meta", None) or {}).get(
            "_expert_main_reset",
        ),
    )
    command = query.strip().split(maxsplit=1)[0]
    if expert and command in {
        "/long_term_memory",
        "/await_summary",
    }:
        yield _command_error("专家主执行不使用 Profile 长期记忆。"), True
        return
    manager = runner.memory_manager
    if expert:
        if hasattr(memory, "_long_term_memory"):
            memory._long_term_memory = ""
        manager = ExpertMainMemoryManager(manager)
    conv_handler = CommandHandler(
        agent_name="Friday",
        memory=memory,
        memory_manager=manager,
        enable_memory_manager=manager is not None,
        request_context={
            "session_id": context.session_id,
            "user_id": context.user_id,
            "channel": context.channel,
            "chat_id": context.chat_id or None,
            "trace_id": getattr(request, "trace_id", None),
        },
    )
    try:
        response_msg = await conv_handler.handle_conversation_command(query)
    except asyncio.CancelledError:
        raise
    except StorageError:
        raise
    except Exception:
        logger.exception("Conversation command failed: %s", query)
        command = query.strip().split(maxsplit=1)[0]
        response_msg = _command_error(
            f"- `{command}` could not be completed. "
            "Please retry or check server logs.",
            prefix="**Command Failed**\n\n",
        )
    if context.session_id:
        if session_execution is not None:
            state = session_execution.state
            agent_state = state.setdefault("agent", {})
            agent_state["memory"] = memory.state_dict()
            await session_execution.commit_state(state)
            yield response_msg, True
            return
        await runner.session.update_session_state(
            session_id=context.session_id,
            key="agent.memory",
            value=memory.state_dict(),
            user_id=context.user_id,
        )
        yield response_msg, True
        return
    yield response_msg, True
    logger.warning(
        "Skipping session_state update for conversation memory due to missing "
        "session_id or user_id (session_id=%r, user_id=%r)",
        context.session_id,
        context.user_id,
    )


async def run_command_path(
    request,
    msgs,
    runner: AgentRunner,
    *,
    session_execution=None,
) -> AsyncIterator[tuple]:
    """Run command path and yield (msg, last) for each response.

    Args:
        request: AgentRequest (session_id, user_id, etc.)
        msgs: List of messages from runtime (last is user input)
        runner: AgentRunner (session, memory_manager, etc.)

    Yields:
        (Msg, bool) compatible with query_handler stream
    """
    query = _get_last_user_text(msgs)
    if not query:
        return
    context = await _resolve_command_context(request, runner)
    parsed = parse_daemon_query(query)
    if parsed is not None:
        async for item in _run_daemon_command(query, parsed, context, runner):
            yield item
        return
    if _is_control_command(query):
        async for item in _run_control_command(
            query,
            request,
            context,
            runner,
        ):
            yield item
        return
    async for item in _run_conversation_command(
        query,
        request,
        context,
        runner,
        session_execution=session_execution,
    ):
        yield item

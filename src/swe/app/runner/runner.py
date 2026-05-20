# -*- coding: utf-8 -*-
# pylint: disable=unused-argument too-many-branches too-many-statements
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable, Collection
from uuid import uuid4

import httpx
from agentscope.mcp import HttpStatefulClient, StdIOStatefulClient
from agentscope.message import Msg, TextBlock
from agentscope.pipeline import stream_printing_messages
from agentscope_runtime.engine.runner import Runner
from agentscope_runtime.engine.schemas.agent_schemas import (
    AgentRequest,
    Event,
)
from agentscope_runtime.engine.schemas.exception import AgentException
from dotenv import load_dotenv
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from ..mcp.stdio_launcher import build_tenant_aware_stdio_launch_config
from .command_dispatch import (
    _get_last_user_text,
    _is_command,
    run_command_path,
)
from .query_error_dump import write_query_error_dump
from .session import SafeJSONSession
from .stream_boundary import normalize_reasoning_boundary_stream
from .task_progress import attach_task_progress
from .utils import build_env_context
from ..channels.schema import DEFAULT_CHANNEL
from ...agents.react_agent import SWEAgent
from ...agents.skill_invocation_detector import SkillInvocationDetector
from ...agents.skills_manager import get_workspace_skills_dir
from ...agents.hook_runtime import HookRuntime
from ...agents.hook_runtime.models import (
    HookConfig,
    HookContext,
    HookDecision,
    HookEventName,
    HookSessionOverlay,
    HookSessionState,
    MergedHookResult,
)
from ...agents.hook_runtime.skill_loader import (
    SkillHookLoadError,
    load_skill_hooks_for_session,
)
from ...security.tool_guard.models import TOOL_GUARD_DENIED_MARK
from ...config.config import (
    MCPClientConfig,
    MCPConfig,
    load_agent_config,
    SuggestionMode,
)
from ...constant import (
    QUERY_CLEANUP_TIMEOUT,
    QUERY_TIMEOUT_SECONDS,
    TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS,
    WORKING_DIR,
)
from ...security.tool_guard.approval import ApprovalDecision
from ...tracing import (
    has_trace_manager,
    get_trace_manager,
)
from ...tracing.models import TraceStatus
from ...config.context import (
    get_current_passthrough_headers,
)
from ..post_turn_continuation_store import (
    consume_pending_continuation,
    store_pending_continuation,
)
from ..post_turn_validation import validate_task_completion
from ..suggestions.source_config import get_follow_up_suggestions_config
from ..suggestions import generate_suggestions, store_suggestions

if TYPE_CHECKING:
    from ...agents.memory import BaseMemoryManager

logger = logging.getLogger(__name__)
TASK_RUNS_STATE_KEY = "task_runs"
_INTERNAL_FOLLOW_UP_METADATA_KEY = "swe_internal_follow_up"
_BEFORE_STOP_FOLLOW_UP_REASON_TEMPLATE = (
    "BeforeStop completion gate blocked stopping: {reason}\n"
    "Continue working until the gate can allow completion."
)
_BEFORE_STOP_INCOMPLETE_MESSAGE_TEMPLATE = (
    "任务未完成：BeforeStop 完成门禁已达到自动续跑上限。最新阻断原因：{reason}"
)

_APPROVE_EXACT = frozenset(
    {
        "approve",
        "/approve",
        "/daemon approve",
    },
)
_MCP_HTTP_TIMEOUT_SECONDS = 240.0
_MCP_HTTP_SSE_READ_TIMEOUT_SECONDS = 60.0 * 5

_DENY_EXACT = frozenset(
    {
        "deny",
        "/deny",
        "/daemon deny",
    },
)


@dataclass
class _QueryPreflight:
    """保存进入 Agent 主流程前已经解析出的请求状态。"""

    response: Msg | None = None
    cleanup_denied_memory: bool = False
    approval_consumed: bool = False
    approved_tool_call: dict[str, Any] | None = None
    agent_config: Any | None = None
    tenant_hooks: HookConfig | None = None
    hook_overlay: HookSessionOverlay | None = None
    hook_additional_context: str = ""


@dataclass
class _QueryRuntime:
    """保存单次 query 执行过程中需要在清理阶段复用的对象。"""

    agent: SWEAgent
    agent_config: Any
    tenant_hooks: HookConfig
    hook_overlay: HookSessionOverlay
    chat: Any
    session_skill_detector: Any
    mcp_clients: list[Any]
    session_id: str
    user_id: str
    channel: str
    turn_id: str
    skip_history: bool


@dataclass
class _RuntimeStartResult:
    """描述运行时初始化是否被 hook 中断。"""

    runtime: _QueryRuntime | None = None
    block_response: Msg | None = None
    blocked_chat: Any = None
    blocked_mcp_clients: list[Any] | None = None
    blocked_session_id: str = ""


@dataclass
class _TurnPlan:
    """保存本轮 agent 调用及后置校验需要的输入。"""

    original_user_message: str
    confirmed_turn_index: int
    turn_msgs: list[Any]
    validation_config: Any | None


@dataclass
class _TurnPlanResult:
    """描述续跑状态是否可用，以及可执行的 turn 计划。"""

    plan: _TurnPlan | None = None
    response: Msg | None = None


@dataclass
class _QueryTurnOutcome:
    """记录 agent 输出和 post-turn validation 的最终状态。"""

    task_completed: bool = True
    assistant_response: str = ""
    last_validation_result: Any | None = None
    auto_follow_up_turns: int = 0
    max_auto_turns: int = 0
    before_stop_follow_up_turns: int = 0
    max_before_stop_turns: int = 0
    automatic_follow_up_turns: int = 0
    max_automatic_follow_up_turns: int = 0
    stop_hook_active: bool = False
    completion_blocked: bool = False
    completion_block_reason: str = ""
    completion_marked_incomplete: bool = False


def _match_command_with_optional_id(
    text: str,
    commands: frozenset[str],
) -> tuple[bool, str | None]:
    normalized = " ".join(text.split()).lower()
    for command in sorted(commands, key=len, reverse=True):
        if normalized == command:
            return True, None
        prefix = f"{command} "
        if normalized.startswith(prefix):
            request_id = normalized[len(prefix) :].strip()
            if request_id:
                return True, request_id
    return False, None


def _extract_memory_entry_payload(entry: Any) -> dict[str, Any] | None:
    """提取内存条目里的消息载荷。"""
    if isinstance(entry, list) and entry and isinstance(entry[0], dict):
        return entry[0]
    if isinstance(entry, dict):
        return entry
    return None


def _extract_text_from_message_content(content: Any) -> str:
    """从消息内容中提取可展示文本。"""
    if isinstance(content, str):
        return content.strip()

    if not isinstance(content, list):
        return ""

    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = ""
        if block.get("type") == "text":
            text = str(block.get("text", "") or "")
        elif block.get("type") == "thinking":
            text = str(block.get("thinking", "") or "")
        if text.strip():
            texts.append(text.strip())
    return "\n".join(texts).strip()


def _build_task_run_record(
    memory_entries: list[Any],
    *,
    memory_start: int,
) -> dict[str, Any] | None:
    """根据本次新增消息构建任务运行元数据。"""
    if not memory_entries:
        return None

    started_at: str | None = None
    ended_at: str | None = None
    preview_text = ""

    for entry in memory_entries:
        payload = _extract_memory_entry_payload(entry)
        if not payload:
            continue
        timestamp = payload.get("timestamp")
        if isinstance(timestamp, str) and timestamp and not started_at:
            started_at = timestamp
        if isinstance(timestamp, str) and timestamp:
            ended_at = timestamp

    for entry in reversed(memory_entries):
        payload = _extract_memory_entry_payload(entry)
        if not payload or payload.get("role") != "assistant":
            continue
        preview_text = _extract_text_from_message_content(
            payload.get("content"),
        )
        if preview_text:
            break

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    started_at = started_at or now
    ended_at = ended_at or started_at

    return {
        "run_id": f"task-run-{uuid4()}",
        "started_at": started_at,
        "ended_at": ended_at,
        "memory_start": memory_start,
        "memory_end": memory_start + len(memory_entries),
        "preview_text": preview_text,
    }


def _is_approval(text: str) -> bool:
    """Return True when *text* is an approve command.

    The command may optionally include an approval request id.
    """
    matched, _request_id = _match_command_with_optional_id(
        text,
        _APPROVE_EXACT,
    )
    return matched


def _is_denial(text: str) -> bool:
    """Return True only when *text* is an explicit deny command."""
    matched, _request_id = _match_command_with_optional_id(text, _DENY_EXACT)
    return matched


def _approval_request_id(text: str) -> str | None:
    matched, request_id = _match_command_with_optional_id(
        text,
        _APPROVE_EXACT,
    )
    return request_id if matched else None


def _denial_request_id(text: str) -> str | None:
    matched, request_id = _match_command_with_optional_id(text, _DENY_EXACT)
    return request_id if matched else None


def _approval_replay_metadata(record) -> dict[str, Any] | None:
    if not isinstance(record.extra, dict):
        return None
    approval_kind = record.extra.get("approval_kind", "tool_guard")
    if approval_kind != "hook_pre_tool_use":
        return None
    tool_call = record.extra.get("tool_call")
    if not isinstance(tool_call, dict):
        return None
    return {
        "request_id": record.request_id,
        "approval_kind": approval_kind,
        "tool_call_id": tool_call.get("id", ""),
        "tool_name": tool_call.get("name") or record.tool_name,
        "tool_input": tool_call.get("input", {}),
        "hook_ask_handler_ids": list(
            record.extra.get("hook_ask_handler_ids") or [],
        ),
    }


async def _select_pending_approval(
    svc,
    *,
    session_id: str,
    request_id: str | None,
):
    if request_id:
        pending = await svc.get_request(request_id)
        if (
            pending is not None
            and pending.session_id == session_id
            and pending.status == "pending"
        ):
            return pending
        return None
    return await svc.get_pending_by_session(session_id)


def _load_tenant_hook_config(tenant_id: str | None) -> HookConfig:
    try:
        from ...config.utils import get_tenant_config_path, load_config

        config_path = get_tenant_config_path(tenant_id) if tenant_id else None
        return load_config(config_path).hooks
    except Exception:
        logger.debug("Failed to load tenant hook config", exc_info=True)
        return HookConfig()


def _hook_config_enabled(
    tenant_hooks: HookConfig | None,
    agent_config: Any,
    session_state: HookSessionState | None = None,
) -> bool:
    agent_hooks = getattr(agent_config, "hooks", None)
    return bool(
        (tenant_hooks is not None and tenant_hooks.enabled)
        or (agent_hooks is not None and agent_hooks.enabled)
        or (
            session_state is not None
            and session_state.has_loaded_skill_sources()
        ),
    )


async def _load_session_hook_overlay(
    session: Any,
    *,
    session_id: str,
    user_id: str,
) -> HookSessionOverlay:
    if session is None or not session_id:
        return HookSessionOverlay()
    try:
        state = await session.get_session_state_dict(
            session_id=session_id,
            user_id=user_id,
            allow_not_exist=True,
        )
    except Exception:
        logger.debug("Failed to load hook overlay from session", exc_info=True)
        return HookSessionOverlay()
    raw_overlay = (
        state.get("hook_overlay") if isinstance(state, dict) else None
    )
    if not isinstance(raw_overlay, dict):
        return HookSessionOverlay()
    try:
        return HookSessionOverlay.model_validate(raw_overlay)
    except Exception:
        logger.warning("Invalid hook_overlay session state", exc_info=True)
        return HookSessionOverlay()


def _load_tenant_approved_skill_hook_http_urls(
    tenant_id: str | None,
) -> set[str]:
    try:
        from ...config.utils import get_tenant_config_path, load_config

        config_path = get_tenant_config_path(tenant_id) if tenant_id else None
        security = load_config(config_path).security
        skill_hook_http = getattr(security, "skill_hook_http", None)
        urls = getattr(skill_hook_http, "approved_urls", None) or []
        return {str(url) for url in urls if str(url).strip()}
    except Exception:
        logger.debug(
            "Failed to load tenant skill hook HTTP approvals",
            exc_info=True,
        )
        return set()


def _create_session_skill_detector(
    *,
    workspace_dir: Path,
    tenant_id: str | None,
    user_id: str,
    session_id: str,
    channel: str,
    source_id: str,
    enabled_skills: list[str],
    get_hook_state: Callable[[], HookSessionState],
    set_hook_state: Callable[[HookSessionState], None],
    approved_http_urls: Collection[str] | None = None,
) -> SkillInvocationDetector:
    workspace = Path(workspace_dir)
    approvals = (
        set(approved_http_urls)
        if approved_http_urls is not None
        else _load_tenant_approved_skill_hook_http_urls(tenant_id)
    )

    async def _load_skill_hooks(skill_name: str) -> None:
        skill_root = get_workspace_skills_dir(workspace) / skill_name
        try:
            next_state = load_skill_hooks_for_session(
                skill_name=skill_name,
                skill_root=skill_root,
                workspace_dir=workspace,
                session_state=get_hook_state(),
                approved_http_urls=approvals,
            )
        except SkillHookLoadError as exc:
            logger.warning(
                "Rejected hooks for skill '%s': %s",
                skill_name,
                exc,
            )
            return
        set_hook_state(next_state)

    detector = SkillInvocationDetector(
        user_id=user_id,
        session_id=session_id,
        channel=channel,
        source_id=source_id,
        skill_hook_loader=_load_skill_hooks,
    )
    detector.set_enabled_skills(enabled_skills)
    return detector


def _build_runner_hook_context(
    event_name: HookEventName,
    *,
    request: Any,
    runner: "AgentRunner",
    prompt: str | None = None,
    assistant_response: str | None = None,
    source: str | None = None,
    model: str | None = None,
) -> HookContext:
    session_id = str(getattr(request, "session_id", "") or "")
    user_id = str(getattr(request, "user_id", "") or "")
    channel = str(
        getattr(request, "channel", DEFAULT_CHANNEL) or DEFAULT_CHANNEL,
    )
    channel_meta = getattr(request, "channel_meta", {}) or {}
    workspace_dir = Path(runner.workspace_dir or WORKING_DIR)
    transcript_path = ""
    session_obj = getattr(runner, "session", None)
    if session_obj is not None and hasattr(session_obj, "_get_save_path"):
        try:
            transcript_path = str(
                session_obj._get_save_path(session_id, user_id),
            )
        except Exception:
            transcript_path = ""

    effective_tenant_id = runner.tenant_id or "default"
    try:
        from ...config.context import get_current_effective_tenant_id

        effective_tenant_id = (
            get_current_effective_tenant_id() or effective_tenant_id
        )
    except Exception:
        pass

    return HookContext(
        session_id=session_id,
        transcript_path=transcript_path,
        cwd=str(workspace_dir),
        hook_event_name=event_name,
        tenant_id=runner.tenant_id or effective_tenant_id,
        effective_tenant_id=effective_tenant_id,
        user_id=user_id,
        agent_id=runner.agent_id,
        channel=channel,
        source_id=getattr(request, "source_id", None)
        or channel_meta.get("source_id"),
        workspace_dir=str(workspace_dir),
        chat_id=channel_meta.get("chat_id"),
        turn_id=channel_meta.get("turn_id"),
        prompt=prompt,
        assistant_response=assistant_response,
        source=source,
        model=model,
    )


async def _emit_runner_hook(
    event_name: HookEventName,
    *,
    request: Any,
    runner: "AgentRunner",
    tenant_hooks: HookConfig,
    agent_config: Any,
    overlay: HookSessionOverlay,
    prompt: str | None = None,
    assistant_response: str | None = None,
    source: str | None = None,
    model: str | None = None,
) -> MergedHookResult:
    agent_hooks = getattr(agent_config, "hooks", None)
    if not isinstance(agent_hooks, HookConfig):
        agent_hooks = HookConfig()
    runtime = HookRuntime(
        tenant_config=tenant_hooks,
        agent_config=agent_hooks,
        session_overlay=overlay,
    )
    context = _build_runner_hook_context(
        event_name,
        request=request,
        runner=runner,
        prompt=prompt,
        assistant_response=assistant_response,
        source=source,
        model=model,
    )
    return await runtime.emit(
        context,
        workspace_dir=Path(runner.workspace_dir or WORKING_DIR),
    )


def _format_hook_additional_context(result: MergedHookResult) -> str:
    if not result.additional_context:
        return ""
    lines = []
    for item in result.additional_context:
        lines.append(f"[{item.handler_id}] {item.context}")
    return "\n".join(lines)


def _hook_block_message(result: MergedHookResult) -> Msg:
    reason = result.reason or "Hook blocked this request."
    return Msg(name="Friday", role="assistant", content=reason)


def _resolve_active_model_label(tenant_id: str | None) -> str | None:
    try:
        from ...providers.provider_manager import ProviderManager

        manager = ProviderManager.get_instance(tenant_id)
        active = manager.get_active_model()
        if active and active.provider_id and active.model:
            return f"{active.provider_id}/{active.model}"
    except Exception:
        logger.debug(
            "Failed to resolve active model for hook context",
            exc_info=True,
        )
    return None


async def _build_and_connect_mcp_clients(
    mcp_config: MCPConfig | None,
    passthrough_headers: dict[str, str] | None = None,
) -> list[Any]:
    """Build and connect MCP clients from config for single request use.

    Args:
        mcp_config: MCP configuration from agent_config.mcp
        passthrough_headers: Headers to merge for HTTP transport clients

    Returns:
        List of connected MCP client instances (all created for this request)
    """
    if mcp_config is None or not mcp_config.clients:
        return []

    clients = []
    for key, client_config in mcp_config.clients.items():
        if not client_config.enabled:
            continue

        try:
            client = await _create_mcp_client_with_headers(
                client_config,
                passthrough_headers,
            )
            if client is not None:
                await client.connect()
                clients.append(client)
                logger.info(f"MCP client '{key}' created and connected")
        except Exception as e:
            logger.warning(
                f"Failed to create MCP client '{key}': {e}",
                exc_info=True,
            )

    return clients


async def _create_mcp_client_with_headers(
    client_config: MCPClientConfig,
    passthrough_headers: dict[str, str] | None = None,
) -> Any:
    """Create a single MCP client with optional header passthrough.

    For HTTP transport, merges static config headers with passthrough headers.
    For StdIO transport, uses static config directly.

    Args:
        client_config: Single MCP client configuration
        passthrough_headers: Headers to merge for HTTP transport

    Returns:
        MCP client instance (not yet connected)
    """
    rebuild_info = {
        "name": client_config.name,
        "transport": client_config.transport,
        "url": client_config.url,
        "headers": client_config.headers or None,
        "command": client_config.command,
        "args": list(client_config.args),
        "env": dict(client_config.env),
        "cwd": client_config.cwd or None,
    }

    if client_config.transport == "stdio":
        launch_config = build_tenant_aware_stdio_launch_config(
            client_config.command,
            client_config.args,
            client_config.env,
            client_config.cwd or None,
        )
        client = StdIOStatefulClient(
            name=client_config.name,
            command=launch_config.launch_command,
            args=launch_config.launch_args,
            env=launch_config.env,
            cwd=launch_config.cwd,
        )
        setattr(
            client,
            "_swe_rebuild_info",
            {
                **rebuild_info,
                "launch_command": launch_config.launch_command,
                "launch_args": launch_config.launch_args,
                "launch_diagnostic": launch_config.diagnostic,
            },
        )
        setattr(client, "_swe_temp_client", True)
        return client

    # HTTP transport (streamable_http or sse)
    headers = client_config.headers
    if headers:
        headers = {k: os.path.expandvars(v) for k, v in headers.items()}

    # Merge passthrough headers for HTTP transport
    merged_headers = dict(headers or {})
    if passthrough_headers:
        merged_headers.update(passthrough_headers)

    client = HttpStatefulClient(
        name=client_config.name,
        transport=client_config.transport,
        url=client_config.url,
        headers=None,  # Headers are in http_client
    )

    # Create appropriate transport context
    if client_config.transport == "sse":
        client_context = sse_client(
            url=client_config.url,
            headers=merged_headers,
            timeout=_MCP_HTTP_TIMEOUT_SECONDS,
            sse_read_timeout=_MCP_HTTP_SSE_READ_TIMEOUT_SECONDS,
        )
        http_client = None
    else:  # streamable_http
        http_client = httpx.AsyncClient(
            headers=merged_headers,
            timeout=httpx.Timeout(
                connect=_MCP_HTTP_TIMEOUT_SECONDS,
                read=_MCP_HTTP_SSE_READ_TIMEOUT_SECONDS,
                write=_MCP_HTTP_TIMEOUT_SECONDS,
                pool=_MCP_HTTP_TIMEOUT_SECONDS,
            ),
        )
        client_context = streamable_http_client(
            url=client_config.url,
            http_client=http_client,
        )

    client.client = client_context

    setattr(
        client,
        "_swe_rebuild_info",
        {
            **rebuild_info,
            "headers": merged_headers,
            "_temp_client": True,
            "_http_client": http_client,
        },
    )
    setattr(client, "_swe_temp_client", True)

    return client


async def _cleanup_mcp_clients(clients: list[Any]) -> None:
    """Clean up all MCP clients created for a request.

    Args:
        clients: List of MCP client instances to close
    """
    for client in clients:
        try:
            await client.close()
            # For HTTP clients, also close the httpx client
            rebuild_info = getattr(client, "_swe_rebuild_info", {})
            http_client = rebuild_info.get("_http_client")
            if http_client is not None:
                await http_client.aclose()
        except Exception as e:
            logger.warning(f"Error closing MCP client: {e}")


def _extract_text_from_blocks(blocks: list) -> str:
    """从 content blocks 中提取文本."""
    texts = []
    for block in blocks:
        if hasattr(block, "text"):
            texts.append(block.text)
        elif isinstance(block, dict) and "text" in block:
            texts.append(block["text"])
    return "\n".join(texts) if texts else ""


def _extract_assistant_response(agent: SWEAgent) -> str:
    """从 agent memory 中提取最后的助手响应文本."""
    if not agent or not hasattr(agent, "memory"):
        return ""

    try:
        # memory.content 是 list of (Msg, marks) tuples
        for msg, _marks in reversed(agent.memory.content):
            if msg.role != "assistant" or not hasattr(msg, "content"):
                continue
            # content 可能是 list of blocks 或 string
            if isinstance(msg.content, str):
                return msg.content
            if isinstance(msg.content, list):
                return _extract_text_from_blocks(msg.content)
    except Exception as e:
        logger.debug("Failed to extract assistant response: %s", e)

    return ""


def _build_internal_follow_up_msg(follow_up_prompt: str) -> Msg:
    """Build a hidden continuation turn for the same agent."""
    return Msg(
        name="system-follow-up",
        role="user",
        content=(
            "[内部续跑指令]\n"
            "继续当前用户任务。不要把本段当作用户的新需求，"
            "不要向用户复述本指令。\n"
            f"{follow_up_prompt.strip()}"
        ),
        metadata={
            _INTERNAL_FOLLOW_UP_METADATA_KEY: True,
        },
    )


def _build_before_stop_follow_up_msg(reason: str) -> Msg:
    """构造 BeforeStop 阻断后的内部续跑指令。"""
    return _build_internal_follow_up_msg(
        _BEFORE_STOP_FOLLOW_UP_REASON_TEMPLATE.format(
            reason=(reason or "BeforeStop blocked completion").strip(),
        ),
    )


def _build_before_stop_incomplete_msg(reason: str) -> Msg:
    """构造自动续跑预算耗尽后的显式未完成消息。"""
    return Msg(
        name="Friday",
        role="assistant",
        content=_BEFORE_STOP_INCOMPLETE_MESSAGE_TEMPLATE.format(
            reason=(reason or "BeforeStop blocked completion").strip(),
        ),
    )


def _resolve_max_confirmed_turns(validation_config: Any) -> int:
    """Resolve the post-turn confirmation limit with backward compatibility."""
    confirmed_turns = getattr(validation_config, "max_confirmed_turns", None)
    if confirmed_turns is None:
        confirmed_turns = 2
    try:
        return max(int(confirmed_turns), 0)
    except (TypeError, ValueError):
        return 2


def _resolve_max_auto_turns(validation_config: Any) -> int:
    """Resolve the automatic continuation limit."""
    auto_turns = getattr(validation_config, "max_auto_turns", 2)
    try:
        return max(int(auto_turns), 0)
    except (TypeError, ValueError):
        return 2


def _resolve_max_before_stop_turns(agent_config: Any) -> int:
    """解析 BeforeStop 自动续跑上限，未配置时使用保守默认值。"""
    running_config = getattr(agent_config, "running", None)
    hook_runtime_config = getattr(running_config, "hook_runtime", None)
    configured_turns = getattr(
        hook_runtime_config,
        "max_before_stop_turns",
        None,
    )
    if configured_turns is None:
        configured_turns = getattr(
            running_config,
            "max_before_stop_turns",
            2,
        )
    before_stop_turns = 2 if configured_turns is None else configured_turns
    try:
        return max(int(before_stop_turns), 0)
    except (TypeError, ValueError):
        return 2


def _resolve_max_automatic_follow_up_turns(
    agent_config: Any,
    default_limit: int,
) -> int:
    """解析请求级自动续跑总上限，确保多套续跑机制共享同一预算。"""
    running_config = getattr(agent_config, "running", None)
    hook_runtime_config = getattr(running_config, "hook_runtime", None)
    configured_turns = getattr(
        hook_runtime_config,
        "max_automatic_follow_up_turns",
        None,
    )
    if configured_turns is None:
        configured_turns = getattr(
            running_config,
            "max_automatic_follow_up_turns",
            default_limit,
        )
    aggregate_turns = (
        default_limit if configured_turns is None else configured_turns
    )
    try:
        return max(int(aggregate_turns), 0)
    except (TypeError, ValueError):
        return default_limit


def _strip_internal_follow_up_messages_from_state(
    agent_state: dict[str, Any],
) -> int:
    """Remove hidden continuation prompts before persisting session state."""
    memory_state = agent_state.get("memory")
    if not isinstance(memory_state, dict):
        return 0

    content = memory_state.get("content")
    if not isinstance(content, list):
        return 0

    kept_entries = []
    removed = 0
    for entry in content:
        msg_payload = entry[0] if isinstance(entry, list) and entry else None
        metadata = (
            msg_payload.get("metadata")
            if isinstance(msg_payload, dict)
            else None
        )
        if isinstance(metadata, dict) and metadata.get(
            _INTERNAL_FOLLOW_UP_METADATA_KEY,
        ):
            removed += 1
            continue
        kept_entries.append(entry)

    if removed:
        memory_state["content"] = kept_entries

    return removed


async def _index_model_output_to_monitor(
    trace_id: str,
    model_output: str,
) -> None:
    """通过 Monitor API 写入 model_output 到 ES.

    Args:
        trace_id: 追踪 ID
        model_output: 模型输出文本
    """
    monitor_url = os.environ.get(
        "SWE_MONITOR_API_URL",
        "http://127.0.0.1:9090",
    )
    url = f"{monitor_url}/monitor/tracing/model-output"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                url,
                json={
                    "trace_id": trace_id,
                    "model_output": model_output,
                },
            )
            logger.debug(
                "Monitor API response: status=%s, body=%s",
                response.status_code,
                response.text[:200] if response.text else "",
            )
            if response.status_code == 200:
                result = response.json()
                if result.get("status") == "success":
                    logger.info(
                        "Model output indexed via Monitor API: trace_id=%s",
                        trace_id,
                    )
                else:
                    logger.info(
                        "Model output write skipped: trace_id=%s, reason=%s",
                        trace_id,
                        result.get("reason", "unknown"),
                    )
            else:
                logger.warning(
                    "Monitor API returned %s: trace_id=%s",
                    response.status_code,
                    trace_id,
                )
    except httpx.TimeoutException:
        logger.warning("Monitor API timeout: trace_id=%s", trace_id)
    except Exception as e:
        logger.warning(
            "Failed to call Monitor API for model_output: %s",
            e,
        )


async def _generate_and_store_suggestions(
    session_id: str,
    user_message: str,
    assistant_response: str,
    config,  # SuggestionConfig
    tenant_id: str | None = None,
    prompt_template: str | None = None,
    turn_id: str | None = None,
) -> None:
    """执行一次建议生成与存储，通常由后台调度任务调用。"""
    from ...config.context import resolve_runtime_tenant_id

    runtime_tenant_id = (
        resolve_runtime_tenant_id(tenant_id, None)
        if tenant_id is not None
        else None
    )
    logger.info(
        "Generating suggestions for session %s: user_msg=%s chars, "
        "assistant_msg=%s chars",
        session_id,
        len(user_message),
        len(assistant_response),
    )
    try:
        suggestions = await generate_suggestions(
            user_message=user_message,
            assistant_response=assistant_response,
            max_suggestions=config.max_suggestions,
            timeout_seconds=config.timeout_seconds,
            user_message_max_length=config.user_message_max_length,
            assistant_response_max_length=config.assistant_response_max_length,
            prompt_template=prompt_template,
        )
        logger.info(
            "Generated %d suggestions for session %s",
            len(suggestions),
            session_id,
        )
        if suggestions:
            await store_suggestions(
                session_id,
                suggestions,
                tenant_id=runtime_tenant_id,
                turn_id=turn_id,
            )
            logger.info(
                "Stored %d suggestions for session %s: %s",
                len(suggestions),
                session_id,
                suggestions,
            )
    except Exception as e:
        logger.warning("Suggestion generation task failed: %s", e)


def _log_background_suggestion_task_result(task: asyncio.Task[None]) -> None:
    """消费后台建议任务结果，避免未处理异常泄漏到事件循环。"""
    try:
        task.result()
    except asyncio.CancelledError:
        logger.debug("Background suggestion task was cancelled")
    except Exception as e:  # pragma: no cover
        logger.warning("Background suggestion task failed: %s", e)


def _with_hook_context(
    env_context: str,
    hook_context: str,
) -> str:
    """追加 hook 上下文，避免主流程重复拼接同一段格式。"""
    if not hook_context:
        return env_context
    return f"{env_context}\n\n[Hook additional context]\n{hook_context}"


def _chat_name_from_messages(msgs: list[Any]) -> str:
    """从首条消息派生会话名，保持原有文本和媒体消息规则。"""
    if not msgs:
        return "New Chat"

    content = msgs[0].get_text_content()
    if content:
        return content[:10]
    return "Media Message"


def _request_source_id(request: AgentRequest) -> str:
    """从请求属性和 channel_meta 中解析追踪或 hook 的来源标识。"""
    channel_meta = getattr(request, "channel_meta", None) or {}
    return getattr(request, "source_id", None) or channel_meta.get(
        "source_id",
        "default",
    )


def _request_user_name(request: AgentRequest) -> str | None:
    """按兼容顺序读取通道注入的用户名称。"""
    return getattr(request, "user_name", None) or getattr(
        getattr(request, "state", None),
        "user_name",
        None,
    )


def _request_bbk_id(request: AgentRequest) -> str | None:
    """按兼容顺序读取通道注入的 BBK 标识。"""
    return getattr(request, "bbk_id", None) or getattr(
        getattr(request, "state", None),
        "bbk_id",
        None,
    )


def _session_name_from_messages(msgs: list[Any]) -> str | None:
    """从第一条消息提取 trace 中展示的短会话名。"""
    if not msgs:
        return None

    content = msgs[0].get_text_content()
    if not content:
        return None
    return content[:10]


def _validation_enabled(
    validation_config: Any | None,
    assistant_response: str,
    original_user_message: str,
) -> bool:
    """集中判断本轮是否需要执行 post-turn validation。"""
    return bool(
        assistant_response
        and original_user_message
        and validation_config is not None
        and getattr(validation_config, "enabled", False),
    )


def _should_auto_follow_up(
    validation_result: Any,
    auto_follow_up_turns: int,
    max_auto_turns: int,
) -> bool:
    """判断校验失败时是否仍允许自动续跑。"""
    return bool(
        not validation_result.completed
        and validation_result.follow_up_prompt
        and auto_follow_up_turns < max_auto_turns,
    )


def _has_automatic_follow_up_budget(outcome: _QueryTurnOutcome) -> bool:
    """判断本请求级自动续跑总预算是否仍可消耗。"""
    return outcome.automatic_follow_up_turns < (
        outcome.max_automatic_follow_up_turns
    )


def _should_before_stop_follow_up(outcome: _QueryTurnOutcome) -> bool:
    """判断 BeforeStop 阻断后是否允许再自动续跑一次。"""
    return bool(
        outcome.before_stop_follow_up_turns < outcome.max_before_stop_turns
        and _has_automatic_follow_up_budget(outcome),
    )


def _merge_cron_agent_memory(
    existing_state: dict[str, Any],
    current_agent_state: dict[str, Any],
) -> dict[str, Any]:
    """为 cron 保存路径合并旧消息和本次新增消息。"""
    existing_memory = existing_state.get("agent", {}).get("memory", {}) or {}
    if "content" not in existing_memory:
        return current_agent_state

    current_memory = dict(current_agent_state.get("memory", {}) or {})
    current_memory["content"] = list(
        existing_memory.get("content", []) or [],
    ) + list(current_memory.get("content", []) or [])

    merged_agent_state = dict(current_agent_state)
    merged_agent_state["memory"] = current_memory
    return merged_agent_state


def _build_cron_merged_state(
    existing_state: dict[str, Any],
    current_agent_state: dict[str, Any],
    hook_overlay: HookSessionOverlay | None,
) -> tuple[dict[str, Any], list[Any], list[Any], int]:
    """构建 cron 任务保存所需的完整 session state。"""
    existing_memory = existing_state.get("agent", {}).get("memory", {}) or {}
    current_memory = current_agent_state.get("memory", {}) or {}
    existing_content = list(existing_memory.get("content", []) or [])
    current_content = list(current_memory.get("content", []) or [])
    stripped_count = _strip_internal_follow_up_messages_from_state(
        current_agent_state,
    )

    merged_state = dict(existing_state)
    merged_state["agent"] = _merge_cron_agent_memory(
        existing_state,
        current_agent_state,
    )
    if hook_overlay is not None:
        merged_state["hook_overlay"] = hook_overlay.model_dump(
            mode="json",
            by_alias=True,
        )

    task_run = _build_task_run_record(
        current_content,
        memory_start=len(existing_content),
    )
    if task_run is not None:
        task_runs = list(existing_state.get(TASK_RUNS_STATE_KEY, []) or [])
        task_runs.append(task_run)
        merged_state[TASK_RUNS_STATE_KEY] = task_runs

    return merged_state, existing_content, current_content, stripped_count


class AgentRunner(Runner):
    def __init__(
        self,
        agent_id: str = "default",
        workspace_dir: Path | None = None,
        task_tracker: Any | None = None,
        tenant_id: str | None = None,
    ) -> None:
        from ...config.context import resolve_runtime_tenant_id

        super().__init__()
        self.framework_type = "agentscope"
        self.agent_id = agent_id  # Store agent_id for config loading
        self.workspace_dir = (
            workspace_dir  # Store workspace_dir for prompt building
        )
        self.tenant_id = (
            resolve_runtime_tenant_id(tenant_id, None)
            if tenant_id is not None
            else None
        )  # Store tenant_id for config loading
        self._chat_manager = None  # Store chat_manager reference
        self._workspace: Any = None  # Workspace instance for control commands
        self.memory_manager: BaseMemoryManager | None = None
        self._task_tracker = task_tracker  # Task tracker for background tasks

    def set_chat_manager(self, chat_manager):
        """Set chat manager for auto-registration.

        Args:
            chat_manager: ChatManager instance
        """
        self._chat_manager = chat_manager

    def set_workspace(self, workspace):
        """Set workspace for control command handlers.

        Args:
            workspace: Workspace instance
        """
        self._workspace = workspace

    _APPROVAL_TIMEOUT_SECONDS = TOOL_GUARD_APPROVAL_TIMEOUT_SECONDS

    async def _resolve_pending_approval(
        self,
        session_id: str,
        query: str | None,
    ) -> tuple[Msg | None, bool, dict[str, Any] | None]:
        """Check for a pending tool-guard approval for *session_id*.

        Returns ``(response_msg, was_consumed, approved_tool_call)``:

        - ``(None, False, None)`` — no pending approval, continue normally.
        - ``(Msg, True, None)``   — denied; yield the Msg and stop.
        - ``(None, True, dict)``  — approved with stored tool call.

        Approvals are resolved FIFO per session (oldest pending first).
        """
        if not session_id:
            return None, False, None

        from ..approvals import get_approval_service

        svc = get_approval_service()
        normalized = (query or "").strip().lower()
        request_id = _approval_request_id(normalized) or _denial_request_id(
            normalized,
        )
        pending = await _select_pending_approval(
            svc,
            session_id=session_id,
            request_id=request_id,
        )
        if pending is None:
            return None, False, None

        elapsed = time.time() - pending.created_at
        if elapsed > self._APPROVAL_TIMEOUT_SECONDS:
            await svc.resolve_request(
                pending.request_id,
                ApprovalDecision.TIMEOUT,
            )
            return (
                Msg(
                    name="Friday",
                    role="assistant",
                    content=[
                        TextBlock(
                            type="text",
                            text=(
                                f"⏰ Tool `{pending.tool_name}` approval "
                                f"timed out ({int(elapsed)}s) — denied.\n"
                                f"工具 `{pending.tool_name}` 审批超时"
                                f"（{int(elapsed)}s），已拒绝执行。"
                            ),
                        ),
                    ],
                ),
                True,
                None,
            )

        if _is_approval(normalized):
            resolved = await svc.resolve_request(
                pending.request_id,
                ApprovalDecision.APPROVED,
            )
            approved_tool_call: dict[str, Any] | None = None
            record = resolved or pending
            if isinstance(record.extra, dict):
                candidate = record.extra.get("tool_call")
                if isinstance(candidate, dict):
                    approved_tool_call = dict(candidate)
                    siblings = record.extra.get("sibling_tool_calls")
                    if isinstance(siblings, list):
                        approved_tool_call["_sibling_tool_calls"] = siblings
                    remaining = record.extra.get("remaining_queue")
                    if isinstance(remaining, list):
                        approved_tool_call["_remaining_queue"] = remaining
                    thinking_blocks = record.extra.get("thinking_blocks")
                    if isinstance(thinking_blocks, list):
                        approved_tool_call["_thinking_blocks"] = (
                            thinking_blocks
                        )
                    replay_metadata = _approval_replay_metadata(record)
                    if replay_metadata is not None:
                        approved_tool_call["_approval_replay"] = (
                            replay_metadata
                        )
            return None, True, approved_tool_call

        explicit_deny = _is_denial(normalized)
        denial_decision = (
            ApprovalDecision.DENIED
            if explicit_deny
            else ApprovalDecision.DENIED
        )
        await svc.resolve_request(
            pending.request_id,
            denial_decision,
        )
        return (
            Msg(
                name="Friday",
                role="assistant",
                content=[
                    TextBlock(
                        type="text",
                        text=(
                            f"❌ Tool `{pending.tool_name}` denied.\n"
                            f"工具 `{pending.tool_name}` 已拒绝执行。"
                        ),
                    ),
                ],
            ),
            True,
            None,
        )

    async def _prepare_query_preflight(
        self,
        *,
        session_id: str,
        user_id: str,
        query: str | None,
        request: AgentRequest,
    ) -> _QueryPreflight:
        """处理审批与用户 prompt hook，返回主流程需要的前置状态。"""
        (
            approval_response,
            approval_consumed,
            approved_tool_call,
        ) = await self._resolve_pending_approval(session_id, query)
        if approval_response is not None:
            return _QueryPreflight(
                response=approval_response,
                cleanup_denied_memory=True,
                approval_consumed=approval_consumed,
                approved_tool_call=approved_tool_call,
            )

        agent_config = load_agent_config(
            self.agent_id,
            tenant_id=self.tenant_id,
        )
        tenant_hooks = _load_tenant_hook_config(self.tenant_id)
        hook_overlay = await _load_session_hook_overlay(
            getattr(self, "session", None),
            session_id=session_id,
            user_id=user_id,
        )
        hook_additional_context = ""
        if query and _hook_config_enabled(
            tenant_hooks,
            agent_config,
            hook_overlay,
        ):
            prompt_hook_result = await _emit_runner_hook(
                HookEventName.USER_PROMPT_SUBMIT,
                request=request,
                runner=self,
                tenant_hooks=tenant_hooks,
                agent_config=agent_config,
                overlay=hook_overlay,
                prompt=query,
            )
            if prompt_hook_result.decision in {
                HookDecision.BLOCK,
                HookDecision.DENY,
                HookDecision.STOP,
            }:
                return _QueryPreflight(
                    response=_hook_block_message(prompt_hook_result),
                    approval_consumed=approval_consumed,
                    approved_tool_call=approved_tool_call,
                )
            if prompt_hook_result.session_title:
                request.channel_meta = {
                    **(getattr(request, "channel_meta", None) or {}),
                    "session_title": prompt_hook_result.session_title,
                }
            hook_additional_context = _format_hook_additional_context(
                prompt_hook_result,
            )

        return _QueryPreflight(
            approval_consumed=approval_consumed,
            approved_tool_call=approved_tool_call,
            agent_config=agent_config,
            tenant_hooks=tenant_hooks,
            hook_overlay=hook_overlay,
            hook_additional_context=hook_additional_context,
        )

    async def _start_query_trace(
        self,
        request: AgentRequest,
        msgs: list[Any],
    ) -> str | None:
        """启动 query 追踪；追踪不可用时只记录日志并继续主流程。"""
        if not has_trace_manager():
            return None

        try:
            trace_mgr = get_trace_manager()
            if not trace_mgr.enabled:
                return None
            return await trace_mgr.start_trace(
                user_id=getattr(request, "user_id", "") or "",
                session_id=getattr(request, "session_id", "") or "",
                channel=getattr(request, "channel", DEFAULT_CHANNEL),
                source_id=_request_source_id(request),
                user_message=_get_last_user_text(msgs),
                user_name=_request_user_name(request),
                bbk_id=_request_bbk_id(request),
                session_name=_session_name_from_messages(msgs),
            )
        except Exception as e:
            logger.warning("Failed to start trace: %s", e)
            return None

    async def _get_or_create_chat(
        self,
        *,
        session_id: str,
        user_id: str,
        channel: str,
        name: str,
        request: AgentRequest,
        turn_id: str,
    ) -> Any:
        """按原有规则注册或复用 chat，并把 chat_id 写回请求元数据。"""
        logger.debug(
            f"DEBUG chat_manager status: "
            f"_chat_manager={self._chat_manager}, "
            f"is_none={self._chat_manager is None}, "
            f"agent_id={self.agent_id}",
        )
        if self._chat_manager is None:
            logger.warning(
                f"ChatManager is None! Cannot auto-register chat for "
                f"session_id={session_id}",
            )
            return None

        logger.debug(
            f"Runner: Calling get_or_create_chat for "
            f"session_id={session_id}, user_id={user_id}, "
            f"channel={channel}, name={name}",
        )
        chat = await self._chat_manager.get_or_create_chat(
            session_id,
            user_id,
            channel,
            name=name,
            meta={"agent_id": self.agent_id},
        )
        logger.debug(f"Runner: Got chat: {chat.id}")
        request.channel_meta = {
            **(getattr(request, "channel_meta", None) or {}),
            "chat_id": chat.id,
            "turn_id": turn_id,
        }
        return chat

    async def _emit_session_start_hook(
        self,
        *,
        request: AgentRequest,
        tenant_hooks: HookConfig,
        agent_config: Any,
        hook_overlay: HookSessionOverlay,
        skip_history: bool,
        env_context: str,
    ) -> tuple[str, Msg | None]:
        """执行 SESSION_START hook，并返回可能追加的上下文或阻断消息。"""
        if not _hook_config_enabled(tenant_hooks, agent_config, hook_overlay):
            return env_context, None

        session_start_result = await _emit_runner_hook(
            HookEventName.SESSION_START,
            request=request,
            runner=self,
            tenant_hooks=tenant_hooks,
            agent_config=agent_config,
            overlay=hook_overlay,
            source="resume" if not skip_history else "startup",
            model=_resolve_active_model_label(self.tenant_id),
        )
        if session_start_result.decision in {
            HookDecision.BLOCK,
            HookDecision.DENY,
            HookDecision.STOP,
        }:
            return env_context, _hook_block_message(session_start_result)

        session_start_context = _format_hook_additional_context(
            session_start_result,
        )
        return _with_hook_context(env_context, session_start_context), None

    def _create_agent_for_query(
        self,
        *,
        agent_config: Any,
        env_context: str,
        mcp_clients: list[Any],
        request: AgentRequest,
        session_id: str,
        user_id: str,
        channel: str,
        chat: Any,
        turn_id: str,
        hook_overlay: HookSessionOverlay,
        auth_token: str | None,
        approved_tool_call: dict[str, Any] | None,
    ) -> SWEAgent:
        """创建 SWEAgent，并注入本轮请求上下文。"""
        request_context = {
            "session_id": session_id,
            "user_id": user_id,
            "channel": channel,
            "chat_id": chat.id if chat is not None else "",
            "turn_id": turn_id,
            "agent_id": self.agent_id,
            "tenant_id": self.tenant_id or "",
            "transcript_path": (
                self.session._get_save_path(session_id, user_id)
                if hasattr(self.session, "_get_save_path")
                else ""
            ),
            "hook_overlay": hook_overlay.model_dump(
                mode="json",
                by_alias=True,
            ),
            "_hook_overlay_model": hook_overlay,
        }
        if auth_token:
            request_context["auth_token"] = auth_token
        if approved_tool_call:
            request_context["forced_tool_call_json"] = json.dumps(
                approved_tool_call,
                ensure_ascii=False,
            )
        return SWEAgent(
            agent_config=agent_config,
            env_context=env_context,
            mcp_clients=mcp_clients,
            memory_manager=self.memory_manager,
            request_context=request_context,
            workspace_dir=self.workspace_dir,
            task_tracker=self._task_tracker,
        )

    def _attach_session_skill_detector(
        self,
        *,
        runtime: _QueryRuntime,
        request: AgentRequest,
    ) -> None:
        """挂载会话级技能探测器，并同步 hook overlay 的后续变更。"""

        def _get_session_hook_state() -> HookSessionState:
            return HookSessionState.model_validate(
                runtime.hook_overlay.model_dump(mode="json", by_alias=True),
            )

        def _set_session_hook_state(
            next_state: HookSessionState,
        ) -> None:
            runtime.hook_overlay = HookSessionOverlay.model_validate(
                next_state.model_dump(mode="json", by_alias=True),
            )
            dumped = runtime.hook_overlay.model_dump(
                mode="json",
                by_alias=True,
            )
            runtime.agent._request_context["_hook_overlay_model"] = (
                runtime.hook_overlay
            )
            runtime.agent._request_context["hook_overlay"] = dumped

        source_id_for_hooks = _request_source_id(request)
        runtime.session_skill_detector = _create_session_skill_detector(
            workspace_dir=Path(self.workspace_dir or WORKING_DIR),
            tenant_id=self.tenant_id,
            user_id=runtime.user_id,
            session_id=runtime.session_id,
            channel=runtime.channel,
            source_id=source_id_for_hooks,
            enabled_skills=(
                runtime.agent.get_effective_skills()
                if hasattr(runtime.agent, "get_effective_skills")
                else []
            ),
            get_hook_state=_get_session_hook_state,
            set_hook_state=_set_session_hook_state,
        )
        if not hasattr(runtime.agent, "_request_context"):
            runtime.agent._request_context = {}
        runtime.agent._request_context["_skill_invocation_detector"] = (
            runtime.session_skill_detector
        )

    async def _start_declared_session_skill(
        self,
        *,
        runtime: _QueryRuntime,
        user_message: str,
    ) -> None:
        """当用户消息显式声明技能时，启动本轮技能状态记录。"""
        if not user_message:
            return

        skill, confidence = (
            runtime.session_skill_detector.detect_from_user_message(
                user_message,
            )
        )
        if skill and confidence >= 0.7:
            await runtime.session_skill_detector.start_skill(
                skill_name=skill,
                trigger_tool="user_message",
                trigger_reason="declared",
                confidence=confidence,
            )

    async def _prepare_query_runtime(
        self,
        *,
        request: AgentRequest,
        msgs: list[Any],
        query: str | None,
        preflight: _QueryPreflight,
    ) -> _RuntimeStartResult:
        """装配 agent、chat、MCP 客户端以及会话级 hook 运行状态。"""
        session_id = request.session_id
        user_id = request.user_id
        channel = getattr(request, "channel", DEFAULT_CHANNEL)
        skip_history = getattr(request, "skip_history", False)

        logger.info(
            "Handle agent query:\n%s",
            json.dumps(
                {
                    "session_id": session_id,
                    "user_id": user_id,
                    "channel": channel,
                    "msgs_len": len(msgs) if msgs else 0,
                    "msgs_str": str(msgs)[:300] + "...",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )

        env_context = build_env_context(
            session_id=session_id,
            user_id=user_id,
            channel=channel,
            working_dir=(
                str(self.workspace_dir)
                if self.workspace_dir
                else str(WORKING_DIR)
            ),
        )
        env_context = _with_hook_context(
            env_context,
            preflight.hook_additional_context,
        )

        agent_config = (
            preflight.agent_config
            if preflight.agent_config is not None
            else load_agent_config(
                self.agent_id,
                tenant_id=self.tenant_id,
            )
        )
        tenant_hooks = (
            preflight.tenant_hooks
            if preflight.tenant_hooks is not None
            else _load_tenant_hook_config(self.tenant_id)
        )
        hook_overlay = (
            preflight.hook_overlay
            if preflight.hook_overlay is not None
            else HookSessionOverlay()
        )
        mcp_clients: list[Any] = []
        try:
            auth_token = getattr(request, "auth_token", None)
            cookie_header = getattr(request, "cookie", None)
            passthrough_headers = dict[str, str](
                get_current_passthrough_headers() or {},
            )
            if cookie_header:
                passthrough_headers["cookie"] = cookie_header
            mcp_clients = await _build_and_connect_mcp_clients(
                agent_config.mcp,
                passthrough_headers=passthrough_headers or None,
            )

            channel_meta = getattr(request, "channel_meta", {}) or {}
            turn_id = str(
                channel_meta.get("turn_id")
                or getattr(request, "turn_id", None)
                or f"turn-{uuid4().hex}",
            )
            chat = await self._get_or_create_chat(
                session_id=session_id,
                user_id=user_id,
                channel=channel,
                name=_chat_name_from_messages(msgs),
                request=request,
                turn_id=turn_id,
            )
            env_context, block_response = await self._emit_session_start_hook(
                request=request,
                tenant_hooks=tenant_hooks,
                agent_config=agent_config,
                hook_overlay=hook_overlay,
                skip_history=skip_history,
                env_context=env_context,
            )
            if block_response is not None:
                return _RuntimeStartResult(
                    block_response=block_response,
                    blocked_chat=chat,
                    blocked_mcp_clients=mcp_clients,
                    blocked_session_id=session_id,
                )

            agent = self._create_agent_for_query(
                agent_config=agent_config,
                env_context=env_context,
                mcp_clients=mcp_clients,
                request=request,
                session_id=session_id,
                user_id=user_id,
                channel=channel,
                chat=chat,
                turn_id=turn_id,
                hook_overlay=hook_overlay,
                auth_token=auth_token,
                approved_tool_call=preflight.approved_tool_call,
            )
            await agent.register_mcp_clients()
            agent.set_console_output_enabled(enabled=False)

            runtime = _QueryRuntime(
                agent=agent,
                agent_config=agent_config,
                tenant_hooks=tenant_hooks,
                hook_overlay=hook_overlay,
                chat=chat,
                session_skill_detector=None,
                mcp_clients=mcp_clients,
                session_id=session_id,
                user_id=user_id,
                channel=channel,
                turn_id=turn_id,
                skip_history=skip_history,
            )
            self._attach_session_skill_detector(
                runtime=runtime,
                request=request,
            )
            await self._start_declared_session_skill(
                runtime=runtime,
                user_message=query or _get_last_user_text(msgs) or "",
            )
            return _RuntimeStartResult(runtime=runtime)
        except Exception:
            if mcp_clients:
                await _cleanup_mcp_clients(mcp_clients)
            raise

    async def _build_turn_plan(
        self,
        *,
        runtime: _QueryRuntime,
        request: AgentRequest,
        msgs: list[Any],
        query: str | None,
    ) -> _TurnPlanResult:
        """根据普通请求或 validation 续跑请求构建本轮输入。"""
        channel_meta = getattr(request, "channel_meta", {}) or {}
        resume_id = channel_meta.get("post_turn_validation_resume_id")
        original_user_message = query or _get_last_user_text(msgs) or ""
        validation_config = getattr(
            runtime.agent_config.running,
            "post_turn_validation",
            None,
        )
        if not resume_id:
            return _TurnPlanResult(
                plan=_TurnPlan(
                    original_user_message=original_user_message,
                    confirmed_turn_index=0,
                    turn_msgs=list(msgs),
                    validation_config=validation_config,
                ),
            )

        pending_continuation = await consume_pending_continuation(
            validation_id=resume_id,
            session_id=runtime.session_id,
            tenant_id=self.tenant_id,
        )
        if pending_continuation is None:
            return _TurnPlanResult(
                response=Msg(
                    name="Friday",
                    role="assistant",
                    content="续跑请求已过期或不存在，请重新发起任务。",
                ),
            )

        return _TurnPlanResult(
            plan=_TurnPlan(
                original_user_message=(
                    pending_continuation.user_message or original_user_message
                ),
                confirmed_turn_index=(
                    pending_continuation.confirmed_turn_index + 1
                ),
                turn_msgs=[
                    _build_internal_follow_up_msg(
                        pending_continuation.follow_up_prompt,
                    ),
                ],
                validation_config=validation_config,
            ),
        )

    async def _validate_turn_if_needed(
        self,
        *,
        plan: _TurnPlan,
        assistant_response: str,
    ) -> Any | None:
        """按配置执行 post-turn validation，未启用时返回 None。"""
        if not _validation_enabled(
            plan.validation_config,
            assistant_response,
            plan.original_user_message,
        ):
            return None

        return await validate_task_completion(
            user_message=plan.original_user_message,
            assistant_response=assistant_response,
            agent_id=self.agent_id,
            timeout_seconds=getattr(
                plan.validation_config,
                "timeout_seconds",
                8.0,
            ),
            user_message_max_length=getattr(
                plan.validation_config,
                "user_message_max_length",
                300,
            ),
            assistant_response_max_length=getattr(
                plan.validation_config,
                "assistant_response_max_length",
                1200,
            ),
        )

    async def _stream_agent_turns(
        self,
        *,
        runtime: _QueryRuntime,
        plan: _TurnPlan,
        outcome: _QueryTurnOutcome,
    ):
        """流式执行 agent，并在 validation 需要时自动续跑。"""
        turn_msgs = plan.turn_msgs
        validation_auto_turns = (
            _resolve_max_auto_turns(plan.validation_config)
            if plan.validation_config is not None
            else 0
        )
        before_stop_turns = _resolve_max_before_stop_turns(
            runtime.agent_config,
        )
        outcome.max_before_stop_turns = before_stop_turns
        outcome.max_auto_turns = validation_auto_turns
        outcome.max_automatic_follow_up_turns = (
            _resolve_max_automatic_follow_up_turns(
                runtime.agent_config,
                validation_auto_turns + before_stop_turns,
            )
        )
        while True:
            async for msg, last in self._enforce_query_timeout(
                stream_printing_messages(
                    agents=[runtime.agent],
                    coroutine_task=runtime.agent(turn_msgs),
                ),
                session_id=runtime.session_id,
                agent=runtime.agent,
                run_key=(
                    runtime.chat.id if runtime.chat is not None else None
                ),
            ):
                yield msg, last

            outcome.assistant_response = _extract_assistant_response(
                runtime.agent,
            )
            outcome.task_completed = True
            outcome.last_validation_result = (
                await self._validate_turn_if_needed(
                    plan=plan,
                    assistant_response=outcome.assistant_response,
                )
            )
            if outcome.last_validation_result is None:
                break

            outcome.task_completed = outcome.last_validation_result.completed
            if not _should_auto_follow_up(
                outcome.last_validation_result,
                outcome.auto_follow_up_turns,
                outcome.max_auto_turns,
            ) or not _has_automatic_follow_up_budget(outcome):
                break

            outcome.auto_follow_up_turns += 1
            outcome.automatic_follow_up_turns += 1
            turn_msgs = [
                _build_internal_follow_up_msg(
                    outcome.last_validation_result.follow_up_prompt,
                ),
            ]
            logger.info(
                "Post-turn validation scheduled automatic "
                "follow-up turn %d/%d for session %s: %s",
                outcome.auto_follow_up_turns,
                outcome.max_auto_turns,
                runtime.session_id,
                outcome.last_validation_result.reason or "continue",
            )

    async def _emit_before_stop_hook_if_needed(
        self,
        *,
        request: AgentRequest,
        runtime: _QueryRuntime,
        plan: _TurnPlan,
        outcome: _QueryTurnOutcome,
    ) -> MergedHookResult | None:
        """执行 BeforeStop gate，active guard 已设置时跳过递归触发。"""
        if outcome.stop_hook_active:
            return None
        if not outcome.assistant_response:
            return None
        if not _hook_config_enabled(
            runtime.tenant_hooks,
            runtime.agent_config,
            runtime.hook_overlay,
        ):
            return None

        outcome.stop_hook_active = True
        return await _emit_runner_hook(
            HookEventName.BEFORE_STOP,
            request=request,
            runner=self,
            tenant_hooks=runtime.tenant_hooks,
            agent_config=runtime.agent_config,
            overlay=runtime.hook_overlay,
            prompt=plan.original_user_message,
            assistant_response=outcome.assistant_response,
        )

    async def _stream_completion_lifecycle(
        self,
        *,
        request: AgentRequest,
        runtime: _QueryRuntime,
        plan: _TurnPlan,
        outcome: _QueryTurnOutcome,
    ):
        """执行 agent turn、BeforeStop gate 与最终 Stop hook 生命周期。"""
        while True:
            outcome.stop_hook_active = False
            async for msg, last in self._stream_agent_turns(
                runtime=runtime,
                plan=plan,
                outcome=outcome,
            ):
                yield msg, last

            before_stop_result = await self._emit_before_stop_hook_if_needed(
                request=request,
                runtime=runtime,
                plan=plan,
                outcome=outcome,
            )
            if (
                before_stop_result is not None
                and before_stop_result.decision == HookDecision.BLOCK
            ):
                reason = (
                    before_stop_result.reason
                    or "BeforeStop blocked completion"
                )
                if _should_before_stop_follow_up(outcome):
                    outcome.before_stop_follow_up_turns += 1
                    outcome.automatic_follow_up_turns += 1
                    plan.turn_msgs = [_build_before_stop_follow_up_msg(reason)]
                    outcome.stop_hook_active = False
                    logger.info(
                        "BeforeStop scheduled automatic follow-up turn "
                        "%d/%d for session %s: %s",
                        outcome.before_stop_follow_up_turns,
                        outcome.max_before_stop_turns,
                        runtime.session_id,
                        reason,
                    )
                    continue

                outcome.task_completed = False
                outcome.completion_blocked = True
                outcome.completion_block_reason = reason
                outcome.completion_marked_incomplete = True
                outcome.stop_hook_active = False
                incomplete_msg = _build_before_stop_incomplete_msg(reason)
                await runtime.agent.memory.add(incomplete_msg)
                yield incomplete_msg, True
                return

            if (
                before_stop_result is not None
                and before_stop_result.decision
                in {
                    HookDecision.DENY,
                    HookDecision.STOP,
                }
            ):
                outcome.task_completed = False
                outcome.completion_blocked = True
                outcome.completion_block_reason = before_stop_result.reason
                outcome.stop_hook_active = False
                yield _hook_block_message(before_stop_result), True
                return

            stop_response = await self._emit_stop_hook_if_needed(
                request=request,
                runtime=runtime,
                plan=plan,
                outcome=outcome,
            )
            outcome.stop_hook_active = False
            if stop_response is not None:
                outcome.completion_blocked = True
                outcome.completion_block_reason = (
                    stop_response.get_text_content()
                )
                yield stop_response, True
            return

    async def _emit_stop_hook_if_needed(
        self,
        *,
        request: AgentRequest,
        runtime: _QueryRuntime,
        plan: _TurnPlan,
        outcome: _QueryTurnOutcome,
    ) -> Msg | None:
        """执行 STOP hook，必要时把附加上下文写入 agent memory。"""
        if not _hook_config_enabled(
            runtime.tenant_hooks,
            runtime.agent_config,
            runtime.hook_overlay,
        ):
            return None

        stop_hook_result = await _emit_runner_hook(
            HookEventName.STOP,
            request=request,
            runner=self,
            tenant_hooks=runtime.tenant_hooks,
            agent_config=runtime.agent_config,
            overlay=runtime.hook_overlay,
            prompt=plan.original_user_message,
            assistant_response=outcome.assistant_response,
        )
        stop_context = _format_hook_additional_context(stop_hook_result)
        if stop_context:
            await runtime.agent.memory.add(
                Msg(
                    name="system",
                    role="system",
                    content=("[Hook additional context]\n" f"{stop_context}"),
                ),
            )
        if stop_hook_result.decision in {
            HookDecision.BLOCK,
            HookDecision.DENY,
            HookDecision.STOP,
        }:
            outcome.task_completed = False
            return _hook_block_message(stop_hook_result)
        return None

    async def _store_pending_validation_if_needed(
        self,
        *,
        runtime: _QueryRuntime,
        plan: _TurnPlan,
        outcome: _QueryTurnOutcome,
    ) -> None:
        """保存需要用户确认的 post-turn validation 续跑请求。"""
        validation_result = outcome.last_validation_result
        if (
            outcome.task_completed
            or validation_result is None
            or not validation_result.follow_up_prompt
        ):
            return

        max_confirmed_turns = _resolve_max_confirmed_turns(
            plan.validation_config,
        )
        if plan.confirmed_turn_index >= max_confirmed_turns:
            logger.info(
                "Post-turn validation reached confirmed turn "
                "limit %d for session %s",
                max_confirmed_turns,
                runtime.session_id,
            )
            return

        await store_pending_continuation(
            session_id=runtime.session_id,
            user_message=plan.original_user_message,
            assistant_response=_extract_assistant_response(runtime.agent),
            reason=validation_result.reason,
            follow_up_prompt=validation_result.follow_up_prompt,
            tenant_id=self.tenant_id,
            confirmed_turn_index=plan.confirmed_turn_index,
        )
        logger.info(
            "Post-turn validation pending confirmation after "
            "automatic turns %d/%d; confirmed turn %d/%d for "
            "session %s: %s",
            outcome.auto_follow_up_turns,
            outcome.max_auto_turns,
            plan.confirmed_turn_index + 1,
            max_confirmed_turns,
            runtime.session_id,
            validation_result.reason or "continue",
        )

    async def _generate_backend_suggestions_if_needed(
        self,
        *,
        runtime: _QueryRuntime,
        plan: _TurnPlan,
        outcome: _QueryTurnOutcome,
    ) -> None:
        """在任务完成后按配置触发后端建议生成。"""
        suggestions_config = runtime.agent_config.running.suggestions
        if not outcome.task_completed:
            return
        if not suggestions_config.enabled:
            return
        if suggestions_config.mode != SuggestionMode.BACKEND_GENERATE:
            return

        source_config = get_follow_up_suggestions_config()
        if not source_config.enabled:
            return

        user_message = plan.original_user_message
        assistant_response = (
            outcome.assistant_response
            or _extract_assistant_response(runtime.agent)
        )
        if not user_message or not assistant_response:
            return

        task = asyncio.create_task(
            _generate_and_store_suggestions(
                runtime.session_id,
                user_message,
                assistant_response,
                suggestions_config,
                tenant_id=self.tenant_id,
                prompt_template=source_config.prompt_template,
                turn_id=getattr(runtime, "turn_id", None),
            ),
        )
        task.add_done_callback(_log_background_suggestion_task_result)

    async def _index_model_output_if_needed(
        self,
        *,
        trace_id: str | None,
        agent: SWEAgent | None,
    ) -> None:
        """有 trace 时把最终模型输出写入 Monitor。"""
        if not trace_id or agent is None:
            return

        logger.debug(
            "Preparing to index model output: trace_id=%s, agent=%s",
            trace_id,
            type(agent).__name__,
        )
        assistant_response = _extract_assistant_response(agent)
        logger.debug(
            "Extracted assistant response: trace_id=%s, response_len=%d",
            trace_id,
            len(assistant_response) if assistant_response else 0,
        )
        if assistant_response:
            await _index_model_output_to_monitor(trace_id, assistant_response)
            return

        logger.warning("No assistant response to index: trace_id=%s", trace_id)

    async def _end_trace_if_needed(
        self,
        trace_id: str | None,
        status: TraceStatus,
        error: str | None = None,
    ) -> None:
        """结束 trace，失败时只记录日志避免影响主链路。"""
        if not trace_id or not has_trace_manager():
            return

        try:
            trace_mgr = get_trace_manager()
            if error is None:
                await trace_mgr.end_trace(trace_id, status=status)
            else:
                await trace_mgr.end_trace(
                    trace_id,
                    status=status,
                    error=error,
                )
        except Exception as trace_err:
            logger.warning("Failed to end trace: %s", trace_err)

    async def _handle_query_cancelled(
        self,
        *,
        trace_id: str | None,
        session_id: str,
        agent: SWEAgent | None,
        exc: asyncio.CancelledError,
    ) -> None:
        """处理 query 被取消时的 trace 和 agent 中断。"""
        logger.info(f"query_handler: {session_id} cancelled!")
        await self._end_trace_if_needed(trace_id, TraceStatus.CANCELLED)
        if agent is not None:
            await agent.interrupt()
        raise AgentException("Task has been cancelled!") from exc

    async def _handle_query_error(
        self,
        *,
        request: AgentRequest,
        exc: Exception,
        trace_id: str | None,
        locals_snapshot: dict[str, Any],
    ) -> None:
        """记录 query 异常 dump，并把 dump 路径附加到异常信息。"""
        debug_dump_path = write_query_error_dump(
            request=request,
            exc=exc,
            locals_=locals_snapshot,
        )
        path_hint = (
            f"\n(Details:  {debug_dump_path})" if debug_dump_path else ""
        )
        logger.exception(f"Error in query handler: {exc}{path_hint}")
        await self._end_trace_if_needed(
            trace_id,
            TraceStatus.ERROR,
            error=str(exc),
        )
        if not debug_dump_path:
            return

        setattr(exc, "debug_dump_path", debug_dump_path)
        if hasattr(exc, "add_note"):
            exc.add_note(f"(Details:  {debug_dump_path})")
        suffix = f"\n(Details:  {debug_dump_path})"
        exc.args = (
            (f"{exc.args[0]}{suffix}" if exc.args else suffix.strip()),
        ) + exc.args[1:]

    async def _save_state_during_cleanup(
        self,
        *,
        runtime: _QueryRuntime | None,
        session_state_loaded: bool,
    ) -> None:
        """在 finally 阶段保存 session state，并限制单步耗时。"""
        if runtime is None or not session_state_loaded:
            return

        hook_overlay = None
        if _hook_config_enabled(
            runtime.tenant_hooks,
            runtime.agent_config,
            runtime.hook_overlay,
        ):
            hook_overlay = runtime.hook_overlay
        try:
            await asyncio.wait_for(
                self.save_job_session_state(
                    runtime.agent,
                    runtime.session_id,
                    runtime.skip_history,
                    runtime.user_id,
                    hook_overlay=hook_overlay,
                ),
                timeout=QUERY_CLEANUP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Runner finally: session state save timed out "
                "(session_id=%s, timeout=%.0fs)",
                runtime.session_id,
                QUERY_CLEANUP_TIMEOUT,
            )
        except asyncio.CancelledError:
            logger.debug(
                "Runner finally: session state save cancelled (session_id=%s)",
                runtime.session_id,
            )

    async def _update_chat_during_cleanup(
        self,
        runtime: _QueryRuntime | None,
    ) -> None:
        """在 finally 阶段写回 chat 状态。"""
        if (
            runtime is None
            or self._chat_manager is None
            or runtime.chat is None
        ):
            return

        try:
            await asyncio.wait_for(
                self._chat_manager.update_chat(runtime.chat),
                timeout=QUERY_CLEANUP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Runner finally: chat update timed out "
                "(session_id=%s, timeout=%.0fs)",
                runtime.session_id,
                QUERY_CLEANUP_TIMEOUT,
            )
        except asyncio.CancelledError:
            logger.debug(
                "Runner finally: chat update cancelled (session_id=%s)",
                runtime.session_id,
            )

    async def _cleanup_mcp_during_cleanup(
        self,
        runtime: _QueryRuntime | None,
    ) -> None:
        """在 finally 阶段关闭本轮创建的 MCP 客户端。"""
        if runtime is None or not runtime.mcp_clients:
            return

        try:
            await asyncio.wait_for(
                _cleanup_mcp_clients(runtime.mcp_clients),
                timeout=QUERY_CLEANUP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Runner finally: MCP cleanup timed out "
                "(session_id=%s, timeout=%.0fs)",
                runtime.session_id,
                QUERY_CLEANUP_TIMEOUT,
            )
        except asyncio.CancelledError:
            logger.debug(
                "Runner finally: MCP cleanup cancelled (session_id=%s)",
                runtime.session_id,
            )

    async def _end_skill_detector_during_cleanup(
        self,
        runtime: _QueryRuntime | None,
    ) -> None:
        """在 finally 阶段结束会话级技能探测器。"""
        if runtime is None or runtime.session_skill_detector is None:
            return

        try:
            await asyncio.wait_for(
                runtime.session_skill_detector.on_reasoning_end(),
                timeout=QUERY_CLEANUP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Runner finally: skill detector cleanup timed out "
                "(session_id=%s, timeout=%.0fs)",
                runtime.session_id,
                QUERY_CLEANUP_TIMEOUT,
            )
        except asyncio.CancelledError:
            logger.debug(
                "Runner finally: skill detector cleanup cancelled "
                "(session_id=%s)",
                runtime.session_id,
            )

    async def _cleanup_query_resources(
        self,
        *,
        runtime: _QueryRuntime | None,
        session_state_loaded: bool,
        session_id: str,
    ) -> None:
        """集中执行 query finally 阶段的资源清理。"""
        logger.info(
            "Runner finally block executing for session %s",
            session_id,
        )
        await self._save_state_during_cleanup(
            runtime=runtime,
            session_state_loaded=session_state_loaded,
        )
        await self._update_chat_during_cleanup(runtime)
        await self._cleanup_mcp_during_cleanup(runtime)
        await self._end_skill_detector_during_cleanup(runtime)

    async def _cleanup_blocked_runtime_start(
        self,
        runtime_start: _RuntimeStartResult | None,
    ) -> None:
        """清理 SESSION_START hook 阻断前已经创建的资源。"""
        if (
            runtime_start is None
            or runtime_start.block_response is None
            or runtime_start.runtime is not None
        ):
            return

        session_id = runtime_start.blocked_session_id
        chat = runtime_start.blocked_chat
        if self._chat_manager is not None and chat is not None:
            try:
                await asyncio.wait_for(
                    self._chat_manager.update_chat(chat),
                    timeout=QUERY_CLEANUP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Runner finally: blocked chat update timed out "
                    "(session_id=%s, timeout=%.0fs)",
                    session_id,
                    QUERY_CLEANUP_TIMEOUT,
                )
            except asyncio.CancelledError:
                logger.debug(
                    "Runner finally: blocked chat update cancelled "
                    "(session_id=%s)",
                    session_id,
                )

        mcp_clients = runtime_start.blocked_mcp_clients or []
        if not mcp_clients:
            return
        try:
            await asyncio.wait_for(
                _cleanup_mcp_clients(mcp_clients),
                timeout=QUERY_CLEANUP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Runner finally: blocked MCP cleanup timed out "
                "(session_id=%s, timeout=%.0fs)",
                session_id,
                QUERY_CLEANUP_TIMEOUT,
            )
        except asyncio.CancelledError:
            logger.debug(
                "Runner finally: blocked MCP cleanup cancelled "
                "(session_id=%s)",
                session_id,
            )

    async def _store_qa_content_if_needed(
        self,
        *,
        runtime: _QueryRuntime | None,
        query: str | None,
        outcome: _QueryTurnOutcome,
    ) -> None:
        """按 QA_EXTRACTION_ONLY 模式保存本轮问答内容。"""
        if (
            runtime is None
            or runtime.chat is None
            or not outcome.task_completed
        ):
            return

        suggestions_config = runtime.agent_config.running.suggestions
        if (
            not suggestions_config.enabled
            or suggestions_config.mode != SuggestionMode.QA_EXTRACTION_ONLY
        ):
            return

        assistant_response = _extract_assistant_response(runtime.agent)
        user_message = query
        if not assistant_response or not user_message:
            logger.debug(
                "No Q&A content to extract for suggestions: "
                "assistant_response=%s, user_message=%s",
                bool(assistant_response),
                bool(user_message),
            )
            return

        from ..suggestions.service import extract_key_content
        from ..suggestions.store import store_qa_content

        extracted_user = user_message[
            : suggestions_config.user_message_max_length
        ]
        extracted_assistant = extract_key_content(
            assistant_response,
            max_length=min(
                suggestions_config.qa_content_total_max_length
                - len(extracted_user),
                suggestions_config.assistant_response_max_length,
            ),
        )
        await store_qa_content(
            chat_id=runtime.chat.id,
            user_message=extracted_user,
            assistant_response=extracted_assistant,
            tenant_id=self.tenant_id,
        )
        logger.info(
            "Stored Q&A content for suggestions: chat_id=%s, "
            "user_len=%d, assistant_len=%d",
            runtime.chat.id,
            len(extracted_user),
            len(extracted_assistant),
        )

    async def _stream_query_after_preflight(
        self,
        msgs,
        *,
        request: AgentRequest,
        query: str | None,
        session_id: str,
        preflight: _QueryPreflight,
    ):
        """执行已经通过前置校验的普通 Agent query 主流程。"""
        logger.debug(
            f"AgentRunner.stream_query: request={request}, "
            f"agent_id={self.agent_id}",
        )

        from ..agent_context import set_current_agent_id

        set_current_agent_id(self.agent_id)

        runtime: _QueryRuntime | None = None
        runtime_start: _RuntimeStartResult | None = None
        session_state_loaded = False
        trace_id = await self._start_query_trace(request, msgs)
        outcome = _QueryTurnOutcome()

        try:
            runtime_start = await self._prepare_query_runtime(
                request=request,
                msgs=msgs,
                query=query,
                preflight=preflight,
            )
            if runtime_start.block_response is not None:
                yield runtime_start.block_response, True
                return
            runtime = runtime_start.runtime
            if runtime is None:
                return

            if trace_id:
                await runtime.agent.setup_skill_detector(trace_id)

            logger.debug(f"Agent Query msgs {msgs}")

            session_state_loaded = await self.get_state_loaded(
                runtime.agent,
                runtime.session_id,
                session_state_loaded,
                runtime.skip_history,
                runtime.user_id,
            )

            # 会话状态可能保存了旧提示词，执行前强制刷新文件态上下文。
            runtime.agent.rebuild_sys_prompt()

            plan_result = await self._build_turn_plan(
                runtime=runtime,
                request=request,
                msgs=msgs,
                query=query,
            )
            if plan_result.response is not None:
                outcome.task_completed = False
                yield plan_result.response, True
                return
            plan = plan_result.plan
            if plan is None:
                return

            async for msg, last in self._stream_completion_lifecycle(
                request=request,
                runtime=runtime,
                plan=plan,
                outcome=outcome,
            ):
                yield msg, last

            if outcome.completion_blocked:
                if outcome.completion_marked_incomplete:
                    await self._index_model_output_if_needed(
                        trace_id=trace_id,
                        agent=runtime.agent,
                    )
                    await self._end_trace_if_needed(
                        trace_id,
                        TraceStatus.COMPLETED,
                    )
                return

            await self._store_pending_validation_if_needed(
                runtime=runtime,
                plan=plan,
                outcome=outcome,
            )
            await self._generate_backend_suggestions_if_needed(
                runtime=runtime,
                plan=plan,
                outcome=outcome,
            )
            await self._index_model_output_if_needed(
                trace_id=trace_id,
                agent=runtime.agent,
            )
            await self._end_trace_if_needed(trace_id, TraceStatus.COMPLETED)

        except asyncio.CancelledError as exc:
            await self._handle_query_cancelled(
                trace_id=trace_id,
                session_id=session_id,
                agent=runtime.agent if runtime is not None else None,
                exc=exc,
            )
        except Exception as e:
            await self._handle_query_error(
                request=request,
                exc=e,
                trace_id=trace_id,
                locals_snapshot=locals(),
            )
            raise
        finally:
            await self._cleanup_query_resources(
                runtime=runtime,
                session_state_loaded=session_state_loaded,
                session_id=session_id,
            )
            await self._cleanup_blocked_runtime_start(runtime_start)
            await self._store_qa_content_if_needed(
                runtime=runtime,
                query=query,
                outcome=outcome,
            )

    async def _stream_query_entry(
        self,
        msgs,
        *,
        request: AgentRequest,
        query: str | None,
        session_id: str,
        user_id: str,
    ):
        """处理 query 主流程前的审批、prompt hook 与命令分发。"""
        preflight = await self._prepare_query_preflight(
            session_id=session_id,
            user_id=user_id,
            query=query,
            request=request,
        )
        if preflight.response is not None:
            yield preflight.response, True
            if preflight.cleanup_denied_memory:
                await self._cleanup_denied_session_memory(
                    session_id,
                    user_id,
                    denial_response=preflight.response,
                )
            return

        if not preflight.approval_consumed and query and _is_command(query):
            logger.info("Command path: %s", query.strip()[:50])
            async for msg, last in run_command_path(request, msgs, self):
                yield msg, last
            return

        async for msg, last in self._stream_query_after_preflight(
            msgs,
            request=request,
            query=query,
            session_id=session_id,
            preflight=preflight,
        ):
            yield msg, last

    async def query_handler(
        self,
        msgs,
        request: AgentRequest = None,
        **kwargs,
    ):
        """处理 Agent query，并保持 Runner 期望的流式输出格式。"""
        logger.debug(
            f"AgentRunner.query_handler called: agent_id={self.agent_id}, "
            f"msgs={msgs}, request={request}",
        )
        query = _get_last_user_text(msgs)
        session_id = getattr(request, "session_id", "") or ""
        user_id = getattr(request, "user_id", "") or ""

        async for msg, last in self._stream_query_entry(
            msgs,
            request=request,
            query=query,
            session_id=session_id,
            user_id=user_id,
        ):
            yield msg, last

    async def get_state_loaded(
        self,
        agent: SWEAgent,
        session_id: str | None,
        session_state_loaded: bool,
        skip_history: bool | Any,
        user_id: str | None,
    ) -> bool:
        # 对于 cron 任务，跳过会话历史加载（不读取旧历史）
        if skip_history:
            logger.info(
                "Cron task: skipping session state load (session_id=%s)",
                session_id,
            )
            session_state_loaded = True
        else:
            try:
                await self.session.load_session_state(
                    session_id=session_id,
                    user_id=user_id,
                    agent=agent,
                )
            except KeyError as e:
                logger.warning(
                    "load_session_state skipped (state schema mismatch): %s; "
                    "will save fresh state on completion to recover file",
                    e,
                )
            session_state_loaded = True
        return session_state_loaded

    async def _save_cron_session_state(
        self,
        agent: SWEAgent,
        session_id: str | None | Any,
        user_id: str | None,
        hook_overlay: HookSessionOverlay | None = None,
    ) -> None:
        """保存 cron 任务状态，保留旧历史并追加本轮新增消息。"""
        existing_state = await self.session.get_session_state_dict(
            session_id=session_id,
            user_id=user_id,
            allow_not_exist=True,
        )
        current_agent_state = agent.state_dict()
        (
            merged_state,
            existing_content,
            current_content,
            stripped_count,
        ) = _build_cron_merged_state(
            existing_state,
            current_agent_state,
            hook_overlay,
        )
        await self.session.save_merged_state(
            session_id,
            user_id=user_id,
            state=merged_state,
        )
        logger.info(
            "Cron task: saved merged session state "
            "(session_id=%s, existing_memory_content=%s, new_content=%s, "
            "stripped_internal_follow_ups=%s)",
            session_id,
            len(existing_content),
            len(current_content),
            stripped_count,
        )

    async def _save_legacy_session_state(
        self,
        agent: SWEAgent,
        session_id: str | None | Any,
        user_id: str | None,
        hook_overlay: HookSessionOverlay | None = None,
    ) -> None:
        """兼容不支持 save_merged_state 的旧 session 实现。"""
        await self.session.save_session_state(
            session_id=session_id,
            user_id=user_id,
            agent=agent,
        )
        if hook_overlay is None or not hasattr(
            self.session,
            "update_session_state",
        ):
            return

        await self.session.update_session_state(
            session_id,
            "hook_overlay",
            hook_overlay.model_dump(mode="json", by_alias=True),
            user_id=user_id,
        )

    async def _save_regular_session_state(
        self,
        agent: SWEAgent,
        session_id: str | None | Any,
        user_id: str | None,
        hook_overlay: HookSessionOverlay | None = None,
    ) -> None:
        """保存普通请求状态，并在落盘前剔除内部续跑提示。"""
        if not hasattr(agent, "state_dict") or not hasattr(
            self.session,
            "save_merged_state",
        ):
            await self._save_legacy_session_state(
                agent,
                session_id,
                user_id,
                hook_overlay,
            )
            return

        state_modules = {
            "agent": agent.state_dict(),
        }
        if hook_overlay is not None:
            state_modules["hook_overlay"] = hook_overlay.model_dump(
                mode="json",
                by_alias=True,
            )
        stripped_count = _strip_internal_follow_up_messages_from_state(
            state_modules["agent"],
        )
        await self.session.save_merged_state(
            session_id=session_id,
            user_id=user_id,
            state=state_modules,
        )
        logger.info(
            "Saved session state with stripped_internal_follow_ups=%s "
            "(session_id=%s)",
            stripped_count,
            session_id,
        )

    async def save_job_session_state(
        self,
        agent: SWEAgent,
        session_id: str | None | Any,
        skip_history: bool | Any,
        user_id: str | None,
        hook_overlay: HookSessionOverlay | None = None,
    ):
        """按请求类型保存 session state。"""
        if skip_history:
            await self._save_cron_session_state(
                agent,
                session_id,
                user_id,
                hook_overlay,
            )
            return

        await self._save_regular_session_state(
            agent,
            session_id,
            user_id,
            hook_overlay,
        )

    async def _cleanup_denied_session_memory(
        self,
        session_id: str,
        user_id: str,
        denial_response: "Msg | None" = None,
    ) -> None:
        """Clean up session memory after a tool-guard denial.

        In the deny path (no agent is created), this method:

        1. Removes the LLM denial explanation (the assistant message
           immediately following the last marked entry).
        2. Strips ``TOOL_GUARD_DENIED_MARK`` from all marks lists so
           the kept tool-call info becomes normal memory entries.
        3. Appends *denial_response* (e.g. "❌ Tool denied") to the
           persisted session memory.
        """
        if not hasattr(self, "session") or self.session is None:
            return

        path = self.session._get_save_path(  # pylint: disable=protected-access
            session_id,
            user_id,
        )
        if not Path(path).exists():
            return

        try:
            with open(
                path,
                "r",
                encoding="utf-8",
                errors="surrogatepass",
            ) as f:
                states = json.load(f)

            agent_state = states.get("agent", {})
            memory_state = agent_state.get("memory", {})
            content = memory_state.get("content", [])

            if not content:
                return

            def _is_marked(entry):
                return (
                    isinstance(entry, list)
                    and len(entry) >= 2
                    and isinstance(entry[1], list)
                    and TOOL_GUARD_DENIED_MARK in entry[1]
                )

            last_marked_idx = -1
            for i, entry in enumerate(content):
                if _is_marked(entry):
                    last_marked_idx = i

            modified = False

            if last_marked_idx >= 0 and last_marked_idx + 1 < len(content):
                next_entry = content[last_marked_idx + 1]
                if (
                    isinstance(next_entry, list)
                    and len(next_entry) >= 1
                    and isinstance(next_entry[0], dict)
                    and next_entry[0].get("role") == "assistant"
                ):
                    del content[last_marked_idx + 1]
                    modified = True

            for entry in content:
                if _is_marked(entry):
                    entry[1].remove(TOOL_GUARD_DENIED_MARK)
                    modified = True

            if denial_response is not None:
                ts = getattr(denial_response, "timestamp", None)
                msg_dict = {
                    "id": getattr(denial_response, "id", ""),
                    "name": getattr(denial_response, "name", "Friday"),
                    "role": getattr(denial_response, "role", "assistant"),
                    "content": denial_response.content,
                    "metadata": getattr(
                        denial_response,
                        "metadata",
                        None,
                    ),
                    "timestamp": str(ts) if ts is not None else "",
                }
                content.append([msg_dict, []])
                modified = True

            if modified:
                with open(
                    path,
                    "w",
                    encoding="utf-8",
                    errors="surrogatepass",
                ) as f:
                    json.dump(states, f, ensure_ascii=False)
                logger.info(
                    "Tool guard: cleaned up denied session memory in %s",
                    path,
                )
        except Exception:  # pylint: disable=broad-except
            logger.warning(
                "Failed to clean up denied messages from session %s",
                session_id,
                exc_info=True,
            )

    async def _enforce_query_timeout(
        self,
        msg_stream,
        session_id: str,
        agent=None,
        timeout_seconds: float = QUERY_TIMEOUT_SECONDS,
        run_key: str | None = None,
    ):
        """Wrap an async message stream with global wall-clock timeout.

        Iterates over *msg_stream* and yields each ``(msg, last)`` pair.
        If the total elapsed time since the first call exceeds
        *timeout_seconds*, a timeout notification message is yielded,
        the stream is terminated, and the agent is interrupted.

        Args:
            msg_stream: Async iterable of ``(msg, last)`` tuples.
            session_id: Session identifier for logging.
            agent: Agent instance to interrupt on timeout.
            timeout_seconds: Maximum wall-clock seconds for the entire
                query (default: ``QUERY_TIMEOUT_SECONDS``).

        Yields:
            ``(msg, last)`` tuples, with a final timeout notification if
            the limit is exceeded.
        """
        start = time.monotonic()
        async for msg, last in msg_stream:
            elapsed = time.monotonic() - start
            if elapsed > timeout_seconds:
                logger.warning(
                    "Query timeout (%.0fs > %.0fs) for session %s",
                    elapsed,
                    timeout_seconds,
                    session_id,
                )
                if run_key and self._task_tracker is not None:
                    mark_stopping = getattr(
                        self._task_tracker,
                        "mark_stopping",
                        None,
                    )
                    if mark_stopping is not None:
                        try:
                            await mark_stopping(run_key)
                        except Exception as status_err:
                            logger.warning(
                                "Failed to mark run stopping after query "
                                "timeout: %s",
                                status_err,
                            )

                # Interrupt the agent to stop it from continuing
                if agent is not None:
                    try:
                        await agent.interrupt()
                        logger.info(
                            "Agent interrupted after query timeout for "
                            "session %s",
                            session_id,
                        )
                    except Exception as interrupt_err:
                        logger.warning(
                            "Failed to interrupt agent on query timeout: "
                            "%s",
                            interrupt_err,
                        )
                yield (
                    Msg(
                        name="Friday",
                        role="assistant",
                        content=[
                            TextBlock(
                                type="text",
                                text=(
                                    f"⏰ 任务执行超时"
                                    f"（{int(elapsed)}s > "
                                    f"{int(timeout_seconds)}s），"
                                    f"已自动终止。"
                                ),
                            ),
                        ],
                    ),
                    True,
                )
                return
            yield msg, last

    async def stream_query(
        self,
        request,
        **kwargs,
    ) -> AsyncGenerator[Event, None]:
        """Wrap base streaming to normalize reasoning end boundaries."""
        async for event in normalize_reasoning_boundary_stream(
            super().stream_query(request, **kwargs),
        ):
            progress = None
            channel_meta = getattr(request, "channel_meta", None) or {}
            chat_id = channel_meta.get("chat_id")
            if not chat_id and self._chat_manager is not None:
                chat_id = await self._chat_manager.get_chat_id_by_session(
                    getattr(request, "session_id", "") or "",
                    getattr(request, "channel", DEFAULT_CHANNEL),
                )
            if chat_id and self._task_tracker is not None:
                progress = await self._task_tracker.get_task_progress(chat_id)
            yield attach_task_progress(event, progress)

    async def init_handler(self, *args, **kwargs):
        """
        Init handler.
        """
        # Load environment variables from .env file
        # env_path = Path(__file__).resolve().parents[4] / ".env"
        env_path = Path("./") / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            logger.debug(f"Loaded environment variables from {env_path}")
        else:
            logger.debug(
                f".env file not found at {env_path}, "
                "using existing environment variables",
            )

        session_dir = str(
            (self.workspace_dir if self.workspace_dir else WORKING_DIR)
            / "sessions",
        )
        self.session = SafeJSONSession(save_dir=session_dir)

    async def shutdown_handler(self, *args, **kwargs):
        """
        Shutdown handler.
        """

# -*- coding: utf-8 -*-
# pylint: disable=unused-argument too-many-branches too-many-statements
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable, Collection
from uuid import uuid4

import httpx
from agentscope.message import Msg, TextBlock
from agentscope.pipeline import stream_printing_messages
from agentscope_runtime.engine.runner import Runner
from agentscope_runtime.engine.schemas.agent_schemas import (
    AgentRequest,
    Event,
    Message,
)
from agentscope_runtime.engine.schemas.exception import AgentException
from dotenv import load_dotenv

from ..mcp.http_headers import build_mcp_http_headers
from ..mcp.stateful_client import HttpStatefulClient, StdIOStatefulClient
from ..mcp.stdio_launcher import build_tenant_aware_stdio_launch_config
from .command_dispatch import (
    _get_last_user_text,
    _is_command,
    run_command_path,
)
from .hidden_context_injection import (
    append_hidden_context_to_user_message,
)
from .model_call_error_detail import (
    MODEL_CALL_FAILED_MESSAGES_STATE_KEY,
    ModelCallFailureDetail,
    ModelCallFailedException,
    extract_model_call_failure_detail,
)
from .query_error_dump import write_query_error_dump
from .retry_classifier import is_query_retryable
from .session import SafeJSONSession, SESSION_SKILL_SNAPSHOT_STATE_KEY
from .stream_boundary import normalize_reasoning_boundary_stream
from .task_progress import attach_task_progress
from .utils import build_env_context
from ..identity_resolver import resolve_user_identity
from ..channels.schema import (
    DEFAULT_CHANNEL,
    SESSION_TITLE_COMMITTED_META_KEY,
)
from ...agents.react_agent import SWEAgent
from ...agents.skill_invocation_detector import SkillInvocationDetector
from ...agents.tool_guard_mixin import PreToolUseTerminalStop
from ...agents.skills_manager import (
    get_skill_freshness_token,
    get_workspace_skills_dir,
    resolve_effective_skill_dir,
    resolve_effective_skills,
)
from ...agents.hook_runtime import HookRuntime
from ...agents.hook_runtime.conversation_snapshot import (
    capture_conversation_snapshot,
)
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
from ...runtime_invocation_claims import runtime_invocation_claims_context
from ..source_system_config import is_chat_task_progress_enabled
from ..source_system_config.runtime import get_current_source_system_config

if TYPE_CHECKING:
    from ...agents.memory import BaseMemoryManager

logger = logging.getLogger(__name__)
TASK_RUNS_STATE_KEY = "task_runs"
_INTERNAL_FOLLOW_UP_METADATA_KEY = "swe_internal_follow_up"
_SKILL_FRESHNESS_NOTICE_METADATA_KEY = "swe_skill_freshness_notice"
_EXTERNAL_APPROVAL_MESSAGE_META_KEY = "external_approval_message"
_APPROVAL_REQUEST_ID_META_KEY = "approval_request_id"
_APPROVAL_DECISION_META_KEY = "approval_decision"
_SESSION_TITLE_GENERATED_META_KEY = "session_title_generated"
_TASK_SESSION_KIND = "task"
_STOP_FOLLOW_UP_REASON_TEMPLATE = (
    "Stop completion gate blocked stopping: {reason}\n"
    "Continue working until the gate can allow completion."
)
_STOP_INCOMPLETE_MESSAGE_TEMPLATE = (
    "任务未完成：Stop 完成门禁未通过。最新阻断原因：{reason}"
)
_SKILL_FRESHNESS_NOTICE_HEADER = (
    "[Skill freshness notice]\n"
    "The following previously associated skills changed for this turn. "
    "Treat current skill content as superseding earlier assumptions:\n"
)

_APPROVE_EXACT = frozenset(
    {
        "approve",
        "/approve",
        "/daemon approve",
    },
)
_MCP_HTTP_TIMEOUT_SECONDS = 240.0
_MCP_CONNECT_TIMEOUT_SECONDS = float(
    os.environ.get("SWE_MCP_CONNECT_TIMEOUT_SECONDS", "10"),
)
_MCP_CONNECT_MAX_CONCURRENT = max(
    1,
    int(os.environ.get("SWE_MCP_CONNECT_MAX_CONCURRENT", "4")),
)
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
class _QueryRuntimeInputs:
    """保存请求派生的运行时装配输入。"""

    session_id: str
    user_id: str
    channel: str
    skip_history: bool
    agent_config: Any
    tenant_hooks: HookConfig
    hook_overlay: HookSessionOverlay
    env_context: str
    selected_context_directives: list[str]
    auth_token: str | None
    passthrough_headers: dict[str, str]
    selected_skill_directives: list[Any] = field(default_factory=list)


@dataclass
class _QueryRuntimeResources:
    """保存已连接的请求资源和 hook 更新后的上下文。"""

    chat: Any
    turn_id: str
    env_context: str


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
    skip_history: bool
    pending_confirmed_skill_snapshots: dict[str, dict[str, Any]]
    selected_context_directives: list[str] = field(default_factory=list)


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
    """保存本轮 agent 调用需要的输入。"""

    original_user_message: str
    turn_msgs: list[Any]


@dataclass
class _QueryTurnOutcome:
    """记录 agent 输出与完成态。"""

    task_completed: bool = True
    assistant_response: str = ""
    stop_follow_up_turns: int = 0
    max_stop_turns: int = 0
    automatic_follow_up_turns: int = 0
    max_automatic_follow_up_turns: int = 0
    stop_hook_active: bool = False
    completion_blocked: bool = False
    completion_block_reason: str = ""
    completion_marked_incomplete: bool = False
    pre_tool_terminal_stop: bool = False


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


def _is_tool_guard_denied_entry(entry: Any) -> bool:
    return (
        isinstance(entry, list)
        and len(entry) >= 2
        and isinstance(entry[1], list)
        and TOOL_GUARD_DENIED_MARK in entry[1]
    )


def _get_agent_memory_content(states: dict[str, Any]) -> list[Any] | None:
    agent_state = states.get("agent", {})
    if not isinstance(agent_state, dict):
        return None

    memory_state = agent_state.get("memory", {})
    if not isinstance(memory_state, dict):
        return None

    content = memory_state.get("content", [])
    if not isinstance(content, list) or not content:
        return None
    return content


@dataclass(frozen=True)
class _PersistedMemorySnapshot:
    content: list[Any]


def _last_tool_guard_denied_index(content: list[Any]) -> int | None:
    for index in range(len(content) - 1, -1, -1):
        if _is_tool_guard_denied_entry(content[index]):
            return index
    return None


def _is_assistant_memory_entry(entry: Any) -> bool:
    return (
        isinstance(entry, list)
        and len(entry) >= 1
        and isinstance(entry[0], dict)
        and entry[0].get("role") == "assistant"
    )


def _remove_following_denial_explanation(
    content: list[Any],
    denied_entry_index: int | None,
) -> bool:
    if denied_entry_index is None:
        return False

    explanation_index = denied_entry_index + 1
    if explanation_index >= len(content):
        return False

    if not _is_assistant_memory_entry(content[explanation_index]):
        return False

    del content[explanation_index]
    return True


def _strip_tool_guard_denied_marks(content: list[Any]) -> int:
    stripped_count = 0
    for entry in content:
        if _is_tool_guard_denied_entry(entry):
            entry[1].remove(TOOL_GUARD_DENIED_MARK)
            stripped_count += 1
    return stripped_count


def _build_denial_response_memory_entry(
    denial_response: Msg,
) -> list[Any]:
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
    return [msg_dict, []]


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


def _approval_message_key_from_text(text: str) -> tuple[str, str] | None:
    normalized = " ".join(text.split())
    request_id = _approval_request_id(normalized)
    if request_id:
        return ("approve", request_id)

    request_id = _denial_request_id(normalized)
    if request_id:
        return ("deny", request_id)

    return None


def _external_approval_message_key(
    message: dict[str, Any],
) -> tuple[str, str] | None:
    if message.get("role") != "user":
        return None

    text_key = _approval_message_key_from_text(
        _extract_text_from_message_content(message.get("content")),
    )
    metadata = message.get("metadata")
    if not isinstance(metadata, dict):
        return text_key

    meta_decision = metadata.get(_APPROVAL_DECISION_META_KEY)
    if isinstance(meta_decision, str):
        meta_decision = meta_decision.strip().lower()
    if meta_decision not in {"approve", "deny"}:
        meta_decision = text_key[0] if text_key else None

    meta_request_id = metadata.get(_APPROVAL_REQUEST_ID_META_KEY)
    if isinstance(meta_request_id, str) and meta_request_id.strip():
        request_id = meta_request_id.strip().lower()
    else:
        request_id = text_key[1] if text_key else None

    if meta_decision and request_id:
        return (meta_decision, request_id)
    return text_key


def _dedupe_external_approval_messages_from_state(
    agent_state: dict[str, Any],
) -> int:
    memory_state = agent_state.get("memory")
    if not isinstance(memory_state, dict):
        return 0

    content = memory_state.get("content")
    if not isinstance(content, list):
        return 0

    external_keys: set[tuple[str, str]] = set()
    for entry in content:
        message = _extract_memory_entry_payload(entry)
        if not isinstance(message, dict):
            continue
        metadata = message.get("metadata")
        if not isinstance(metadata, dict):
            continue
        if not metadata.get(_EXTERNAL_APPROVAL_MESSAGE_META_KEY):
            continue
        key = _external_approval_message_key(message)
        if key is not None:
            external_keys.add(key)

    if not external_keys:
        return 0

    kept_entries: list[Any] = []
    removed = 0
    for entry in content:
        message = _extract_memory_entry_payload(entry)
        if not isinstance(message, dict):
            kept_entries.append(entry)
            continue

        metadata = message.get("metadata")
        is_external_entry = isinstance(metadata, dict) and bool(
            metadata.get(_EXTERNAL_APPROVAL_MESSAGE_META_KEY),
        )
        key = _external_approval_message_key(message)
        if key in external_keys and not is_external_entry:
            removed += 1
            continue
        kept_entries.append(entry)

    if removed:
        memory_state["content"] = kept_entries

    return removed


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


def _approved_tool_call_from_record(record) -> dict[str, Any] | None:
    """从审批记录恢复需要重放的工具调用和队列上下文。"""
    if not isinstance(record.extra, dict):
        return None
    candidate = record.extra.get("tool_call")
    if not isinstance(candidate, dict):
        return None

    approved_tool_call = dict(candidate)
    _copy_list_extra(
        approved_tool_call,
        record.extra,
        source_key="sibling_tool_calls",
        target_key="_sibling_tool_calls",
    )
    _copy_list_extra(
        approved_tool_call,
        record.extra,
        source_key="remaining_queue",
        target_key="_remaining_queue",
    )
    _copy_list_extra(
        approved_tool_call,
        record.extra,
        source_key="thinking_blocks",
        target_key="_thinking_blocks",
    )
    replay_metadata = _approval_replay_metadata(record)
    if replay_metadata is not None:
        approved_tool_call["_approval_replay"] = replay_metadata
    return approved_tool_call


def _copy_list_extra(
    target: dict[str, Any],
    extra: dict[str, Any],
    *,
    source_key: str,
    target_key: str,
) -> None:
    value = extra.get(source_key)
    if isinstance(value, list):
        target[target_key] = value


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
    session: Any | None,
    *,
    session_id: str,
    user_id: str,
    workspace_dir: Path,
    channel: str,
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
        overlay = HookSessionOverlay.model_validate(raw_overlay)
    except Exception:
        logger.warning("Invalid hook_overlay session state", exc_info=True)
        return HookSessionOverlay()
    return _reload_persisted_skill_hook_sources(
        overlay,
        workspace_dir=workspace_dir,
        channel=channel,
    )


def _reload_persisted_skill_hook_sources(
    overlay: HookSessionOverlay,
    *,
    workspace_dir: Path,
    channel: str,
) -> HookSessionOverlay:
    """Reload persisted skill hooks only when the skill remains enabled."""
    workspace = Path(workspace_dir)
    non_skill_entries = [
        entry
        for entry in overlay.entries
        if not entry.hook_id.startswith("skill:")
    ]
    state = HookSessionState(
        entries=non_skill_entries,
        once_executed=overlay.once_executed,
    )
    try:
        enabled_skills = set(resolve_effective_skills(workspace, channel))
    except Exception:
        logger.warning(
            "Failed to resolve enabled skills for persisted hook sources",
            exc_info=True,
        )
        return HookSessionOverlay.model_validate(
            state.model_dump(mode="json", by_alias=True),
        )

    for source in overlay.loaded_skill_sources:
        if source.skill_name not in enabled_skills:
            continue
        skill_root = resolve_effective_skill_dir(
            workspace,
            source.skill_name,
        )
        if skill_root is None:
            continue
        try:
            state = load_skill_hooks_for_session(
                skill_name=source.skill_name,
                skill_root=skill_root,
                workspace_dir=workspace,
                session_state=state,
            )
        except SkillHookLoadError as exc:
            logger.warning(
                "Discarded persisted hooks for skill '%s': %s",
                source.skill_name,
                exc,
            )

    available_skill_handler_ids = state.loaded_skill_handler_ids()
    refreshed_entries = [
        *non_skill_entries,
        *(
            entry
            for entry in overlay.entries
            if entry.hook_id in available_skill_handler_ids
        ),
    ]
    return HookSessionOverlay(
        loaded_skill_sources=state.loaded_skill_sources,
        entries=refreshed_entries,
        once_executed=overlay.once_executed,
    )


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
    skill_runtime_profiles: dict[str, Any] | None = None,
    get_hook_state: Callable[[], HookSessionState],
    set_hook_state: Callable[[HookSessionState], None],
    approved_http_urls: Collection[str] | None = None,
    confirmed_skill_callback: Callable[[str], Any] | None = None,
) -> SkillInvocationDetector:
    workspace = Path(workspace_dir)
    approvals = (
        set(approved_http_urls)
        if approved_http_urls is not None
        else _load_tenant_approved_skill_hook_http_urls(tenant_id)
    )

    async def _load_skill_hooks(skill_name: str) -> None:
        skill_root = resolve_effective_skill_dir(workspace, skill_name)
        if skill_root is None:
            return
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
        workspace_dir=workspace_dir,
        skill_hook_loader=_load_skill_hooks,
        confirmed_skill_callback=confirmed_skill_callback,
    )
    detector.set_enabled_skills(enabled_skills)
    if skill_runtime_profiles:
        detector.set_skill_runtime_profiles(skill_runtime_profiles)
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
        trace_id=getattr(request, "trace_id", None),
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
    agent: Any | None = None,
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

    async def _conversation_snapshot_provider():
        if agent is not None:
            return await capture_conversation_snapshot(
                getattr(agent, "memory", None),
            )
        return await _capture_persisted_runner_conversation_snapshot(
            request=request,
            runner=runner,
        )

    return await runtime.emit(
        context,
        workspace_dir=Path(runner.workspace_dir or WORKING_DIR),
        conversation_snapshot_provider=_conversation_snapshot_provider,
    )


async def _capture_persisted_runner_conversation_snapshot(
    *,
    request: Any,
    runner: "AgentRunner",
) -> dict[str, Any] | None:
    if getattr(request, "skip_history", False):
        return None

    session = getattr(runner, "session", None)
    get_session_state_dict = getattr(session, "get_session_state_dict", None)
    if not callable(get_session_state_dict):
        return None

    session_id = getattr(request, "session_id", None)
    if not session_id:
        return None

    try:
        state = await get_session_state_dict(
            session_id=_coerce_session_storage_id(session_id),
            user_id=_coerce_session_storage_user_id(
                getattr(request, "user_id", None),
            ),
            allow_not_exist=True,
        )
    except Exception:
        logger.debug(
            "Failed to load persisted memory for hook snapshot",
            exc_info=True,
        )
        return None

    if not isinstance(state, dict):
        return None

    content = _get_agent_memory_content(state)
    if content is None:
        return None

    return await capture_conversation_snapshot(
        _PersistedMemorySnapshot(content=content),
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
        from ..crons.model_slot_context import (
            get_current_model_slot_override,
        )
        from ...providers.provider_manager import ProviderManager

        override = get_current_model_slot_override()
        if override and override.provider_id and override.model:
            return f"{override.provider_id}/{override.model}"
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
    session_id: str | None = None,
    chat_id: str | None = None,
    trace_id: str | None = None,
) -> list[Any]:
    """Build and connect MCP clients from config for single request use.

    Args:
        mcp_config: MCP configuration from agent_config.mcp
        passthrough_headers: Headers to merge for HTTP transport clients
        session_id: Request-scoped session identifier for reserved headers
        chat_id: Persistent chat UUID for reserved transport claims
        trace_id: Request-scoped trace identifier for reserved headers

    Returns:
        List of connected MCP client instances (all created for this request)
    """
    started_at = time.perf_counter()
    if mcp_config is None or not mcp_config.clients:
        logger.debug(
            "mcp_client_connect_duration_ms=%d client_count=0",
            int((time.perf_counter() - started_at) * 1000),
        )
        return []

    enabled_configs = [
        (key, client_config)
        for key, client_config in mcp_config.clients.items()
        if client_config.enabled
    ]
    semaphore = asyncio.Semaphore(_MCP_CONNECT_MAX_CONCURRENT)

    async def _create_and_connect(
        key: str,
        client_config: MCPClientConfig,
    ) -> Any | None:
        client = None
        try:
            async with semaphore:
                client = await _create_mcp_client_with_headers(
                    client_config,
                    passthrough_headers,
                    session_id=session_id,
                    chat_id=chat_id,
                    trace_id=trace_id,
                )
                if client is None:
                    return None
                await asyncio.wait_for(
                    client.connect(timeout=_MCP_CONNECT_TIMEOUT_SECONDS),
                    timeout=_MCP_CONNECT_TIMEOUT_SECONDS,
                )
            logger.info("MCP client '%s' created and connected", key)
            return client
        except asyncio.CancelledError:
            # MCP 连接阶段的取消（如远端 502 导致），降级跳过而非取消整个查询
            logger.warning(
                f"MCP client '{key}' connection cancelled, skipping",
            )
        except TimeoutError:
            logger.warning(
                "MCP client '%s' connection timed out after %.1fs",
                key,
                _MCP_CONNECT_TIMEOUT_SECONDS,
            )
        except Exception as e:
            logger.warning(
                f"Failed to create MCP client '{key}': {e}",
                exc_info=True,
            )
        if client is not None:
            await _cleanup_mcp_clients([client])
        return None

    results = await asyncio.gather(
        *(
            _create_and_connect(key, client_config)
            for key, client_config in enabled_configs
        ),
    )
    clients = [client for client in results if client is not None]

    logger.debug(
        "mcp_client_connect_duration_ms=%d client_count=%d",
        int((time.perf_counter() - started_at) * 1000),
        len(clients),
    )
    return clients


async def _create_mcp_client_with_headers(
    client_config: MCPClientConfig,
    passthrough_headers: dict[str, str] | None = None,
    session_id: str | None = None,
    chat_id: str | None = None,
    trace_id: str | None = None,
) -> Any:
    """Create a single MCP client with optional header passthrough.

    For HTTP transport, merges static config headers with passthrough headers.
    For StdIO transport, uses static config directly.

    Args:
        client_config: Single MCP client configuration
        passthrough_headers: Headers to merge for HTTP transport
        session_id: Request-scoped session identifier for reserved headers
        chat_id: Persistent chat UUID for reserved transport claims
        trace_id: Request-scoped trace identifier for reserved headers

    Returns:
        MCP client instance (not yet connected)
    """
    rebuild_info = {
        "name": client_config.name,
        "transport": client_config.transport,
        "url": client_config.url,
        "headers": client_config.headers or None,
        "passthrough_headers": dict(passthrough_headers or {}) or None,
        "session_id": session_id,
        "chat_id": chat_id,
        "trace_id": trace_id,
        "timeout": _MCP_HTTP_TIMEOUT_SECONDS,
        "sse_read_timeout": _MCP_HTTP_SSE_READ_TIMEOUT_SECONDS,
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
            chat_id=chat_id,
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
    merged_headers = build_mcp_http_headers(
        client_config.headers,
        passthrough_headers=passthrough_headers,
        session_id=session_id,
        chat_id=chat_id,
        trace_id=trace_id,
    )

    client = HttpStatefulClient(
        name=client_config.name,
        transport=client_config.transport,
        url=client_config.url,
        headers=merged_headers,
        timeout=_MCP_HTTP_TIMEOUT_SECONDS,
        sse_read_timeout=_MCP_HTTP_SSE_READ_TIMEOUT_SECONDS,
    )

    setattr(
        client,
        "_swe_rebuild_info",
        {
            **rebuild_info,
            "_temp_client": True,
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


def _extract_assistant_response(
    agent: SWEAgent,
    *,
    memory_start: int = 0,
) -> str:
    """从 agent memory 的当前 turn 中提取最后的助手响应文本."""
    if not agent or not hasattr(agent, "memory"):
        return ""

    try:
        # memory.content 是 list of (Msg, marks) tuples
        memory = agent.memory.content
        for msg, _marks in reversed(memory[max(memory_start, 0) :]):
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


def _build_stop_follow_up_msg(reason: str) -> Msg:
    """构造 Stop 阻断后的内部续跑指令。"""
    return _build_internal_follow_up_msg(
        _STOP_FOLLOW_UP_REASON_TEMPLATE.format(
            reason=(reason or "Stop blocked completion").strip(),
        ),
    )


def _build_stop_incomplete_msg(reason: str) -> Msg:
    """构造自动续跑预算耗尽后的显式未完成消息。"""
    return Msg(
        name="Friday",
        role="assistant",
        content=_STOP_INCOMPLETE_MESSAGE_TEMPLATE.format(
            reason=(reason or "Stop blocked completion").strip(),
        ),
    )


def _normalize_session_skill_snapshot(
    snapshot: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(snapshot, dict):
        return {}

    normalized: dict[str, dict[str, Any]] = {}
    for skill_name, entry in snapshot.items():
        if isinstance(skill_name, str) and isinstance(entry, dict):
            normalized[skill_name] = dict(entry)
    return normalized


def _coerce_session_storage_id(session_id: str | None | Any) -> str:
    return "" if session_id is None else str(session_id)


def _coerce_session_storage_user_id(user_id: str | None) -> str:
    return user_id or ""


def _build_session_skill_snapshot_entry(
    *,
    skill_name: str,
    resolved_skill_dir: Path,
    freshness_token: Any,
    confirmed_at: Any | None = None,
) -> dict[str, Any]:
    entry = {
        "skill_name": skill_name,
        "resolved_skill_dir": str(resolved_skill_dir),
        "freshness_token": freshness_token,
    }
    if confirmed_at is not None:
        entry["confirmed_at"] = confirmed_at
    return entry


def _upsert_session_skill_snapshot_entry(
    snapshot: dict[str, dict[str, Any]],
    *,
    skill_name: str,
    resolved_skill_dir: Path,
    freshness_token: Any,
    confirmed_at: Any | None = None,
) -> None:
    snapshot[skill_name] = _build_session_skill_snapshot_entry(
        skill_name=skill_name,
        resolved_skill_dir=resolved_skill_dir,
        freshness_token=freshness_token,
        confirmed_at=confirmed_at,
    )


def _remove_session_skill_snapshot_entry(
    snapshot: dict[str, dict[str, Any]],
    *,
    skill_name: str,
) -> None:
    snapshot.pop(skill_name, None)


def _skill_freshness_notice_text(
    changes: list[str],
) -> str:
    return _SKILL_FRESHNESS_NOTICE_HEADER + "\n".join(
        f"- {item}" for item in changes
    )


def _build_skill_freshness_notice_msg(text: str) -> Msg:
    return Msg(
        name="system",
        role="system",
        content=[TextBlock(type="text", text=text)],
        metadata={
            _SKILL_FRESHNESS_NOTICE_METADATA_KEY: True,
        },
    )


@dataclass(frozen=True)
class _SkillFreshnessRefreshResult:
    notice_text: str | None = None
    stored_snapshot: dict[str, dict[str, Any]] | None = None
    refreshed_snapshot: dict[str, dict[str, Any]] | None = None


def _select_restorable_session_skill(
    snapshot: dict[str, dict[str, Any]] | None,
    *,
    enabled_skills: Collection[str],
) -> str | None:
    """从已持久化 snapshot 中选出可恢复的最近确认 skill。"""
    normalized = _normalize_session_skill_snapshot(snapshot)
    candidates = [
        entry
        for skill_name, entry in normalized.items()
        if skill_name in enabled_skills
    ]
    if not candidates:
        return None

    with_confirmed_at = [
        entry for entry in candidates if entry.get("confirmed_at") is not None
    ]
    if with_confirmed_at:
        chosen = max(
            with_confirmed_at,
            key=lambda item: float(item.get("confirmed_at") or 0.0),
        )
        return str(chosen.get("skill_name") or "") or None

    if len(candidates) == 1:
        return str(candidates[0].get("skill_name") or "") or None

    return None


def _can_restore_confirmed_session_skill_context(
    *,
    session_id: str | None,
    session_skill_detector: Any,
    session: Any,
) -> bool:
    """Return whether persisted skill context has the required capabilities."""
    return all(
        (
            session_id,
            session_skill_detector is not None,
            hasattr(session_skill_detector, "restore_confirmed_skill"),
            hasattr(session, "get_session_skill_snapshot"),
        ),
    )


def _supports_session_skill_freshness_refresh(
    *,
    session: Any,
    runtime: "_QueryRuntime",
) -> bool:
    if runtime.skip_history or session is None or not runtime.session_id:
        return False
    if not hasattr(runtime.agent, "get_effective_skills"):
        return False
    return all(
        hasattr(session, attr)
        for attr in (
            "get_session_skill_snapshot",
            "save_session_skill_snapshot",
        )
    )


def _refresh_switched_session_skill_snapshot_entry(
    next_snapshot: dict[str, dict[str, Any]],
    *,
    skill_name: str,
    stored_dir: Path,
    current_dir: Path | None,
    confirmed_at: Any | None = None,
) -> str | None:
    if (
        current_dir is None
        or not current_dir.exists()
        or current_dir == stored_dir
    ):
        return None

    current_token = get_skill_freshness_token(current_dir)
    _upsert_session_skill_snapshot_entry(
        next_snapshot,
        skill_name=skill_name,
        resolved_skill_dir=current_dir,
        freshness_token=current_token,
        confirmed_at=confirmed_at,
    )
    return (
        f"{skill_name}: detected skill-directory switch "
        f"{stored_dir} -> {current_dir}. Treat current skill "
        "content as superseding earlier assumptions. You MUST "
        f"re-read {current_dir / 'SKILL.md'} before relying on this skill."
    )


def _refresh_withdrawn_session_skill_snapshot_entry(
    next_snapshot: dict[str, dict[str, Any]],
    *,
    skill_name: str,
    current_dir: Path | None,
) -> str | None:
    if current_dir is not None and current_dir.exists():
        return None

    _remove_session_skill_snapshot_entry(
        next_snapshot,
        skill_name=skill_name,
    )
    return (
        f"{skill_name}: no longer effective for this turn. "
        "Stop relying on earlier assumptions from this skill."
    )


def _refresh_changed_session_skill_snapshot_entry(
    next_snapshot: dict[str, dict[str, Any]],
    *,
    skill_name: str,
    entry: dict[str, Any],
    current_dir: Path,
) -> str | None:
    current_token = get_skill_freshness_token(current_dir)
    if current_token == entry.get("freshness_token"):
        return None

    _upsert_session_skill_snapshot_entry(
        next_snapshot,
        skill_name=skill_name,
        resolved_skill_dir=current_dir,
        freshness_token=current_token,
        confirmed_at=entry.get("confirmed_at"),
    )
    return (
        f"{skill_name}: detected skill-directory change at "
        f"{current_dir}. Treat current skill content as "
        "superseding earlier assumptions. You MUST "
        f"re-read {current_dir / 'SKILL.md'} before relying on this skill."
    )


def _refresh_session_skill_snapshot_entry(
    next_snapshot: dict[str, dict[str, Any]],
    *,
    skill_name: str,
    entry: dict[str, Any],
    effective_skill_dirs: dict[str, Path],
) -> str | None:
    stored_dir = Path(str(entry.get("resolved_skill_dir", "")))
    current_dir = effective_skill_dirs.get(skill_name)

    switch_notice = _refresh_switched_session_skill_snapshot_entry(
        next_snapshot,
        skill_name=skill_name,
        stored_dir=stored_dir,
        current_dir=current_dir,
        confirmed_at=entry.get("confirmed_at"),
    )
    if switch_notice is not None:
        return switch_notice

    if not stored_dir.exists():
        _remove_session_skill_snapshot_entry(
            next_snapshot,
            skill_name=skill_name,
        )
        return None

    withdrawal_notice = _refresh_withdrawn_session_skill_snapshot_entry(
        next_snapshot,
        skill_name=skill_name,
        current_dir=current_dir,
    )
    if withdrawal_notice is not None:
        return withdrawal_notice

    assert current_dir is not None
    return _refresh_changed_session_skill_snapshot_entry(
        next_snapshot,
        skill_name=skill_name,
        entry=entry,
        current_dir=current_dir,
    )


def _refresh_session_skill_snapshot_entries(
    next_snapshot: dict[str, dict[str, Any]],
    *,
    stored_snapshot: dict[str, dict[str, Any]],
    effective_skill_dirs: dict[str, Path],
) -> list[str]:
    changes: list[str] = []
    for skill_name, entry in stored_snapshot.items():
        notice = _refresh_session_skill_snapshot_entry(
            next_snapshot,
            skill_name=skill_name,
            entry=entry,
            effective_skill_dirs=effective_skill_dirs,
        )
        if notice is not None:
            changes.append(notice)
    return changes


def _resolve_max_stop_turns(agent_config: Any) -> int:
    """解析 Stop 自动续跑上限，未配置时使用保守默认值。"""
    running_config = getattr(agent_config, "running", None)
    hook_runtime_config = getattr(running_config, "hook_runtime", None)
    configured_turns = getattr(
        hook_runtime_config,
        "max_stop_turns",
        None,
    )
    if configured_turns is None:
        configured_turns = getattr(
            running_config,
            "max_stop_turns",
            2,
        )
    stop_turns = 2 if configured_turns is None else configured_turns
    try:
        return max(int(stop_turns), 0)
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
    """Remove ephemeral system prompts before persisting session state."""
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
        if isinstance(metadata, dict) and (
            metadata.get(_INTERNAL_FOLLOW_UP_METADATA_KEY)
            or metadata.get(_SKILL_FRESHNESS_NOTICE_METADATA_KEY)
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


def _with_hook_context(
    env_context: str,
    hook_context: str,
) -> str:
    """追加 hook 上下文，避免主流程重复拼接同一段格式。"""
    if not hook_context:
        return env_context
    return f"{env_context}\n\n[Hook additional context]\n{hook_context}"


def _request_system_prompt_injections(request: AgentRequest) -> list[str]:
    channel_meta = getattr(request, "channel_meta", None) or {}
    value = getattr(request, "system_prompt_injections", None)
    if value is None and isinstance(channel_meta, dict):
        value = channel_meta.get("system_prompt_injections")
    return _normalize_system_prompt_injections(value)


def _request_selected_skill_names(request: AgentRequest) -> list[object]:
    channel_meta = getattr(request, "channel_meta", None) or {}
    value = getattr(request, "selected_skill_names", None)
    if value is None and isinstance(channel_meta, dict):
        value = channel_meta.get("selected_skill_names")
    return list(value) if isinstance(value, list) else []


def _request_context_references(request: AgentRequest) -> list[object]:
    """Read the Console's typed, one-turn context references."""
    channel_meta = getattr(request, "channel_meta", None) or {}
    value = getattr(request, "context_references", None)
    if value is None and isinstance(channel_meta, dict):
        value = channel_meta.get("context_references")
    return list(value) if isinstance(value, list) else []


def _request_file_url_network(request: AgentRequest) -> str:
    """从请求属性和 channel_meta 中读取静态文件访问网络。"""
    from ...config.context import normalize_file_url_network

    channel_meta = getattr(request, "channel_meta", None) or {}
    value = getattr(request, "file_url_network", None)
    if value is None and isinstance(channel_meta, dict):
        value = channel_meta.get("file_url_network")
    return normalize_file_url_network(value)


def _normalize_system_prompt_injections(value: Any) -> list[str]:
    from ..source_system_config.registry import (
        normalize_system_prompt_injections,
    )

    try:
        return normalize_system_prompt_injections(value)
    except ValueError:
        logger.warning(
            "Ignored invalid system_prompt_injections payload",
            exc_info=True,
        )
        return []


def _merge_system_prompt_injections(*sources: Any) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for source in sources:
        for item in _normalize_system_prompt_injections(source):
            if item in seen:
                continue
            seen.add(item)
            merged.append(item)
    return merged


def _with_system_prompt_injections(
    env_context: str,
    injections: list[str],
) -> str:
    if not injections:
        return env_context
    body = "\n\n".join(injections)
    return f"{env_context}\n\n[System prompt injections]\n{body}"


def _chat_name_from_messages(msgs: list[Any]) -> str:
    """从首条消息派生会话名，保持原有文本和媒体消息规则。"""
    if not msgs:
        return "New Chat"

    content = msgs[0].get_text_content()
    if content:
        return content[:10]
    return "Media Message"


def _should_generate_session_title(
    chat: Any,
    *,
    fallback_name: str,
) -> bool:
    """判断当前 chat 是否仍可由自动标题覆盖。

    只允许覆盖尚未生成过标题且仍保持默认/历史自动名称的会话，避免后续
    轮次或人工改名被后台任务再次覆盖。
    """
    if chat is None:
        return False

    meta = getattr(chat, "meta", None) or {}
    if meta.get("session_kind") == _TASK_SESSION_KIND:
        return False

    if meta.get(_SESSION_TITLE_GENERATED_META_KEY):
        return False

    current_name = (getattr(chat, "name", "") or "").strip()
    auto_names = {"New Chat", "新会话", fallback_name}
    return current_name in auto_names


def _clear_session_title_meta(request: AgentRequest) -> None:
    """清理通道标题元数据，避免跳过生成后仍向前端推送标题更新。"""
    channel_meta = getattr(request, "channel_meta", None)
    if not isinstance(channel_meta, dict):
        return
    channel_meta.pop("session_title", None)
    channel_meta.pop(SESSION_TITLE_COMMITTED_META_KEY, None)
    request.channel_meta = channel_meta


def _request_source_id(request: AgentRequest) -> str:
    """从请求属性和 channel_meta 中解析追踪或 hook 的来源标识。"""
    channel_meta = getattr(request, "channel_meta", None) or {}
    return getattr(request, "source_id", None) or channel_meta.get(
        "source_id",
        "default",
    )


def _request_approval_source_channel(
    request: Any | None,
) -> str | None:
    channel_meta = getattr(request, "channel_meta", None) or {}
    if not isinstance(channel_meta, dict):
        return None
    source_channel = channel_meta.get("approval_source_channel")
    if isinstance(source_channel, str) and source_channel.strip():
        return source_channel.strip()
    return None


def _external_approval_submission(record: Any) -> dict[str, Any] | None:
    extra = getattr(record, "extra", None)
    if not isinstance(extra, dict):
        return None
    submission = extra.get("external_submission")
    if not isinstance(submission, dict):
        return None
    return submission


def _request_matches_external_approval_submission(
    request: Any | None,
    submission: dict[str, Any],
) -> bool:
    submitted_channel = submission.get("source_channel")
    if not isinstance(submitted_channel, str) or not submitted_channel.strip():
        return False
    request_channel = _request_approval_source_channel(request)
    return request_channel == submitted_channel.strip()


def _request_user_name(request: AgentRequest) -> str | None:
    """按兼容顺序读取通道注入的用户名称。"""
    channel_meta = getattr(request, "channel_meta", None) or {}
    return (
        getattr(request, "user_name", None)
        or getattr(getattr(request, "state", None), "user_name", None)
        or channel_meta.get("user_name")
    )


def _request_bbk_id(request: AgentRequest) -> str | None:
    """按兼容顺序读取通道注入的 BBK 标识。"""
    channel_meta = getattr(request, "channel_meta", None) or {}
    return (
        getattr(request, "bbk_id", None)
        or getattr(getattr(request, "state", None), "bbk_id", None)
        or channel_meta.get("bbk_id")
    )


def _request_b3_trace_id(request: AgentRequest) -> str | None:
    channel_meta = getattr(request, "channel_meta", None) or {}
    trace_id = getattr(request, "b3_trace_id", None) or channel_meta.get(
        "b3_trace_id",
    )
    if not isinstance(trace_id, str):
        return None
    trace_id = trace_id.strip()
    return trace_id or None


def _request_passthrough_headers(request: AgentRequest) -> dict[str, str]:
    channel_meta = getattr(request, "channel_meta", None) or {}
    headers = getattr(request, "passthrough_headers", None)
    if headers is None and isinstance(channel_meta, dict):
        headers = channel_meta.get("passthrough_headers")
    if not isinstance(headers, dict):
        return {}

    normalized: dict[str, str] = {}
    for name, value in headers.items():
        if value is None:
            continue
        header_name = str(name).strip()
        header_value = str(value).strip()
        if header_name and header_value:
            normalized[header_name] = header_value
    return normalized


def _session_name_from_messages(msgs: list[Any]) -> str | None:
    """从第一条消息提取 trace 中展示的短会话名。"""
    if not msgs:
        return None

    content = msgs[0].get_text_content()
    if not content:
        return None
    return content[:10]


def _has_automatic_follow_up_budget(outcome: _QueryTurnOutcome) -> bool:
    """判断本请求级自动续跑总预算是否仍可消耗。"""
    return outcome.automatic_follow_up_turns < (
        outcome.max_automatic_follow_up_turns
    )


def _should_stop_follow_up(outcome: _QueryTurnOutcome) -> bool:
    """判断 Stop 阻断后是否允许再自动续跑一次。"""
    return bool(
        outcome.stop_follow_up_turns < outcome.max_stop_turns
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
    else:
        merged_state.pop("hook_overlay", None)

    task_run = _build_task_run_record(
        current_content,
        memory_start=len(existing_content),
    )
    if task_run is not None:
        task_runs = list(existing_state.get(TASK_RUNS_STATE_KEY, []) or [])
        task_runs.append(task_run)
        merged_state[TASK_RUNS_STATE_KEY] = task_runs

    return merged_state, existing_content, current_content, stripped_count


@dataclass
class _RetryState:
    """查询级重试过程中需要跨重试轮次保持的状态。"""

    agent: Any = None
    prev_agent: Any = None
    session_state_loaded: bool = False
    prev_session_state_loaded: bool = False
    task_completed: bool = False


@dataclass
class _QueryAttemptState:
    """记录当前 query 尝试的运行时对象与退出原因。"""

    runtime: _QueryRuntime | None = None
    runtime_start: _RuntimeStartResult | None = None
    session_state_loaded: bool = False
    should_return: bool = False
    succeeded: bool = False


@dataclass(frozen=True)
class _QueryAttemptInput:
    """封装单次 query 尝试不随执行过程变化的输入。"""

    request: AgentRequest
    msgs: list[Any]
    query: str | None
    preflight: _QueryPreflight
    trace_id: str | None


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
        self.session: Any | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._title_tasks: dict[str, asyncio.Task[Any]] = {}

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
        request: Any | None = None,
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

        external_submission = _external_approval_submission(pending)
        if external_submission is not None and (
            not _request_matches_external_approval_submission(
                request,
                external_submission,
            )
        ):
            source_channel = external_submission.get("source_channel")
            if not isinstance(source_channel, str) or not source_channel:
                source_channel = "external"
            return (
                Msg(
                    name="Friday",
                    role="assistant",
                    content=[
                        TextBlock(
                            type="text",
                            text=(
                                f"Approval request `{pending.request_id}` "
                                f"has already been submitted from "
                                f"`{source_channel}`. Refresh the session "
                                "to see the latest approval state."
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
            record = resolved or pending
            approved_tool_call = _approved_tool_call_from_record(record)
            await self._notify_console_approval_result(
                record,
                ApprovalDecision.APPROVED,
                request,
            )
            return None, True, approved_tool_call

        explicit_deny = _is_denial(normalized)
        denial_decision = (
            ApprovalDecision.DENIED
            if explicit_deny
            else ApprovalDecision.DENIED
        )
        resolved = await svc.resolve_request(
            pending.request_id,
            denial_decision,
        )
        await self._notify_console_approval_result(
            resolved or pending,
            denial_decision,
            request,
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

    async def _notify_console_approval_result(
        self,
        pending: Any,
        decision: ApprovalDecision,
        request: Any | None,
    ) -> None:
        from ..approvals.external import (
            CONSOLE_CHANNEL,
            ExternalApprovalDecision,
            notify_cron_approval_result,
        )

        source_channel = _request_approval_source_channel(request)
        if source_channel and source_channel != CONSOLE_CHANNEL:
            return

        workspace = getattr(self, "_workspace", None)
        if workspace is None:
            return

        external_decision = (
            ExternalApprovalDecision.APPROVE
            if decision == ApprovalDecision.APPROVED
            else ExternalApprovalDecision.DENY
        )
        try:
            await notify_cron_approval_result(
                workspace,
                pending,
                decision=external_decision,
                source_channel=source_channel or CONSOLE_CHANNEL,
            )
        except Exception:
            logger.exception(
                "zhaohu console approval result notification failed: "
                "request_id=%s",
                getattr(pending, "request_id", None),
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
        ) = await self._resolve_pending_approval(
            session_id,
            query,
            request=request,
        )
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
            workspace_dir=Path(self.workspace_dir or WORKING_DIR),
            channel=getattr(request, "channel", DEFAULT_CHANNEL),
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
        """启动 query 追踪；追踪不可用时只记录日志并继续主流程。

        默认情况下，query 请求总是创建新的 trace。
        只有显式声明要续接外部 trace 时，才会使用 attach_existing 模式。
        """
        if not has_trace_manager():
            return None

        try:
            trace_mgr = get_trace_manager()
            if not trace_mgr.enabled:
                return None
            existing_trace_id = getattr(request, "trace_id", None)
            attach_existing = self._should_attach_existing_trace(request)
            b3_trace_id = _request_b3_trace_id(request)
            resolved_identity = await resolve_user_identity(
                tenant_id=getattr(request, "user_id", None),
                source_id=_request_source_id(request),
                user_name=_request_user_name(request),
                bbk_id=_request_bbk_id(request),
                allow_remote_lookup=False,
            )
            trace_id = await trace_mgr.start_trace(
                user_id=getattr(request, "user_id", "") or "",
                session_id=getattr(request, "session_id", "") or "",
                channel=getattr(request, "channel", DEFAULT_CHANNEL),
                source_id=_request_source_id(request),
                user_message=_get_last_user_text(msgs),
                user_name=resolved_identity.user_name,
                bbk_id=resolved_identity.bbk_id,
                session_name=_session_name_from_messages(msgs),
                trace_id=(
                    existing_trace_id if attach_existing else b3_trace_id
                ),
                attach_existing=attach_existing,
                b3_trace_id=b3_trace_id,
            )
            if trace_id:
                # 通道层负责把事件发给前端，这里写回 request 让 SSE 能透传 trace_id。
                setattr(request, "trace_id", trace_id)

            return trace_id
        except Exception as e:
            logger.warning("Failed to start trace: %s", e)
            return None

    @staticmethod
    def _should_attach_existing_trace(request: AgentRequest) -> bool:
        """判断当前请求是否显式要求续接已有 trace。"""
        trace_id = getattr(request, "trace_id", None)
        if not trace_id:
            return False

        if bool(getattr(request, "trace_attach_existing", False)):
            return True

        channel_meta = getattr(request, "channel_meta", None) or {}
        return bool(channel_meta.get("trace_attach_existing"))

    async def _generate_session_title_before_stream(
        self,
        *,
        request: AgentRequest,
        chat: Any,
        msgs: list[Any],
        trace_id: str | None,
    ) -> None:
        """生成标题并写回会话元数据，供通道以独立事件刷新。"""
        if not trace_id:
            return

        chat_meta = getattr(chat, "meta", None) or {}
        if chat_meta.get("session_kind") == _TASK_SESSION_KIND:
            _clear_session_title_meta(request)
            return

        fallback_name = _chat_name_from_messages(msgs)
        channel_meta = getattr(request, "channel_meta", None) or {}
        existing_title = channel_meta.get("session_title")
        if existing_title:
            await self._persist_session_title(
                request=request,
                title=str(existing_title),
                trace_id=trace_id,
                chat_id=getattr(chat, "id", None),
                fallback_name=fallback_name,
            )
            return

        user_question = _get_last_user_text(msgs)
        if not user_question or not user_question.strip():
            return

        if not _should_generate_session_title(
            chat,
            fallback_name=fallback_name,
        ):
            return

        await self._generate_and_update_title(
            request=request,
            user_question=user_question,
            trace_id=trace_id,
            chat_id=getattr(chat, "id", None),
            fallback_name=fallback_name,
        )

    async def _persist_session_title(
        self,
        *,
        request: AgentRequest,
        title: str,
        trace_id: str,
        chat_id: str | None = None,
        fallback_name: str | None = None,
    ) -> None:
        """把已确定的标题写回 chat、trace 和 SSE 元数据。"""
        channel_meta = getattr(request, "channel_meta", None) or {}
        resolved_chat_id = chat_id or channel_meta.get("chat_id")
        if resolved_chat_id:
            channel_meta["chat_id"] = resolved_chat_id

        persisted = False
        if not resolved_chat_id:
            logger.warning("跳过会话标题刷新：缺少 chat_id")
        elif self._chat_manager is None:
            logger.warning(
                "跳过会话标题刷新：ChatManager 不可用 chat_id=%s",
                resolved_chat_id,
            )
        else:
            try:
                if fallback_name is None:
                    persisted = await self._chat_manager.update_chat_name(
                        resolved_chat_id,
                        title,
                        meta={
                            _SESSION_TITLE_GENERATED_META_KEY: True,
                        },
                    )
                else:
                    persisted = (
                        await self._chat_manager.update_generated_chat_title(
                            resolved_chat_id,
                            title,
                            fallback_name=fallback_name,
                        )
                    )
            except Exception:
                logger.warning(
                    "更新 chats.json 标题失败 chat_id=%s",
                    resolved_chat_id,
                    exc_info=True,
                )
            if not persisted:
                logger.warning(
                    "更新 chats.json 标题未命中 chat_id=%s",
                    resolved_chat_id,
                )

        if not persisted:
            channel_meta.pop("session_title", None)
            channel_meta.pop(SESSION_TITLE_COMMITTED_META_KEY, None)
            request.channel_meta = channel_meta
            return

        if has_trace_manager():
            try:
                trace_mgr = get_trace_manager()
                await trace_mgr.update_session_name(trace_id, title)
            except Exception:
                logger.warning(
                    "更新 tracing 会话标题失败 trace_id=%s",
                    trace_id,
                    exc_info=True,
                )

        channel_meta["session_title"] = title
        channel_meta[SESSION_TITLE_COMMITTED_META_KEY] = True
        request.channel_meta = channel_meta

    async def _generate_and_update_title(
        self,
        request: AgentRequest,
        user_question: str | None,
        trace_id: str,
        chat_id: str | None = None,
        fallback_name: str | None = None,
    ) -> None:
        """生成会话标题并更新存储。

        调用外部标题 API，成功后更新 chats.json、MySQL 和 channel_meta；
        失败只记日志，不修改任何数据。
        """
        if not user_question or not user_question.strip():
            return

        try:
            from ..title_generator import generate_title

            title = await generate_title(user_question)
            if not title:
                return

            await self._persist_session_title(
                request=request,
                title=title,
                trace_id=trace_id,
                chat_id=chat_id,
                fallback_name=fallback_name,
            )

            logger.info(
                "会话标题已更新: trace_id=%s title=%s",
                trace_id,
                title,
            )
        except Exception:
            logger.warning(
                "异步标题生成失败 trace_id=%s",
                trace_id,
                exc_info=True,
            )

    def _schedule_session_title_generation(
        self,
        *,
        request: AgentRequest,
        chat: Any,
        msgs: list[Any],
        trace_id: str | None,
    ) -> None:
        """启动受管标题任务，不阻塞 Agent 首答路径。"""
        if not trace_id:
            return

        chat_id = getattr(chat, "id", None)
        if not chat_id:
            return
        existing_task = self._title_tasks.get(chat_id)
        if existing_task is not None and not existing_task.done():
            return

        task = asyncio.create_task(
            self._generate_session_title_before_stream(
                request=request,
                chat=chat,
                msgs=msgs,
                trace_id=trace_id,
            ),
            name=f"session-title:{chat_id}",
        )
        self._background_tasks.add(task)
        self._title_tasks[chat_id] = task
        setattr(request, "_session_title_task", task)

        def _discard_completed_task(completed_task: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(completed_task)
            if self._title_tasks.get(chat_id) is completed_task:
                self._title_tasks.pop(chat_id, None)
            try:
                completed_task.exception()
            except asyncio.CancelledError:
                logger.debug("Session title task cancelled")
            except Exception:
                logger.warning("Session title task failed", exc_info=True)

        task.add_done_callback(_discard_completed_task)

    @staticmethod
    def _attach_trace_id_to_event(event: Any, trace_id: str | None) -> Any:
        """把当前轮次 trace_id 附加到消息元数据，便于前端关联反馈。"""
        if not trace_id or not isinstance(event, Message):
            return event

        metadata = event.metadata
        if isinstance(metadata, dict):
            if metadata.get("trace_id"):
                return event
            event.metadata = {**metadata, "trace_id": trace_id}
        else:
            event.metadata = {"trace_id": trace_id}
        return event

    @staticmethod
    def _attach_trace_id_to_msg(msg: Any, trace_id: str | None) -> Any:
        """在适配 Runtime Message 前，把 trace_id 写入 AgentScope 消息。"""
        if not trace_id or not isinstance(msg, Msg):
            return msg

        metadata = getattr(msg, "metadata", None)
        if isinstance(metadata, dict):
            if metadata.get("trace_id"):
                return msg
            msg.metadata = {**metadata, "trace_id": trace_id}
        else:
            msg.metadata = {"trace_id": trace_id}
        return msg

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
            "source_id": _request_source_id(request),
            "user_name": _request_user_name(request),
            "bbk_id": _request_bbk_id(request),
            "trace_id": getattr(request, "trace_id", None),
            "channel_manager": getattr(
                getattr(self, "_workspace", None),
                "channel_manager",
                None,
            ),
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
        from ..source_tools.service import get_source_tool_service

        source_tool_versions = ()
        source_tool_service = get_source_tool_service()
        if source_tool_service is not None and request_context["source_id"]:
            try:
                source_tool_versions = source_tool_service.get_active_catalog(
                    request_context["source_id"],
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Source tool catalogue unavailable; source tools fail closed",
                )
        return SWEAgent(
            agent_config=agent_config,
            env_context=env_context,
            mcp_clients=mcp_clients,
            memory_manager=self.memory_manager,
            request_context=request_context,
            workspace_dir=self.workspace_dir,
            task_tracker=self._task_tracker,
            source_tool_versions=source_tool_versions,
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

        async def _queue_confirmed_skill_snapshot_update(
            skill_name: str,
        ) -> None:
            if (
                self.session is None
                or not runtime.session_id
                or not hasattr(self.session, "get_session_skill_snapshot")
                or not hasattr(self.session, "save_session_skill_snapshot")
            ):
                return

            skill_dir = resolve_effective_skill_dir(
                Path(self.workspace_dir or WORKING_DIR),
                skill_name,
            )
            if skill_dir is None or not skill_dir.exists():
                return

            runtime.pending_confirmed_skill_snapshots[skill_name] = (
                _build_session_skill_snapshot_entry(
                    skill_name=skill_name,
                    resolved_skill_dir=skill_dir,
                    freshness_token=get_skill_freshness_token(skill_dir),
                    confirmed_at=time.time(),
                )
            )

        source_id_for_hooks = _request_source_id(request)
        runtime.session_skill_detector = _create_session_skill_detector(
            workspace_dir=Path(self.workspace_dir or WORKING_DIR),
            tenant_id=self.tenant_id,
            user_id=runtime.user_id,
            session_id=runtime.session_id,
            channel=runtime.channel,
            source_id=source_id_for_hooks,
            enabled_skills=(
                runtime.agent.get_runtime_skills()
                if hasattr(runtime.agent, "get_runtime_skills")
                else []
            ),
            skill_runtime_profiles=(
                runtime.agent.get_skill_runtime_profiles()
                if hasattr(runtime.agent, "get_skill_runtime_profiles")
                else {}
            ),
            get_hook_state=_get_session_hook_state,
            set_hook_state=_set_session_hook_state,
            confirmed_skill_callback=(_queue_confirmed_skill_snapshot_update),
        )
        if not hasattr(runtime.agent, "_request_context"):
            runtime.agent._request_context = {}
        runtime.agent._request_context["_skill_invocation_detector"] = (
            runtime.session_skill_detector
        )
        trace_id = getattr(request, "trace_id", None)
        if trace_id and has_trace_manager():
            try:
                trace_mgr = get_trace_manager()
                runtime.session_skill_detector.set_tracing_context(
                    trace_mgr,
                    trace_id,
                    runtime.user_id,
                    runtime.session_id,
                    runtime.channel,
                    source_id_for_hooks,
                )
                from ...tracing import get_current_trace

                trace_ctx = get_current_trace()
                if trace_ctx and trace_ctx.trace_id == trace_id:
                    trace_ctx.set_skill_detector(
                        runtime.session_skill_detector,
                        (
                            runtime.agent.get_runtime_skills()
                            if hasattr(runtime.agent, "get_runtime_skills")
                            else (
                                runtime.agent.get_effective_skills()
                                if hasattr(
                                    runtime.agent,
                                    "get_effective_skills",
                                )
                                else []
                            )
                        ),
                    )
            except Exception:
                logger.warning(
                    "Failed to attach tracing context to session skill detector",
                    exc_info=True,
                )

    def _rebind_trace_skill_detector_if_needed(
        self,
        *,
        runtime: _QueryRuntime,
        trace_id: str | None,
    ) -> None:
        """确保 trace context 与会话级 detector 收敛到同一实例。"""
        if trace_id is None or runtime.session_skill_detector is None:
            return

        from ...tracing import get_current_trace

        trace_ctx = get_current_trace()
        if trace_ctx is None or trace_ctx.trace_id != trace_id:
            return
        if trace_ctx.skill_detector is runtime.session_skill_detector:
            return

        previous_detector = trace_ctx.skill_detector
        enabled_skills = (
            runtime.agent.get_runtime_skills()
            if hasattr(runtime.agent, "get_runtime_skills")
            else (
                runtime.agent.get_effective_skills()
                if hasattr(runtime.agent, "get_effective_skills")
                else []
            )
        )
        trace_ctx.set_skill_detector(
            runtime.session_skill_detector,
            enabled_skills,
        )

    async def _refresh_session_skill_freshness(
        self,
        *,
        runtime: _QueryRuntime,
    ) -> _SkillFreshnessRefreshResult:
        if not _supports_session_skill_freshness_refresh(
            session=self.session,
            runtime=runtime,
        ):
            return _SkillFreshnessRefreshResult()

        stored_snapshot = _normalize_session_skill_snapshot(
            await self.session.get_session_skill_snapshot(
                session_id=runtime.session_id,
                user_id=runtime.user_id,
                allow_not_exist=True,
            ),
        )
        if not stored_snapshot:
            return _SkillFreshnessRefreshResult(
                stored_snapshot={},
                refreshed_snapshot={},
            )

        workspace_dir = Path(self.workspace_dir or WORKING_DIR)
        effective_skill_dirs = {
            skill_name: resolved_effective_skill_dir
            for skill_name in runtime.agent.get_effective_skills()
            if (
                resolved_effective_skill_dir := resolve_effective_skill_dir(
                    workspace_dir,
                    skill_name,
                )
            )
            is not None
        }

        next_snapshot = _normalize_session_skill_snapshot(stored_snapshot)
        changes = _refresh_session_skill_snapshot_entries(
            next_snapshot,
            stored_snapshot=stored_snapshot,
            effective_skill_dirs=effective_skill_dirs,
        )

        notice_text = (
            _skill_freshness_notice_text(changes) if changes else None
        )
        return _SkillFreshnessRefreshResult(
            notice_text=notice_text,
            stored_snapshot=stored_snapshot,
            refreshed_snapshot=next_snapshot,
        )

    async def _build_skill_snapshot_to_persist(
        self,
        *,
        runtime: _QueryRuntime,
        refresh_result: _SkillFreshnessRefreshResult,
    ) -> dict[str, dict[str, Any]] | None:
        if (
            runtime.skip_history
            or self.session is None
            or not runtime.session_id
            or refresh_result.stored_snapshot is None
            or refresh_result.refreshed_snapshot is None
        ):
            return None

        next_snapshot = _normalize_session_skill_snapshot(
            refresh_result.refreshed_snapshot,
        )
        if runtime.pending_confirmed_skill_snapshots:
            next_snapshot.update(
                _normalize_session_skill_snapshot(
                    runtime.pending_confirmed_skill_snapshots,
                ),
            )

        if next_snapshot != refresh_result.stored_snapshot:
            return next_snapshot
        return None

    async def _restore_confirmed_session_skill_context(
        self,
        *,
        runtime: _QueryRuntime,
    ) -> None:
        """从持久化的 session snapshot 恢复一次性 skill 续接候选。"""
        detector = getattr(runtime, "session_skill_detector", None)
        if runtime.skip_history or self.session is None:
            return
        if not _can_restore_confirmed_session_skill_context(
            session_id=runtime.session_id,
            session_skill_detector=detector,
            session=self.session,
        ):
            return

        stored_snapshot = _normalize_session_skill_snapshot(
            await self.session.get_session_skill_snapshot(
                session_id=runtime.session_id,
                user_id=runtime.user_id,
                allow_not_exist=True,
            ),
        )
        skill_name = _select_restorable_session_skill(
            stored_snapshot,
            enabled_skills=(
                runtime.agent.get_runtime_skills()
                if hasattr(runtime.agent, "get_runtime_skills")
                else (
                    runtime.agent.get_effective_skills()
                    if hasattr(runtime.agent, "get_effective_skills")
                    else []
                )
            ),
        )
        if not skill_name:
            return

        restored = detector.restore_confirmed_skill(
            skill_name,
            allow_one_shot_continuation=True,
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
        inputs = await self._build_query_runtime_inputs(
            request=request,
            msgs=msgs,
            preflight=preflight,
        )
        mcp_clients: list[Any] = []
        try:
            resources, block_result = (
                await self._start_query_runtime_resources(
                    request=request,
                    msgs=msgs,
                    inputs=inputs,
                    mcp_clients=mcp_clients,
                )
            )
            if block_result is not None:
                return block_result
            runtime = await self._finalize_query_runtime(
                request=request,
                query=query,
                msgs=msgs,
                preflight=preflight,
                inputs=inputs,
                resources=resources,
                mcp_clients=mcp_clients,
            )
            return _RuntimeStartResult(runtime=runtime)
        except Exception:
            if mcp_clients:
                await _cleanup_mcp_clients(mcp_clients)
            raise

    async def _build_query_runtime_inputs(
        self,
        *,
        request: AgentRequest,
        msgs: list[Any],
        preflight: _QueryPreflight,
    ) -> _QueryRuntimeInputs:
        """Resolve request values needed before connecting runtime resources."""
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
        env_context = _with_hook_context(
            build_env_context(
                session_id=session_id,
                user_id=user_id,
                channel=channel,
                working_dir=str(self.workspace_dir or WORKING_DIR),
                source_id=_request_source_id(request),
                user_name=_request_user_name(request),
            ),
            preflight.hook_additional_context,
        )
        from ..source_system_config.runtime import (
            get_system_prompt_injections,
        )

        agent_config = (
            preflight.agent_config
            if preflight.agent_config is not None
            else load_agent_config(
                self.agent_id,
                tenant_id=self.tenant_id,
            )
        )
        passthrough_headers = dict[str, str](
            get_current_passthrough_headers() or {},
        )
        passthrough_headers.update(_request_passthrough_headers(request))
        cookie_header = getattr(request, "cookie", None)
        if cookie_header:
            passthrough_headers["cookie"] = cookie_header
        return _QueryRuntimeInputs(
            session_id=session_id,
            user_id=user_id,
            channel=channel,
            skip_history=skip_history,
            agent_config=agent_config,
            tenant_hooks=(
                preflight.tenant_hooks
                if preflight.tenant_hooks is not None
                else _load_tenant_hook_config(self.tenant_id)
            ),
            hook_overlay=(
                preflight.hook_overlay
                if preflight.hook_overlay is not None
                else HookSessionOverlay()
            ),
            env_context=_with_system_prompt_injections(
                env_context,
                _merge_system_prompt_injections(
                    get_system_prompt_injections(),
                    _request_system_prompt_injections(request),
                ),
            ),
            selected_context_directives=[],
            selected_skill_directives=[],
            auth_token=getattr(request, "auth_token", None),
            passthrough_headers=passthrough_headers,
        )

    async def _start_query_runtime_resources(
        self,
        *,
        request: AgentRequest,
        msgs: list[Any],
        inputs: _QueryRuntimeInputs,
        mcp_clients: list[Any],
    ) -> tuple[_QueryRuntimeResources, _RuntimeStartResult | None]:
        """Connect request resources and run the session-start hook."""
        turn_id = f"turn-{uuid4().hex}"
        chat = await self._get_or_create_chat(
            session_id=inputs.session_id,
            user_id=inputs.user_id,
            channel=inputs.channel,
            name=_chat_name_from_messages(msgs),
            request=request,
            turn_id=turn_id,
        )
        with runtime_invocation_claims_context(
            chat_id=chat.id if chat is not None else None,
        ):
            from .context_references import build_context_reference_directives
            from .skill_selection import (
                SkillUseDirective,
                build_skill_use_directives,
            )

            context_reference_directives = (
                await build_context_reference_directives(
                    workspace_dir=Path(self.workspace_dir or WORKING_DIR),
                    channel=inputs.channel,
                    agent_config=inputs.agent_config,
                    references=_request_context_references(request),
                )
            )
            selected_context_skill_names = {
                directive.name
                for directive in context_reference_directives
                if isinstance(directive, SkillUseDirective)
            }
            selected_skill_directives = build_skill_use_directives(
                workspace_dir=Path(self.workspace_dir or WORKING_DIR),
                channel=inputs.channel,
                selected_skill_names=[
                    name
                    for name in _request_selected_skill_names(request)
                    if name not in selected_context_skill_names
                ],
            )
            all_context_directives = [
                *selected_skill_directives,
                *context_reference_directives,
            ]
            inputs.selected_skill_directives = [
                directive
                for directive in all_context_directives
                if isinstance(directive, SkillUseDirective)
            ]
            inputs.selected_context_directives = [
                directive.render() for directive in all_context_directives
            ]
        mcp_clients.extend(
            await _build_and_connect_mcp_clients(
                inputs.agent_config.mcp,
                passthrough_headers=inputs.passthrough_headers or None,
                session_id=inputs.session_id,
                chat_id=chat.id if chat is not None else None,
                trace_id=getattr(request, "trace_id", None),
            ),
        )
        env_context, block_response = await self._emit_session_start_hook(
            request=request,
            tenant_hooks=inputs.tenant_hooks,
            agent_config=inputs.agent_config,
            hook_overlay=inputs.hook_overlay,
            skip_history=inputs.skip_history,
            env_context=inputs.env_context,
        )
        resources = _QueryRuntimeResources(
            chat=chat,
            turn_id=turn_id,
            env_context=env_context,
        )
        if block_response is None:
            inputs.hook_overlay = await self._load_selected_skill_hooks(
                inputs=inputs,
            )
            self._schedule_session_title_generation(
                request=request,
                chat=chat,
                msgs=msgs,
                trace_id=getattr(request, "trace_id", None),
            )
            return resources, None
        return resources, _RuntimeStartResult(
            block_response=block_response,
            blocked_chat=chat,
            blocked_mcp_clients=mcp_clients,
            blocked_session_id=inputs.session_id,
        )

    async def _load_selected_skill_hooks(
        self,
        *,
        inputs: _QueryRuntimeInputs,
    ) -> HookSessionOverlay:
        """Load validated selected skill hooks after startup hooks complete."""
        state: HookSessionState = inputs.hook_overlay
        workspace = Path(self.workspace_dir or WORKING_DIR)
        approvals = _load_tenant_approved_skill_hook_http_urls(self.tenant_id)

        for directive in inputs.selected_skill_directives:
            try:
                next_state = load_skill_hooks_for_session(
                    skill_name=directive.name,
                    skill_root=directive.path.parent,
                    workspace_dir=workspace,
                    session_state=state,
                    approved_http_urls=approvals,
                )
            except SkillHookLoadError as exc:
                logger.warning(
                    "Rejected hooks for explicitly selected skill '%s': %s",
                    directive.name,
                    exc,
                )
                continue
            state = next_state

        return HookSessionOverlay.model_validate(
            state.model_dump(mode="json", by_alias=True),
        )

    async def _finalize_query_runtime(
        self,
        *,
        request: AgentRequest,
        query: str | None,
        msgs: list[Any],
        preflight: _QueryPreflight,
        inputs: _QueryRuntimeInputs,
        resources: _QueryRuntimeResources,
        mcp_clients: list[Any],
    ) -> _QueryRuntime:
        """Create the agent and initialize session-skill state for one turn."""
        agent_build_started_at = time.perf_counter()
        agent = self._create_agent_for_query(
            agent_config=inputs.agent_config,
            env_context=resources.env_context,
            mcp_clients=mcp_clients,
            request=request,
            session_id=inputs.session_id,
            user_id=inputs.user_id,
            channel=inputs.channel,
            chat=resources.chat,
            turn_id=resources.turn_id,
            hook_overlay=inputs.hook_overlay,
            auth_token=inputs.auth_token,
            approved_tool_call=preflight.approved_tool_call,
        )
        await agent.register_mcp_clients()
        agent.set_console_output_enabled(enabled=False)
        logger.debug(
            "swe_agent_build_duration_ms=%d agent_id=%s tenant_id=%s "
            "mcp_client_count=%d",
            int((time.perf_counter() - agent_build_started_at) * 1000),
            self.agent_id,
            self.tenant_id,
            len(mcp_clients),
        )
        runtime = _QueryRuntime(
            agent=agent,
            agent_config=inputs.agent_config,
            tenant_hooks=inputs.tenant_hooks,
            hook_overlay=inputs.hook_overlay,
            chat=resources.chat,
            session_skill_detector=None,
            mcp_clients=mcp_clients,
            session_id=inputs.session_id,
            user_id=inputs.user_id,
            channel=inputs.channel,
            skip_history=inputs.skip_history,
            pending_confirmed_skill_snapshots={},
            selected_context_directives=inputs.selected_context_directives,
        )
        self._attach_session_skill_detector(runtime=runtime, request=request)
        return runtime

    async def _build_turn_plan(
        self,
        *,
        runtime: _QueryRuntime,
        request: AgentRequest,
        msgs: list[Any],
        query: str | None,
    ) -> _TurnPlan:
        """根据普通请求构建本轮输入。"""
        del request
        original_user_message = query or _get_last_user_text(msgs) or ""
        turn_msgs = list(msgs)
        if (
            turn_msgs
            and runtime.selected_context_directives
            and getattr(turn_msgs[-1], "role", None) == "user"
        ):
            turn_msgs[-1] = append_hidden_context_to_user_message(
                turn_msgs[-1],
                runtime.selected_context_directives,
            )
        return _TurnPlan(
            original_user_message=original_user_message,
            turn_msgs=turn_msgs,
        )

    async def _stream_agent_turns(
        self,
        *,
        runtime: _QueryRuntime,
        plan: _TurnPlan,
        outcome: _QueryTurnOutcome,
    ):
        """流式执行当前 agent turn。"""
        turn_msgs = plan.turn_msgs
        outcome.assistant_response = ""
        memory_start = len(getattr(runtime.agent.memory, "content", []))
        stop_turns = _resolve_max_stop_turns(
            runtime.agent_config,
        )
        outcome.max_stop_turns = stop_turns
        outcome.max_automatic_follow_up_turns = (
            _resolve_max_automatic_follow_up_turns(
                runtime.agent_config,
                stop_turns,
            )
        )
        reset_terminal_stop = getattr(
            runtime.agent,
            "reset_pre_tool_terminal_stop",
            None,
        )
        if callable(reset_terminal_stop):
            reset_terminal_stop()

        try:
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
        except PreToolUseTerminalStop as exc:
            consume_terminal_stop = getattr(
                runtime.agent,
                "consume_pre_tool_terminal_stop",
                None,
            )
            reason = (
                consume_terminal_stop()
                if callable(consume_terminal_stop)
                else None
            )
            reason = (reason or exc.reason or "Hook requested stop").strip()
            outcome.task_completed = False
            outcome.completion_blocked = True
            outcome.completion_block_reason = reason
            outcome.pre_tool_terminal_stop = True
            terminal_msg = Msg(
                name="Friday",
                role="assistant",
                content=reason,
            )
            await runtime.agent.memory.add(terminal_msg)
            yield terminal_msg, True
            return

        outcome.assistant_response = _extract_assistant_response(
            runtime.agent,
            memory_start=memory_start,
        )
        outcome.task_completed = True

    async def _emit_stop_hook_if_needed(
        self,
        *,
        request: AgentRequest,
        runtime: _QueryRuntime,
        plan: _TurnPlan,
        outcome: _QueryTurnOutcome,
    ) -> MergedHookResult | None:
        """执行 Stop completion gate，active guard 已设置时跳过递归触发。"""
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
            HookEventName.STOP,
            request=request,
            runner=self,
            tenant_hooks=runtime.tenant_hooks,
            agent_config=runtime.agent_config,
            overlay=runtime.hook_overlay,
            prompt=plan.original_user_message,
            assistant_response=outcome.assistant_response,
            agent=runtime.agent,
        )

    async def _stream_completion_lifecycle(
        self,
        *,
        request: AgentRequest,
        runtime: _QueryRuntime,
        plan: _TurnPlan,
        outcome: _QueryTurnOutcome,
    ):
        """执行 agent turn 与统一 Stop completion gate 生命周期。"""
        while True:
            outcome.stop_hook_active = False
            async for msg, last in self._stream_agent_turns(
                runtime=runtime,
                plan=plan,
                outcome=outcome,
            ):
                yield msg, last

            if outcome.pre_tool_terminal_stop:
                return

            stop_result = await self._emit_stop_hook_if_needed(
                request=request,
                runtime=runtime,
                plan=plan,
                outcome=outcome,
            )
            if (
                stop_result is not None
                and stop_result.decision == HookDecision.BLOCK
            ):
                reason = (
                    stop_result.blocking_failure_reason
                    if stop_result.has_blocking_failure
                    else stop_result.reason
                ) or "Stop blocked completion"
                if (
                    not stop_result.has_blocking_failure
                    and _should_stop_follow_up(
                        outcome,
                    )
                ):
                    outcome.stop_follow_up_turns += 1
                    outcome.automatic_follow_up_turns += 1
                    plan.turn_msgs = [_build_stop_follow_up_msg(reason)]
                    outcome.stop_hook_active = False
                    logger.info(
                        "Stop scheduled automatic follow-up turn "
                        "%d/%d for session %s: %s",
                        outcome.stop_follow_up_turns,
                        outcome.max_stop_turns,
                        runtime.session_id,
                        reason,
                    )
                    continue

                outcome.task_completed = False
                outcome.completion_blocked = True
                outcome.completion_block_reason = reason
                outcome.completion_marked_incomplete = True
                outcome.stop_hook_active = False
                incomplete_msg = _build_stop_incomplete_msg(reason)
                await runtime.agent.memory.add(incomplete_msg)
                yield incomplete_msg, True
                return
            outcome.stop_hook_active = False
            return

    async def _generate_backend_suggestions_if_needed(
        self,
        *,
        runtime: _QueryRuntime,
        plan: _TurnPlan,
        outcome: _QueryTurnOutcome,
    ) -> None:
        """保留旧调用点，但不再由 runner 重复生成 suggestions。"""
        del runtime, plan, outcome
        logger.debug(
            "Suggestions generation handled by frontend external API; "
            "backend does not schedule duplicate generation.",
        )

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
        """结束 trace，失败时只记录日志避免影响主链路。

        如果 trace 是 attach 到外部已存在的 trace（attached=True），则跳过结束，
        让外部创建者负责结束。
        """
        if not trace_id or not has_trace_manager():
            return

        # 检查是否是 attach 的 trace，如果是则跳过结束
        from ...tracing import get_current_trace

        ctx = get_current_trace()
        if ctx and ctx.attached and ctx.trace_id == trace_id:
            logger.debug(
                "Skip ending attached trace (owned by external): trace_id=%s",
                trace_id[:20] if trace_id else "(empty)",
            )
            from ...tracing import set_current_trace

            # 清除 context，让后续请求不会继承外部 trace。
            set_current_trace(None)
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

    async def _persist_model_call_failed_detail_safely(
        self,
        *,
        request: AgentRequest,
        detail: ModelCallFailureDetail,
    ) -> None:
        """保存模型调用失败详情，持久化异常不应覆盖原始失败信息。"""
        try:
            await self._persist_model_call_failed_detail(
                request=request,
                detail=detail,
            )
        except Exception:
            logger.warning(
                "Failed to persist model-call failure detail for history",
                exc_info=True,
            )

    async def _raise_console_model_call_failed_if_needed(
        self,
        *,
        request: AgentRequest,
        exc: Exception,
        trace_id: str | None,
    ) -> None:
        """在 Console 模型调用失败时抛出用户可见的结构化异常。"""
        if getattr(request, "channel", DEFAULT_CHANNEL) != DEFAULT_CHANNEL:
            return

        detail = extract_model_call_failure_detail(exc)
        if detail is None:
            return

        logger.warning(
            "Console model call failed after final attempt: "
            "kind=%s status=%s truncated=%s",
            detail.kind.value,
            detail.provider_status,
            detail.truncated,
        )
        await self._end_trace_if_needed(
            trace_id,
            TraceStatus.ERROR,
            error=detail.message,
        )
        await self._persist_model_call_failed_detail_safely(
            request=request,
            detail=detail,
        )
        raise ModelCallFailedException(detail) from exc

    async def _persist_model_call_failed_detail(
        self,
        *,
        request: AgentRequest,
        detail: ModelCallFailureDetail,
    ) -> None:
        """Persist user-visible model-call failure detail outside memory."""
        if self.session is None or not hasattr(
            self.session,
            "mutate_session_state",
        ):
            return

        session_id = _coerce_session_storage_id(
            getattr(request, "session_id", None),
        )
        user_id = _coerce_session_storage_user_id(
            getattr(request, "user_id", None),
        )
        if not session_id:
            return

        record = {
            "id": f"model-call-error-{uuid4().hex}",
            "type": "error",
            "role": "assistant",
            "status": "failed",
            "code": detail.code,
            "message": detail.message,
            "content": [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": {
                "model_call_failed": True,
                "kind": detail.kind.value,
                "provider_status": detail.provider_status,
                "truncated": detail.truncated,
            },
        }

        def _append_record(state: dict[str, Any]) -> dict[str, Any]:
            next_state = state if isinstance(state, dict) else {}
            existing = next_state.get(MODEL_CALL_FAILED_MESSAGES_STATE_KEY)
            records = list(existing) if isinstance(existing, list) else []
            records.append(record)
            next_state[MODEL_CALL_FAILED_MESSAGES_STATE_KEY] = records
            return next_state

        await self.session.mutate_session_state(
            session_id=session_id,
            mutator=_append_record,
            user_id=user_id,
            create_if_not_exist=True,
        )

    async def _save_state_during_cleanup(
        self,
        *,
        runtime: _QueryRuntime | None,
        session_state_loaded: bool,
    ) -> None:
        """在 finally 阶段保存 session state，并限制单步耗时。"""
        logger.info(
            "_save_state_during_cleanup: runtime=%s session_state_loaded=%s",
            runtime is not None,
            session_state_loaded,
        )
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

    # ── Query 级别重试辅助方法 ──

    @staticmethod
    def _summarize_retry_error(exc: BaseException) -> str:
        """从重试异常中提取用户可读的错误摘要。"""
        for candidate in (
            exc,
            getattr(exc, "__cause__", None),
            getattr(exc, "__context__", None),
        ):
            if candidate is None:
                continue
            status_code = getattr(candidate, "status_code", None)
            if status_code is not None:
                status_messages = {
                    429: "请求频率超限",
                    432: "输入Token数已达上限",
                    433: "服务过载",
                    500: "服务内部错误",
                    502: "网关错误",
                    503: "服务暂不可用",
                    504: "请求超时",
                    529: "站点过载",
                }
                return status_messages.get(
                    status_code,
                    f"服务错误({status_code})",
                )
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return "请求超时"
        if isinstance(
            exc,
            (ConnectionError, ConnectionResetError, BrokenPipeError),
        ):
            return "网络连接异常"
        msg = str(exc)
        if "rate limiter" in msg.lower():
            return "请求频率超限"
        if "timed out" in msg.lower():
            return "请求超时"
        return "服务暂时不可用"

    @staticmethod
    def _extract_retry_config(
        agent_config,
    ) -> tuple[bool, int, float, float]:
        """从 agent 配置中提取重试参数。

        Returns:
            (retry_enabled, max_retries, backoff_base, backoff_cap)
        """
        query_retry_config = getattr(
            getattr(agent_config, "running", None),
            "query_retry",
            None,
        )
        if not query_retry_config:
            return False, 0, 2.0, 30.0
        return (
            getattr(query_retry_config, "enabled", False),
            getattr(query_retry_config, "max_retries", 0),
            getattr(query_retry_config, "backoff_base", 2.0),
            getattr(query_retry_config, "backoff_cap", 30.0),
        )

    @staticmethod
    def _compute_retry_backoff(
        retry_attempt: int,
        backoff_cap: float,
        backoff_base: float,
    ) -> float:
        """计算重试退避时间。"""
        return min(
            backoff_cap,
            backoff_base * (2 ** (retry_attempt - 1)),
        )

    @staticmethod
    def _should_retry(
        retry_attempt: int,
        max_retry_attempts: int,
        exc: BaseException,
    ) -> bool:
        """判断当前异常是否应该重试。"""
        return retry_attempt < max_retry_attempts - 1 and is_query_retryable(
            exc,
        )

    async def _save_state_before_retry(
        self,
        agent: Any,
        session_state_loaded: bool,
        session_id: str,
        skip_history: bool,
        user_id: str,
    ) -> None:
        """重试前保存当前会话状态。"""
        if agent is None or not session_state_loaded:
            return
        try:
            await asyncio.wait_for(
                self.save_job_session_state(
                    agent,
                    session_id,
                    skip_history,
                    user_id,
                ),
                timeout=QUERY_CLEANUP_TIMEOUT,
            )
        except Exception as save_err:
            logger.warning(
                "Failed to save state before retry: %s",
                save_err,
            )

    def _load_query_retry_settings(self) -> tuple[int, int, float, float]:
        """读取 query 重试配置，配置不可用时回退为单次执行。"""
        agent_config_for_retry = None
        try:
            agent_config_for_retry = load_agent_config(
                self.agent_id,
                tenant_id=self.tenant_id,
            )
        except Exception:
            pass

        retry_enabled, max_retries, backoff_base, backoff_cap = (
            self._extract_retry_config(
                self._resolve_source_query_retry_config(
                    agent_config_for_retry,
                ),
            )
        )
        max_retry_attempts = max_retries + 1 if retry_enabled else 1
        return max_retry_attempts, max_retries, backoff_base, backoff_cap

    @staticmethod
    def _resolve_source_query_retry_config(agent_config):
        """应用当前 source 的显式 Query 重试覆盖。"""
        if agent_config is None:
            return None
        try:
            from ..source_system_config import resolve_query_retry_config

            running = getattr(agent_config, "running", None)
            if running is None:
                return agent_config
            query_retry = getattr(running, "query_retry", None)
            if query_retry is None:
                return agent_config
            resolved = resolve_query_retry_config(query_retry)
            if hasattr(agent_config, "model_copy"):
                return agent_config.model_copy(
                    update={
                        "running": running.model_copy(
                            update={"query_retry": resolved},
                        ),
                    },
                )
            setattr(running, "query_retry", resolved)
        except Exception:
            return agent_config
        return agent_config

    async def _add_retry_notice_to_memory(
        self,
        agent: Any,
        retry_msg: Msg,
    ) -> None:
        """把重试状态写入 memory，失败时不影响主重试流程。"""
        if agent is None:
            return
        try:
            await agent.memory.add(retry_msg)
        except Exception:
            pass

    @staticmethod
    def _build_retry_status_msg(text: str) -> Msg:
        """构造面向用户展示的 query 重试状态消息。"""
        return Msg(
            name="Friday",
            role="assistant",
            content=[
                TextBlock(
                    type="text",
                    text=text,
                ),
            ],
            metadata={"retry_status": True},
        )

    async def _stream_retry_backoff_notice(
        self,
        *,
        retry_attempt: int,
        max_retries: int,
        backoff_base: float,
        backoff_cap: float,
        session_id: str,
        retry_state: _RetryState,
    ):
        """重试下一轮前通知用户并等待退避时间。"""
        if retry_attempt <= 0:
            return

        backoff = self._compute_retry_backoff(
            retry_attempt,
            backoff_cap,
            backoff_base,
        )
        logger.info(
            "Query retry attempt %d/%d, backoff=%.1fs (session=%s)",
            retry_attempt,
            max_retries,
            backoff,
            session_id,
        )
        retry_msg = self._build_retry_status_msg(
            f"正在重试 ({retry_attempt}/{max_retries})...",
        )
        yield retry_msg, False
        await self._add_retry_notice_to_memory(
            retry_state.prev_agent,
            retry_msg,
        )
        await asyncio.sleep(backoff)

    async def _stream_retryable_query_error(
        self,
        *,
        exc: BaseException,
        retry_attempt: int,
        max_retry_attempts: int,
        max_retries: int,
        retry_state: _RetryState,
        runtime: _QueryRuntime | None,
        session_id: str,
    ):
        """处理可重试异常，保持先提示用户再保存状态的顺序。"""
        error_summary = self._summarize_retry_error(exc)
        logger.warning(
            "Query failed with retryable error (attempt %d/%d): %s "
            "(summary: %s)",
            retry_attempt + 1,
            max_retry_attempts,
            exc,
            error_summary,
        )
        retry_msg = self._build_retry_status_msg(
            f"{error_summary}，"
            f"正在重试 ({retry_attempt + 1}/{max_retries})...",
        )
        yield retry_msg, False
        await self._add_retry_notice_to_memory(retry_state.agent, retry_msg)
        await self._save_state_before_retry(
            retry_state.agent,
            retry_state.session_state_loaded,
            session_id,
            runtime.skip_history if runtime is not None else False,
            runtime.user_id if runtime is not None else "",
        )

    async def _complete_successful_query_attempt(
        self,
        *,
        runtime: _QueryRuntime,
        plan: _TurnPlan,
        outcome: _QueryTurnOutcome,
        trace_id: str | None,
        skill_snapshot_to_persist: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """执行 agent 输出后的持久化、suggestion 与 trace 收尾。"""
        await self._generate_backend_suggestions_if_needed(
            runtime=runtime,
            plan=plan,
            outcome=outcome,
        )
        await self._index_model_output_if_needed(
            trace_id=trace_id,
            agent=runtime.agent,
        )
        await self._end_trace_if_needed(
            trace_id,
            TraceStatus.COMPLETED,
        )
        if skill_snapshot_to_persist is not None:
            await self.session.save_session_skill_snapshot(
                session_id=runtime.session_id,
                user_id=runtime.user_id,
                snapshot=skill_snapshot_to_persist,
            )

    async def _finish_blocked_query_attempt(
        self,
        *,
        runtime: _QueryRuntime,
        outcome: _QueryTurnOutcome,
        trace_id: str | None,
        skill_snapshot_to_persist: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Stop 耗尽预算时仍需写入最终输出并结束 trace。"""
        if outcome.pre_tool_terminal_stop:
            await self._end_trace_if_needed(
                trace_id,
                TraceStatus.COMPLETED,
            )
        elif outcome.completion_marked_incomplete:
            await self._index_model_output_if_needed(
                trace_id=trace_id,
                agent=runtime.agent,
            )
            await self._end_trace_if_needed(
                trace_id,
                TraceStatus.COMPLETED,
            )
        if skill_snapshot_to_persist is not None:
            await self.session.save_session_skill_snapshot(
                session_id=runtime.session_id,
                user_id=runtime.user_id,
                snapshot=skill_snapshot_to_persist,
            )

    async def _stream_single_query_attempt(
        self,
        *,
        attempt_input: _QueryAttemptInput,
        outcome: _QueryTurnOutcome,
        retry_state: _RetryState,
        attempt_state: _QueryAttemptState,
    ):
        """执行一次 query 尝试，调用方负责重试和 finally 清理。"""
        attempt_state.runtime_start = await self._prepare_query_runtime(
            request=attempt_input.request,
            msgs=attempt_input.msgs,
            query=attempt_input.query,
            preflight=attempt_input.preflight,
        )
        if attempt_state.runtime_start.block_response is not None:
            await self._end_trace_if_needed(
                attempt_input.trace_id,
                TraceStatus.COMPLETED,
            )
            yield attempt_state.runtime_start.block_response, True
            attempt_state.should_return = True
            return

        runtime = attempt_state.runtime_start.runtime
        attempt_state.runtime = runtime
        if runtime is None:
            attempt_state.should_return = True
            return

        with runtime_invocation_claims_context(
            chat_id=runtime.chat.id if runtime.chat is not None else None,
        ):
            self._rebind_trace_skill_detector_if_needed(
                runtime=runtime,
                trace_id=attempt_input.trace_id,
            )
            if (
                attempt_input.trace_id
                and runtime.session_skill_detector is None
            ):
                await runtime.agent.setup_skill_detector(
                    attempt_input.trace_id,
                )

            logger.debug(f"Agent Query msgs {attempt_input.msgs}")
            attempt_state.session_state_loaded = await self.get_state_loaded(
                runtime.agent,
                runtime.session_id,
                attempt_state.session_state_loaded,
                runtime.skip_history,
                runtime.user_id,
            )
            retry_state.agent = runtime.agent
            retry_state.session_state_loaded = (
                attempt_state.session_state_loaded
            )

            skill_freshness_refresh = (
                await self._refresh_session_skill_freshness(
                    runtime=runtime,
                )
            )

            # 会话状态可能保存了旧提示词，执行前强制刷新文件态上下文。
            runtime.agent.rebuild_sys_prompt()

            plan = await self._build_turn_plan(
                runtime=runtime,
                request=attempt_input.request,
                msgs=attempt_input.msgs,
                query=attempt_input.query,
            )
            if skill_freshness_refresh.notice_text:
                notice_msg = _build_skill_freshness_notice_msg(
                    skill_freshness_refresh.notice_text,
                )
                plan.turn_msgs.insert(0, notice_msg)

            async for msg, last in self._stream_completion_lifecycle(
                request=attempt_input.request,
                runtime=runtime,
                plan=plan,
                outcome=outcome,
            ):
                yield msg, last

            skill_snapshot_to_persist = (
                await self._build_skill_snapshot_to_persist(
                    runtime=runtime,
                    refresh_result=skill_freshness_refresh,
                )
            )

            if outcome.completion_blocked:
                await self._finish_blocked_query_attempt(
                    runtime=runtime,
                    outcome=outcome,
                    trace_id=attempt_input.trace_id,
                    skill_snapshot_to_persist=skill_snapshot_to_persist,
                )
                attempt_state.should_return = True
                return

            await self._complete_successful_query_attempt(
                runtime=runtime,
                plan=plan,
                outcome=outcome,
                trace_id=attempt_input.trace_id,
                skill_snapshot_to_persist=skill_snapshot_to_persist,
            )
        retry_state.task_completed = outcome.task_completed
        attempt_state.succeeded = True

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
        from ...config.context import (
            reset_current_file_url_network,
            set_current_file_url_network,
        )

        set_current_agent_id(self.agent_id)
        file_url_network_token = set_current_file_url_network(
            _request_file_url_network(request),
        )

        trace_id = await self._start_query_trace(request, msgs)
        runtime_claims_context = runtime_invocation_claims_context(
            session_id=session_id,
            trace_id=trace_id,
        )
        runtime_claims_context.__enter__()
        outcome = _QueryTurnOutcome()

        # ── Query 级别重试循环 ──
        retry_state = _RetryState()
        max_retry_attempts, max_retries, backoff_base, backoff_cap = (
            self._load_query_retry_settings()
        )
        attempt_input = _QueryAttemptInput(
            request=request,
            msgs=msgs,
            query=query,
            preflight=preflight,
            trace_id=trace_id,
        )
        attempt_state = _QueryAttemptState()

        try:
            for retry_attempt in range(max_retry_attempts):
                retry_state.prev_agent = retry_state.agent
                retry_state.prev_session_state_loaded = (
                    retry_state.session_state_loaded
                )
                retry_state.agent = None
                retry_state.session_state_loaded = False
                attempt_state = _QueryAttemptState()

                async for msg, last in self._stream_retry_backoff_notice(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    backoff_base=backoff_base,
                    backoff_cap=backoff_cap,
                    session_id=session_id,
                    retry_state=retry_state,
                ):
                    yield msg, last

                try:
                    async for msg, last in self._stream_single_query_attempt(
                        attempt_input=attempt_input,
                        outcome=outcome,
                        retry_state=retry_state,
                        attempt_state=attempt_state,
                    ):
                        yield msg, last

                    if attempt_state.should_return:
                        return
                    if attempt_state.succeeded:
                        break

                except asyncio.CancelledError as exc:
                    await self._handle_query_cancelled(
                        trace_id=trace_id,
                        session_id=session_id,
                        agent=(
                            attempt_state.runtime.agent
                            if attempt_state.runtime is not None
                            else None
                        ),
                        exc=exc,
                    )
                    return
                except Exception as e:
                    if not self._should_retry(
                        retry_attempt,
                        max_retry_attempts,
                        e,
                    ):
                        await self._raise_console_model_call_failed_if_needed(
                            request=request,
                            exc=e,
                            trace_id=trace_id,
                        )
                        await self._handle_query_error(
                            request=request,
                            exc=e,
                            trace_id=trace_id,
                            locals_snapshot=locals(),
                        )
                        raise

                    async for msg, last in self._stream_retryable_query_error(
                        exc=e,
                        retry_attempt=retry_attempt,
                        max_retry_attempts=max_retry_attempts,
                        max_retries=max_retries,
                        retry_state=retry_state,
                        runtime=attempt_state.runtime,
                        session_id=session_id,
                    ):
                        yield msg, last
        finally:
            try:
                runtime_claims_context.__exit__(None, None, None)
            except ValueError:
                logger.debug(
                    "Skipped runtime invocation claims context reset from a "
                    "different async context",
                    exc_info=True,
                )
            try:
                reset_current_file_url_network(file_url_network_token)
            except ValueError:
                logger.debug(
                    "Skipped file URL network context reset from a different "
                    "async context",
                    exc_info=True,
                )
            cleanup_runtime = attempt_state.runtime
            cleanup_state_loaded = attempt_state.session_state_loaded
            if cleanup_runtime is None and retry_state.prev_agent is not None:
                # 重试循环中被取消时 runtime 可能为 None，
                # 但仍需保存 prev_agent 的状态
                cleanup_state_loaded = (
                    retry_state.session_state_loaded
                    or retry_state.prev_session_state_loaded
                )
            await self._cleanup_query_resources(
                runtime=cleanup_runtime,
                session_state_loaded=cleanup_state_loaded,
                session_id=session_id,
            )
            await self._cleanup_blocked_runtime_start(
                attempt_state.runtime_start,
            )
            await self._store_qa_content_if_needed(
                runtime=cleanup_runtime,
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
            trace_id = await self._start_query_trace(request, msgs)
            try:
                async for msg, last in run_command_path(request, msgs, self):
                    yield msg, last
            except asyncio.CancelledError:
                await self._end_trace_if_needed(
                    trace_id,
                    TraceStatus.CANCELLED,
                )
                raise
            except Exception as exc:
                await self._end_trace_if_needed(
                    trace_id,
                    TraceStatus.ERROR,
                    str(exc),
                )
                raise
            await self._end_trace_if_needed(
                trace_id,
                TraceStatus.COMPLETED,
            )
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
            trace_id = getattr(request, "trace_id", None)
            msg = self._attach_trace_id_to_msg(msg, trace_id)
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
        storage_session_id = _coerce_session_storage_id(session_id)
        storage_user_id = _coerce_session_storage_user_id(user_id)
        if skip_history:
            logger.info(
                "Cron task: skipping session state load (session_id=%s)",
                session_id,
            )
            session_state_loaded = True
        else:
            try:
                await self.session.load_session_state(
                    session_id=storage_session_id,
                    user_id=storage_user_id,
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
        storage_session_id = _coerce_session_storage_id(session_id)
        storage_user_id = _coerce_session_storage_user_id(user_id)
        current_agent_state = agent.state_dict()
        merge_stats: dict[str, Any] = {}

        def _merge(existing_state: dict[str, Any]) -> dict[str, Any]:
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
            merge_stats["existing_content"] = existing_content
            merge_stats["current_content"] = current_content
            merge_stats["stripped_count"] = stripped_count
            return merged_state

        await self.session.mutate_session_state(
            session_id=storage_session_id,
            mutator=_merge,
            user_id=storage_user_id,
            create_if_not_exist=True,
        )

        logger.info(
            "Cron task: saved merged session state "
            "(session_id=%s, existing_memory_content=%s, new_content=%s, "
            "stripped_internal_follow_ups=%s)",
            session_id,
            len(merge_stats.get("existing_content", [])),
            len(merge_stats.get("current_content", [])),
            merge_stats.get("stripped_count", 0),
        )

    async def _save_legacy_session_state(
        self,
        agent: SWEAgent,
        session_id: str | None | Any,
        user_id: str | None,
        hook_overlay: HookSessionOverlay | None = None,
    ) -> None:
        """兼容不支持 state_dict 的 agent 落盘路径。"""
        storage_session_id = _coerce_session_storage_id(session_id)
        storage_user_id = _coerce_session_storage_user_id(user_id)
        await self.session.save_session_state(
            session_id=storage_session_id,
            user_id=storage_user_id,
            agent=agent,
        )
        if hook_overlay is None:
            await self.session.mutate_session_state(
                session_id=storage_session_id,
                mutator=lambda state: {
                    key: value
                    for key, value in state.items()
                    if key != "hook_overlay"
                },
                user_id=storage_user_id,
                create_if_not_exist=True,
            )
            return

        await self.session.update_session_state(
            storage_session_id,
            "hook_overlay",
            hook_overlay.model_dump(mode="json", by_alias=True),
            user_id=storage_user_id,
        )

    async def _save_regular_session_state(
        self,
        agent: SWEAgent,
        session_id: str | None | Any,
        user_id: str | None,
        hook_overlay: HookSessionOverlay | None = None,
    ) -> None:
        """保存普通请求状态，并在落盘前剔除内部续跑提示。"""
        storage_session_id = _coerce_session_storage_id(session_id)
        storage_user_id = _coerce_session_storage_user_id(user_id)
        if not hasattr(agent, "state_dict"):
            await self._save_legacy_session_state(
                agent,
                session_id,
                user_id,
                hook_overlay,
            )
            return

        current_agent_state = agent.state_dict()
        stripped_count = 0
        deduped_external_approvals = 0

        def _merge(existing_state: dict[str, Any]) -> dict[str, Any]:
            nonlocal stripped_count, deduped_external_approvals
            state_modules: dict[str, Any] = (
                dict(existing_state)
                if isinstance(existing_state, dict)
                else {}
            )
            if SESSION_SKILL_SNAPSHOT_STATE_KEY in state_modules:
                state_modules[SESSION_SKILL_SNAPSHOT_STATE_KEY] = (
                    _normalize_session_skill_snapshot(
                        state_modules.get(
                            SESSION_SKILL_SNAPSHOT_STATE_KEY,
                        ),
                    )
                )
            state_modules["agent"] = current_agent_state
            if hook_overlay is not None:
                state_modules["hook_overlay"] = hook_overlay.model_dump(
                    mode="json",
                    by_alias=True,
                )
            else:
                state_modules.pop("hook_overlay", None)
            stripped_count = _strip_internal_follow_up_messages_from_state(
                state_modules["agent"],
            )
            deduped_external_approvals = (
                _dedupe_external_approval_messages_from_state(
                    state_modules["agent"],
                )
            )
            return state_modules

        await self.session.mutate_session_state(
            session_id=storage_session_id,
            mutator=_merge,
            user_id=storage_user_id,
            create_if_not_exist=True,
        )
        logger.info(
            "Saved session state with stripped_internal_follow_ups=%s "
            "deduped_external_approvals=%s "
            "(session_id=%s)",
            stripped_count,
            deduped_external_approvals,
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

        storage_session_id = _coerce_session_storage_id(session_id)
        storage_user_id = _coerce_session_storage_user_id(user_id)
        path = self.session._get_save_path(  # pylint: disable=protected-access
            storage_session_id,
            storage_user_id,
        )
        if not Path(path).exists():
            return

        try:
            modified = False

            def _cleanup_state(
                states: dict[str, Any],
            ) -> dict[str, Any] | None:
                nonlocal modified
                content = _get_agent_memory_content(states)
                if content is None:
                    return None

                last_marked_idx = _last_tool_guard_denied_index(content)
                if _remove_following_denial_explanation(
                    content,
                    last_marked_idx,
                ):
                    modified = True

                if _strip_tool_guard_denied_marks(content):
                    modified = True

                if denial_response is not None:
                    content.append(
                        _build_denial_response_memory_entry(denial_response),
                    )
                    modified = True

                return states if modified else None

            await self.session.mutate_session_state(
                session_id=storage_session_id,
                mutator=_cleanup_state,
                user_id=storage_user_id,
                create_if_not_exist=False,
            )

            if not modified:
                return
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
        task_progress_enabled = is_chat_task_progress_enabled(
            get_current_source_system_config(),
        )
        async for event in normalize_reasoning_boundary_stream(
            super().stream_query(request, **kwargs),
        ):
            trace_id = getattr(request, "trace_id", None)
            event = self._attach_trace_id_to_event(event, trace_id)
            progress = None
            if task_progress_enabled:
                channel_meta = getattr(request, "channel_meta", None) or {}
                chat_id = channel_meta.get("chat_id")
                if not chat_id and self._chat_manager is not None:
                    chat_id = await self._chat_manager.get_chat_id_by_session(
                        getattr(request, "session_id", "") or "",
                        getattr(request, "channel", DEFAULT_CHANNEL),
                    )
                if chat_id and self._task_tracker is not None:
                    progress = await self._task_tracker.get_task_progress(
                        chat_id,
                    )
            yield attach_task_progress(
                event,
                progress,
                enabled=task_progress_enabled,
            )

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

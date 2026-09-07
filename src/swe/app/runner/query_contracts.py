# -*- coding: utf-8 -*-
"""Contracts shared by the query preflight and runtime collaborators."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from agentscope.message import Msg
from agentscope_runtime.engine.schemas.agent_schemas import AgentRequest

from ...agents.hook_runtime.models import HookConfig, HookSessionOverlay
from ...agents.react_agent import SWEAgent
from ...agents.skill_runtime_snapshot import WorkspaceSkillSnapshot


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
    workspace_skill_snapshot: WorkspaceSkillSnapshot | None = None
    session_execution: Any | None = None


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
    session_execution: Any | None = None
    session_state_committed: bool = False
    session_state_commit_attempted: bool = False


@dataclass
class _RuntimeStartResult:
    """描述运行时初始化是否被 hook 中断。"""

    runtime: _QueryRuntime | None = None
    block_response: Msg | None = None
    blocked_chat: Any = None
    blocked_mcp_clients: list[Any] | None = None
    blocked_session_id: str = ""


class QueryPreflightOwner(Protocol):
    """Preflight collaborator needs only these runner capabilities."""

    async def _resolve_pending_approval(
        self,
        session_id: str,
        query: str | None,
        *,
        request: AgentRequest,
    ) -> tuple[Msg | None, bool, dict[str, Any] | None]:
        """Resolve any pending tool approval for a query."""

    def _load_query_preflight_config(self) -> tuple[Any, HookConfig]:
        """Load request-independent agent and tenant hook configuration."""

    async def _load_query_preflight_overlay(
        self,
        *,
        session_id: str,
        user_id: str,
        session_execution: Any = None,
    ) -> HookSessionOverlay:
        """Load the hook overlay persisted for this session."""

    def _query_preflight_hooks_enabled(
        self,
        tenant_hooks: HookConfig,
        agent_config: Any,
        overlay: HookSessionOverlay,
    ) -> bool:
        """Report whether user-prompt hooks are enabled for this request."""

    async def _emit_query_user_prompt_submit_hook(
        self,
        *,
        request: AgentRequest,
        tenant_hooks: HookConfig,
        agent_config: Any,
        overlay: HookSessionOverlay,
        prompt: str,
        session_execution: Any = None,
    ) -> Any:
        """Emit the USER_PROMPT_SUBMIT hook through the runner."""

    def _query_preflight_hook_block_message(self, result: Any) -> Msg:
        """Render the terminal response from a blocking hook result."""

    def _query_preflight_additional_context(self, result: Any) -> str:
        """Format non-blocking hook context for the agent prompt."""


class QueryRuntimeOwner(Protocol):
    """Runtime collaborator needs only the ordered assembly operations."""

    tenant_id: str | None

    async def _build_query_runtime_inputs(
        self,
        *,
        request: AgentRequest,
        msgs: list[Any],
        preflight: _QueryPreflight,
        session_execution: Any = None,
    ) -> _QueryRuntimeInputs:
        """Build request-derived inputs before resource connection."""

    async def _start_query_runtime_resources(
        self,
        *,
        request: AgentRequest,
        msgs: list[Any],
        inputs: _QueryRuntimeInputs,
        mcp_clients: list[Any],
    ) -> tuple[_QueryRuntimeResources, _RuntimeStartResult | None]:
        """Create chat/context/MCP resources and run start hooks."""

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
        """Build the agent and register its MCP clients."""

    async def _cleanup_query_runtime_mcp_clients(
        self,
        mcp_clients: list[Any],
    ) -> None:
        """Release MCP clients after failed runtime assembly."""

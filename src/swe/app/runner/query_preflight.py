# -*- coding: utf-8 -*-
"""Query preflight orchestration independent of ``AgentRunner``."""

from __future__ import annotations

from agentscope_runtime.engine.schemas.agent_schemas import AgentRequest

from ...agents.hook_runtime.models import HookDecision
from ...runtime_workers import run_runtime_state_work
from .query_contracts import _QueryPreflight, QueryPreflightOwner


async def prepare_query_preflight(
    owner: QueryPreflightOwner,
    *,
    session_id: str,
    user_id: str,
    query: str | None,
    request: AgentRequest,
    session_execution: object | None = None,
) -> _QueryPreflight:
    """Resolve approval and user-prompt hook state before a query starts."""
    (
        approval_response,
        approval_consumed,
        approved_tool_call,
    ) = await owner._resolve_pending_approval(
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

    agent_config, tenant_hooks = await run_runtime_state_work(
        owner._load_query_preflight_config,
    )
    hook_overlay = await owner._load_query_preflight_overlay(
        session_id=session_id,
        user_id=user_id,
        session_execution=session_execution,
    )
    hook_additional_context = ""
    if query and owner._query_preflight_hooks_enabled(
        tenant_hooks,
        agent_config,
        hook_overlay,
    ):
        prompt_hook_args = {
            "request": request,
            "tenant_hooks": tenant_hooks,
            "agent_config": agent_config,
            "overlay": hook_overlay,
            "prompt": query,
        }
        if session_execution is not None:
            prompt_hook_args["session_execution"] = session_execution
        prompt_hook_result = await owner._emit_query_user_prompt_submit_hook(
            **prompt_hook_args,
        )
        if prompt_hook_result.decision in {
            HookDecision.BLOCK,
            HookDecision.DENY,
            HookDecision.STOP,
        }:
            return _QueryPreflight(
                response=owner._query_preflight_hook_block_message(
                    prompt_hook_result,
                ),
                approval_consumed=approval_consumed,
                approved_tool_call=approved_tool_call,
            )
        if prompt_hook_result.session_title:
            request.channel_meta = {
                **(getattr(request, "channel_meta", None) or {}),
                "session_title": prompt_hook_result.session_title,
            }
        hook_additional_context = owner._query_preflight_additional_context(
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

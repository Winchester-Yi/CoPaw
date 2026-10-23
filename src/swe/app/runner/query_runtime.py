# -*- coding: utf-8 -*-
"""Ordered query runtime assembly independent of ``AgentRunner``."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from types import MappingProxyType
from typing import Any

from agentscope_runtime.engine.schemas.agent_schemas import AgentRequest

from ...agents.hook_runtime.models import HookSessionOverlay
from ...agents.hook_runtime.models import HookSessionState
from ...agents.hook_runtime.skill_loader import (
    SkillHookLoadError,
    load_skill_hooks_for_session,
)
from ...constant import WORKING_DIR
from ...providers.provider_manager import ProviderManager
from ...runtime_workers import run_runtime_state_work
from ..source_system_config.runtime import get_system_prompt_injections
from .query_contracts import (
    _QueryPreflight,
    _QueryRuntime,
    _QueryRuntimeInputs,
    _QueryRuntimeResources,
    _RuntimeStartResult,
    QueryRuntimeOwner,
)

logger = logging.getLogger(__name__)


def build_runtime_mcp_clients(
    clients: list[Any],
    *,
    agent_config: Any,
    tenant_id: str | None,
    user_id: str,
    passthrough_headers: dict[str, str],
    session_id: str,
    chat_id: str | None,
    trace_id: str | None,
    frozen_tools_by_key: dict[str, list[dict[str, Any]]],
    build_lazy_clients: Any,
) -> None:
    """Attach this runtime's request-scoped lazy MCP clients."""
    clients.extend(
        build_lazy_clients(
            agent_config.mcp,
            tenant_id=tenant_id,
            user_id=user_id,
            passthrough_headers=passthrough_headers or None,
            session_id=session_id,
            chat_id=chat_id,
            trace_id=trace_id,
            frozen_tools_by_key=frozen_tools_by_key,
        ),
    )


async def select_runtime_context_directives(
    inputs: _QueryRuntimeInputs,
    request: AgentRequest,
    *,
    workspace_dir: Any,
    chat: Any,
    request_scenario_snapshot: Any,
    with_scenario_mcp: Any,
    request_context_references: Any,
    request_selected_skill_names: Any,
) -> Any:
    """Resolve scenario, reference, and explicit Skill directives for a chat."""
    from .context_references import build_context_reference_directives
    from .skill_selection import SkillUseDirective, build_skill_use_directives
    from ..scenario_preset.runtime import (
        scenario_snapshot_skill_directives,
        scenario_snapshot_skill_names,
    )

    scenario_snapshot = request_scenario_snapshot(request) if chat else None
    inputs.agent_config = with_scenario_mcp(
        inputs.agent_config,
        scenario_snapshot,
        workspace_dir=workspace_dir,
        chat_id=chat.id if chat else "",
    )
    reference_directives = await build_context_reference_directives(
        workspace_dir=workspace_dir,
        channel=inputs.channel,
        agent_config=inputs.agent_config,
        references=request_context_references(request),
        snapshot=inputs.workspace_skill_snapshot,
    )
    reference_skill_names = {
        directive.name
        for directive in reference_directives
        if isinstance(directive, SkillUseDirective)
    }
    selected_directives = build_skill_use_directives(
        workspace_dir=workspace_dir,
        channel=inputs.channel,
        selected_skill_names=[
            name
            for name in [
                *request_selected_skill_names(request),
                *scenario_snapshot_skill_names(scenario_snapshot),
            ]
            if name not in reference_skill_names
        ],
        snapshot=inputs.workspace_skill_snapshot,
    )
    if scenario_snapshot is not None and chat is not None:
        selected_directives.extend(
            await asyncio.to_thread(
                scenario_snapshot_skill_directives,
                scenario_snapshot,
                workspace_dir=workspace_dir,
                chat_id=chat.id,
            ),
        )
    all_directives = [*selected_directives, *reference_directives]
    inputs.selected_skill_directives = [
        directive
        for directive in all_directives
        if isinstance(directive, SkillUseDirective)
    ]
    inputs.selected_context_directives = [
        directive.render() for directive in all_directives
    ]
    logger.debug(
        "runtime_skill_snapshot_generation=%d selected_skill_count=%d "
        "reference_skill_count=%d",
        getattr(inputs.workspace_skill_snapshot, "generation", 0),
        sum(
            1
            for directive in selected_directives
            if isinstance(directive, SkillUseDirective)
        ),
        sum(
            1
            for directive in reference_directives
            if isinstance(directive, SkillUseDirective)
        ),
    )
    return scenario_snapshot


async def complete_runtime_activation(
    *,
    request: AgentRequest,
    inputs: _QueryRuntimeInputs,
    chat: Any,
    turn_id: str,
    mcp_clients: list[Any],
    emit_session_start: Any,
    load_selected_hooks: Any,
) -> tuple[_QueryRuntimeResources, _RuntimeStartResult | None]:
    """Run SESSION_START and return either resources or its blocked lease."""
    session_start_args = {
        "request": request,
        "tenant_hooks": inputs.tenant_hooks,
        "agent_config": inputs.agent_config,
        "hook_overlay": inputs.hook_overlay,
        "skip_history": inputs.skip_history,
        "env_context": inputs.env_context,
    }
    if inputs.session_execution is not None:
        session_start_args["session_execution"] = inputs.session_execution
    env_context, block_response = await emit_session_start(
        **session_start_args,
    )
    resources = _QueryRuntimeResources(
        chat=chat,
        turn_id=turn_id,
        env_context=env_context,
    )
    if block_response is None:
        # Keep the observation acquired at Query startup, even across expiry.
        inputs.hook_overlay = await load_selected_hooks(inputs=inputs)
        return resources, None
    return resources, _RuntimeStartResult(
        block_response=block_response,
        blocked_chat=chat,
        blocked_mcp_clients=mcp_clients,
        blocked_session_id=inputs.session_id,
    )


async def load_selected_skill_hooks(
    *,
    inputs: _QueryRuntimeInputs,
    workspace_dir: Any,
    tenant_id: str | None,
    approved_http_urls: set[str],
) -> HookSessionOverlay:
    """Load validated selected skill Hooks after the session-start phase."""
    del tenant_id
    state: HookSessionState = inputs.hook_overlay
    for directive in inputs.selected_skill_directives:
        try:
            content_signature = getattr(directive, "content_signature", None)
            if content_signature:
                from ...agents.skills_manager import _build_signature

                current_signature = await asyncio.to_thread(
                    _build_signature,
                    directive.path.parent,
                )
                if current_signature != content_signature:
                    logger.warning(
                        "Skipping hooks for changed skill '%s'",
                        directive.name,
                    )
                    continue
            state = await asyncio.to_thread(
                load_skill_hooks_for_session,
                skill_name=directive.name,
                skill_root=directive.path.parent,
                workspace_dir=workspace_dir,
                session_state=state,
                approved_http_urls=approved_http_urls,
            )
        except SkillHookLoadError as exc:
            logger.warning(
                "Rejected hooks for explicitly selected skill '%s': %s",
                directive.name,
                exc,
            )
    return HookSessionOverlay.model_validate(
        state.model_dump(mode="json", by_alias=True),
    )


async def build_query_runtime_inputs(
    owner: Any,
    *,
    request: AgentRequest,
    msgs: list[Any],
    preflight: _QueryPreflight,
    session_execution: Any = None,
    build_environment_context: Any,
    request_source_id: Any,
    request_user_name: Any,
    request_passthrough_headers: Any,
    with_hook_context: Any,
    merge_system_prompt_injections: Any,
    with_system_prompt_injections: Any,
    request_system_prompt_injections: Any,
    load_tenant_hooks: Any,
    load_agent_configuration: Any,
    current_passthrough_headers: Any,
) -> _QueryRuntimeInputs:
    """Resolve request values before connecting query runtime resources."""
    session_id = request.session_id
    user_id = request.user_id
    channel = getattr(request, "channel", "console")
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
    env_context = with_hook_context(
        build_environment_context(
            session_id=session_id,
            user_id=user_id,
            channel=channel,
            working_dir=str(owner.workspace_dir or WORKING_DIR),
            source_id=request_source_id(request),
            user_name=request_user_name(request),
        ),
        preflight.hook_additional_context,
    )
    agent_config = (
        preflight.agent_config
        if preflight.agent_config is not None
        else await run_runtime_state_work(
            load_agent_configuration,
            owner.agent_id,
            tenant_id=owner.tenant_id,
        )
    )
    workspace_skill_snapshot = preflight.workspace_skill_snapshot
    if workspace_skill_snapshot is None and getattr(
        agent_config,
        "enable_workspace_skills",
        True,
    ):
        from ...agents.skill_runtime_snapshot import (
            get_workspace_skill_snapshot_async,
        )

        try:
            workspace_skill_snapshot = (
                await get_workspace_skill_snapshot_async(
                    owner.workspace_dir or WORKING_DIR,
                )
            )
        except Exception as exc:  # noqa: BLE001
            # A failed reconcile must not prevent ordinary queries from
            # running; the Agent receives no workspace skills for this turn.
            logger.warning(
                "Workspace skill snapshot unavailable; loading no workspace skills: %s",
                exc,
            )
            from ...agents.skill_runtime_snapshot import (
                ManifestStat,
                WorkspaceSkillSnapshot,
            )

            workspace_skill_snapshot = WorkspaceSkillSnapshot(
                workspace_dir=(owner.workspace_dir or WORKING_DIR),
                generation=0,
                manifest_stat=ManifestStat(0, 0, 0),
                skills=MappingProxyType({}),
                manifest_available=False,
            )
    # Preflight belongs to the whole Query and survives automatic retries.
    preflight.workspace_skill_snapshot = workspace_skill_snapshot
    passthrough_headers = dict[str, str](
        current_passthrough_headers() or {},
    )
    passthrough_headers.update(request_passthrough_headers(request))
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
            else await run_runtime_state_work(
                load_tenant_hooks,
                owner.tenant_id,
            )
        ),
        hook_overlay=(
            preflight.hook_overlay
            if preflight.hook_overlay is not None
            else HookSessionOverlay()
        ),
        env_context=with_system_prompt_injections(
            env_context,
            merge_system_prompt_injections(
                get_system_prompt_injections(),
                request_system_prompt_injections(request),
            ),
        ),
        selected_context_directives=[],
        selected_skill_directives=[],
        workspace_skill_snapshot=workspace_skill_snapshot,
        auth_token=getattr(request, "auth_token", None),
        passthrough_headers=passthrough_headers,
        session_execution=session_execution,
    )


async def finalize_query_runtime(
    owner: Any,
    *,
    request: AgentRequest,
    query: str | None,
    msgs: list[Any],
    preflight: _QueryPreflight,
    inputs: _QueryRuntimeInputs,
    resources: _QueryRuntimeResources,
    mcp_clients: list[Any],
    get_last_user_text: Any,
    debug_log: Any,
) -> _QueryRuntime:
    """Create and initialize the Agent for one assembled query runtime."""
    agent_build_started_at = time.perf_counter()
    workspace_skill_snapshot = inputs.workspace_skill_snapshot
    prepared_memory_kwargs: dict[str, Any] = {}
    memory_manager = getattr(owner, "memory_manager", None)
    prepare_memory = getattr(memory_manager, "prepare_request_memory", None)
    if (
        resources.chat is not None
        and callable(prepare_memory)
        and os.getenv("ENABLE_MEMORY_MANAGER", "").lower() != "false"
    ):
        prepared_memory_kwargs["prepared_request_memory"] = (
            await prepare_memory(resources.chat.id)
        )
    agent_kwargs = {
        "agent_config": inputs.agent_config,
        "env_context": resources.env_context,
        "mcp_clients": mcp_clients,
        "request": request,
        "session_id": inputs.session_id,
        "user_id": inputs.user_id,
        "channel": inputs.channel,
        "chat": resources.chat,
        "turn_id": resources.turn_id,
        "hook_overlay": inputs.hook_overlay,
        "auth_token": inputs.auth_token,
        "approved_tool_call": preflight.approved_tool_call,
        "current_user_text": query or get_last_user_text(msgs) or "",
        "workspace_skill_snapshot": workspace_skill_snapshot,
        "prepared_model_config": getattr(
            inputs,
            "prepared_model_config",
            None,
        ),
        **prepared_memory_kwargs,
    }
    create_async = getattr(owner, "_create_agent_for_query_async", None)
    agent = (
        await create_async(**agent_kwargs)
        if callable(create_async)
        else owner._create_agent_for_query(**agent_kwargs)
    )
    await agent.register_mcp_clients()
    agent.set_console_output_enabled(enabled=False)
    debug_log(
        "swe_agent_build_duration_ms=%d agent_id=%s tenant_id=%s "
        "mcp_client_count=%d",
        int((time.perf_counter() - agent_build_started_at) * 1000),
        owner.agent_id,
        owner.tenant_id,
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
        session_execution=inputs.session_execution,
    )
    owner._attach_session_skill_detector(runtime=runtime, request=request)
    return runtime


async def prepare_query_runtime(
    owner: QueryRuntimeOwner,
    *,
    request: AgentRequest,
    msgs: list[Any],
    query: str | None,
    preflight: _QueryPreflight,
    session_execution: Any = None,
) -> _RuntimeStartResult:
    """Assemble provider, request resources, hooks, agent, and MCP clients."""
    manager = await ProviderManager.get_or_create_instance(owner.tenant_id)
    await manager.refresh_if_due()
    runtime_input_args = {
        "request": request,
        "msgs": msgs,
        "preflight": preflight,
    }
    if session_execution is not None:
        runtime_input_args["session_execution"] = session_execution
    inputs = await owner._build_query_runtime_inputs(**runtime_input_args)
    mcp_clients: list[Any] = []
    try:
        resources, block_result = await owner._start_query_runtime_resources(
            request=request,
            msgs=msgs,
            inputs=inputs,
            mcp_clients=mcp_clients,
        )
        if block_result is not None:
            return block_result
        from ...agents.model_factory import prepare_model_config_async

        inputs.prepared_model_config = await prepare_model_config_async(
            agent_id=inputs.agent_config.id,
            tenant_id=owner.tenant_id,
            manager=manager,
        )
        runtime = await owner._finalize_query_runtime(
            request=request,
            query=query,
            msgs=msgs,
            preflight=preflight,
            inputs=inputs,
            resources=resources,
            mcp_clients=mcp_clients,
        )
        return _RuntimeStartResult(runtime=runtime)
    except asyncio.CancelledError:
        if mcp_clients:
            cleanup = asyncio.create_task(
                owner._cleanup_query_runtime_mcp_clients(mcp_clients),
            )
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                cleanup.result()
            except Exception:
                logger.exception(
                    "MCP cleanup failed after runtime preparation cancellation",
                )
        raise
    except Exception:
        if mcp_clients:
            await owner._cleanup_query_runtime_mcp_clients(mcp_clients)
        raise

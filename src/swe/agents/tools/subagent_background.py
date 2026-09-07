# -*- coding: utf-8 -*-
"""Background SubAgent management tools."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from ...app.subagents import (
    AgentOwnedDefinitionRepository,
    BackgroundSubAgentNotManageable,
    BackgroundSubAgentScope,
    BackgroundSubAgentStartBlocked,
    BackgroundSubAgentSupervisor,
    BackgroundSubAgentWaitSnapshot,
    DelegationSpec,
    DefinitionMatchMetadata,
    PermissionPolicy,
    SubAgentDefinition,
    SubAgentStartRequest,
    builtin_definition_provider,
    build_definition_catalog,
    load_skill_owned_definitions,
)
from ...config.config import AgentProfileConfig
from ...config.utils import get_tenant_working_dir

_RUN_ID_CONTEXT_KEYS = (
    "subagent_run_id",
    "requested_subagent_run_id",
)
_FAILURE_SUMMARY_MAX_CHARS = 1024
_DEFAULT_SUPERVISOR = BackgroundSubAgentSupervisor()


async def _wake_goal_after_subagent(
    *,
    supervisor: BackgroundSubAgentSupervisor,
    scope: BackgroundSubAgentScope,
    goal_id: str,
    run_id: str,
) -> None:
    """Bridge a terminal Background SubAgent result into Goal wake-up."""
    from ...app.goals.registry import get_goal_service

    terminal = await supervisor.wait_for_run(scope, run_id)
    if terminal is None or terminal.status not in {"completed", "partial"}:
        return
    service = get_goal_service()
    if service is not None:
        goal = await service.get(goal_id)
        if getattr(getattr(goal, "state", None), "value", None) != "WAITING":
            return
        await service.wake(goal_id, "Background SubAgent completed")


_START_SUBAGENT_DESCRIPTION = (
    "Start a Background SubAgent Run. Use an exact listed Skill-qualified "
    "name when delegating to a Skill-owned definition."
)


def get_default_background_subagent_supervisor() -> (
    BackgroundSubAgentSupervisor
):
    """Return the process-local default Background SubAgent supervisor."""
    return _DEFAULT_SUPERVISOR


def has_explicit_subagent_run_id(request_context: dict[str, Any]) -> bool:
    """Return whether this request explicitly carries a Background Run id."""
    return any(
        str(request_context.get(key) or "").strip()
        for key in _RUN_ID_CONTEXT_KEYS
    )


def build_background_subagent_scope(
    *,
    parent_agent_config: AgentProfileConfig,
    request_context: dict[str, Any],
) -> BackgroundSubAgentScope:
    """Build the current tenant-and-agent Background SubAgent scope."""
    tenant_id = str(request_context.get("tenant_id") or "default")
    agent_id = str(
        request_context.get("agent_id") or parent_agent_config.id or "default",
    )
    explicit_run_store_dir = request_context.get("_subagent_run_store_dir")
    if explicit_run_store_dir:
        run_store_dir = Path(explicit_run_store_dir)
    else:
        run_store_dir = (
            get_tenant_working_dir(tenant_id)
            / "workspaces"
            / agent_id
            / "subagent_runs"
        )
    return BackgroundSubAgentScope(
        tenant_id=tenant_id,
        agent_id=agent_id,
        run_store_dir=run_store_dir,
    )


def create_background_subagent_tools(
    *,
    supervisor: BackgroundSubAgentSupervisor,
    parent_agent_config: AgentProfileConfig,
    workspace_dir: Path,
    request_context: dict[str, Any],
    effective_skill_names: list[str] | None = None,
    skill_snapshot_signatures: dict[str, str] | None = None,
    skill_snapshot_dirs: Mapping[str, Path] | None = None,
    selected_expert_id: str | None = None,
) -> dict[str, Callable[..., Any]]:
    """Create start/wait/get/cancel Background SubAgent tool callables."""
    tool_scope = build_background_subagent_scope(
        parent_agent_config=parent_agent_config,
        request_context=request_context,
    )
    definition_catalog = _build_definition_catalog(
        tool_scope=tool_scope,
        workspace_dir=workspace_dir,
        effective_skill_names=effective_skill_names,
        skill_snapshot_dirs=skill_snapshot_dirs,
        selected_expert_id=selected_expert_id,
    )
    directory = _format_skill_definition_directory(definition_catalog)

    async def start_subagent(
        name: str | None = None,
        instruction: str | None = None,
        objective: str | None = None,
        background: str = "",
        **extra: Any,
    ) -> ToolResponse:
        """Start a Background SubAgent Run and return its run identity."""
        try:
            if extra:
                raise ValueError(
                    "unexpected fields: " + ", ".join(sorted(extra)),
                )
            start_request = SubAgentStartRequest.model_validate(
                {
                    "name": name,
                    "instruction": instruction,
                    "objective": objective,
                    "background": background,
                },
            )
        except Exception as exc:
            return _json_response(
                {
                    "status": "failed",
                    "reason": "invalid_request",
                    "message": str(exc),
                },
            )
        try:
            definition = (
                definition_catalog.resolve_exact(start_request.name)
                if definition_catalog is not None
                else None
            )
            if selected_expert_id:
                if definition is None:
                    return _json_response(
                        {
                            "status": "not_found",
                            "reason": "selected_expert_not_available",
                            "name": start_request.name,
                            "selected_expert_id": selected_expert_id,
                        },
                    )
                if start_request.name != definition.name:
                    return _json_response(
                        {
                            "status": "failed",
                            "reason": "selected_expert_name_mismatch",
                            "name": start_request.name,
                            "selected_expert_name": definition.name,
                        },
                    )
            if definition is None:
                if start_request.instruction is None:
                    return _json_response(
                        {
                            "status": "not_found",
                            "reason": "subagent_definition_not_found",
                            "name": start_request.name,
                        },
                    )
                definition_match = DefinitionMatchMetadata(
                    matched=False,
                    reason="run_scoped",
                )
                definition = SubAgentDefinition(
                    name=start_request.name,
                    source="run_scoped",
                    owner_scope=(
                        f"run:{tool_scope.tenant_id}:{tool_scope.agent_id}"
                    ),
                    description="Temporary caller-defined SubAgent.",
                    instruction=start_request.instruction,
                )
            else:
                definition_match = DefinitionMatchMetadata(
                    matched=True,
                    definition_name=definition.name,
                    definition_source=definition.source,
                    score=1.0,
                    reason="exact_name",
                )
            spec = DelegationSpec(
                parent_thread_id=str(request_context.get("session_id") or ""),
                parent_chat_id=str(request_context.get("chat_id") or ""),
                parent_msgid=str(request_context.get("msgid") or ""),
                goal_id=str(request_context.get("goal_id") or ""),
                name=start_request.name,
                objective=start_request.objective,
                background=start_request.background,
            )
            goal_id = str(request_context.get("goal_id") or "").strip()
            if goal_id:
                request_context.setdefault("goal_subagent_run_ids", [])
            result = await supervisor.start(
                scope=tool_scope,
                spec=spec,
                parent_agent_config=parent_agent_config,
                workspace_dir=workspace_dir,
                parent_policy=_parent_policy_from_config(
                    parent_agent_config,
                ),
                request_context=request_context,
                definition=definition,
                start_request=start_request,
                definition_match=definition_match,
                effective_skill_names=(
                    list(effective_skill_names or [])
                    if (
                        definition.skill_owned is not None
                        or definition.agent_owned is not None
                    )
                    else []
                ),
                skill_snapshot_signatures=skill_snapshot_signatures,
                skill_snapshot_dirs=skill_snapshot_dirs,
            )
            if goal_id:
                run_id = getattr(result, "run_id", None)
                if run_id:
                    request_context["goal_subagent_run_ids"].append(run_id)
                    from ...app.goals.registry import get_goal_service

                    goal_service = get_goal_service()
                    if goal_service is not None:
                        await goal_service.link_subagent(goal_id, run_id)
                    asyncio.create_task(
                        _wake_goal_after_subagent(
                            supervisor=supervisor,
                            scope=tool_scope,
                            goal_id=goal_id,
                            run_id=run_id,
                        ),
                    )
        except Exception as exc:
            return _json_response(
                {
                    "status": "failed",
                    "reason": "invalid_request",
                    "message": str(exc),
                },
            )
        return _json_response(_serialize_start_result(result))

    start_subagent.__doc__ = directory

    async def wait_subagent(timeout_ms: int = 3000) -> ToolResponse:
        """Wait briefly and return current Background SubAgent statuses."""
        snapshot = await supervisor.wait(
            tool_scope,
            timeout_ms=timeout_ms,
        )
        return _json_response(_serialize_wait_snapshot(snapshot))

    async def get_subagent(
        run_id: str,
        include_details: bool = False,
    ) -> ToolResponse:
        """Fetch one Background SubAgent Run in the current scope."""
        try:
            record = await supervisor.get(
                tool_scope,
                run_id,
            )
        except ValueError:
            return _json_response({"status": "not_found", "run_id": run_id})
        if record is None:
            return _json_response({"status": "not_found", "run_id": run_id})
        return _json_response(
            _compact_record(
                record,
                include_details,
                manageable=_is_manageable(supervisor, tool_scope, run_id),
                run_store_dir=tool_scope.run_store_dir,
            ),
        )

    async def cancel_subagent(run_id: str) -> ToolResponse:
        """Cancel one active Background SubAgent Run in the current scope."""
        try:
            result = await supervisor.cancel(
                tool_scope,
                run_id,
            )
        except ValueError:
            return _json_response({"status": "not_found", "run_id": run_id})
        if result is None:
            return _json_response({"status": "not_found", "run_id": run_id})
        if isinstance(result, BackgroundSubAgentNotManageable):
            return _json_response(result.model_dump(mode="json"))
        return _json_response(
            _compact_record(
                result,
                include_details=False,
                manageable=False,
                run_store_dir=tool_scope.run_store_dir,
            ),
        )

    tools: dict[str, Callable[..., Any]] = {
        "start_subagent": start_subagent,
        "wait_subagent": wait_subagent,
        "get_subagent": get_subagent,
        "cancel_subagent": cancel_subagent,
    }
    return tools


def _build_definition_catalog(
    *,
    tool_scope: BackgroundSubAgentScope,
    workspace_dir: Path,
    effective_skill_names: list[str] | None,
    skill_snapshot_dirs: Mapping[str, Path] | None = None,
    selected_expert_id: str | None = None,
):
    """Build the catalog only for an explicit delegation-intent turn."""
    builtin_definitions = builtin_definition_provider().list_definitions()
    agent_packages = AgentOwnedDefinitionRepository(
        workspace_dir / "agents",
        owner_scope=f"{tool_scope.tenant_id}/{tool_scope.agent_id}",
        builtin_names={definition.name for definition in builtin_definitions},
    ).list()
    if selected_expert_id:
        selected_package = next(
            (
                package
                for package in agent_packages
                if package.definition_id == selected_expert_id
                and package.definition is not None
                and package.definition.enabled
            ),
            None,
        )
        return build_definition_catalog(
            skill_definitions=[],
            builtin_definitions=[],
            agent_owned_definitions=(
                [selected_package.definition]
                if selected_package is not None
                else []
            ),
        )
    return build_definition_catalog(
        skill_definitions=load_skill_owned_definitions(
            workspace_dir=workspace_dir,
            effective_skill_names=effective_skill_names or [],
            skill_snapshot_dirs=skill_snapshot_dirs,
        ).definitions,
        builtin_definitions=builtin_definitions,
        agent_owned_definitions=[
            package.definition
            for package in agent_packages
            if package.definition is not None
        ],
    )


def _format_skill_definition_directory(catalog) -> str:
    """Build the bounded Definition directory included in tool metadata."""
    definitions = catalog.list_definitions() if catalog is not None else []
    if not definitions:
        return _START_SUBAGENT_DESCRIPTION
    lines = [_START_SUBAGENT_DESCRIPTION, "Available definitions:"]
    for definition in definitions:
        keywords = ", ".join(definition.trigger_keywords) or "(none)"
        lines.append(
            f"- {definition.name}: {definition.description} "
            f"[keywords: {keywords}]",
        )
    return "\n".join(lines)


def _json_response(payload: dict[str, Any]) -> ToolResponse:
    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=json.dumps(payload, ensure_ascii=False),
            ),
        ],
    )


def _serialize_start_result(
    result: Any,
) -> dict[str, Any]:
    if isinstance(result, BackgroundSubAgentStartBlocked):
        payload = result.model_dump(mode="json")
        payload["accepted"] = False
        return payload
    return {
        **_parent_facing_record(result, include_result=False),
        "accepted": True,
    }


def _serialize_wait_snapshot(
    snapshot: BackgroundSubAgentWaitSnapshot,
) -> dict[str, Any]:
    return {
        "timed_out": snapshot.timed_out,
        "active_runs": [
            _parent_facing_record(record, include_result=False)
            for record in snapshot.active_runs
        ],
        "terminal_runs": [
            _parent_facing_record(record, include_result=True)
            for record in snapshot.terminal_runs
        ],
    }


def _parent_facing_record(
    record: Any,
    *,
    include_result: bool,
) -> dict[str, Any]:
    payload = {
        "run_id": record.run_id,
        "status": record.status,
        "agent_name": record.spec.name,
        "nickname": getattr(record, "nickname", None),
        "objective": record.spec.objective,
    }
    result = getattr(record, "result", None)
    if include_result and result is not None:
        payload["result"] = _compact_agent_result(result)
    elif include_result:
        failure_result = _compact_failure_without_result(record)
        if failure_result is not None:
            payload["result"] = failure_result
    return payload


def _compact_failure_without_result(record: Any) -> dict[str, str] | None:
    if getattr(record, "status", None) != "failed":
        return None
    errors = getattr(record, "errors", []) or []
    if not errors:
        return None
    error = errors[-1]
    code = str(getattr(error, "code", "") or "").strip()
    message = str(getattr(error, "message", "") or "").strip()
    if code and message.startswith(f"{code}:"):
        summary = message
    else:
        summary = ": ".join(part for part in (code, message) if part)
    if not summary:
        return None
    return {
        "status": "failed",
        "summary": summary[:_FAILURE_SUMMARY_MAX_CHARS],
    }


def _compact_agent_result(result: Any) -> dict[str, Any]:
    payload = {"summary": result.summary}
    errors = getattr(result, "errors", []) or []
    if any(
        getattr(error, "code", "") == "text_finalization_failed"
        for error in errors
    ):
        payload["error_code"] = "text_finalization_failed"
    return payload


def _compact_record(
    record: Any,
    include_details: bool,
    manageable: bool = False,
    run_store_dir: Path | None = None,
) -> dict[str, Any]:
    payload = _parent_facing_record(record, include_result=True)
    stderr_tail = _stderr_tail(record, run_store_dir)
    if include_details:
        payload.update(
            {
                "definition_match": _dump_json_value(
                    getattr(record, "definition_match", None),
                ),
                "created_at": _dump_json_value(
                    getattr(record, "created_at", None),
                ),
                "started_at": _dump_json_value(
                    getattr(record, "started_at", None),
                ),
                "finished_at": _dump_json_value(
                    getattr(record, "finished_at", None),
                ),
                "errors": _dump_json_value(getattr(record, "errors", [])),
                "worker": _compact_worker(
                    getattr(record, "worker", None),
                ),
                "launch_diagnostics": _safe_launch_diagnostics(
                    getattr(record, "launch_diagnostics", {}),
                ),
                "manageable": manageable,
            },
        )
        if stderr_tail is not None:
            payload["stderr_tail"] = stderr_tail
        payload["delegation_spec"] = record.spec.model_dump(mode="json")
        payload["effective_policy"] = record.effective_policy.model_dump(
            mode="json",
        )
    return payload


def _compact_worker(worker: Any) -> dict[str, Any] | None:
    if worker is None:
        return None
    return {
        "pid": getattr(worker, "pid", None),
        "started_at": _dump_json_value(getattr(worker, "started_at", None)),
        "exit_code": getattr(worker, "exit_code", None),
        "exited_at": _dump_json_value(getattr(worker, "exited_at", None)),
    }


def _safe_launch_diagnostics(value: Any) -> dict[str, Any]:
    """Return the allowlisted launch diagnostics for detailed inspection."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for field in (
        "loaded_skills",
        "skipped_skills",
        "snapshotted_mcps",
        "connected_mcps",
        "skipped_mcps",
        "resolved_model",
    ):
        if field in value and value[field] not in (None, [], {}):
            result[field] = _dump_json_value(value[field])
    return result


def _parent_policy_from_config(parent_agent_config: Any) -> PermissionPolicy:
    """Build a bounded parent policy from enabled parent built-in tools."""
    from ...app.subagents.models import KNOWN_BUILTIN_TOOLS, MutationPolicy
    from ...config.config import _default_builtin_tools

    builtin_defaults = _default_builtin_tools()
    tools_config = getattr(parent_agent_config, "tools", None)
    builtin_tools = getattr(tools_config, "builtin_tools", None)
    if not isinstance(builtin_tools, dict):
        builtin_tools = builtin_defaults
    enabled = {
        name
        for name, config in builtin_tools.items()
        if name in KNOWN_BUILTIN_TOOLS and getattr(config, "enabled", True)
    }
    return PermissionPolicy.bounded(
        allow_tools=sorted(enabled),
        deny_tools=sorted(KNOWN_BUILTIN_TOOLS - enabled),
        mutation=MutationPolicy(
            allow_file_write="write_file" in enabled,
            allow_patch="edit_file" in enabled,
        ),
    )


def _is_manageable(
    supervisor: BackgroundSubAgentSupervisor,
    scope: BackgroundSubAgentScope,
    run_id: str,
) -> bool:
    checker = getattr(supervisor, "is_manageable", None)
    if checker is None:
        return False
    return bool(checker(scope, run_id))


def _stderr_tail(record: Any, run_store_dir: Path | None) -> str | None:
    if getattr(record, "status", "") not in {"failed", "cancelled"}:
        return None
    worker = getattr(record, "worker", None)
    stderr_log_path = getattr(worker, "stderr_log_path", None)
    if not stderr_log_path or run_store_dir is None:
        return None
    path = Path(stderr_log_path)
    try:
        path.resolve().relative_to(run_store_dir.resolve())
    except (OSError, ValueError):
        return None
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")[-4096:]


def _dump_json_value(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_dump_json_value(item) for item in value]
    return value

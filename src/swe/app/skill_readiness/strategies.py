# -*- coding: utf-8 -*-
"""技能可执行性检查策略。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ...app.crons.auth_state import (
    CRON_AUTH_FILE_NAME,
    CronAuthState,
    ensure_utc,
    utc_now,
)
from ...app.crons.models import CronJobSpec, cron_skill_ids_contains
from ...config.config import load_agent_config
from ...config.context import resolve_request_effective_tenant_id
from ...config.utils import get_tenant_secrets_dir, get_tenant_working_dir
from ...providers.models import ModelSlotConfig
from ...providers.provider_manager import ProviderManager
from .models import (
    SkillReadinessCheckConfig,
    SkillReadinessCheckResult,
    SkillReadinessOwner,
)

PROFILE_CHECK = "profile_identity_block"
BOUND_CRON_CHECK = "bound_cron_job"
CRON_AUTH_CHECK = "cron_auth_valid"
CRON_MODEL_CHECK = "cron_model_connection"
MCP_TOOLS_CHECK = "mcp_tools_available"

CHECK_DISPLAY_NAMES = {
    PROFILE_CHECK: "用户身份信息",
    BOUND_CRON_CHECK: "绑定定时任务",
    CRON_AUTH_CHECK: "定时任务鉴权",
    CRON_MODEL_CHECK: "模型连通性",
    MCP_TOOLS_CHECK: "MCP 工具可用性",
}

_IDENTITY_HEADING = "用户身份信息"
_IDENTITY_HEADING_PATTERN = re.compile(
    r"^\s*(?:#+\s*)?用户身份信息\s*$",
    re.MULTILINE,
)
_IDENTITY_FIELDS = ("分行号", "网点机构编号", "岗位编号", "客户经理ID")
_FIELD_PATTERN_TEMPLATE = r"{field}\s*[:：]\s*(?P<value>\S+)"
_MCP_SERVER_TIMEOUT_SECONDS = 10.0


class SkillReadinessStrategy(Protocol):
    """单个可执行性检查策略。"""

    name: str
    display_name: str

    async def run(
        self,
        context: "SkillReadinessCheckContext",
        config: SkillReadinessCheckConfig,
    ) -> SkillReadinessCheckResult:
        """执行检查并返回标准结果。"""


@dataclass(slots=True)
class SkillReadinessCheckContext:
    """单个用户执行检查时需要的运行时上下文。"""

    source_id: str
    skill_id: str
    owner: SkillReadinessOwner
    job_reader: Any | None = None
    workspace: Any | None = None
    passthrough_headers: dict[str, str] = field(default_factory=dict)

    @property
    def scope_id(self) -> str:
        return (
            resolve_request_effective_tenant_id(
                self.owner.user_id,
                self.source_id,
            )
            or self.owner.user_id
        )


class SkillReadinessStrategyRegistry:
    """可插拔检查策略注册表。"""

    def __init__(self, strategies: list[SkillReadinessStrategy] | None = None):
        self._strategies: dict[str, SkillReadinessStrategy] = {}
        for strategy in strategies or []:
            self.register(strategy)

    def register(self, strategy: SkillReadinessStrategy) -> None:
        self._strategies[strategy.name] = strategy

    def display_name_for(self, check_name: str) -> str:
        strategy = self._strategies.get(check_name)
        if strategy is not None:
            return strategy.display_name
        return CHECK_DISPLAY_NAMES.get(check_name, check_name)

    async def run_check(
        self,
        context: SkillReadinessCheckContext,
        config: SkillReadinessCheckConfig,
    ) -> SkillReadinessCheckResult:
        strategy = self._strategies.get(config.name)
        if strategy is None:
            return _result(
                config.name,
                self.display_name_for(config.name),
                "fail",
                "未注册的自检项",
                {"check_name": config.name},
                0,
            )
        try:
            return await strategy.run(context, config)
        except Exception as exc:  # noqa: BLE001
            return _result(
                config.name,
                strategy.display_name,
                "fail",
                "自检执行失败",
                {"error": str(exc)},
                0,
            )


class ProfileIdentityBlockStrategy:
    name = PROFILE_CHECK
    display_name = CHECK_DISPLAY_NAMES[PROFILE_CHECK]

    async def run(
        self,
        context: SkillReadinessCheckContext,
        config: SkillReadinessCheckConfig,
    ) -> SkillReadinessCheckResult:
        _ = config
        started_at = time.perf_counter()
        profile_path = _resolve_profile_path(context)
        if not profile_path.is_file():
            return _result(
                self.name,
                self.display_name,
                "fail",
                "PROFILE.md 不存在",
                {"path": str(profile_path)},
                _elapsed_ms(started_at),
            )
        try:
            content = profile_path.read_text(encoding="utf-8")
        except OSError as exc:
            return _result(
                self.name,
                self.display_name,
                "fail",
                "PROFILE.md 无法读取",
                {"error": str(exc)},
                _elapsed_ms(started_at),
            )

        if _IDENTITY_HEADING_PATTERN.search(content) is None:
            return _result(
                self.name,
                self.display_name,
                "fail",
                "PROFILE.md 缺少用户身份信息段落",
                {"heading": _IDENTITY_HEADING},
                _elapsed_ms(started_at),
            )

        missing_fields = [
            field
            for field in _IDENTITY_FIELDS
            if not _extract_profile_field(content, field)
        ]
        if missing_fields:
            return _result(
                self.name,
                self.display_name,
                "fail",
                "PROFILE.md 用户身份信息不完整",
                {"missing_fields": missing_fields},
                _elapsed_ms(started_at),
            )
        return _result(
            self.name,
            self.display_name,
            "pass",
            "PROFILE.md 用户身份信息完整",
            {},
            _elapsed_ms(started_at),
        )


class BoundCronJobStrategy:
    name = BOUND_CRON_CHECK
    display_name = CHECK_DISPLAY_NAMES[BOUND_CRON_CHECK]

    async def run(
        self,
        context: SkillReadinessCheckContext,
        config: SkillReadinessCheckConfig,
    ) -> SkillReadinessCheckResult:
        _ = config
        started_at = time.perf_counter()
        jobs = await _list_bound_jobs(context)
        enabled_jobs = [job for job in jobs if bool(job.enabled)]
        if enabled_jobs:
            return _result(
                self.name,
                self.display_name,
                "pass",
                "存在绑定当前技能的启用定时任务",
                {"job_ids": [job.id for job in enabled_jobs]},
                _elapsed_ms(started_at),
            )
        if jobs:
            return _result(
                self.name,
                self.display_name,
                "fail",
                "存在绑定任务但均未启用",
                {"job_ids": [job.id for job in jobs]},
                _elapsed_ms(started_at),
            )
        return _result(
            self.name,
            self.display_name,
            "fail",
            "未找到绑定当前技能的定时任务",
            {},
            _elapsed_ms(started_at),
        )


class CronAuthValidStrategy:
    name = CRON_AUTH_CHECK
    display_name = CHECK_DISPLAY_NAMES[CRON_AUTH_CHECK]

    async def run(
        self,
        context: SkillReadinessCheckContext,
        config: SkillReadinessCheckConfig,
    ) -> SkillReadinessCheckResult:
        _ = config
        started_at = time.perf_counter()
        auth_path = (
            get_tenant_secrets_dir(context.scope_id) / CRON_AUTH_FILE_NAME
        )
        if not auth_path.is_file():
            return _result(
                self.name,
                self.display_name,
                "fail",
                "cron_auth.json 不存在",
                {"path": str(auth_path)},
                _elapsed_ms(started_at),
            )
        try:
            raw_state = json.loads(auth_path.read_text(encoding="utf-8"))
            state = CronAuthState.model_validate(raw_state)
            expires_at = ensure_utc(state.user_info_expires_at)
        except (OSError, ValueError, TypeError) as exc:
            return _result(
                self.name,
                self.display_name,
                "fail",
                "cron_auth.json 内容无效",
                {"error": str(exc)},
                _elapsed_ms(started_at),
            )

        if expires_at is None:
            return _result(
                self.name,
                self.display_name,
                "fail",
                "cron_auth.json 缺少 user_info_expires_at",
                {},
                _elapsed_ms(started_at),
            )
        if expires_at <= utc_now():
            return _result(
                self.name,
                self.display_name,
                "fail",
                "cron_auth 已过期",
                {"user_info_expires_at": expires_at.isoformat()},
                _elapsed_ms(started_at),
            )
        return _result(
            self.name,
            self.display_name,
            "pass",
            "cron_auth 有效",
            {"user_info_expires_at": expires_at.isoformat()},
            _elapsed_ms(started_at),
        )


class CronModelConnectionStrategy:
    name = CRON_MODEL_CHECK
    display_name = CHECK_DISPLAY_NAMES[CRON_MODEL_CHECK]

    async def run(
        self,
        context: SkillReadinessCheckContext,
        config: SkillReadinessCheckConfig,
    ) -> SkillReadinessCheckResult:
        _ = config
        started_at = time.perf_counter()
        jobs = [
            job
            for job in await _list_bound_jobs(context)
            if job.enabled and job.task_type == "agent"
        ]
        if not jobs:
            return _result(
                self.name,
                self.display_name,
                "skip",
                "没有需要模型执行的绑定定时任务",
                {},
                _elapsed_ms(started_at),
            )

        manager = _get_provider_manager(context.scope_id)
        model_slots, missing_default = _resolve_model_slots(manager, jobs)
        if missing_default:
            return _result(
                self.name,
                self.display_name,
                "fail",
                "存在未配置模型且租户默认模型缺失的任务",
                {"job_ids": missing_default},
                _elapsed_ms(started_at),
            )

        failures: list[dict[str, Any]] = []
        tested: list[dict[str, str]] = []
        for slot in model_slots:
            provider = manager.get_provider(slot.provider_id)
            if provider is None:
                failures.append(
                    {
                        "provider_id": slot.provider_id,
                        "model": slot.model,
                        "message": "provider not found",
                    },
                )
                continue
            if not provider.has_model(slot.model):
                failures.append(
                    {
                        "provider_id": slot.provider_id,
                        "model": slot.model,
                        "message": "model not found",
                    },
                )
                continue
            ok, message = await provider.check_model_connection(
                model_id=slot.model,
            )
            tested.append(
                {"provider_id": slot.provider_id, "model": slot.model},
            )
            if not ok:
                failures.append(
                    {
                        "provider_id": slot.provider_id,
                        "model": slot.model,
                        "message": message,
                    },
                )

        if failures:
            return _result(
                self.name,
                self.display_name,
                "fail",
                "部分模型不可联通",
                {"failures": failures, "tested": tested},
                _elapsed_ms(started_at),
            )
        return _result(
            self.name,
            self.display_name,
            "pass",
            "绑定任务模型均可联通",
            {"tested": tested},
            _elapsed_ms(started_at),
        )


class McpToolsAvailableStrategy:
    name = MCP_TOOLS_CHECK
    display_name = CHECK_DISPLAY_NAMES[MCP_TOOLS_CHECK]

    async def run(
        self,
        context: SkillReadinessCheckContext,
        config: SkillReadinessCheckConfig,
    ) -> SkillReadinessCheckResult:
        started_at = time.perf_counter()
        servers = config.params.get("servers") or []
        if not servers:
            return _result(
                self.name,
                self.display_name,
                "skip",
                "未配置需要检查的 MCP 服务",
                {},
                _elapsed_ms(started_at),
            )

        mcp_config = _get_mcp_config(context)
        configured_clients = getattr(mcp_config, "clients", {}) or {}
        failures: list[dict[str, Any]] = []
        for requirement in servers:
            server_name = str(requirement.get("name") or "").strip()
            required_tools = [
                str(tool) for tool in requirement.get("tools") or []
            ]
            client_config = _find_mcp_client_config(
                configured_clients,
                server_name,
            )
            if client_config is None:
                failures.append(
                    {"server": server_name, "message": "server not found"},
                )
                continue
            if not getattr(client_config, "enabled", False):
                failures.append(
                    {"server": server_name, "message": "server disabled"},
                )
                continue
            missing_tools = await _list_missing_mcp_tools(
                client_config,
                required_tools,
                context,
            )
            if missing_tools:
                failures.append(
                    {
                        "server": server_name,
                        "message": "tools missing or list failed",
                        "missing_tools": missing_tools,
                    },
                )

        if failures:
            return _result(
                self.name,
                self.display_name,
                "fail",
                "MCP 服务或工具不可用",
                {"failures": failures},
                _elapsed_ms(started_at),
            )
        return _result(
            self.name,
            self.display_name,
            "pass",
            "MCP 服务和工具均可用",
            {},
            _elapsed_ms(started_at),
        )


def build_default_strategy_registry() -> SkillReadinessStrategyRegistry:
    """构造内置策略注册表。"""
    return SkillReadinessStrategyRegistry(
        [
            ProfileIdentityBlockStrategy(),
            BoundCronJobStrategy(),
            CronAuthValidStrategy(),
            CronModelConnectionStrategy(),
            McpToolsAvailableStrategy(),
        ],
    )


def _result(
    check_name: str,
    display_name: str,
    status: str,
    message: str,
    details: dict[str, Any],
    duration_ms: int,
) -> SkillReadinessCheckResult:
    return SkillReadinessCheckResult(
        check_name=check_name,
        display_name=display_name,
        status=status,
        message=message,
        details=details,
        duration_ms=duration_ms,
    )


def _elapsed_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


def _extract_profile_field(content: str, field: str) -> str | None:
    match = re.search(
        _FIELD_PATTERN_TEMPLATE.format(field=re.escape(field)),
        content,
    )
    if match is None:
        return None
    value = match.group("value").strip()
    return value or None


def _resolve_profile_path(context: SkillReadinessCheckContext) -> Path:
    workspace_dir = getattr(context.workspace, "workspace_dir", None)
    if workspace_dir:
        return Path(workspace_dir).expanduser() / "PROFILE.md"
    return get_tenant_working_dir(context.scope_id) / "PROFILE.md"


async def _list_bound_jobs(
    context: SkillReadinessCheckContext,
) -> list[CronJobSpec]:
    from ..crons.managed_query_client import ManagedCronQueryClient
    from ..crons.managed_models import load_managed_spec

    reader = context.job_reader or ManagedCronQueryClient(
        headers=context.passthrough_headers,
    )
    rows = await reader.list_jobs(
        context.owner.user_id,
        context.source_id,
        getattr(context.workspace, "agent_id", "default"),
    )
    jobs = [load_managed_spec(row) for row in rows]
    return [
        job
        for job in jobs
        if cron_skill_ids_contains(
            getattr(job, "skill_ids", ""),
            context.skill_id,
        )
    ]


def _get_provider_manager(scope_id: str) -> ProviderManager:
    ProviderManager.ensure_tenant_provider_storage(scope_id)
    return ProviderManager.get_instance(scope_id)


def _resolve_model_slots(
    manager: ProviderManager,
    jobs: list[CronJobSpec],
) -> tuple[list[ModelSlotConfig], list[str]]:
    active_model = manager.get_active_model()
    slots_by_key: dict[tuple[str, str], ModelSlotConfig] = {}
    missing_default: list[str] = []
    for job in jobs:
        slot = job.model_slot or active_model
        if slot is None:
            missing_default.append(job.id)
            continue
        key = (slot.provider_id, slot.model)
        slots_by_key[key] = slot
    return list(slots_by_key.values()), missing_default


def _find_mcp_client_config(
    configured_clients: dict[str, Any],
    server_name: str,
) -> Any | None:
    if server_name in configured_clients:
        return configured_clients[server_name]
    for client in configured_clients.values():
        if getattr(client, "name", None) == server_name:
            return client
    return None


def _get_mcp_config(context: SkillReadinessCheckContext) -> Any | None:
    workspace = context.workspace
    agent_id = getattr(workspace, "agent_id", None)
    if agent_id:
        tenant_id = getattr(workspace, "tenant_id", None) or context.scope_id
        try:
            return getattr(
                load_agent_config(str(agent_id), tenant_id=tenant_id),
                "mcp",
                None,
            )
        except Exception:  # noqa: BLE001
            pass
    return getattr(getattr(workspace, "config", None), "mcp", None)


async def _list_missing_mcp_tools(
    client_config: Any,
    required_tools: list[str],
    context: SkillReadinessCheckContext,
) -> list[str]:
    from ...app.runner.runner import (
        _cleanup_mcp_clients,
        _create_mcp_client_with_headers,
    )

    client = await _create_mcp_client_with_headers(
        client_config,
        passthrough_headers=context.passthrough_headers,
    )
    if client is None:
        return required_tools or ["<connect>"]
    try:
        await asyncio.wait_for(
            client.connect(),
            timeout=_MCP_SERVER_TIMEOUT_SECONDS,
        )
        tools_response = await asyncio.wait_for(
            client.list_tools(timeout=_MCP_SERVER_TIMEOUT_SECONDS),
            timeout=_MCP_SERVER_TIMEOUT_SECONDS,
        )
        available = _extract_tool_names(tools_response)
        return [tool for tool in required_tools if tool not in available]
    except Exception as exc:  # noqa: BLE001
        return required_tools or [str(exc)]
    finally:
        await _cleanup_mcp_clients([client])


def _extract_tool_names(tools_response: Any) -> set[str]:
    tools = getattr(tools_response, "tools", tools_response)
    if isinstance(tools, dict):
        tools = tools.get("tools", [])
    names: set[str] = set()
    for tool in tools or []:
        if isinstance(tool, dict):
            name = tool.get("name")
        else:
            name = getattr(tool, "name", None)
        if name:
            names.add(str(name))
    return names

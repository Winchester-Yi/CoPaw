# -*- coding: utf-8 -*-
"""心跳和梦境实际执行，不持有旧任务仓库、状态缓存或调度平台绑定。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from ...config.context import is_valid_identity_value, resolve_runtime_identity
from ...config.llm_workload import LLM_WORKLOAD_CRON, bind_llm_workload
from ..continuous_governance.models import GOVERNANCE_ID_MAX_LENGTH
from ..source_system_config.runtime import (
    bind_source_system_config,
    reset_current_source_system_config,
    set_current_source_system_config,
)
from ..tenant_context import bind_tenant_context

logger = logging.getLogger(__name__)
_EVERY_PATTERN = re.compile(
    r"^(?:(?P<hours>\d+)h|(?P<minutes>\d+)m)$",
    re.IGNORECASE,
)


def every_to_cron(every: str) -> str:
    """将 interval 字符串转换为 5 字段 cron 表达式。

    支持格式：
    - "Nm"（1-59 分钟）→ "*/N * * * *"
    - "Nh"（1-23 小时）→ "0 */N * * *"
    如果 every 已经是 5 字段 cron，直接返回。
    """
    from .heartbeat import is_cron_expression

    every = (every or "").strip()
    if not every:
        return ""
    if is_cron_expression(every):
        return every

    m = _EVERY_PATTERN.match(every)
    if not m:
        logger.warning(
            "Failed to parse every=%r, fallback to 0/30 * * * *",
            every,
        )
        return "0/30 * * * *"

    if m.group("hours"):
        hours = int(m.group("hours"))
        if 1 <= hours <= 23:
            return f"0 0/{hours} * * *"
        logger.warning(
            "Hours out of range (1-23): %s, fallback to 0/30 * * * *",
            hours,
        )
        return "0/30 * * * *"

    minutes = int(m.group("minutes"))
    if 1 <= minutes <= 59:
        return f"0/{minutes} * * * *"
    logger.warning(
        "Minutes out of range (1-59): %s, fallback to 0/30 * * * *",
        minutes,
    )
    return "0/30 * * * *"


class SystemTaskRuntime:
    def __init__(
        self,
        *,
        runner,
        channel_manager,
        agent_id=None,
        tenant_id=None,
        source_system_config_service=None,
        continuous_governance_service=None,
    ):
        self._runner = runner
        self._channel_manager = channel_manager
        self._agent_id = agent_id
        self._tenant_id = tenant_id
        self._source_system_config_service = source_system_config_service
        self._continuous_governance_service = continuous_governance_service

    @classmethod
    def from_workspace(cls, workspace):
        return cls(
            runner=workspace.runner,
            channel_manager=workspace.channel_manager,
            agent_id=workspace.agent_id,
            tenant_id=workspace.tenant_id,
            source_system_config_service=getattr(
                workspace,
                "_source_system_config_service",
                None,
            ),
            continuous_governance_service=getattr(
                workspace,
                "_continuous_governance_service",
                None,
            ),
        )

    def _resolve_scheduled_run_source_id(
        self,
        source_id: str | None,
        scope_id: str | None,
    ) -> str | None:
        """优先用显式 source_id，缺失时从 runtime scope 解码。"""
        if source_id:
            return source_id
        if scope_id:
            _, resolved_source_id, _ = resolve_runtime_identity(scope_id)
            if resolved_source_id:
                return resolved_source_id
        return None

    async def _resolve_scheduled_run_source_system_config(
        self,
        *,
        source_id: str | None,
        scope_id: str | None,
        boundary_name: str,
    ) -> Any | None:
        """按 Scheduled Run Boundary 规则解析 source system config。"""
        resolved_source_id = self._resolve_scheduled_run_source_id(
            source_id,
            scope_id,
        )
        if resolved_source_id is None:
            logger.warning(
                "Scheduled run source config binding skipped: boundary=%s "
                "reason=no_source_identity",
                boundary_name,
            )
            return None

        if self._source_system_config_service is None:
            logger.debug(
                "Scheduled run source config binding skipped: boundary=%s "
                "source_id=%s reason=no_service",
                boundary_name,
                resolved_source_id,
            )
            return None

        return await self._source_system_config_service.resolve_config(
            resolved_source_id,
        )

    @contextmanager
    def _bind_scheduled_run_source_system_config(
        self,
        source_system_config: Any | None,
    ):
        """Bind resolved source config, or explicitly clear inherited request config."""
        if source_system_config is not None:
            with bind_source_system_config(source_system_config):
                yield
            return

        token = set_current_source_system_config(cast(Any, None))
        try:
            yield
        finally:
            reset_current_source_system_config(token)

    async def run_heartbeat(self, *, managed_execution: bool = False) -> None:
        """执行一次心跳任务（供外部调度平台调用）。"""
        from .heartbeat import run_heartbeat_once

        workspace_dir, tenant_id, source_id, scope_id, runtime_tenant_id = (
            self._resolve_runtime_context()
        )
        source_system_config = (
            await self._resolve_scheduled_run_source_system_config(
                source_id=source_id,
                scope_id=scope_id,
                boundary_name="heartbeat",
            )
        )
        with (
            bind_tenant_context(
                tenant_id=tenant_id,
                workspace_dir=workspace_dir,
                source_id=source_id,
                scope_id=scope_id,
            ),
            bind_llm_workload(LLM_WORKLOAD_CRON),
            self._bind_scheduled_run_source_system_config(
                source_system_config,
            ),
        ):
            await run_heartbeat_once(
                runner=self._runner,
                channel_manager=self._channel_manager,
                agent_id=self._agent_id,
                tenant_id=runtime_tenant_id,
                workspace_dir=workspace_dir,
                **({"managed_execution": True} if managed_execution else {}),
            )

    async def run_dream(
        self,
        *,
        include_archive_maintenance: bool = True,
    ) -> None:
        """执行一次梦境任务（供外部调度平台调用）。"""
        workspace_dir, tenant_id, source_id, scope_id, runtime_tenant_id = (
            self._resolve_runtime_context()
        )
        source_system_config = (
            await self._resolve_scheduled_run_source_system_config(
                source_id=source_id,
                scope_id=scope_id,
                boundary_name="dream",
            )
        )
        with (
            bind_tenant_context(
                tenant_id=tenant_id,
                workspace_dir=workspace_dir,
                source_id=source_id,
                scope_id=scope_id,
            ),
            bind_llm_workload(LLM_WORKLOAD_CRON),
            self._bind_scheduled_run_source_system_config(
                source_system_config,
            ),
        ):
            before_record_ids = await self._load_dream_record_ids(
                workspace_dir,
            )
            await self._runner.memory_manager.dream_memory(
                tenant_id=runtime_tenant_id,
                trigger="cron",
            )
            governance_agent_id = self._continuous_governance_target_agent_id()
            await self._dual_write_dream_records(
                workspace_dir=workspace_dir,
                source_id=source_id,
                tenant_id=tenant_id,
                agent_id=governance_agent_id,
                before_record_ids=before_record_ids,
            )
            if include_archive_maintenance and workspace_dir is not None:
                from ..routers.dream_logs import (
                    dual_write_dream_archive_maintenance_result,
                    run_dream_archive_maintenance,
                )

                maintenance = await asyncio.to_thread(
                    run_dream_archive_maintenance,
                    workspace_dir,
                    actor="dream_cron",
                )
                if (
                    maintenance is not None
                    and self._continuous_governance_service is not None
                    and source_id
                    and tenant_id
                    and governance_agent_id
                ):
                    await dual_write_dream_archive_maintenance_result(
                        service=self._continuous_governance_service,
                        source_id=source_id,
                        target_user_id=tenant_id,
                        target_agent_id=governance_agent_id,
                        maintenance=maintenance,
                        actor="dream_cron",
                        source_name=source_id,
                    )
        logger.debug("Dream task executed successfully")

    async def _load_dream_record_ids(
        self,
        workspace_dir: Path | None,
    ) -> set[str]:
        """读取 dream 执行前已有记录 id。"""
        if workspace_dir is None:
            return set()
        data = await self._load_dream_logs(workspace_dir)
        return {
            str(record.get("id") or "")
            for record in data.get("records", [])
            if isinstance(record, dict) and record.get("id")
        }

    def _continuous_governance_target_agent_id(self) -> str | None:
        """返回可安全写入持续治理读模型的 agent 标识。"""
        agent_id = self._agent_id or "default"
        if len(
            agent_id,
        ) > GOVERNANCE_ID_MAX_LENGTH or not is_valid_identity_value(agent_id):
            logger.warning(
                "Skip continuous governance dual-write for invalid agent id",
            )
            return None
        return agent_id

    async def _dual_write_dream_records(
        self,
        *,
        workspace_dir: Path | None,
        source_id: str | None,
        tenant_id: str | None,
        agent_id: str | None,
        before_record_ids: set[str],
    ) -> None:
        """cron dream 完成后把新增治理记录写入数据库读模型。"""
        service = self._continuous_governance_service
        if (
            workspace_dir is None
            or not source_id
            or not tenant_id
            or not agent_id
        ):
            return
        if service is None:
            return
        data = await self._load_dream_logs(workspace_dir)
        for record in data.get("records", []):
            if not isinstance(record, dict):
                continue
            record_id = str(record.get("id") or "")
            if not record_id or record_id in before_record_ids:
                continue
            await service.upsert_workspace_governance_record_with_health(
                source_id=source_id,
                target_user_id=tenant_id,
                target_user_name=None,
                bbk_id=None,
                target_agent_id=agent_id,
                record=record,
            )

    async def _load_dream_logs(self, workspace_dir: Path) -> dict[str, Any]:
        """在线程池中读取 workspace dream_logs.json。"""
        return await asyncio.to_thread(
            self._load_dream_logs_sync,
            workspace_dir,
        )

    def _load_dream_logs_sync(self, workspace_dir: Path) -> dict[str, Any]:
        """同步读取 workspace dream_logs.json，供线程池包装调用。"""
        path = workspace_dir / "dream_logs.json"
        if not path.exists():
            return {"records": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"records": []}
        return data if isinstance(data, dict) else {"records": []}

    def _resolve_runtime_context(
        self,
    ) -> tuple[Path | None, str | None, str | None, str | None, str | None]:
        """从工作区 runtime tenant 还原 logical tenant/source/scope。"""
        workspace_dir_value = getattr(self._runner, "workspace_dir", None)
        workspace_dir = (
            Path(workspace_dir_value) if workspace_dir_value else None
        )
        runtime_tenant_id = self._tenant_id
        workspace = getattr(self._runner, "_workspace", None)
        workspace_tenant_id = getattr(workspace, "tenant_id", None)
        if workspace_tenant_id:
            runtime_tenant_id = workspace_tenant_id

        tenant_id, source_id, scope_id = resolve_runtime_identity(
            runtime_tenant_id,
        )
        return (
            workspace_dir,
            tenant_id,
            source_id,
            scope_id,
            scope_id or tenant_id,
        )

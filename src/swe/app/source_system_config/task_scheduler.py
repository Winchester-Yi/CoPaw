# -*- coding: utf-8 -*-
"""Source 级系统任务调度器。

配置保存仅提交Scheduler刷新意图，业务清理/归档仍在SWE执行。
旧构造参数保留兼容，但新链路不读写旧绑定或调用外部调度适配器。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from swe.app.crons.scheduler_adapter import SchedulerAdapter
from swe.app.file_governance.archive_maintenance import (
    archive_old_orphans_for_workspace,
)
from swe.config.context import encode_scope_id, is_valid_identity_value

from .task_binding_store import (
    SourceSystemTaskBinding,
    SourceSystemTaskBindingStore,
)

from .runtime import (
    resolve_archive_maintenance_config,
)

SOURCE_TASK_SESSION_CLEANUP_JOB_ID = "_source_task_session_cleanup"
SOURCE_TASK_SESSION_CLEANUP_NAME = "task_session_cleanup"
SOURCE_ARCHIVE_MAINTENANCE_JOB_ID = "_source_archive_maintenance"
SOURCE_ARCHIVE_MAINTENANCE_NAME = "archive_maintenance"
SOURCE_ARCHIVE_MAINTENANCE_TASK_TYPE = "archive_maintenance"


@dataclass(frozen=True, slots=True)
class SourceSchedulerIdentity:
    """记录最后一次修改 source 系统任务配置的调度身份。"""

    tenant_id: str
    from_id: str
    updated_by: str | None = None
    role: str | None = None


@dataclass(frozen=True, slots=True)
class SourceTaskSessionCleanupRefreshResult:
    """描述 source 级 cleanup 调度刷新的结果。"""

    action: str
    binding: SourceSystemTaskBinding | None = None
    scheduler_sync: dict[str, Any] | None = None


SourceArchiveMaintenanceRefreshResult = SourceTaskSessionCleanupRefreshResult


class SourceTaskBindingStoreLike(Protocol):
    """source 系统任务绑定存储所需的最小接口。"""

    async def get_binding(
        self,
        source_id: str,
        task_type: str,
    ) -> SourceSystemTaskBinding | None:
        """读取 source 系统任务绑定。"""

    async def upsert_binding(
        self,
        *,
        source_id: str,
        task_type: str,
        external_job_id: str,
        cron: str,
        enabled: bool,
        scheduler_tenant_id: str | None = None,
        scheduler_from_id: str | None = None,
        updated_by: str | None = None,
    ) -> SourceSystemTaskBinding:
        """创建或更新 source 系统任务绑定。"""


class SourceTenantScopeStoreLike(Protocol):
    """按 source 查询 tenant 初始化记录的最小接口。"""

    async def get_by_source(
        self,
        source_id: str,
        *,
        include_templates: bool = False,
    ) -> list[dict[str, Any]]:
        """返回 source 下的 tenant 初始化记录。"""


class SourceWorkspaceManagerLike(Protocol):
    """按 runtime scope 获取工作区的最小接口。"""

    async def get_agent(
        self,
        agent_id: str,
        tenant_id: str | None = None,
    ) -> object:
        """返回指定 agent 和 runtime tenant scope 对应的工作区。"""


class SourceSystemTaskScheduler:
    """同步 source 级系统任务到外部调度平台。"""

    def __init__(
        self,
        binding_store: (
            SourceSystemTaskBindingStore | SourceTaskBindingStoreLike
        ),
        scheduler_adapter: SchedulerAdapter,
        callback_url: str,
        tenant_scope_store: SourceTenantScopeStoreLike | None = None,
        tenant_scope_store_factory: (
            Callable[[], SourceTenantScopeStoreLike | None] | None
        ) = None,
        multi_agent_manager: SourceWorkspaceManagerLike | None = None,
        tenant_dir_resolver: Callable[[str], Path] | None = None,
        continuous_governance_service_factory: (
            Callable[[], object | None] | None
        ) = None,
        source_config_resolver: (
            Callable[[str], Awaitable[object]] | None
        ) = None,
        agent_id: str = "default",
    ) -> None:
        """初始化调度器依赖。"""
        self._binding_store = binding_store
        self._scheduler_adapter = scheduler_adapter
        self._callback_url = callback_url
        self._tenant_scope_store = tenant_scope_store
        self._tenant_scope_store_factory = tenant_scope_store_factory
        self._multi_agent_manager = multi_agent_manager
        self._tenant_dir_resolver = tenant_dir_resolver
        self._continuous_governance_service_factory = (
            continuous_governance_service_factory
        )
        self._source_config_resolver = source_config_resolver
        self._agent_id = agent_id

    async def refresh_task_session_cleanup(
        self,
        source_id: str,
        config: object,
        identity: SourceSchedulerIdentity,
    ) -> SourceTaskSessionCleanupRefreshResult:
        """保留配置保存挂点，只提交新版意图，不读取旧来源绑定。"""
        from ..crons.system_bridge import submit_source_refresh

        result = await submit_source_refresh(
            source_id,
            identity,
            "task_session_cleanup",
        )
        return SourceTaskSessionCleanupRefreshResult(
            action=(
                "pending"
                if result["submission_status"] == "accepted"
                else "submission_failed"
            ),
            scheduler_sync=result,
        )

    async def refresh_archive_maintenance(
        self,
        source_id: str,
        config: object,
        identity: SourceSchedulerIdentity,
    ) -> SourceArchiveMaintenanceRefreshResult:
        """归档配置只提交刷新，由Scheduler读当前设置并管理新平台绑定。"""
        from ..crons.system_bridge import submit_source_refresh

        result = await submit_source_refresh(
            source_id,
            identity,
            "archive_maintenance",
        )
        return SourceArchiveMaintenanceRefreshResult(
            action=(
                "pending"
                if result["submission_status"] == "accepted"
                else "submission_failed"
            ),
            scheduler_sync=result,
        )

    async def run_task_session_cleanup(
        self,
        *,
        source_id: str,
        managed_cleanup=None,
    ) -> dict[str, Any]:
        """清理指定 source 下所有 runtime scope 的任务会话历史。"""
        result: dict[str, Any] = {
            "source_id": source_id,
            "scopes_seen": 0,
            "scopes_failed": 0,
            "sessions_seen": 0,
            "sessions_cleaned": 0,
            "sessions_skipped_locked": 0,
            "runs_removed": 0,
            "messages_removed": 0,
            "results": [],
            "errors": [],
        }
        tenant_scope_store = self._get_tenant_scope_store()
        if tenant_scope_store is None or self._multi_agent_manager is None:
            result["errors"].append("source cleanup dependencies unavailable")
            return result

        rows = await tenant_scope_store.get_by_source(
            source_id,
            include_templates=False,
        )
        result["scopes_seen"] = len(rows)
        for row in rows:
            await self._run_scope_cleanup(
                source_id,
                row,
                result,
                managed_cleanup,
            )
        return result

    def _get_tenant_scope_store(self) -> SourceTenantScopeStoreLike | None:
        """运行时获取 tenant/source 初始化来源存储。"""
        if self._tenant_scope_store is not None:
            return self._tenant_scope_store
        if self._tenant_scope_store_factory is None:
            return None
        return self._tenant_scope_store_factory()

    async def _run_scope_cleanup(
        self,
        source_id: str,
        row: dict[str, Any],
        summary: dict[str, Any],
        managed_cleanup=None,
    ) -> None:
        """执行单个 runtime scope 的清理并把结果累加到 source 汇总。"""
        tenant_id = str(row.get("tenant_id") or "")
        row_source_id = str(row.get("source_id") or source_id)
        runtime_tenant_id = ""
        try:
            if not tenant_id:
                raise RuntimeError("tenant_id missing")
            runtime_tenant_id = encode_scope_id(tenant_id, row_source_id)
            assert self._multi_agent_manager is not None
            workspace = await self._multi_agent_manager.get_agent(
                self._agent_id,
                tenant_id=runtime_tenant_id,
            )
            scope_result, cleanup_error = await self._cleanup_scope_business(
                workspace,
                tenant_id,
                row_source_id,
                managed_cleanup,
            )
            if cleanup_error is not None and managed_cleanup is None:
                raise cleanup_error
            self._merge_scope_cleanup_result(
                summary,
                tenant_id,
                runtime_tenant_id,
                scope_result,
            )
            if cleanup_error is not None:
                raise cleanup_error
        except Exception as exc:  # noqa: BLE001
            summary["scopes_failed"] += 1
            summary["errors"].append(
                {
                    "tenant_id": tenant_id,
                    "runtime_tenant_id": runtime_tenant_id,
                    "error": str(exc),
                },
            )

    @staticmethod
    async def _cleanup_scope_business(
        workspace,
        tenant_id,
        source_id,
        managed_cleanup,
    ):
        """保留旧清理行为，但旧分支失败不能阻止数据库任务的会话维护。"""
        legacy, error = {}, None
        try:
            cron = getattr(workspace, "cron_manager", None)
            if cron is not None:
                legacy = await cron.run_task_session_cleanup()
            else:
                from ..crons.legacy_session_cleanup import (
                    cleanup_legacy_workspace_sessions,
                )

                legacy = await cleanup_legacy_workspace_sessions(workspace)
        except Exception as exc:  # noqa: BLE001
            error = exc
        if managed_cleanup is None:
            return legacy, error
        try:
            managed = await managed_cleanup(workspace, tenant_id, source_id)
        except Exception as exc:  # noqa: BLE001
            return legacy, error or exc
        return (
            SourceSystemTaskScheduler._combined_cleanup_result(
                legacy,
                managed,
            ),
            error,
        )

    @staticmethod
    def _combined_cleanup_result(legacy, managed):
        """只合并维护计数，不把新版绑定写回旧任务仓库。"""
        result = dict(legacy or {})
        for key in (
            "sessions_seen",
            "sessions_cleaned",
            "sessions_skipped_locked",
            "runs_removed",
            "messages_removed",
        ):
            result[key] = int(result.get(key, 0) or 0) + int(
                managed.get(key, 0) or 0,
            )
        return result

    @staticmethod
    def _merge_scope_cleanup_result(
        summary: dict[str, Any],
        tenant_id: str,
        runtime_tenant_id: str,
        scope_result: dict[str, Any] | None,
    ) -> None:
        """把单个 scope 的清理结果累加到 source 汇总。"""
        result = scope_result if isinstance(scope_result, dict) else {}
        summary["results"].append(
            {
                "tenant_id": tenant_id,
                "runtime_tenant_id": runtime_tenant_id,
                **result,
            },
        )
        for key in (
            "sessions_seen",
            "sessions_cleaned",
            "sessions_skipped_locked",
            "runs_removed",
            "messages_removed",
        ):
            summary[key] += int(result.get(key, 0) or 0)

    async def run_archive_maintenance(
        self,
        *,
        source_id: str,
        config: object | None = None,
    ) -> dict[str, Any]:
        archive_config = await self._resolve_archive_maintenance_config(
            source_id,
            config,
        )
        result: dict[str, Any] = {
            "source_id": source_id,
            "enabled": archive_config.enabled,
            "tenants_seen": 0,
            "workspaces_seen": 0,
            "workspaces_processed": 0,
            "workspaces_failed": 0,
            "files_archived": 0,
            "archived_size_bytes": 0,
            "candidates_seen": 0,
            "files_skipped_limit": 0,
            "results": [],
            "errors": [],
            "timed_out": False,
        }
        if not archive_config.enabled:
            return result

        tenant_scope_store = self._get_tenant_scope_store()
        if tenant_scope_store is None or self._tenant_dir_resolver is None:
            result["errors"].append("source archive dependencies unavailable")
            return result

        rows = await tenant_scope_store.get_by_source(
            source_id,
            include_templates=False,
        )
        result["tenants_seen"] = len(rows)
        started_at = time.monotonic()
        for row in rows:
            if self._archive_run_timed_out(started_at, archive_config):
                result["timed_out"] = True
                break
            if result["files_archived"] >= archive_config.max_files_per_run:
                break
            await self._run_scope_archive_maintenance(
                source_id,
                row,
                archive_config,
                result,
                started_at,
            )
            if (
                result["workspaces_processed"]
                >= archive_config.max_workspaces_per_run
            ):
                break
        return result

    async def _resolve_archive_maintenance_config(
        self,
        source_id: str,
        config: object | None,
    ):
        if config is not None:
            return resolve_archive_maintenance_config(config)
        if self._source_config_resolver is None:
            return resolve_archive_maintenance_config(None)
        resolved = await self._source_config_resolver(source_id)
        return resolve_archive_maintenance_config(resolved)

    @staticmethod
    def _archive_run_timed_out(
        started_at: float,
        archive_config: object,
    ) -> bool:
        timeout_seconds = int(getattr(archive_config, "timeout_seconds", 0))
        return 0 < timeout_seconds <= time.monotonic() - started_at

    async def _run_scope_archive_maintenance(
        self,
        source_id: str,
        row: dict[str, Any],
        archive_config: object,
        summary: dict[str, Any],
        started_at: float,
    ) -> None:
        tenant_id = str(row.get("tenant_id") or "")
        row_source_id = str(row.get("source_id") or source_id)
        runtime_tenant_id = ""
        try:
            if not tenant_id:
                raise RuntimeError("tenant_id missing")
            runtime_tenant_id = encode_scope_id(tenant_id, row_source_id)
            assert self._tenant_dir_resolver is not None
            tenant_dir = self._tenant_dir_resolver(runtime_tenant_id)
            for agent_id, workspace_dir in self._iter_agent_workspace_dirs(
                tenant_dir,
            ):
                if self._archive_run_timed_out(started_at, archive_config):
                    summary["timed_out"] = True
                    return
                if summary["workspaces_processed"] >= getattr(
                    archive_config,
                    "max_workspaces_per_run",
                ):
                    return
                if summary["files_archived"] >= getattr(
                    archive_config,
                    "max_files_per_run",
                ):
                    return
                await self._run_workspace_archive_maintenance(
                    source_id,
                    tenant_id,
                    runtime_tenant_id,
                    agent_id,
                    workspace_dir,
                    archive_config,
                    summary,
                )
        except Exception as exc:  # noqa: BLE001
            summary["workspaces_failed"] += 1
            summary["errors"].append(
                {
                    "tenant_id": tenant_id,
                    "runtime_tenant_id": runtime_tenant_id,
                    "error": str(exc),
                },
            )

    async def _run_workspace_archive_maintenance(
        self,
        source_id: str,
        tenant_id: str,
        runtime_tenant_id: str,
        agent_id: str,
        workspace_dir: Path,
        archive_config: object,
        summary: dict[str, Any],
    ) -> None:
        summary["workspaces_seen"] += 1
        try:
            remaining_files = int(
                getattr(archive_config, "max_files_per_run"),
            ) - int(summary["files_archived"])
            workspace_result = archive_old_orphans_for_workspace(
                workspace_dir,
                old_orphan_days=int(
                    getattr(archive_config, "old_orphan_days"),
                ),
                max_files=int(
                    getattr(archive_config, "max_files_per_workspace"),
                ),
                remaining_files=remaining_files,
                actor="source_archive_maintenance",
            )
            summary["workspaces_processed"] += 1
            summary["files_archived"] += len(workspace_result.archived_items)
            summary[
                "archived_size_bytes"
            ] += workspace_result.archived_size_bytes
            summary["candidates_seen"] += workspace_result.candidates_count
            summary["files_skipped_limit"] += workspace_result.skipped_files
            summary["results"].append(
                {
                    "tenant_id": tenant_id,
                    "runtime_tenant_id": runtime_tenant_id,
                    "agent_id": agent_id,
                    "workspace_dir": str(workspace_dir),
                    "files_archived": len(workspace_result.archived_items),
                    "archived_paths": workspace_result.archived_paths,
                    "archived_size_bytes": (
                        workspace_result.archived_size_bytes
                    ),
                    "candidates_seen": workspace_result.candidates_count,
                    "files_skipped_limit": workspace_result.skipped_files,
                    "errors": workspace_result.errors,
                },
            )
            await self._upsert_archive_read_model(
                source_id,
                tenant_id,
                agent_id,
                workspace_result.archived_items,
            )
        except Exception as exc:  # noqa: BLE001
            summary["workspaces_failed"] += 1
            summary["errors"].append(
                {
                    "tenant_id": tenant_id,
                    "runtime_tenant_id": runtime_tenant_id,
                    "agent_id": agent_id,
                    "workspace_dir": str(workspace_dir),
                    "error": str(exc),
                },
            )

    async def _upsert_archive_read_model(
        self,
        source_id: str,
        tenant_id: str,
        agent_id: str,
        items: list[dict[str, Any]],
    ) -> None:
        if not items or self._continuous_governance_service_factory is None:
            return
        service = self._continuous_governance_service_factory()
        if service is None:
            return
        upsert_archive_items = getattr(service, "upsert_archive_items", None)
        if upsert_archive_items is None:
            return
        await upsert_archive_items(
            source_id=source_id,
            target_user_id=tenant_id,
            target_agent_id=agent_id,
            items=items,
        )

    @staticmethod
    def _iter_agent_workspace_dirs(tenant_dir: Path):
        workspaces_dir = tenant_dir / "workspaces"
        if not workspaces_dir.exists():
            return
        for workspace_dir in sorted(
            workspaces_dir.iterdir(),
            key=lambda item: item.name,
        ):
            if not workspace_dir.is_dir() or workspace_dir.name.startswith(
                ".",
            ):
                continue
            if not is_valid_identity_value(workspace_dir.name):
                continue
            yield workspace_dir.name, workspace_dir

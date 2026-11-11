# -*- coding: utf-8 -*-
"""固定四种系统业务适配；归档不初始化Agent，也不接受任意命令或路径。"""

from ...config.context import encode_scope_id
from .system_runtime import SystemTaskRuntime

TASK_SCOPES = {
    "heartbeat": "agent",
    "dream": "agent",
    "task_session_cleanup": "source",
    "archive_maintenance": "source",
}


class SystemBusinessActions:
    """资源只在持久受理后准备，业务动作只能由系统执行围栏持有者调用。"""

    def __init__(self, app):
        self.app = app

    async def prepare(self, job):
        kind = job["task_type"]
        if TASK_SCOPES.get(kind) != job["scope_kind"]:
            raise ValueError("invalid_system_task_type")
        if job["scope_kind"] == "source":
            scheduler = getattr(
                self.app.state,
                "source_system_task_scheduler",
                None,
            )
            if scheduler is None:
                raise RuntimeError("system_maintenance_unavailable")

            async def source_action():
                if kind == "task_session_cleanup":
                    return await self._source_cleanup(
                        scheduler,
                        job["source_id"],
                    )
                return await scheduler.run_archive_maintenance(
                    source_id=job["source_id"],
                )

            return source_action
        manager = getattr(self.app.state, "multi_agent_manager", None)
        if manager is None:
            raise RuntimeError("system_workspace_unavailable")
        workspace = await manager.get_agent(
            job["agent_id"],
            tenant_id=encode_scope_id(job["tenant_id"], job["source_id"]),
        )
        cron = SystemTaskRuntime.from_workspace(workspace)

        async def agent_action():
            if kind == "heartbeat":
                return await cron.run_heartbeat(managed_execution=True)
            return await cron.run_dream(include_archive_maintenance=False)

        return agent_action

    async def _source_cleanup(self, scheduler, source_id):
        """新系统维护同时覆盖旧文件任务和新版DB任务，不改变原来源枚举范围。"""
        from ..source_system_config.runtime import (
            resolve_cron_task_session_cleanup_config,
        )
        from .managed_session_cleanup import ManagedTaskSessionCleaner

        service = self.app.state.source_system_config_service
        effective = await service.resolve_config(source_id, force_refresh=True)
        if getattr(effective, "stale", False):
            raise RuntimeError("system_config_unavailable")
        policy = resolve_cron_task_session_cleanup_config(effective)
        cleaner = ManagedTaskSessionCleaner(self.app.state.db_connection)

        async def managed_cleanup(workspace, tenant, source):
            return await cleaner.cleanup(workspace, tenant, source, policy)

        return await scheduler.run_task_session_cleanup(
            source_id=source_id,
            managed_cleanup=managed_cleanup,
        )

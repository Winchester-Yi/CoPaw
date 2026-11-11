# -*- coding: utf-8 -*-
"""只读当前系统业务配置并返回安全摘要，禁止旧绑定和运行时初始化。"""

from fastapi import HTTPException

from ...config.config import HeartbeatConfig
from ...config.context import resolve_storage_tenant_id
from ...runtime_workers import run_runtime_state_work
from ..agent_profile_read import read_agent_profile
from ..source_system_config.runtime import (
    resolve_archive_maintenance_config,
    resolve_cron_task_session_cleanup_config,
)


def _disabled():
    return {
        "enabled": False,
        "cron": "",
        "timezone": "Asia/Shanghai",
        "timeout_seconds": 7200,
    }


def _agent_config(job):
    storage = resolve_storage_tenant_id(job["tenant_id"], job["source_id"])
    try:
        profile, zone, enabled = read_agent_profile(storage, job["agent_id"])
    except FileNotFoundError:
        return _disabled()
    except HTTPException as error:
        if error.status_code == 404:
            return _disabled()
        raise
    if job["task_type"] == "heartbeat":
        from .system_runtime import every_to_cron

        heartbeat = profile.heartbeat or HeartbeatConfig()
        cron = every_to_cron(heartbeat.every)
        active = enabled and heartbeat.enabled
    else:
        cron = profile.running.memory_summary.dream_cron.strip()
        active = enabled and bool(cron)
    return {
        "enabled": bool(active),
        "cron": cron,
        "timezone": zone,
        "timeout_seconds": 7200,
    }


class SystemConfigReader:
    """Agent只读既有配置文件；source只强制读取当前来源配置。"""

    def __init__(self, source_service):
        self.source_service = source_service

    async def read(self, job):
        """配置不可确认时拒绝启动维护，不使用陈旧缓存假装最新设置。"""
        kind = job["task_type"]
        if kind in {"heartbeat", "dream"}:
            return await run_runtime_state_work(_agent_config, job)
        if kind not in {"task_session_cleanup", "archive_maintenance"}:
            raise ValueError("invalid_system_task_type")
        if self.source_service is None:
            raise RuntimeError("system_config_unavailable")
        effective = await self.source_service.resolve_config(
            job["source_id"],
            force_refresh=True,
        )
        if getattr(effective, "stale", False):
            raise RuntimeError("system_config_unavailable")
        config = (
            resolve_cron_task_session_cleanup_config(effective)
            if kind == "task_session_cleanup"
            else resolve_archive_maintenance_config(effective)
        )
        return {
            "enabled": config.enabled,
            "cron": config.cron,
            "timezone": "Asia/Shanghai",
            "timeout_seconds": getattr(config, "timeout_seconds", 7200),
        }

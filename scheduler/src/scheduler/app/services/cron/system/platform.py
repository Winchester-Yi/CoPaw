# -*- coding: utf-8 -*-
"""复用现有平台传输协议，系统回调使用独立入口和标记。"""

import base64
import json

from croniter import croniter

from ..calendar_schedule import validate_cron
from ..managed.identity import encode_scope
from ..managed.platform import ManagedPlatformClient

PROTOCOL = "scheduler_system_v1"
NAMES = {
    "heartbeat": "心跳",
    "dream": "梦境",
    "task_session_cleanup": "任务会话清理",
    "archive_maintenance": "归档维护",
}


def system_platform_cron(expression):
    """保留系统旧排程语义，不把W/LW展开为用户日历的每日候选。"""
    parts = validate_cron(expression).split()
    if len(parts) != 5:
        raise ValueError("invalid_system_cron")
    if parts[4] in {"*", "?"}:
        return "0 " + " ".join((*parts[:4], "?"))
    expanded, _ = croniter.expand(expression)
    weekdays = ",".join(str(int(day) + 1) for day in expanded[4])
    return f"0 {parts[0]} {parts[1]} ? {parts[3]} {weekdays}"


class _SystemTransport(ManagedPlatformClient):
    """只替换载荷组装，HTTP错误处理和已知ID更新仍沿用原协议。"""

    def _payload(self, operation, spec, mode):
        row, config = operation, spec
        tenant, source = row["route_tenant_id"], row["source_id"]
        params = {
            "tenant_id": tenant,
            "source_id": source,
            "agent_id": row["route_agent_id"],
            "job_id": row["job_id"],
            "task_type": row["task_type"],
            "scopeId": encode_scope(tenant, source),
            "fromId": tenant,
            "callback_protocol": PROTOCOL,
            "operation_id": row["operation_id"],
        }
        scope = (
            source
            if row["scope_kind"] == "source"
            else f"{tenant}/{source}/{row['agent_id']}"
        )
        return {
            **self._credentials(),
            "jobGroup": self.config.get("job_group", 0),
            "author": self.config.get("author", ""),
            "alarmEmail": self.config.get("alarm_email", ""),
            "glueType": "GLUE_GROOVY",
            "glueRemark": row["job_id"],
            "jobDesc": f"[Scheduler][系统任务] {scope} - {NAMES[row['task_type']]}"[
                :200
            ],
            "jobCron": system_platform_cron(config["cron"]),
            "jobAddress": f"{self.callback_base}/api/scheduler/cron/system/callback",
            "jobParam": base64.urlsafe_b64encode(
                json.dumps(params).encode(),
            ).decode(),
        }


class SystemPlatformClient:
    """提供系统同步所需的最小平台注册、更新和启停接口。"""

    def __init__(self, config, *, http=None):
        self.transport = _SystemTransport(config, http=http)

    async def register(self, row, config):
        """没有查询补偿协议，未取得ID的注册仍明确失败。"""
        return await self.transport.register(row, config, "system")

    async def update(self, external_id, row, config):
        """只更新已知新版ID，不接管旧绑定。"""
        return await self.transport.update(external_id, row, config, "system")

    async def set_enabled(self, external_id, enabled):
        """使用现有平台启停协议，不增加配置开关。"""
        return await self.transport.set_enabled(external_id, enabled)

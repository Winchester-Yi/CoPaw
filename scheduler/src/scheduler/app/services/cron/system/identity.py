# -*- coding: utf-8 -*-
"""系统任务拥有独立稳定身份，来源级任务不按最后编辑管理员拆分。"""

from dataclasses import asdict, dataclass
import json
from uuid import NAMESPACE_URL, uuid5

from ..managed.identity import identity_value

TASK_SCOPES = {
    "heartbeat": "agent",
    "dream": "agent",
    "task_session_cleanup": "source",
    "archive_maintenance": "source",
}


@dataclass(frozen=True)
class SystemIdentity:
    """逻辑身份不包含调度路由管理员，也不进入用户模型池。"""

    scope_kind: str
    source_id: str
    tenant_id: str
    agent_id: str
    task_type: str

    @classmethod
    def from_caller(cls, caller, task_type):
        """按现有网关身份检查权限，并规范化来源级空维度。"""
        if task_type == "cleanup":
            task_type = "task_session_cleanup"
        if task_type not in TASK_SCOPES:
            raise ValueError("invalid_system_task_type")
        scope = TASK_SCOPES[task_type]
        if scope == "source" and not caller.is_manager:
            raise PermissionError("cron_system_scope_forbidden")
        if not caller.is_manager and caller.actor_id != caller.tenant_id:
            raise PermissionError("cron_system_scope_forbidden")
        return cls(
            scope,
            identity_value(caller.source_id),
            identity_value(caller.tenant_id) if scope == "agent" else "",
            identity_value(caller.agent_id) if scope == "agent" else "",
            task_type,
        )

    @property
    def job_id(self):
        """同一逻辑任务反复刷新仍得到同一任务标识。"""
        value = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return str(uuid5(NAMESPACE_URL, "scheduler-system:" + value))

    def fields(self):
        """返回可绑定 SQL 的完整规范化身份，不读取旧绑定。"""
        return {"job_id": self.job_id, **asdict(self)}

    @classmethod
    def for_workspace(cls, caller):
        """仅可信内部工作区启动入口使用的固定授权，不把普通用户伪装成管理员。"""
        if caller.actor_id != caller.tenant_id:
            raise PermissionError("cron_system_scope_forbidden")
        agent = cls.from_caller(caller, "heartbeat")
        return [
            cls(
                scope,
                agent.source_id,
                agent.tenant_id if scope == "agent" else "",
                agent.agent_id if scope == "agent" else "",
                kind,
            )
            for kind, scope in TASK_SCOPES.items()
        ]


def authorize(caller, row):
    """公开操作须与完整规范化身份一致，裸任务ID不构成权限。"""
    if row is None:
        raise LookupError("cron_system_job_not_found")
    expected = SystemIdentity.from_caller(caller, row["task_type"]).fields()
    if any(row.get(key) != value for key, value in expected.items()):
        raise LookupError("cron_system_job_not_found")
    return row

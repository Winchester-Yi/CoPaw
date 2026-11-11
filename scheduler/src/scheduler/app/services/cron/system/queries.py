# -*- coding: utf-8 -*-
"""系统状态只读投影，公开响应不暴露执行或同步租约令牌。"""

from datetime import datetime, timezone
import json

from .identity import SystemIdentity, authorize
from .management_store import JOBS


def _config(value):
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError("system_config_data_unavailable") from error
    if not isinstance(parsed, dict):
        raise RuntimeError("system_config_data_unavailable")
    return {
        key: parsed[key]
        for key in ("enabled", "cron", "timezone", "timeout_seconds")
        if key in parsed
    }


def _stamp(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return (
        value.replace(tzinfo=timezone.utc).isoformat()
        if value.tzinfo is None
        else value.isoformat()
    )


def public_job(row):
    """期望配置不冒充已生效，保留管理员定位待核实执行所需的标识。"""
    fields = (
        "job_id",
        "scope_kind",
        "source_id",
        "tenant_id",
        "agent_id",
        "task_type",
        "operation_id",
        "generation",
        "sync_generation",
        "synced_generation",
        "sync_status",
        "sync_stage",
        "version",
        "active_execution_id",
        "last_status",
    )
    return {
        **{name: row[name] for name in fields},
        "management_owner": "scheduler",
        "enabled": bool(row["enabled"]),
        "desired_config": _config(row["desired_config_json"]),
        "effective_config": _config(row["effective_config_json"]),
        "binding_known": bool(row["external_job_id"]),
        "has_effective_binding": bool(
            row["external_job_id"] and row["effective_operation_id"],
        ),
        "error_code": row["sync_error_code"],
        "last_run_at": _stamp(row["last_run_at"]),
        "updated_at": _stamp(row["updated_at"]),
        "needs_verification": row["last_status"] == "unknown"
        and bool(row["active_execution_id"]),
    }


def public_execution(row):
    """只输出执行事实和核实审计，内部租约及路由凭据永不出现在页面。"""
    fields = (
        "execution_id",
        "job_id",
        "scope_kind",
        "tenant_id",
        "source_id",
        "agent_id",
        "task_type",
        "operation_id",
        "config_generation",
        "status",
        "error_code",
        "version",
        "resolution_request_id",
        "resolved_by",
        "resolution_reason",
    )
    times = (
        "received_at",
        "accepted_at",
        "started_at",
        "finished_at",
        "resolved_at",
    )
    result = json.loads(row["result_json"]) if row["result_json"] else {}
    if not isinstance(result, dict):
        raise RuntimeError("system_result_data_unavailable")
    counters = {
        key: value
        for key, value in result.items()
        if type(value) is int and value >= 0
    }
    return {
        **{key: row[key] for key in fields},
        **{key: _stamp(row[key]) for key in times},
        "result": counters,
    }


class SystemQueries:
    """仅查询当前Header身份允许的系统任务，不启动工作区或补建任务。"""

    def __init__(self, db):
        self.db = db

    async def detail(self, caller, job_id):
        row = await self.db.fetch_one(
            f"SELECT * FROM {JOBS} WHERE job_id=%s",
            (job_id,),
        )
        return public_job(authorize(caller, row))

    async def page(self, caller, scope_kind=None):
        SystemIdentity.from_caller(caller, "heartbeat")
        scope = "(scope_kind='agent' AND tenant_id=%s AND agent_id=%s)"
        if caller.is_manager:
            scope = f"({scope} OR scope_kind='source')"
        rows = await self.db.fetch_all(
            f"SELECT * FROM {JOBS} WHERE source_id=%s AND {scope} ORDER BY task_type,job_id",
            (caller.source_id, caller.tenant_id, caller.agent_id),
        )
        result = []
        for row in rows:
            if scope_kind is None or row["scope_kind"] == scope_kind:
                result.append(public_job(authorize(caller, row)))
        return {"items": result}

    async def executions(self, caller, job_id, *, limit=50, offset=0):
        """管理员读取当前完整身份的系统执行，不混入用户历史数字ID。"""
        if not caller.is_manager:
            raise PermissionError("cron_system_scope_forbidden")
        job = await self.detail(caller, job_id)
        identity = (
            job_id,
            job["scope_kind"],
            job["tenant_id"],
            job["source_id"],
            job["agent_id"],
            job["task_type"],
        )
        rows = await self.db.fetch_all(
            "SELECT * FROM swe_cron_system_executions WHERE job_id=%s AND scope_kind=%s AND tenant_id=%s AND source_id=%s AND agent_id=%s AND task_type=%s ORDER BY received_at DESC,execution_id DESC LIMIT %s OFFSET %s",
            (*identity, limit + 1, offset),
        )
        return {
            "items": [public_execution(row) for row in rows[:limit]],
            "has_more": len(rows) > limit,
            "next_offset": offset + limit if len(rows) > limit else None,
        }

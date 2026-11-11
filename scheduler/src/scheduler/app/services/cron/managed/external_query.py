# -*- coding: utf-8 -*-
"""Scheduler独立对外查询新版用户任务，不访问SWE文件或运行时。"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import re

from ..calendar_schedule import _MemoCalendar, next_run_times
from ..workday_calendar import get_calendar_service
from .views import public_value, task_view
from .date_policy import search_window

KEYS = ("tenant_id", "source_id", "agent_id", "job_id")
STATE_FIELDS = (
    "task_chat_id",
    "task_session_id",
    "ordinary_external_job_id",
    "batch_external_job_id",
    "dispatch_state",
    "pause_reason",
    "auto_paused_at",
    "last_status",
    "last_run_at",
    "last_prefetch_at",
    "last_error",
    "has_scheduled_result",
    "latest_scheduled_preview",
    "last_scheduled_run_at",
    "unread_execution_count",
)
PAGE_SIZE = 200
_REDACTIONS = (
    (
        r"(?i)(authorization\s*:\s*(?:(?:bearer|basic)\s+)?)[^\s,;]+",
        r"\1[REDACTED]",
    ),
    (r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1[REDACTED]"),
    (r"(?i)(cookie\s*:\s*)[^\n\r]+", r"\1[REDACTED]"),
    (
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|secret|"
        r"password|session[_-]?token)\b\s*[:=]\s*([\"']?)[^\"'\s,;]+(\2)",
        r"\1=[REDACTED]",
    ),
)


def _public(value):
    """过滤控制面私有键与凭据，并保留原对外元数据的文本脱敏。"""
    return _redact(public_value(value))


def _redact(value):
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        for pattern, replacement in _REDACTIONS:
            value = re.sub(pattern, replacement, value)
    return value


def _definition(row):
    value = row["spec_json"]
    spec = (
        json.loads(value)
        if isinstance(value, (str, bytes))
        else deepcopy(value)
    )
    if not isinstance(spec, dict):
        raise ValueError("cron_definition_data_unavailable")
    schedule = spec.get("schedule") or {}
    allow = schedule.get("run_on_non_workday", False)
    if type(allow) is not bool:
        raise ValueError("cron_definition_data_unavailable")
    spec.update(
        id=row["job_id"],
        name=row["name"],
        enabled=bool(row["enabled"]),
        task_type=row["task_type"],
        tenant_id=row["tenant_id"],
        source_id=row["source_id"],
        scope_id=row["scope_id"],
        schedule={
            "type": "cron",
            "cron": row["cron_expr"],
            "timezone": row["timezone"],
            "run_on_non_workday": allow,
        },
    )
    for field in ("meta", "model_slot"):
        if field in spec:
            spec[field] = _public(spec[field])
    dispatch = spec.get("dispatch")
    if isinstance(dispatch, dict) and "meta" in dispatch:
        dispatch["meta"] = _public(dispatch["meta"])
    return spec


def _meta(row, spec, decision, task):
    meta = dict(spec.get("meta") or {})
    columns = {
        "task_chat_id": "task_chat_id",
        "task_session_id": "task_session_id",
        "task_has_scheduled_result": "has_scheduled_result",
        "task_last_scheduled_preview": "latest_scheduled_preview",
        "task_last_scheduled_run_at": "last_scheduled_run_at",
        "task_unread_execution_count": "unread_execution_count",
        "auto_paused_at": "auto_paused_at",
        "external_job_id": "ordinary_external_job_id",
        "batch_dispatch_external_job_id": "batch_external_job_id",
    }
    meta.update(
        {key: row["state_" + column] for key, column in columns.items()},
    )
    meta.update(
        creator_user_id=row["creator_user_id"],
        management_owner="scheduler",
        agent_id=row["agent_id"],
        scope_id=row["scope_id"],
        calendar_schedule=spec["schedule"],
        calendar_source=decision.get("source"),
        broadcast_dispatch_intents_enabled=row["state_dispatch_state"]
        in {"batch_enabled", "batch_paused"},
    )
    meta.update(
        task_has_scheduled_result=bool(row["state_has_scheduled_result"]),
        task_unread_execution_count=int(
            row["state_unread_execution_count"] or 0,
        ),
        task_last_scheduled_preview=row["state_latest_scheduled_preview"]
        or "",
        pause_reason=task["pause_reason"],
    )
    return meta


def _item(row, raw, decision, viewer):
    spec = deepcopy(raw)
    # 兼容字段复用有效任务状态，避免漏掉日期限制或改变暂停优先级。
    task = task_view(row, viewer)
    spec["meta"] = _meta(row, spec, decision, task)
    state = {
        field: row["state_" + field]
        for field in (
            "last_status",
            "last_run_at",
            "last_prefetch_at",
            "last_error",
        )
    }
    if state["last_status"] not in {
        None,
        "success",
        "error",
        "running",
        "skipped",
        "cancelled",
    }:
        raise ValueError("cron_state_data_unavailable")
    state.update(
        next_run_times=decision["next_run_times"],
        next_run_at=next(iter(decision["next_run_times"]), None),
        external_job_id=row["state_ordinary_external_job_id"],
    )
    task["visible_in_my_tasks"] = bool(
        viewer and row["creator_user_id"] == viewer,
    )
    return {**spec, "state": state, "task": task}


def _preview_rule(spec):
    """和Scheduler列表一致：先算父规则的逻辑日，再应用分发提前量。"""
    meta, schedule = spec.get("meta") or {}, spec["schedule"]
    return (
        meta.get("broadcast_original_cron") or schedule["cron"],
        meta.get("broadcast_original_timezone") or schedule["timezone"],
        schedule["run_on_non_workday"],
        int(meta.get("broadcast_offset_minutes", 0) or 0),
    )


async def _preview(key, now, calendar, until=None):
    advance = timedelta(minutes=key[3])
    values = await next_run_times(
        key[0],
        key[1],
        calendar,
        run_on_non_workday=key[2],
        count=3,
        now=now + advance,
        until=until + advance if until else None,
    )
    return {
        "source": (
            "fallback"
            if any(
                day["source"] == "fallback" for day in calendar.days.values()
            )
            else "api"
        ),
        "next_run_times": [value - advance for value in values],
    }


class ExternalManagedJobs:
    """不伪造actor权限；完整scope和Agent用于筛选，viewer只决定个人任务可见性。"""

    def __init__(self, db, *, clock=None, calendar=None):
        self.db = db
        self.calendar = calendar or get_calendar_service()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def _page(self, scope, after):
        if self.db is None or not getattr(self.db, "is_connected", False):
            raise RuntimeError("cron_database_unavailable")
        joins = " AND ".join(
            f"d.`{key}`=s.`{key}` AND d.`{key}` COLLATE utf8mb4_bin=s.`{key}`"
            for key in KEYS
        )
        select = ",".join(
            f"s.`{key}` AS `state_{key}`" for key in ("job_id", *STATE_FIELDS)
        )
        clauses = " AND ".join(
            f"d.`{key}`=%s AND d.`{key}` COLLATE utf8mb4_bin=%s"
            for key in KEYS[:3]
        )
        params = tuple(value for value in scope for _ in range(2))
        return await self.db.fetch_all(
            f"SELECT d.*,{select} FROM swe_cron_jobs_v2 d LEFT JOIN swe_cron_job_states s ON {joins} WHERE {clauses} AND d.management_owner='scheduler' AND d.deleted_at IS NULL AND d.job_id COLLATE utf8mb4_bin>%s ORDER BY d.job_id COLLATE utf8mb4_bin LIMIT %s",
            (*params, after, PAGE_SIZE),
        )

    async def list(self, tenant, source, agent, viewer=None):
        """稳定分段读完整数组；每行定义/状态/绑定来自同次JOIN，不借旧内存补值。"""
        rows = []
        after = ""
        scope = (tenant, source, agent)
        while True:
            page = await self._page(scope, after)
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                break
            after = page[-1]["job_id"]
        return await self._render(rows, scope, viewer)

    async def _render(self, rows, scope, viewer):
        now = self.clock()
        memo, items = {}, []
        calendar = _MemoCalendar(self.calendar)
        for row in rows:
            if (
                tuple(row[key] for key in KEYS[:3]) != scope
                or row["state_job_id"] != row["job_id"]
            ):
                raise RuntimeError("cron_state_record_missing")
            spec = _definition(row)
            rule = _preview_rule(spec)
            base, until = search_window({**spec, **row}, now)
            key = (*rule, base, until)
            if key not in memo:
                memo[key] = await _preview(rule, base, calendar, until)
            items.append(_item(row, spec, memo[key], viewer))
        return items

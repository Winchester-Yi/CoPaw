# -*- coding: utf-8 -*-
"""某业务日有排程的任务及其最近状态摘要，不冒充逐次执行历史。"""

import base64
import json
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ..calendar_schedule import _MemoCalendar, allows, candidate_cron
from .contracts import ManagedCronUnavailable, QueryOptions
from .cron_syntax import next_times
from .date_policy import BEIJING, reason
from .views import list_item

_STATUS = {
    "completed": ("已完成", "已完成"),
    "in_progress": ("进行中", "已启动"),
    "pending": ("待开始", "等待执行"),
    "error": ("执行失败", "执行失败"),
    "cancelled": ("已取消", "已取消"),
}


def _cursor(token, day):
    if token is None:
        return None
    try:
        raw = base64.b64decode(
            token + "=" * (-len(token) % 4),
            altchars=b"-_",
            validate=True,
        )
        value = json.loads(raw)
        if value["date"] != day.isoformat() or not isinstance(
            value["cursor"],
            str,
        ):
            raise ValueError("invalid_cron_cursor")
        return value["cursor"]
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        raise ValueError("invalid_cron_cursor") from error


def _next_cursor(token, day):
    if token is None:
        return None
    payload = json.dumps({"date": day.isoformat(), "cursor": token}).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _instant(value):
    if value is None:
        return None
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    return (
        value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    )


def _eligible(row, start):
    if row.get("management_status") == "state_missing":
        raise ManagedCronUnavailable("cron_state_record_missing")
    pause = row["task"].get("pause_reason")
    return (
        row["enabled"]
        and row["task_type"] in {"agent", "workflow", "text"}
        and pause in {None, "date_before_start", "date_expired"}
        and row.get("dispatch_state") != "batch_paused"
        and reason(row, start) is None
    )


def _status(row, scheduled, now):
    state = row["state"]
    last = _instant(state.get("last_run_at"))
    if (
        state.get("last_status") == "running"
        and scheduled.date() == now.astimezone(BEIJING).date()
    ):
        return "in_progress", last
    if (
        scheduled > now
        or last is None
        or last.astimezone(BEIJING).date() != scheduled.date()
    ):
        return "pending", last
    return {
        "success": "completed",
        "error": "error",
        "cancelled": "cancelled",
    }.get(
        state.get("last_status"),
        "pending",
    ), last


async def _item(row, day, calendar, now):
    start = datetime.combine(day, time.min, BEIJING)
    if not _eligible(row, start):
        return None
    end = start + timedelta(days=1) - timedelta(microseconds=1)
    schedule = row["schedule"]
    # 批成员的内部提前量不能把业务日期平移到前一天。
    expression = (
        row.get("meta", {}).get("broadcast_original_cron") or schedule["cron"]
    )
    candidates = next_times(
        candidate_cron(expression),
        (start - timedelta(microseconds=1)).astimezone(
            ZoneInfo(schedule["timezone"]),
        ),
        1,
        until=end,
    )
    if not candidates:
        return None
    scheduled = candidates[0].astimezone(BEIJING)
    decision = await allows(
        expression,
        scheduled,
        calendar,
        run_on_non_workday=schedule.get("run_on_non_workday", False),
    )
    if not decision["allowed"]:
        return None
    status, last = _status(row, scheduled, now)
    label, suffix = _STATUS[status]
    info = (
        f"将于{scheduled:%H:%M}执行"
        if status == "pending"
        else (f"{last.astimezone(BEIJING):%H:%M}{suffix}" if last else suffix)
    )
    return {
        "job_id": row["id"],
        "task_name": row["name"],
        "tenant_id": row["tenant_id"],
        "source_id": row["source_id"],
        "agent_id": row["agent_id"],
        "management_owner": "scheduler",
        "status": status,
        "status_text": label,
        "time_info": info,
        "status_basis": "latest_task_state",
        "scheduled_time": scheduled.isoformat(),
        "last_run_at": last.isoformat() if last else None,
        "calendar_source": decision["source"],
        "meta": {
            "task_chat_id": row["task"].get("chat_id"),
            "task_session_id": row["task"].get("session_id"),
        },
    }


async def progress_page(repo, caller, day, *, limit=200, cursor=None):
    """按任务分页；过滤后可为空页，必须继续读取 next_cursor。"""
    page = await repo.page_rows(
        caller,
        QueryOptions(limit, _cursor(cursor, day)),
    )
    calendar = _MemoCalendar(repo.calendar)
    now = repo.clock()
    items = []
    for row in page["items"]:
        item = await _item(
            list_item(row, caller.actor_id, next_times=[]),
            day,
            calendar,
            now,
        )
        if item is not None:
            items.append(item)
    return {
        "items": items,
        "date": day.isoformat(),
        "timezone": "Asia/Shanghai",
        "next_cursor": _next_cursor(page["next_cursor"], day),
    }

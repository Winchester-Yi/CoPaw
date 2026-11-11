# -*- coding: utf-8 -*-
"""异步预取日历预览，纯字段映射函数不做网络查询或任务写入。"""

from ..calendar_schedule import _MemoCalendar, next_run_times
from datetime import timedelta
from .contracts import ManagedCronUnavailable
from .views import _json, job_view
from .date_policy import search_window


async def render_rows(rows, actor_id, calendar, now, mapper=job_view):
    """同一页复用日期观测，字段与现有任务视图保持一致。"""
    calendar = _MemoCalendar(calendar)
    result = []
    for row in rows:
        times = []
        if row.get("state_job_id") is not None:
            times = await _preview(row, calendar, now)
        result.append(mapper(row, actor_id, next_times=times))
    return result


async def _preview(row, calendar, now):
    try:
        spec = _json(row.get("spec_json"), dict, {})
        switch = spec.get("schedule", {}).get("run_on_non_workday", False)
        if type(switch) is not bool:
            raise ValueError("invalid calendar switch")
        meta = spec.get("meta", {})
        expression = meta.get("broadcast_original_cron") or row["cron_expr"]
        zone = meta.get("broadcast_original_timezone") or row["timezone"]
        advance = timedelta(
            minutes=int(meta.get("broadcast_offset_minutes", 0) or 0),
        )
        base, until = search_window(row, now)
        values = await next_run_times(
            expression,
            zone,
            calendar,
            run_on_non_workday=switch,
            now=base + advance,
            until=until + advance if until else None,
        )
        return [(value - advance).isoformat() for value in values]
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        raise ManagedCronUnavailable("cron_data_unavailable") from exc

# -*- coding: utf-8 -*-
"""在任务事务外取得一次准入结论，并作为执行快照的一部分冻结。"""

from dataclasses import dataclass
from datetime import datetime

from ..calendar_schedule import allows, calendar_rule, candidate_cron


@dataclass(frozen=True)
class TriggerObservation:
    """绑定引用、触发时间与日历判定属于同一次平台请求。"""

    binding: dict
    kind: str
    logical: datetime
    physical: datetime
    receipt_id: str
    now: datetime
    calendar: dict


async def admission(spec, logical, calendar, definition_version):
    """子任务使用原始业务表达式，批次仅按父任务的逻辑日期判断。"""
    schedule = spec["schedule"]
    expression = (
        spec.get("meta", {}).get("broadcast_original_cron") or schedule["cron"]
    )
    switch = schedule.get("run_on_non_workday", False)
    if type(switch) is not bool:
        raise ValueError("invalid_cron_calendar_switch")
    result = await allows(
        expression,
        logical,
        calendar,
        run_on_non_workday=switch,
    )
    return {
        **result,
        "definition_version": definition_version,
        "rule_cron": expression,
    }


def timer_expression(spec):
    """只平移平台日历候选，不改写用户保存的W/LW业务定义。"""
    from .scatter import shift_cron_expression

    expression = spec["schedule"]["cron"]
    if not calendar_rule(expression):
        return expression
    candidate = candidate_cron(expression)
    meta = spec.get("meta", {})
    advance = int(
        meta.get(
            "_scheduler_calendar_advance_minutes",
            meta.get("broadcast_offset_minutes", 0),
        )
        or 0,
    )
    if not advance:
        return candidate
    shifted = shift_cron_expression(
        candidate,
        spec["schedule"]["timezone"],
        offset_minutes=advance,
    )
    if shifted.error:
        raise ValueError("calendar_timer_cron_not_shiftable")
    return shifted.cron

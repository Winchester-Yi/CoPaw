# -*- coding: utf-8 -*-
"""复用 Scheduler 共库日历，服务不可用时按北京时间周末规则降级。"""

import calendar
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from croniter import croniter

from .cron_utils import compute_next_run_times
from .monitor_sync_client import get_scheduler_api_url

logger = logging.getLogger(__name__)
BEIJING = ZoneInfo("Asia/Shanghai")
_retry_after = 0.0


def callback_time(metadata):
    """只信任带时区的原计划时间，缺失或无时区时使用接收时刻。"""
    for key in (
        "parent_scheduled_fire_at",
        "scheduled_fire_at",
        "callback_received_at",
    ):
        value = metadata.get(key)
        try:
            parsed = (
                value
                if isinstance(value, datetime)
                else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            )
        except (ValueError, TypeError):
            continue
        if parsed.tzinfo is not None:
            return parsed
    return datetime.now(timezone.utc)


async def _request_calendar(body):
    url = f"{get_scheduler_api_url().rstrip('/')}/scheduler/cron/calendar/resolve"
    calendar_rule = re.search(r"\b(?:LW|\d+W)\b", body["cron"], re.I)
    async with httpx.AsyncClient(
        timeout=30.0 if body["count"] or calendar_rule else 5.0,
    ) as client:
        response = await client.post(url, json=body)
        response.raise_for_status()
        return response.json()


def _fallback(cron, at, run_on_non_workday):
    day = at.astimezone(BEIJING).date()
    allowed = run_on_non_workday or day.weekday() < 5
    reason = "" if allowed else "non_workday"
    parts = cron.split()
    if len(parts) == 5 and re.fullmatch(
        r"LW|(?:[1-9]|[12][0-9]|3[01])W",
        parts[2],
        re.I,
    ):
        last = calendar.monthrange(day.year, day.month)[1]
        anchor = (
            last if parts[2].upper() == "LW" else min(int(parts[2][:-1]), last)
        )
        candidates = (
            range(last, 0, -1)
            if parts[2].upper() == "LW"
            else sorted(
                range(1, last + 1),
                key=lambda value: (abs(value - anchor), value),
            )
        )
        target = next(
            value
            for value in candidates
            if day.replace(day=value).weekday() < 5
        )
        allowed = target == day.day
        nominal = datetime(day.year, day.month, day.day, tzinfo=BEIJING)
        allowed = allowed and croniter.match(f"0 0 * {parts[3]} *", nominal)
        reason = "" if allowed else "calendar_rule"
    return {
        "business_date": day.isoformat(),
        "is_workday": day.weekday() < 5,
        "allowed": bool(allowed),
        "source": "fallback",
        "skipped": reason,
    }


def _fallback_runs(cron, tz_name, at, run_on_non_workday, count):
    runs = []
    cursor = at
    for _ in range(3660):
        physical = _calendar_candidate_cron(cron)
        if physical.split() != cron.split():
            candidate = croniter(
                physical,
                cursor.astimezone(ZoneInfo(tz_name or "UTC")),
            ).get_next(datetime)
        else:
            candidate = compute_next_run_times(
                cron,
                tz_name,
                count=1,
                now=cursor,
            )[0]
        if _fallback(cron, candidate, run_on_non_workday)["allowed"]:
            runs.append(candidate)
            cursor = candidate + timedelta(seconds=1)
        else:
            local = candidate.astimezone(BEIJING)
            cursor = local.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            ) + timedelta(days=1, microseconds=-1)
        if len(runs) == count:
            return runs
    return runs


def _calendar_candidate_cron(expression):
    """仅供新版日历降级预览使用，不改变旧 Cron 时间计算行为。"""
    parts = expression.split()
    if len(parts) == 5 and re.fullmatch(
        r"LW|(?:[1-9]|[12][0-9]|3[01])W",
        parts[2],
        re.I,
    ):
        parts[2:4] = ["*", "*"]
        if parts[4] == "?":
            parts[4] = "*"
    return " ".join(parts)


def _validate_response(result, at, count):
    if (
        type(result.get("allowed")) is not bool
        or type(result.get("is_workday")) is not bool
    ):
        raise ValueError("Invalid calendar decision")
    if (
        result.get("business_date")
        != at.astimezone(BEIJING).date().isoformat()
    ):
        raise ValueError("Calendar decision date mismatch")
    if not result["allowed"] and result.get("skipped") not in {
        "non_workday",
        "calendar_rule",
    }:
        raise ValueError("Invalid calendar skip reason")
    if count:
        runs = [
            datetime.fromisoformat(value) for value in result["next_run_times"]
        ]
        if len(runs) > count or any(
            run.tzinfo is None or run <= at for run in runs
        ):
            raise ValueError("Invalid calendar preview")
        if runs != sorted(set(runs)):
            raise ValueError("Calendar preview is not ordered")
        result["next_run_times"] = runs
    return result


async def resolve_calendar(
    *,
    cron="",
    at=None,
    timezone_name="Asia/Shanghai",
    run_on_non_workday=False,
    count=0,
):
    """只访问日历接口及短时降级状态，不写用户任务和执行结果。"""
    global _retry_after
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    if time.monotonic() >= _retry_after:
        try:
            result = await _request_calendar(
                {
                    "cron": cron,
                    "at": at.isoformat(),
                    "timezone": timezone_name,
                    "run_on_non_workday": run_on_non_workday,
                    "count": count,
                },
            )
            return _validate_response(result, at, count)
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ) as exc:
            _retry_after = time.monotonic() + 30
            logger.warning(
                "Scheduler calendar unavailable; using Beijing weekdays: %s",
                type(exc).__name__,
            )
    result = _fallback(cron, at, run_on_non_workday)
    if count:
        result["next_run_times"] = _fallback_runs(
            cron,
            timezone_name,
            at,
            run_on_non_workday,
            count,
        )
    return result

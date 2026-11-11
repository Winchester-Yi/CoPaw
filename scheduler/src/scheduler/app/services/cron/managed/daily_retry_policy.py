# -*- coding: utf-8 -*-
"""补跑的固定业务口径；不将历史最终标识误当作重试耗尽。"""

import json
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .failure_policy import is_model_failure

MARKER = "daily_failure_retry"
PROTOCOL = "scheduler_managed_v1"
STOP_ERRORS = (
    "cron auth user_info is expired",
    "workflow sapid does not match target user",
    "技能任务结果不可用",
)
UNKNOWN_ERRORS = frozenset(
    {"cron_execution_lease_expired", "cron_dispatch_unconfirmed"},
)


def json_object(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def utc_time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    return value.replace(tzinfo=None)


def retry_marker(row):
    value = json_object(row.get("dispatch_meta_json")).get(MARKER)
    return value if isinstance(value, dict) else {}


def day_window(now):
    end = utc_time(now)
    local = end.replace(tzinfo=timezone.utc).astimezone(
        ZoneInfo("Asia/Shanghai"),
    )
    start = datetime.combine(local.date(), time.min, tzinfo=local.tzinfo)
    return str(local.date()), utc_time(start), end


def failure_time(row):
    value = (
        row.get("finished_at")
        if row.get("status") == "error"
        else row.get("settled_at")
    )
    return utc_time(value) if value else None


def excluded_failure(row, history):
    """复用批自动重试排除文本；结果未知另按资格门槛处理。"""
    meta = json_object(row.get("result_json"))
    if meta.get("failure_type") == "auth_expired":
        return "cron_daily_retry_auth_expired"
    if (
        row.get("error_code") in UNKNOWN_ERRORS
        or meta.get("failure_type") == "outcome_unknown"
    ):
        return "cron_daily_retry_outcome_unknown"
    text = " ".join(
        str(value or "")
        for value in (history.get("error_message"), meta.get("error_detail"))
    ).casefold()
    if any(pattern in text for pattern in STOP_ERRORS):
        return "cron_daily_retry_excluded_failure"
    if not is_model_failure(row, history):
        return "cron_daily_retry_non_model_failure"
    return None


def notification_target(row):
    marker = retry_marker(row)
    if marker.get("notification_at"):
        return marker["notification_at"], marker.get(
            "notification_timezone",
            "Asia/Shanghai",
        )
    meta = json_object(row.get("dispatch_meta_json"))
    spec = json_object(row.get("spec_json"))
    original = meta.get("parent_scheduled_fire_at") or row["scheduled_fire_at"]
    delay = int(spec.get("meta", {}).get("notification_delay_minutes") or 0)
    due = utc_time(original) + timedelta(minutes=max(0, min(delay, 10080)))
    return (
        due.replace(tzinfo=timezone.utc).isoformat(),
        spec.get("schedule", {}).get("timezone", "Asia/Shanghai"),
    )

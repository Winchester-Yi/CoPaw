# -*- coding: utf-8 -*-
"""只读取已登记执行，按固定失败窗口分页，再以最新事实筛选。"""

from .....config.constant import CRON_BATCH_MAX_ATTEMPTS
from .daily_retry_policy import (
    excluded_failure,
    failure_time,
    json_object,
    retry_marker,
    utc_time,
)
from .executions import JOB_WHERE, RUN_TABLE
from .repository import IDENTITY_FIELDS

AUTOMATIC = (
    "kind IN ('ordinary','batch') AND "
    "JSON_UNQUOTE(JSON_EXTRACT(dispatch_meta_json,'$.protocol'))='scheduler_managed_v1'"
)


async def candidates(db, call, limit):
    # 两种失败时间分别走索引，每支最多取一页，避免 OR 扫描来源的全部历史。
    base = (
        f"SELECT * FROM {RUN_TABLE} WHERE source_id=%s AND source_id COLLATE utf8mb4_bin=%s "
        f"AND queue_order>%s AND queue_order<=%s AND {AUTOMATIC} AND "
    )
    main = (
        base
        + "status='error' AND finished_at>=%s AND finished_at<=%s ORDER BY queue_order LIMIT %s"
    )
    settled = (
        base
        + "status='success' AND settled_status='error' AND settled_at>=%s AND settled_at<=%s ORDER BY queue_order LIMIT %s"
    )
    params = (
        call["source_id"],
        call["source_id"],
        call["queue_cursor"],
        call["max_queue_order"],
        utc_time(call["window_start"]),
        utc_time(call["cutoff_at"]),
        limit,
    )
    return await db.fetch_all(
        f"SELECT * FROM (SELECT * FROM ({main}) main_failures UNION ALL "
        f"SELECT * FROM ({settled}) settled_failures) failures ORDER BY queue_order LIMIT %s",
        (*params, *params, limit),
    )


async def latest_execution(db, key, *, lock=False):
    return await db.fetch_one(
        f"SELECT * FROM {RUN_TABLE} WHERE {JOB_WHERE} AND {AUTOMATIC} "
        "ORDER BY queue_order DESC LIMIT 1" + (" FOR UPDATE" if lock else ""),
        key,
    )


async def latest_reason(db, row, *, lock=False):
    key = tuple(row[name] for name in IDENTITY_FIELDS)
    latest = await latest_execution(db, key, lock=lock)
    if latest is None or latest["execution_id"] != row["execution_id"]:
        return "cron_daily_retry_not_latest"
    active = await db.fetch_one(
        f"SELECT execution_id FROM {RUN_TABLE} WHERE {JOB_WHERE} AND {AUTOMATIC} "
        "AND (status IN ('queued','dispatching','dispatch_failed','accepted','running') "
        "OR (status='success' AND settled_status IS NULL)) LIMIT 1",
        key,
    )
    return "cron_daily_retry_work_in_progress" if active else None


async def history_reason(db, row):
    history = await db.fetch_one(
        "SELECT * FROM swe_cron_executions WHERE id=%s",
        (row["history_execution_id"],),
    )
    if history is None:
        return "cron_daily_retry_history_missing"
    meta = json_object(history.get("meta"))
    identity = {
        "management_owner": "scheduler",
        "managed_execution_id": row["execution_id"],
        "source_id": row["source_id"],
        "agent_id": row["agent_id"],
    }
    if (
        history["job_id"] != row["job_id"]
        or history["tenant_id"] != row["tenant_id"]
        or not all(meta.get(name) == value for name, value in identity.items())
        or not history["is_final"]
    ):
        return "cron_daily_retry_history_not_current"
    return excluded_failure(row, history)


def terminal_reason(row, call):
    failed = row["status"] == "error" or (
        row["status"] == "success" and row["settled_status"] == "error"
    )
    if not failed:
        return "cron_daily_retry_not_failed"
    when = failure_time(row)
    if when is None or not utc_time(call["window_start"]) <= when <= utc_time(
        call["cutoff_at"],
    ):
        return "cron_daily_retry_outside_window"
    return None


def definition_reason(row):
    if row is None or row["management_owner"] != "scheduler":
        return "cron_daily_retry_definition_missing"
    if (
        row["deleted_at"]
        or not row["enabled"]
        or row.get("state_pause_reason")
    ):
        return "cron_daily_retry_job_disabled"
    if row.get("state_management_status") != "ready":
        return "cron_daily_retry_job_not_ready"
    return None


def native_retry_reason(row, current, member):
    if member is None:
        return None
    if retry_marker(row):
        return None
    attempt = member.get("business_attempt")
    if not isinstance(attempt, int) or attempt < 1:
        return "cron_daily_retry_batch_state_unknown"
    invalidated = row["definition_version"] != current[
        "definition_version"
    ] or row["enqueued_state_version"] < (
        current.get("state_queue_invalidation_version") or 0
    )
    if attempt < CRON_BATCH_MAX_ATTEMPTS and not invalidated:
        return "cron_daily_retry_native_retry_pending"
    return None

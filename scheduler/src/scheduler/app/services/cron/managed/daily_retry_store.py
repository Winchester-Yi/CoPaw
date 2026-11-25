# -*- coding: utf-8 -*-
"""调用批次的持久窗口、短租约和事务进度，不在应用内创建业务表。"""

from datetime import timedelta, timezone
from uuid import uuid4

from .daily_retry_audit import record_filters
from .daily_retry_filters import (
    FILTERED_QUEUED,
    FILTERED_RUNNING,
    DailyRetryFilters,
)
from .daily_retry_policy import day_window, utc_time
from .identity import identity_value
from .operations import utc_now
from .storage import insert, transaction, update

CALLS = "swe_cron_failure_retry_calls"
LEASE_SECONDS = 180
# 筛选调用使用独立状态，避免旧实例忽略筛选参数后领取调用。
PENDING_SQL = f"('queued','running','{FILTERED_QUEUED}','{FILTERED_RUNNING}')"
RUNNING_SQL = f"('running','{FILTERED_RUNNING}')"
FIELDS = (
    "call_id source_id business_date window_start cutoff_at max_queue_order "
    "queue_cursor status scanned_count queued_count skipped_count failed_count "
    "error_code lease_owner lease_until version created_at started_at "
    "finished_at updated_at"
)


def call_summary(row):
    names = (
        "call_id",
        "source_id",
        "status",
        "scanned_count",
        "queued_count",
        "skipped_count",
        "failed_count",
        "error_code",
    )
    result = {name: row.get(name) for name in names}
    result["status"] = result["status"].removeprefix("filtered_")
    result["business_date"] = str(row["business_date"])
    for name in (
        "window_start",
        "cutoff_at",
        "created_at",
        "started_at",
        "finished_at",
    ):
        value = row.get(name)
        result[name] = (
            utc_time(value).replace(tzinfo=timezone.utc).isoformat()
            if value
            else None
        )
    return result


class DailyRetryCalls:
    def __init__(self, db, *, clock=None):
        self.db = db
        self.clock = clock or utc_now

    async def ready(self):
        try:
            await self.db.fetch_all(
                f"SELECT {','.join(FIELDS.split())} FROM {CALLS} LIMIT 0",
            )
        except Exception as exc:
            raise RuntimeError("cron_daily_retry_storage_unavailable") from exc

    async def submit(self, source, *, task_scope="all", job_ids=None):
        if not isinstance(source, str):
            raise ValueError("invalid_cron_source")
        identity_value(source)
        filters = DailyRetryFilters(task_scope=task_scope, job_ids=job_ids)
        filtered = filters.task_scope != "all" or filters.job_ids is not None
        await self.ready()
        date, start, end = day_window(self.clock())
        async with transaction(self.db) as tx:
            maximum = await tx.fetch_one(
                "SELECT MAX(queue_order) AS maximum FROM swe_cron_execution_runs",
            )
            row = {
                "call_id": str(uuid4()),
                "source_id": source,
                "business_date": date,
                "window_start": start,
                "cutoff_at": end,
                "max_queue_order": int(maximum["maximum"] or 0),
                "queue_cursor": 0,
                "status": FILTERED_QUEUED if filtered else "queued",
                "scanned_count": 0,
                "queued_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
                "error_code": None,
                "lease_owner": "",
                "lease_until": None,
                "version": 1,
                "created_at": end,
                "started_at": None,
                "finished_at": None,
                "updated_at": end,
            }
            await insert(tx, CALLS, row)
            if filtered:
                await record_filters(tx, row, filters)
        return call_summary(row)

    async def get(self, source, call_id):
        identity_value(source)
        await self.ready()
        row = await self.db.fetch_one(
            f"SELECT * FROM {CALLS} WHERE call_id=%s AND source_id=%s "
            "AND source_id COLLATE utf8mb4_bin=%s",
            (call_id, source, source),
        )
        if row is None or row["source_id"] != source:
            raise LookupError("cron_daily_retry_call_not_found")
        return call_summary(row)

    async def pending(self, limit=8):
        return await self.db.fetch_all(
            f"SELECT call_id FROM {CALLS} WHERE status IN {PENDING_SQL} "
            "AND (lease_until IS NULL OR lease_until<=%s) "
            "ORDER BY created_at,call_id LIMIT %s",
            (self.clock(), limit),
        )

    async def claim(self, call_id, owner):
        now = self.clock()
        changed = await self.db.execute(
            f"UPDATE {CALLS} SET status=CASE WHEN status IN ('{FILTERED_QUEUED}','{FILTERED_RUNNING}') "
            f"THEN '{FILTERED_RUNNING}' ELSE 'running' END,lease_owner=%s,lease_until=%s, "
            "started_at=COALESCE(started_at,%s),updated_at=%s,version=version+1 "
            f"WHERE call_id=%s AND status IN {PENDING_SQL} "
            "AND (lease_until IS NULL OR lease_until<=%s)",
            (
                owner,
                now + timedelta(seconds=LEASE_SECONDS),
                now,
                now,
                call_id,
                now,
            ),
        )
        return changed == 1

    async def locked(self, tx, call_id, owner):
        row = await tx.fetch_one(
            f"SELECT * FROM {CALLS} WHERE call_id=%s FOR UPDATE",
            (call_id,),
        )
        self.check_owner(row, owner)
        return row

    def check_owner(self, row, owner):
        if (
            row is None
            or row["status"] not in {"running", FILTERED_RUNNING}
            or row["lease_owner"] != owner
            or row["lease_until"] is None
            or utc_time(row["lease_until"]) <= self.clock()
        ):
            raise RuntimeError("cron_daily_retry_lease_lost")

    async def advance(self, tx, row, owner, candidate, decision):
        self.check_owner(row, owner)
        counter = {
            "queued": "queued_count",
            "skipped": "skipped_count",
            "failed": "failed_count",
        }[decision]
        now = self.clock()
        values = {
            "queue_cursor": candidate["queue_order"],
            "scanned_count": row["scanned_count"] + 1,
            counter: row[counter] + 1,
            "lease_until": now + timedelta(seconds=LEASE_SECONDS),
            "version": row["version"] + 1,
            "updated_at": now,
        }
        if decision == "failed":
            values["error_code"] = "cron_daily_retry_items_failed"
        await update(
            tx,
            CALLS,
            values,
            "call_id=%s AND version=%s AND lease_owner=%s",
            (row["call_id"], row["version"], owner),
        )

    async def release(self, call_id, owner, *, complete=False):
        async with transaction(self.db) as tx:
            row = await self.locked(tx, call_id, owner)
            status = (
                FILTERED_QUEUED
                if row["status"] == FILTERED_RUNNING
                else "queued"
            )
            if complete:
                status = (
                    "completed_with_errors"
                    if row["failed_count"]
                    else "completed"
                )
            await update(
                tx,
                CALLS,
                {
                    "status": status,
                    "error_code": (
                        "cron_daily_retry_items_failed"
                        if row["failed_count"]
                        else None
                    ),
                    "lease_owner": "",
                    "lease_until": None,
                    "finished_at": self.clock() if complete else None,
                    "updated_at": self.clock(),
                    "version": row["version"] + 1,
                },
                "call_id=%s AND version=%s",
                (call_id, row["version"]),
            )

    async def defer(self, call_id, owner):
        """扫描级故障保留游标，短暂退避；旧租约不能覆盖接手者。"""
        now = self.clock()
        await self.db.execute(
            f"UPDATE {CALLS} SET status=CASE WHEN status='{FILTERED_RUNNING}' "
            f"THEN '{FILTERED_QUEUED}' ELSE 'queued' END,lease_owner='',lease_until=%s,"
            "error_code='cron_daily_retry_scan_failed',updated_at=%s,version=version+1 "
            f"WHERE call_id=%s AND status IN {RUNNING_SQL} AND lease_owner=%s AND lease_until>%s",
            (now + timedelta(seconds=60), now, call_id, owner, now),
        )

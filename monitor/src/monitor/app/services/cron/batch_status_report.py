# -*- coding: utf-8 -*-
"""One read view for the new batch board and shared model pools."""

from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from aiomysql import DictCursor

from . import managed_board_sql as sql
from .managed_board_occupancy import _LEGACY, _MANAGED

SHANGHAI = ZoneInfo("Asia/Shanghai")
HISTORY_META = "IF(JSON_VALID(h.meta),h.meta,'{}')"
HISTORY_IDENTITY = " AND ".join(
    f"JSON_UNQUOTE(JSON_EXTRACT({HISTORY_META},'$.{key}')) COLLATE utf8mb4_bin={value}"
    for key, value in (
        ("management_owner", "'scheduler'"),
        ("source_id", "r.source_id"),
        ("agent_id", "r.agent_id"),
        ("managed_execution_id", "r.execution_id"),
    )
)


def report_window(business_date, now):
    start = datetime.combine(business_date, time.min, SHANGHAI)
    return (
        sql.db_time(start),
        sql.db_time(start + timedelta(days=1)),
        sql.db_time(now),
    )


def reportable(batch):
    return batch["status"] == "unavailable" or (
        batch["member_count"] > batch["runtime_skipped_count"]
    )


class SnapshotReader:
    def __init__(self, connection):
        self.connection = connection

    async def fetch_all(self, statement, params=()):
        async with self.connection.cursor(DictCursor) as cursor:
            await cursor.execute(statement, params)
            return await cursor.fetchall()


def batch_ctes(source, business_date, now):
    start, end, cutoff = report_window(business_date, now)
    where, params = sql.batch_where(source, start)
    return (
        sql._aggregate_ctes(
            where
            + " AND c.logical_fire_at<%s AND c.logical_fire_at<=%s"
            + " AND ps.batch_follow_level>=1"
            + " AND p.management_owner='scheduler'"
            + " AND p.deleted_at IS NULL",
        ),
        (*params, end, cutoff),
    )


async def batch_activity(reader, source, batches):
    details = {}
    for offset in range(0, len(batches), 200):
        ids = tuple(batches[offset : offset + 200])
        placeholders = ",".join("%s" for _ in ids)
        rows = await reader.fetch_all(
            f"""WITH current_runs AS (
                SELECT DISTINCT m.batch_id,r.execution_id,r.provider_id,r.model_id,
                    r.status,r.settled_status,r.settled_error_code,
                    {sql.TEXT_RUN} AS is_text,
                    h.trace_id,
                    CASE WHEN r.status='error' THEN 'main'
                         WHEN r.settled_status='error' THEN 'subtask' END AS failure_stage,
                    CASE WHEN r.status<>'error' AND r.settled_status='error' THEN
                        CASE WHEN r.settled_error_code='cron_async_subtasks_timeout'
                             THEN '超时' ELSE NULLIF(TRIM(h.error_type),'') END
                    END COLLATE utf8mb4_bin AS subtask_error_type
                FROM {sql.MEMBERS} m
                JOIN {sql.RUNS} r ON r.execution_id=m.current_execution_id
                    AND {sql.identity_join('m', 'r')}
                LEFT JOIN swe_cron_executions h ON h.id=r.history_execution_id
                    AND h.job_id COLLATE utf8mb4_bin=r.job_id
                    AND h.tenant_id COLLATE utf8mb4_bin=r.tenant_id
                    AND {HISTORY_IDENTITY}
                WHERE m.source_id=%s AND m.source_id COLLATE utf8mb4_bin=%s
                    AND m.batch_id IN ({placeholders})
            ), run_subtasks AS (
                SELECT r.batch_id,r.execution_id,r.provider_id,r.model_id,
                    r.failure_stage,r.subtask_error_type,
                    ({sql.ACTIVE} AND NOT r.is_text) AS occupies,
                    SUM(CASE WHEN s.status='FAIL' THEN 1 ELSE 0 END) AS failures,
                    SUM(CASE WHEN s.status='PART_SUC' THEN 1 ELSE 0 END) AS partial,
                    SUM(CASE WHEN s.status='TIMEOUT' THEN 1 ELSE 0 END) AS timeouts,
                    CASE WHEN r.settled_error_code IN
                        ('cron_async_subtasks_failed','cron_async_subtasks_timeout')
                        AND COUNT(s.id)=0 THEN 1 ELSE 0 END AS incomplete
                FROM current_runs r
                LEFT JOIN swe_cron_subtasks s ON s.trace_id=r.trace_id
                    AND s.trace_id COLLATE utf8mb4_bin=r.trace_id
                    AND s.status IN ('FAIL','PART_SUC','TIMEOUT')
                GROUP BY r.batch_id,r.execution_id,r.provider_id,r.model_id,
                    r.status,r.settled_status,r.settled_error_code,r.is_text,
                    r.failure_stage,r.subtask_error_type
            )
            SELECT batch_id,provider_id,model_id,failure_stage,subtask_error_type,
                COUNT(*) AS member_count,SUM(occupies) AS occupied,
                SUM(failures) AS failures,SUM(partial) AS partial,
                SUM(timeouts) AS timeouts,SUM(incomplete) AS incomplete
            FROM run_subtasks
            GROUP BY batch_id,provider_id,model_id,failure_stage,subtask_error_type
            ORDER BY batch_id,provider_id,model_id""",
            (source, source, *ids),
        )
        for row in rows:
            item = details.setdefault(
                row["batch_id"],
                {
                    "subtasks": Counter(),
                    "failure_details": {
                        "main_count": 0,
                        "subtask_types": Counter(),
                    },
                    "workers": [],
                },
            )
            # Keep the previous snapshot counts available to older Scheduler instances.
            for field in ("failures", "partial", "timeouts", "incomplete"):
                item["subtasks"][field] += int(row[field])
            failure = item["failure_details"]
            count = int(row["member_count"])
            if row["failure_stage"] == "main":
                failure["main_count"] += count
            elif row["failure_stage"] == "subtask":
                types = (row["subtask_error_type"] or "").split(",")
                normalized = {value.strip() for value in types}
                normalized.discard("")
                name = " + ".join(sorted(normalized))
                failure["subtask_types"][name or "未记录类型"] += count
            if row["occupied"]:
                item["workers"].append(
                    pool_item(row, occupied=int(row["occupied"])),
                )
    return details


def pool_item(row, **fields):
    return {
        **{name: row[name] for name in ("provider_id", "model_id")},
        **fields,
    }


async def model_pools(reader, source):
    pools = {}
    policies = await reader.fetch_all(
        "SELECT provider_id,model_id FROM swe_cron_dispatch_model_worker_policy "
        "WHERE source_id=%s AND source_id COLLATE utf8mb4_bin=%s",
        (source, source),
    )
    for row in policies:
        pools[row["provider_id"], row["model_id"]] = pool_item(
            row,
            capacity=None,
            capacity_at=None,
            occupied=0,
        )
    capacities = await reader.fetch_all(
        """SELECT c.provider_id,c.model_id,c.effective_workers,c.created_at
            FROM swe_cron_dispatch_worker_capacity c JOIN (
                SELECT MAX(id) AS id FROM swe_cron_dispatch_worker_capacity
                WHERE source_id=%s AND source_id COLLATE utf8mb4_bin=%s
                GROUP BY provider_id COLLATE utf8mb4_bin,model_id COLLATE utf8mb4_bin
            ) latest ON c.id=latest.id""",
        (source, source),
    )
    for row in capacities:
        pools[row["provider_id"], row["model_id"]] = pool_item(
            row,
            capacity=int(row["effective_workers"]),
            occupied=0,
            capacity_at=sql.time_text(row["created_at"], local=True),
        )
    rows = await reader.fetch_all(
        f"WITH occupants AS ({_MANAGED} UNION ALL {_LEGACY}) "
        "SELECT provider_id,model_id,COUNT(*) AS occupied FROM occupants "
        "GROUP BY provider_id COLLATE utf8mb4_bin,model_id COLLATE utf8mb4_bin,"
        "provider_id,model_id",
        (source,) * 4,
    )
    for row in rows:
        pool = pools.setdefault(
            (row["provider_id"], row["model_id"]),
            pool_item(row, capacity=None, capacity_at=None),
        )
        pool["occupied"] = int(row["occupied"])
    return [pools[key] for key in sorted(pools)]


async def collect(reader, source, business_date, now):
    ctes, params = batch_ctes(source, business_date, now)
    rows = await reader.fetch_all(
        f"{ctes} SELECT * FROM board_batches ORDER BY logical_fire_at,batch_id",
        params,
    )
    batches = [row for row in rows if reportable(row)]
    activity = await batch_activity(
        reader,
        source,
        [row["batch_id"] for row in batches if row["status"] != "unavailable"],
    )
    fields = (
        "member_count",
        "done_count",
        "success_count",
        "failed_count",
        "skipped_count",
        "active_count",
        "waiting_count",
    )
    for row in batches:
        info = activity.get(row["batch_id"], {})
        row["subtasks"] = dict(info.get("subtasks", {}))
        row["failure_details"] = info.get("failure_details")
        row["workers"] = info.get("workers", [])
    return {
        "snapshot_at": now.isoformat(),
        "batch_scope": "followed_batch",
        "batches": [
            {
                "batch_id": row["batch_id"],
                "name": row["job_name"] or "未命名任务",
                "logical_fire_at": sql.time_text(row["logical_fire_at"]),
                "finished_at": sql.time_text(row.get("finished_at")),
                "status": row["status"],
                "follow_level": int(row.get("batch_follow_level") or 0),
                **{
                    name: int(row[name])
                    for name in fields
                    if row["status"] != "unavailable"
                },
                "subtasks": row["subtasks"],
                "failure_details": row["failure_details"],
                "workers": row["workers"],
            }
            for row in batches
        ],
        "pools": await model_pools(reader, source),
    }


async def report_snapshot(db, source, business_date):
    async with db.acquire() as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
            )
            await cursor.execute(
                "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY",
            )
        try:
            return await collect(
                SnapshotReader(connection),
                source,
                business_date,
                datetime.now(timezone.utc),
            )
        finally:
            await connection.rollback()

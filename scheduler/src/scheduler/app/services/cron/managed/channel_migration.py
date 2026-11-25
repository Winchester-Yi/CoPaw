# -*- coding: utf-8 -*-
"""渠道迁移清单只读旧定义，通过完整身份关联新版迁移事实。"""

from dataclasses import replace

from .contracts import ManagedCronNotFound
from .identity import identity_value
from .operations import request_hash

AGENT = """NULLIF(JSON_UNQUOTE(JSON_EXTRACT(
    CASE WHEN JSON_VALID(o.meta) THEN o.meta ELSE '{}' END,
    '$.agent_id')), '')"""
PARENT = """NULLIF(JSON_UNQUOTE(JSON_EXTRACT(
    CASE WHEN JSON_VALID(o.meta) THEN o.meta ELSE '{}' END,
    '$.broadcast_source_job_id')), '')"""


def _join(alias):
    return (
        f"{alias}.tenant_id=o.tenant_id AND {alias}.source_id=o.source_id "
        f"AND {alias}.job_id=o.id AND {alias}.agent_id={AGENT} "
        f"AND {alias}.tenant_id COLLATE utf8mb4_bin=o.tenant_id "
        f"AND {alias}.source_id COLLATE utf8mb4_bin=o.source_id "
        f"AND {alias}.job_id COLLATE utf8mb4_bin=o.id "
        f"AND {alias}.agent_id COLLATE utf8mb4_bin={AGENT}"
    )


JOB_QUERY = (
    "SELECT o.id AS job_id,o.name,o.tenant_id,o.source_id,o.cron_expr,"
    f"{AGENT} AS agent_id,{PARENT} AS parent_job_id,"
    "CASE WHEN j.status='completed' AND d.deleted_at IS NOT NULL THEN 'migrated_deleted' "
    "WHEN j.status='completed' THEN 'migrated' WHEN j.operation_id IS NOT NULL THEN 'in_progress' "
    f"WHEN {AGENT} IS NULL THEN 'unknown_agent' ELSE 'not_migrated' END AS migration_status "
    "FROM swe_cron_jobs o "
    f"LEFT JOIN swe_cron_registration_journal j ON {_join('j')} AND j.action='migrate' "
    f"LEFT JOIN swe_cron_jobs_v2 d ON {_join('d')} "
    "WHERE o.source_id=%s AND o.source_id COLLATE utf8mb4_bin=%s "
    "AND o.deleted_at IS NULL "
    "AND (j.operation_id IS NOT NULL OR COALESCE(d.management_owner,'')<>'scheduler')"
)


def _job_filters(tenant, query, status):
    conditions, params = [], []
    if tenant:
        conditions.append("listed.tenant_id COLLATE utf8mb4_bin=%s")
        params.append(tenant)
    if query:
        conditions.append(
            "(listed.job_id LIKE %s ESCAPE '!' OR listed.name LIKE %s ESCAPE '!')",
        )
        query = (
            "%"
            + query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            + "%"
        )
        params.extend((query, query))
    if status:
        conditions.append("listed.migration_status=%s")
        params.append(status)
    return conditions, params


async def jobs(
    db,
    source,
    *,
    cursor,
    limit,
    tenant=None,
    query=None,
    status=None,
):
    conditions, params = _job_filters(tenant, query, status)
    rows = await db.fetch_all(
        f"SELECT * FROM ({JOB_QUERY}) listed WHERE "
        f"{' AND '.join(['listed.job_id>%s', *conditions])} "
        "ORDER BY listed.job_id LIMIT %s",
        (source, source, cursor, *params, limit + 1),
    )
    return {
        "items": rows[:limit],
        "next_cursor": (
            rows[limit - 1]["job_id"] if len(rows) > limit else None
        ),
    }


async def job_groups(
    db,
    source,
    *,
    page,
    limit,
    tenant=None,
    query=None,
    status=None,
):
    conditions, params = _job_filters(tenant, query, status)
    where = " AND ".join(["listed.parent_job_id IS NULL", *conditions])
    # Match the preview's child scope: all undeleted legacy children in the
    # source, including children owned by another tenant.
    rows = await db.fetch_all(
        f"""
        WITH roots AS (
            SELECT * FROM ({JOB_QUERY}) listed WHERE {where}
        ), children AS (
            SELECT {PARENT} COLLATE utf8mb4_bin AS parent_job_id,
                   COUNT(*) AS child_count
            FROM swe_cron_jobs o
            WHERE o.source_id=%s AND o.source_id COLLATE utf8mb4_bin=%s
              AND o.deleted_at IS NULL AND {PARENT} IS NOT NULL
            GROUP BY parent_job_id
        ), task_groups AS (
            SELECT r.*, COALESCE(c.child_count, 0) AS child_count
            FROM roots r LEFT JOIN children c
              ON r.job_id COLLATE utf8mb4_bin=c.parent_job_id
        ), paged AS (
            SELECT * FROM task_groups ORDER BY job_id LIMIT %s OFFSET %s
        )
        SELECT p.*, totals.total, totals.task_total
        FROM (
            SELECT COUNT(*) AS total,
                   COUNT(*) + COALESCE(SUM(child_count), 0) AS task_total
            FROM task_groups
        ) totals LEFT JOIN paged p ON 1=1
        ORDER BY p.job_id
        """,
        (source, source, *params, source, source, limit, (page - 1) * limit),
    )
    return {
        "items": [
            {
                key: value
                for key, value in row.items()
                if key not in {"total", "task_total"}
            }
            for row in rows
            if row["job_id"] is not None
        ],
        "total": int(rows[0]["total"]),
        "task_total": int(rows[0]["task_total"]),
    }


async def selections(db, caller, body):
    ids = sorted({identity_value(job_id) for job_id in body.job_ids})
    markers = ",".join(["%s"] * len(ids))
    rows = await db.fetch_all(
        f"{JOB_QUERY} AND o.id IN ({markers})",
        (caller.source_id, caller.source_id, *ids),
    )
    if {row["job_id"] for row in rows} != set(ids):
        raise ManagedCronNotFound("cron_migration_job_not_found")
    groups = {}
    for row in rows:
        if (
            not row["agent_id"]
            or row["parent_job_id"]
            or row["migration_status"] != "not_migrated"
        ):
            raise ValueError("cron_migration_channel_selection_invalid")
        key = (
            identity_value(row["tenant_id"]),
            identity_value(row["agent_id"]),
        )
        groups.setdefault(key, []).append(row["job_id"])
    return [
        (
            replace(caller, tenant_id=tenant, agent_id=agent),
            {
                "request_id": request_hash([body.request_id, tenant, agent]),
                "job_ids": group,
            },
        )
        for (tenant, agent), group in groups.items()
    ]


async def operation_context(db, caller, operation_id, *, write=False):
    row = await db.fetch_one(
        "SELECT operation_id,tenant_id,agent_id,source_id,actor_id "
        "FROM swe_cron_registration_journal WHERE operation_id=%s AND action='migration_batch'",
        (operation_id,),
    )
    if (
        not row
        or row["operation_id"] != operation_id
        or row["source_id"] != caller.source_id
        or (write and row["actor_id"] != caller.actor_id)
    ):
        raise ManagedCronNotFound("cron_migration_not_found")
    return replace(
        caller,
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
    )

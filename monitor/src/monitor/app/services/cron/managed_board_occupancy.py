# -*- coding: utf-8 -*-
"""按共享模型池的在途口径查询当前占位。"""

from datetime import datetime, timezone

from ...models.managed_board import BoardOccupancyQuery
from . import managed_board_sql as sql

_ORDER = "provider_id,model_id,tenant_id,job_id,id"
_MANAGED = f"""
    SELECT CONCAT('managed:',r.execution_id) AS id,
           'managed' AS origin, r.provider_id, r.model_id, r.tenant_id, r.job_id,
           COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(r.spec_json,'$.tenant_name')),''),
                    NULLIF(d.tenant_name,'')) AS tenant_name,
           COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(r.spec_json,'$.name')),''),
                    NULLIF(d.name,'')) AS job_name,
           r.started_at,
           CASE WHEN r.status='success' THEN 'waiting_subtasks'
                ELSE r.status END AS status,
           CASE
               WHEN JSON_EXTRACT(r.dispatch_meta_json,'$.daily_failure_retry.call_id')
                    IS NOT NULL THEN 'daily_failure_retry'
               WHEN r.kind='batch' AND
                    JSON_EXTRACT(r.dispatch_meta_json,'$.previous_execution_id') IS NOT NULL THEN
                   CASE
                       WHEN EXISTS (SELECT 1 FROM {sql.EVENTS} e
                                    WHERE e.execution_id=r.execution_id
                                      AND e.event_type='auto_retry_scheduled'
                                      AND {sql.identity_join('r', 'e')}) THEN 'batch_auto_retry'
                       WHEN EXISTS (SELECT 1 FROM {sql.EVENTS} e
                                    WHERE e.execution_id=r.execution_id
                                      AND e.event_type='retry_accepted'
                                      AND {sql.identity_join('r', 'e')}) THEN 'batch_manual_retry'
                       ELSE 'batch_retry'
                   END
               WHEN r.kind='batch' OR EXISTS (
                   SELECT 1 FROM {sql.MEMBERS} m WHERE m.current_execution_id=r.execution_id
                     AND {sql.identity_join('r', 'm')}
               ) THEN 'batch'
               WHEN r.kind='manual' THEN 'manual'
               ELSE 'ordinary'
           END AS execution_type
    FROM {sql.RUNS} r
    LEFT JOIN swe_cron_jobs_v2 d ON {sql.identity_join('r', 'd')}
    WHERE r.source_id=%s AND r.source_id COLLATE utf8mb4_bin=%s
      AND JSON_UNQUOTE(JSON_EXTRACT(r.dispatch_meta_json,'$.protocol'))='scheduler_managed_v1'
      AND NOT {sql.TEXT_RUN}
      AND (r.kind NOT IN ('ordinary','manual') OR EXISTS (
          SELECT 1 FROM {sql.MEMBERS} m WHERE m.source_id=r.source_id
            AND m.current_execution_id=r.execution_id
          UNION ALL
          SELECT 1 FROM {sql.MEMBERS} m WHERE m.source_id=r.source_id
            AND m.initial_execution_id=r.execution_id
      ))
      AND {sql.ACTIVE}
"""
_LEGACY = """
    SELECT CONCAT('legacy:',i.id) AS id, 'legacy' AS origin,
           COALESCE(NULLIF(i.provider_id,''),'default') AS provider_id,
           COALESCE(NULLIF(i.model_id,''),'default') AS model_id,
           i.tenant_id, i.job_id, j.tenant_name,
           COALESCE(NULLIF(e.job_name,''),NULLIF(j.name,'')) AS job_name,
           e.actual_time AS started_at,
           CASE WHEN e.status='success' AND COALESCE(e.async_status,'') NOT IN ('success','error')
                    THEN 'waiting_subtasks'
                WHEN e.id IS NOT NULL THEN 'running'
                WHEN i.status='claimed' THEN 'dispatching'
                ELSE 'accepted' END AS status,
           'legacy_batch' AS execution_type
    FROM swe_cron_dispatch_intents i
    LEFT JOIN swe_cron_jobs j
      ON i.job_id=j.id AND i.tenant_id=j.tenant_id AND i.source_id=j.source_id
      AND i.job_id COLLATE utf8mb4_bin=j.id
      AND i.tenant_id COLLATE utf8mb4_bin=j.tenant_id
      AND i.source_id COLLATE utf8mb4_bin=j.source_id
    LEFT JOIN swe_cron_executions e ON e.id=(
        SELECT MAX(h.id) FROM swe_cron_executions h
        WHERE h.dispatch_intent_id=i.id AND h.dispatch_batch_id=i.batch_id
          AND h.dispatch_attempt=i.attempt_count AND h.job_id=i.job_id
          AND h.tenant_id=i.tenant_id
          AND h.dispatch_batch_id COLLATE utf8mb4_bin=i.batch_id
          AND h.job_id COLLATE utf8mb4_bin=i.job_id
          AND h.tenant_id COLLATE utf8mb4_bin=i.tenant_id
    )
    WHERE i.source_id=%s AND i.source_id COLLATE utf8mb4_bin=%s
      AND i.status IN ('claimed','acknowledged','dispatched')
"""


async def worker_occupants(db, source, filters: BoardOccupancyQuery):
    conditions, params = [], [source] * 4
    for column in ("provider_id", "model_id"):
        value = getattr(filters, column)
        if value is not None:
            conditions.append(f"{column} COLLATE utf8mb4_bin=%s")
            params.append(value)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    sampled_at = datetime.now(timezone.utc)
    rows = await db.fetch_all(
        f"WITH occupants AS ({_MANAGED} UNION ALL {_LEGACY}), "
        f"filtered AS (SELECT * FROM occupants{where}), "
        f"paged AS (SELECT * FROM filtered ORDER BY {_ORDER} LIMIT %s OFFSET %s) "
        "SELECT p.*, totals.total FROM (SELECT COUNT(*) AS total FROM filtered) totals "
        f"LEFT JOIN paged p ON 1=1 ORDER BY {_ORDER}",
        (
            *params,
            filters.page_size,
            (filters.page - 1) * filters.page_size,
        ),
    )
    items = [
        {
            **{key: value for key, value in row.items() if key != "total"},
            "started_at": sql.time_text(
                row["started_at"],
                local=row["origin"] == "legacy",
            ),
        }
        for row in rows
        if row["id"] is not None
    ]
    return {
        "total": int(rows[0]["total"]),
        "page": filters.page,
        "page_size": filters.page_size,
        "sampled_at": sampled_at.isoformat(),
        "items": items,
    }

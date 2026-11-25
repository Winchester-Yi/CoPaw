# -*- coding: utf-8 -*-
"""按来源和模型池分桶汇总新版批执行及登记的派发尝试。"""

from datetime import timedelta
from math import ceil

from . import managed_board_sql as sql

_QUERY = f"""
WITH bounds AS (SELECT %s AS since, %s AS until_time, %s AS seconds),
pool AS (
    SELECT r.execution_id,r.tenant_id,r.source_id,r.agent_id,r.job_id,
           r.created_at,r.settled_at,r.settled_status,
           NULLIF(JSON_UNQUOTE(JSON_EXTRACT(r.dispatch_meta_json,'$.previous_execution_id')),'null') AS previous_execution_id
    FROM {sql.RUNS} r CROSS JOIN bounds b
    WHERE r.source_id=%s AND r.source_id COLLATE utf8mb4_bin=%s
      AND r.provider_id=%s AND r.provider_id COLLATE utf8mb4_bin=%s
      AND r.model_id=%s AND r.model_id COLLATE utf8mb4_bin=%s
      AND r.created_at<b.until_time
      AND JSON_UNQUOTE(JSON_EXTRACT(r.dispatch_meta_json,'$.protocol'))='scheduler_managed_v1'
      AND NOT {sql.TEXT_RUN}
      AND (r.kind='batch' OR (r.kind='ordinary' AND EXISTS (
          SELECT 1 FROM {sql.MEMBERS} m WHERE m.source_id=r.source_id
          AND m.current_execution_id=r.execution_id
          UNION ALL
          SELECT 1 FROM {sql.MEMBERS} m WHERE m.source_id=r.source_id
          AND m.initial_execution_id=r.execution_id
      )))
), flow AS (
    SELECT r.created_at AS at_time,1 AS total,0 AS success,0 AS failed,0 AS requests,0 AS retries
    FROM pool r
    UNION ALL
    SELECT r.settled_at,0,r.settled_status='success',r.settled_status='error',0,0
    FROM pool r CROSS JOIN bounds b
    WHERE r.created_at>=b.since AND r.created_at<b.until_time
      AND r.settled_status IN ('success','error')
    UNION ALL
    SELECT e.occurred_at,0,0,0,1,
      CASE WHEN e.event_type='transport_redelivery'
        OR CAST(JSON_UNQUOTE(JSON_EXTRACT(e.details_json,'$.dispatch_attempt')) AS UNSIGNED)>1
        OR r.previous_execution_id IS NOT NULL
        THEN 1 ELSE 0 END
    FROM pool r JOIN {sql.EVENTS} e ON r.execution_id=e.execution_id AND {sql.identity_join('r', 'e')}
    WHERE e.event_type IN ('dispatching','transport_redelivery')
)
SELECT FLOOR(TIMESTAMPDIFF(SECOND,b.since,f.at_time)/b.seconds) AS bucket,
       SUM(total) AS total,SUM(success) AS success,SUM(failed) AS failed,
       SUM(requests) AS requests,SUM(retries) AS retries
FROM flow f CROSS JOIN bounds b
WHERE f.at_time>=b.since AND f.at_time<b.until_time
GROUP BY bucket ORDER BY bucket
"""


async def worker_trends(db, source, provider, model, start, end):
    since, until = sql.db_time(start), sql.db_time(end)
    duration = (until - since).total_seconds()
    if duration <= 0:
        return {"bucket_seconds": 600, "points": []}
    # 长区间自动扩大桶，限制响应点数。
    seconds = max(600, ceil(duration / (180 * 60)) * 60)
    rows = await db.fetch_all(
        _QUERY,
        (
            since.isoformat(" "),
            until.isoformat(" "),
            seconds,
            source,
            source,
            provider,
            provider,
            model,
            model,
        ),
    )
    buckets = {int(row["bucket"]): row for row in rows}
    counters = dict.fromkeys(
        ("total", "success", "failed", "requests", "retries"),
        0,
    )
    points = []
    for index in range(ceil(duration / seconds)):
        row = buckets.get(index, {})
        for key in counters:
            counters[key] += int(row.get(key) or 0)
        offset = min(duration, (index + 1) * seconds)
        points.append(
            {
                "at": sql.time_text(since + timedelta(seconds=offset)),
                **counters,
            },
        )
    return {"bucket_seconds": seconds, "points": points}

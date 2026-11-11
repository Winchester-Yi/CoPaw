# -*- coding: utf-8 -*-
"""按当前失败成员汇总类型及详情；查询仅在看板悬停时使用。"""

from collections import Counter
from datetime import datetime, timezone

from . import managed_board_sql as sql
from .managed_board import ManagedBoardUnavailable

# Monitor独立部署，映射遵循Scheduler失败分类协议，不导入其运行包。
LABELS = {
    "auth_expired": "认证失效",
    "subtask_timeout": "子任务超时",
    "subtask_failed": "子任务失败",
    "configuration": "配置错误",
    "model_rate_limit": "模型限流",
    "outcome_unknown": "执行结果未知",
    "execution_timeout": "执行超时",
    "service_error": "服务异常",
    "other": "其他错误",
}

TYPE_SQL = """CASE
WHEN settled_error_code COLLATE utf8mb4_bin='cron_async_subtasks_timeout' THEN 'subtask_timeout'
WHEN settled_error_code COLLATE utf8mb4_bin='cron_async_subtasks_failed' THEN 'subtask_failed'
WHEN raw_category COLLATE utf8mb4_bin IN (CATEGORIES) THEN raw_category
WHEN error_code COLLATE utf8mb4_bin IN ('cron_execution_lease_expired','cron_dispatch_unconfirmed') THEN 'outcome_unknown'
WHEN error_code COLLATE utf8mb4_bin='cron_execution_timeout' THEN 'execution_timeout'
ELSE 'other' END""".replace(
    "CATEGORIES",
    ",".join(f"'{key}'" for key in LABELS),
)

MESSAGE_SQL = """SUBSTR(CASE
WHEN TRIM(history_error_message)<>'' THEN history_error_message
WHEN JSON_TYPE(JSON_EXTRACT(safe_result,'$.error_detail'))='STRING'
 AND TRIM(JSON_UNQUOTE(JSON_EXTRACT(safe_result,'$.error_detail')))<>''
 THEN JSON_UNQUOTE(JSON_EXTRACT(safe_result,'$.error_detail'))
WHEN COALESCE(NULLIF(settled_error_code,''),error_code) COLLATE utf8mb4_bin='cron_async_subtasks_failed' THEN '子任务执行失败'
ELSE COALESCE(NULLIF(settled_error_code,''),NULLIF(error_code,''),'未记录失败详情')
END,1,2048)"""


def summary_query(where, filtered):
    # 与原列表复用同一份关系校验及状态聚合，清单仍只物化一次。
    selected = "WHERE status=%s OR status='unavailable'" if filtered else ""
    return (
        sql._aggregate_ctes(where)  # pylint: disable=protected-access
        + f", selected_batches AS (SELECT * FROM board_batches {selected}), "
        "relation_guard AS (SELECT SUM(CASE WHEN status='unavailable' THEN 1 ELSE 0 END) AS unavailable_batches FROM selected_batches), "
        "failure_sources AS (SELECT r.error_code,r.settled_error_code,"
        "CASE WHEN JSON_VALID(r.result_json) THEN r.result_json ELSE '{}' END AS safe_result,"
        "h.error_message AS history_error_message,NULLIF(TRIM(h.error_type),'') AS error_type "
        f"FROM selected_batches b JOIN {sql.MEMBERS} m ON b.batch_id=m.batch_id "
        "AND b.source_id=m.source_id AND b.source_id COLLATE utf8mb4_bin=m.source_id "
        f"JOIN {sql.RUNS} r ON m.current_execution_id=r.execution_id AND {sql.identity_join('m', 'r')} "
        "LEFT JOIN swe_cron_executions h ON h.id=r.history_execution_id "
        "AND h.tenant_id=r.tenant_id AND h.tenant_id COLLATE utf8mb4_bin=r.tenant_id "
        "AND h.job_id=r.job_id AND h.job_id COLLATE utf8mb4_bin=r.job_id "
        f"WHERE b.status<>'unavailable' AND {sql.FAILED}), "
        "failure_values AS (SELECT error_code,settled_error_code,error_type,"
        "JSON_UNQUOTE(JSON_EXTRACT(safe_result,'$.failure_type')) AS raw_category,"
        f"{MESSAGE_SQL} AS error_message FROM failure_sources), "
        f"classified_failures AS (SELECT {TYPE_SQL} AS failure_type,error_message,error_type FROM failure_values), "
        "typed_failures AS (SELECT failure_type,error_message,"
        "CASE WHEN failure_type='subtask_failed' THEN error_type END AS subtask_error_type "
        "FROM classified_failures), "
        "failure_groups AS (SELECT failure_type COLLATE utf8mb4_bin AS failure_type,"
        "error_message COLLATE utf8mb4_bin AS error_message,"
        "subtask_error_type COLLATE utf8mb4_bin AS subtask_error_type,COUNT(*) AS count FROM typed_failures "
        "GROUP BY failure_type COLLATE utf8mb4_bin,error_message COLLATE utf8mb4_bin,"
        "subtask_error_type COLLATE utf8mb4_bin) "
        "SELECT relation_guard.unavailable_batches,failure_groups.* FROM relation_guard "
        "LEFT JOIN failure_groups ON 1=1 ORDER BY failure_type,count DESC,error_message"
    )


async def failure_summary(db, source, start, end, status, query):
    where, params = sql.batch_where(source, start, end, query)
    rows = await db.fetch_all(
        summary_query(where, bool(status)),
        (*params, status) if status else params,
    )
    if rows[0]["unavailable_batches"]:
        raise ManagedBoardUnavailable("cron_batch_relation_incomplete")
    groups = {}
    details = {}
    error_types = Counter()
    for row in rows:
        kind = row["failure_type"]
        if kind is None:
            continue
        group = groups.setdefault(
            kind,
            {
                "failure_type": kind,
                "label": LABELS[kind],
                "count": 0,
                "details": [],
            },
        )
        count = int(row["count"])
        group["count"] += count
        details.setdefault(kind, Counter())[row["error_message"]] += count
        if kind == "subtask_failed":
            error_types[row["subtask_error_type"]] += count
    for kind, group in groups.items():
        group["details"] = _counts(details[kind], "message")
        if kind == "subtask_failed":
            group["error_types"] = _counts(error_types, "error_type")
    ordered = sorted(
        groups.values(),
        key=lambda group: (-group["count"], group["failure_type"]),
    )
    return {
        "total": sum(group["count"] for group in ordered),
        "groups": ordered,
        "queried_at": datetime.now(timezone.utc).isoformat(),
    }


def _counts(counts, key):
    return [
        {key: value, "count": count}
        for value, count in sorted(
            counts.items(),
            key=lambda item: (-item[1], item[0] or ""),
        )
    ]

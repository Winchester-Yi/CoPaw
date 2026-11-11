# -*- coding: utf-8 -*-
"""新版批次只读查询的统一关联和状态归约；不会扫描任务定义生成批次。"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

KEYS = ("tenant_id", "source_id", "agent_id", "job_id")
RECEIPTS = "swe_cron_managed_trigger_receipts"
MEMBERS = "swe_cron_managed_batch_members"
RUNS = "swe_cron_execution_runs"
EVENTS = "swe_cron_managed_dispatch_events"
STATS_FIELDS = (
    "total_batches",
    "total_members",
    "done_members",
    "failed_members",
    "running_batches",
)


def identity_join(left, right):
    return " AND ".join(
        f"{left}.{name}={right}.{name} AND {left}.{name} COLLATE utf8mb4_bin={right}.{name}"
        for name in KEYS
    )


MEMBER_FROM = (
    f"FROM {MEMBERS} m LEFT JOIN {RUNS} r ON m.current_execution_id=r.execution_id AND {identity_join('m', 'r')} "
    f"LEFT JOIN swe_cron_jobs_v2 d ON {identity_join('m', 'd')} "
    f"LEFT JOIN swe_cron_job_states s ON {identity_join('m', 's')} "
)
FAILED = "(r.status='error' OR r.settled_status='error')"
DONE = "(r.status IN ('error','skipped','cancelled') OR (r.status='success' AND r.settled_status IN ('success','error')))"
ACTIVE = "(r.status IN ('dispatching','accepted','running') OR (r.status='success' AND r.settled_status IS NULL))"
MANIFEST_IDENTITY = " AND ".join(
    f"JSON_UNQUOTE(JSON_EXTRACT(c.members_json,CONCAT('$[',m.dispatch_order,'].identity[{index}]'))) COLLATE utf8mb4_bin=m.{name}"
    for index, name in enumerate(KEYS)
)
VALID = (
    "(r.execution_id IS NOT NULL AND d.management_owner='scheduler' AND s.job_id IS NOT NULL AND m.member_id=m.initial_execution_id "
    "AND JSON_UNQUOTE(JSON_EXTRACT(r.dispatch_meta_json,'$.protocol'))='scheduler_managed_v1' "
    "AND r.kind IN ('ordinary','batch') AND r.scheduled_fire_at=c.logical_fire_at "
    "AND JSON_UNQUOTE(JSON_EXTRACT(c.members_json,CONCAT('$[',m.dispatch_order,'].execution_id')))=m.initial_execution_id "
    f"AND {MANIFEST_IDENTITY})"
)
RECEIPT_SELECT = f"""
SELECT c.receipt_id,c.batch_id,c.tenant_id,c.source_id,c.agent_id,c.job_id,
c.logical_fire_at,c.physical_fire_at,c.created_at,c.batch_paused,
CAST(c.members_json AS JSON) AS members_json,
JSON_UNQUOTE(JSON_EXTRACT(p.spec_json,'$.name')) AS job_name,
CASE WHEN ps.dispatch_state='batch_paused' THEN 1 ELSE 0 END AS dispatch_paused,
CASE WHEN p.management_owner='scheduler' THEN 0 ELSE 1 END AS invalid_parent
FROM {RECEIPTS} c
LEFT JOIN swe_cron_jobs_v2 p ON {identity_join('c', 'p')}
LEFT JOIN swe_cron_job_states ps ON {identity_join('c', 'ps')}
"""
BATCH_FROM = (
    "FROM board_receipts c "
    f"LEFT JOIN {MEMBERS} m ON c.batch_id=m.batch_id AND c.source_id=m.source_id AND c.source_id COLLATE utf8mb4_bin=m.source_id "
    f"LEFT JOIN {RUNS} r ON m.current_execution_id=r.execution_id AND {identity_join('m', 'r')} "
    f"LEFT JOIN swe_cron_jobs_v2 d ON {identity_join('m', 'd')} "
    f"LEFT JOIN swe_cron_job_states s ON {identity_join('m', 's')} "
)
BATCH_SELECT = f"""
SELECT /*+ NO_MERGE(c) */ c.receipt_id,c.batch_id,c.tenant_id,c.source_id,c.agent_id,c.job_id,
c.logical_fire_at,c.physical_fire_at,c.created_at,c.job_name,
MAX(JSON_LENGTH(c.members_json)) AS expected_member_count,
MAX(JSON_TYPE(c.members_json)) AS manifest_type,
MAX(CASE WHEN m.member_role='parent' THEN r.provider_id END) AS provider_id,
MAX(CASE WHEN m.member_role='parent' THEN r.model_id END) AS model_id,
MAX(c.dispatch_paused) AS dispatch_paused,
MAX(c.batch_paused) AS batch_paused,
COUNT(m.member_id) AS member_count,
SUM(CASE WHEN {DONE} THEN 1 ELSE 0 END) AS done_count,
SUM(CASE WHEN {FAILED} THEN 1 ELSE 0 END) AS failed_count,
SUM(CASE WHEN r.status='success' AND r.settled_status='success' THEN 1 ELSE 0 END) AS success_count,
SUM(CASE WHEN r.status IN ('skipped','cancelled') THEN 1 ELSE 0 END) AS skipped_count,
SUM(CASE WHEN {ACTIVE} THEN 1 ELSE 0 END) AS active_count,
SUM(CASE WHEN r.status IN ('queued','dispatch_failed') THEN 1 ELSE 0 END) AS waiting_count,
SUM(CASE WHEN m.member_id IS NOT NULL AND NOT COALESCE({VALID},0) THEN 1 ELSE 0 END) AS invalid_count,
MAX(c.invalid_parent) AS invalid_parent
{BATCH_FROM}
"""
STATE = "CASE WHEN invalid_count>0 OR invalid_parent>0 OR COALESCE(UPPER(manifest_type),'')<>'ARRAY' OR member_count<>expected_member_count THEN 'unavailable' WHEN active_count>0 THEN 'running' WHEN waiting_count>0 THEN 'pending' WHEN member_count=0 THEN 'received' WHEN done_count<>member_count THEN 'unavailable' WHEN failed_count>0 THEN 'failed' ELSE 'completed' END"


def db_time(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def time_text(value, *, local=False):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=ZoneInfo("Asia/Shanghai") if local else timezone.utc,
        )
    return parsed.isoformat()


def like_text(value):
    return (
        "%"
        + value.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        + "%"
    )


def batch_where(source, start=None, end=None, query=None, batch_id=None):
    clauses = [
        "c.source_id=%s",
        "c.source_id COLLATE utf8mb4_bin=%s",
        "c.kind='batch'",
        "c.status='accepted'",
        "c.batch_id IS NOT NULL",
    ]
    params = [source, source]
    for operator, value in ((">=", start), ("<=", end)):
        if value is not None:
            clauses.append(f"c.logical_fire_at{operator}%s")
            params.append(db_time(value))
    if batch_id is not None:
        clauses.append("c.batch_id=%s")
        params.append(batch_id)
    if query:
        fields = (
            "c.batch_id",
            "c.job_id",
            "c.tenant_id",
            "c.agent_id",
            "JSON_UNQUOTE(JSON_EXTRACT(p.spec_json,'$.name'))",
        )
        root_match = " OR ".join(
            f"{name} LIKE %s ESCAPE '!'" for name in fields
        )
        member_fields = (
            "search.tenant_id",
            "search.agent_id",
            "search.job_id",
            "search_run.provider_id",
            "search_run.model_id",
        )
        member_match = " OR ".join(
            f"{name} LIKE %s ESCAPE '!'" for name in member_fields
        )
        clauses.append(
            f"({root_match} OR EXISTS (SELECT 1 FROM {MEMBERS} search JOIN {RUNS} search_run ON search.current_execution_id=search_run.execution_id AND {identity_join('search', 'search_run')} WHERE search.batch_id=c.batch_id AND ({member_match})))",
        )
        params.extend([like_text(query)] * (len(fields) + len(member_fields)))
    return " AND ".join(clauses), tuple(params)


def _aggregate_ctes(where):
    # NO_MERGE 将文本清单物化为原生 JSON，避免逐成员重新解析整份 LONGTEXT。
    return (
        f"WITH board_receipts AS ({RECEIPT_SELECT} WHERE {where}), "
        f"board_aggregates AS ({BATCH_SELECT} GROUP BY c.receipt_id), "
        f"board_batches AS (SELECT a.*, {STATE} AS status FROM board_aggregates a)"
    )


def aggregate_query(where):
    return f"{_aggregate_ctes(where)} SELECT * FROM board_batches"


def list_query(where, filtered):
    stats_filter = (
        "WHERE status=%s OR status='unavailable'" if filtered else ""
    )
    page_filter = "WHERE status=%s" if filtered else ""
    return (
        f"{_aggregate_ctes(where)}, board_stats AS ("
        "SELECT COUNT(*) AS total_batches, SUM(member_count) AS total_members, "
        "SUM(done_count) AS done_members, SUM(failed_count) AS failed_members, "
        "SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running_batches, "
        "SUM(CASE WHEN status='unavailable' THEN 1 ELSE 0 END) AS unavailable_batches "
        f"FROM board_batches {stats_filter}), board_page AS ("
        f"SELECT * FROM board_batches {page_filter} "
        "ORDER BY logical_fire_at DESC,batch_id DESC LIMIT %s OFFSET %s) "
        "SELECT board_stats.*,board_page.* FROM board_stats "
        "LEFT JOIN board_page ON 1=1 "
        "ORDER BY board_page.logical_fire_at DESC,board_page.batch_id DESC"
    )


def event_selection(source, batch_id):
    """按批索引和执行索引取并集，避免 OR/相关子查询扫描来源的全部历史事件。"""
    direct = f"SELECT event_id FROM {EVENTS} WHERE source_id=%s AND source_id COLLATE utf8mb4_bin=%s AND batch_id=%s"
    parts, params = [direct], [source, source, batch_id]
    for pointer in ("initial_execution_id", "current_execution_id"):
        parts.append(
            f"SELECT e.event_id FROM {MEMBERS} bm JOIN {EVENTS} e ON bm.{pointer}=e.execution_id WHERE bm.batch_id=%s AND bm.source_id=%s AND bm.source_id COLLATE utf8mb4_bin=%s AND e.source_id=%s AND e.source_id COLLATE utf8mb4_bin=%s",
        )
        params.extend((batch_id, source, source, source, source))
    return " UNION ".join(parts), tuple(params)

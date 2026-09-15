"""报表设计的可执行参考；不是已接入生产的服务。契约见 README.md。"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

TASK_TYPES = ("push_plan", "ask_plan", "push_other")
LABELS = dict(
    zip(
        TASK_TYPES,
        ("推送(名单+方案)", "主动提问(名单+方案)", "推送(非名单方案)"),
    )
)
COUNTS = (
    "skill_count",
    "permission_manager_count",
    "active_manager_count",
    "suc_execute_job",
    "read_tasks",
    "recommended_customers",
    "read_customer_count",
    "insight_customer_count",
    "insight_count",
    "phone_customer_count",
    "phone_count",
)
RATIOS = {
    "read_rate": ("read_tasks", "suc_execute_job"),
    "plan_read_rate": ("read_customer_count", "recommended_customers"),
    "click_to_insight_rate": (
        "insight_customer_count",
        "recommended_customers",
    ),
    "click_to_phone_rate": ("phone_customer_count", "recommended_customers"),
}


@dataclass(frozen=True)
class Scope:
    source_id: str
    sync_date: str
    start: datetime
    stop: datetime
    group_by: str = "overall"
    first_bbk_id: str | None = None
    org_id: str | None = None

    def __post_init__(self):
        if not self.source_id.strip() or not self.sync_date:
            raise ValueError("source_id and sync_date are required")
        date.fromisoformat(self.sync_date)
        if self.group_by not in ("overall", "branch", "org"):
            raise ValueError("invalid group_by")
        if self.start.tzinfo is not None or self.stop.tzinfo is not None:
            raise ValueError(
                "pass timestamps converted to the verified DB timezone"
            )
        if not timedelta(0) < self.stop - self.start <= timedelta(days=93):
            raise ValueError(
                "date window must be positive and at most 93 days"
            )


def date_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    """日期首尾均包含；数据库查询统一使用半开区间。"""
    return datetime.combine(start_date, time.min), datetime.combine(
        end_date + timedelta(days=1), time.min
    )


def bind(sql: str, values: dict) -> tuple[str, tuple]:
    """编译内部命名占位符；值不进入 SQL 文本，兼容现有 fetch_all。"""
    names = re.findall(r":([a-z_]+)\b", sql)
    return re.sub(r":([a-z_]+)\b", "%s", sql), tuple(
        values[name] for name in names
    )


def build_queries(scope: Scope) -> dict[str, tuple[str, tuple]]:
    """一次构造所有分组查询；查询数与机构数无关。"""
    values = vars(scope)
    roster_filter = ""
    if scope.first_bbk_id is not None:
        roster_filter += " AND first_bbk_id = :first_bbk_id"
    if scope.org_id is not None:
        roster_filter += " AND org_id = :org_id"
    roster = f"""SELECT user_id, first_bbk_id, org_id,
        MIN(first_bbk_nm) AS first_bbk_nm, MIN(org_nm) AS org_nm
        FROM jkh_user_inf WHERE sync_date = :sync_date
        AND user_id IS NOT NULL AND user_id <> '' {roster_filter}
        GROUP BY user_id, first_bbk_id, org_id"""
    dimensions = {
        "overall": ("''", "''"),
        "branch": ("r.first_bbk_id", "''"),
        "org": ("r.first_bbk_id", "r.org_id"),
    }
    bbk, org = dimensions[scope.group_by]
    dims = f"{bbk} AS group_bbk, {org} AS group_org"
    groups = "group_bbk, group_org"
    has_sub = "EXISTS (SELECT 1 FROM swe_cron_subtasks s WHERE s.trace_id = e.trace_id AND e.trace_id <> '')"
    push = f"""SELECT e.id, e.trace_id, e.tenant_id AS user_id,
        e.status, e.async_status, e.is_read, j.id AS job_id,
        j.tenant_id AS job_user_id, j.source_id, j.skill_ids,
        j.status AS job_status, j.deleted_at,
        CASE WHEN {has_sub} THEN 'push_plan' ELSE 'push_other' END AS task_type
        FROM swe_cron_executions e JOIN swe_cron_jobs j ON j.id = e.job_id
        WHERE j.source_id = :source_id AND e.actual_time >= :start AND e.actual_time < :stop
        AND ({has_sub} OR (e.status = 'success' AND e.async_status = 'success'))"""
    ask_qualifier = """sp.trace_id <> ''
        AND sp.skill_id IS NOT NULL AND sp.skill_id <> ''
        AND EXISTS (SELECT 1 FROM swe_cron_subtasks s WHERE s.trace_id = sp.trace_id)
        AND NOT EXISTS (SELECT 1 FROM swe_cron_executions e WHERE e.trace_id = sp.trace_id)"""
    ask = f"""SELECT sp.trace_id, sp.source_id, sp.user_id, sp.skill_id
        FROM swe_tracing_spans sp WHERE sp.source_id = :source_id
        AND sp.start_time >= :start AND sp.start_time < :stop AND {ask_qualifier}"""
    stat_job = """EXISTS (SELECT 1 FROM swe_marketplace_skills k
        WHERE k.source_id = p.source_id AND k.include_in_statistics = 1
        AND k.skill_id IS NOT NULL AND k.skill_id <> ''
        AND FIND_IN_SET(k.skill_id, p.skill_ids))"""
    stat_ask = """EXISTS (SELECT 1 FROM swe_marketplace_skills k
        WHERE k.source_id = sp.source_id AND k.skill_id = sp.skill_id
        AND k.include_in_statistics = 1 AND k.skill_id <> '')"""
    query = {}
    # 在名称/机构筛选之前查冲突，防止用户所属机构不唯一被筛选掩盖。
    query["roster_conflicts"] = """SELECT user_id FROM (
        SELECT DISTINCT user_id, first_bbk_id, org_id FROM jkh_user_inf
        WHERE sync_date = :sync_date AND user_id IS NOT NULL AND user_id <> ''
        ) r GROUP BY user_id HAVING COUNT(*) > 1 LIMIT 1"""
    query[
        "permissions"
    ] = f"""SELECT {dims}, MIN(r.first_bbk_nm) AS first_bbk_name,
        MIN(r.org_nm) AS org_name,
        COUNT(DISTINCT CASE WHEN EXISTS (SELECT 1 FROM swe_tenant_init_source i
            WHERE i.tenant_id = r.user_id AND i.source_id = :source_id)
            THEN r.user_id END) AS permission_manager_count
        FROM ({roster}) r GROUP BY {groups}"""
    query["push_tasks"] = f"""SELECT {dims}, p.task_type,
        SUM(CASE WHEN p.status = 'success' AND p.async_status = 'success' THEN 1 ELSE 0 END) AS suc_execute_job,
        SUM(CASE WHEN p.is_read = 1 THEN 1 ELSE 0 END) AS read_tasks
        FROM ({push}) p JOIN ({roster}) r ON r.user_id = p.user_id
        GROUP BY {groups}, p.task_type"""
    query["ask_tasks"] = f"""SELECT {dims}, 'ask_plan' AS task_type,
        COUNT(DISTINCT sp.trace_id) AS suc_execute_job,
        COUNT(DISTINCT sp.trace_id) AS read_tasks
        FROM ({ask}) sp JOIN ({roster}) r ON r.user_id = sp.user_id
        GROUP BY {groups}"""
    query["active"] = f"""SELECT {dims}, p.task_type,
        COUNT(DISTINCT p.job_user_id) AS active_manager_count
        FROM ({push}) p JOIN ({roster}) r ON r.user_id = p.job_user_id
        WHERE p.job_status = 'active' AND p.deleted_at IS NULL
        GROUP BY {groups}, p.task_type"""
    query[
        "push_skills"
    ] = f"""SELECT {dims}, p.task_type, COUNT(DISTINCT k.skill_id) AS skill_count
        FROM ({push}) p JOIN ({roster}) r ON r.user_id = p.user_id
        JOIN swe_marketplace_skills k ON k.source_id = p.source_id
            AND FIND_IN_SET(k.skill_id, p.skill_ids)
        WHERE k.include_in_statistics = 1 AND k.skill_id <> ''
            AND p.deleted_at IS NULL AND p.job_status <> 'deleted'
        GROUP BY {groups}, p.task_type"""
    query[
        "ask_skills"
    ] = f"""SELECT {dims}, 'ask_plan' AS task_type, COUNT(DISTINCT k.skill_id) AS skill_count
        FROM ({ask}) sp JOIN ({roster}) r ON r.user_id = sp.user_id
        JOIN swe_marketplace_skills k ON k.source_id = sp.source_id AND k.skill_id = sp.skill_id
        WHERE k.include_in_statistics = 1 AND k.skill_id <> '' GROUP BY {groups}"""
    query["push_customers"] = f"""SELECT {dims}, 'push_plan' AS task_type,
        COUNT(DISTINCT s.custuid) AS recommended_customers
        FROM ({push}) p JOIN ({roster}) r ON r.user_id = p.user_id
        JOIN swe_cron_subtasks s ON s.trace_id = p.trace_id
        WHERE p.task_type = 'push_plan' AND s.custuid IS NOT NULL AND s.custuid <> ''
        AND {stat_job} GROUP BY {groups}"""
    query["ask_customers"] = f"""SELECT {dims}, 'ask_plan' AS task_type,
        COUNT(DISTINCT s.custuid) AS recommended_customers
        FROM ({ask}) sp JOIN ({roster}) r ON r.user_id = sp.user_id
        JOIN swe_cron_subtasks s ON s.trace_id = sp.trace_id
        WHERE s.custuid IS NOT NULL AND s.custuid <> '' GROUP BY {groups}"""
    # 点击必须从 clicked_at 缩小范围；关联任务不附加任务生成时间限制。
    click_push = """EXISTS (SELECT 1 FROM swe_cron_executions e
        JOIN swe_cron_jobs j ON j.id = e.job_id
        WHERE e.trace_id = c.trace_id AND j.id = c.cron_task_id
        AND j.source_id = c.source_id AND j.deleted_at IS NULL AND j.status <> 'deleted'
        AND EXISTS (SELECT 1 FROM swe_cron_subtasks s WHERE s.trace_id = e.trace_id)
        AND EXISTS (SELECT 1 FROM swe_marketplace_skills k
            WHERE k.source_id = j.source_id AND k.include_in_statistics = 1
            AND k.skill_id <> '' AND FIND_IN_SET(k.skill_id, j.skill_ids)))"""
    click_ask = f"""EXISTS (SELECT 1 FROM swe_tracing_spans sp
        WHERE sp.trace_id = c.trace_id AND sp.source_id = c.source_id
        AND {ask_qualifier} AND {stat_ask})"""
    click_rows = f"""SELECT c.*, CASE WHEN {click_push} THEN 'push_plan' ELSE 'ask_plan' END AS task_type
        FROM swe_html_preview_click_events c WHERE c.source_id = :source_id
        AND c.clicked_at >= :start AND c.clicked_at < :stop
        AND c.trace_id IS NOT NULL AND c.trace_id <> ''
        AND c.customer_id IS NOT NULL AND c.customer_id <> ''
        AND ((c.event_type = 'preview_view' AND c.template_type = 'sub')
            OR (c.event_type = 'button_click' AND c.button_type IN ('insight', 'phone')))
        AND ({click_push} OR {click_ask})"""
    query["clicks"] = f"""SELECT {dims}, c.task_type,
        COUNT(DISTINCT CASE WHEN c.event_type = 'preview_view' AND c.template_type = 'sub'
            THEN c.customer_id END) AS read_customer_count,
        COUNT(DISTINCT CASE WHEN c.event_type = 'button_click' AND c.button_type = 'insight'
            THEN c.customer_id END) AS insight_customer_count,
        COUNT(CASE WHEN c.event_type = 'button_click' AND c.button_type = 'insight'
            THEN 1 END) AS insight_count,
        COUNT(DISTINCT CASE WHEN c.event_type = 'button_click' AND c.button_type = 'phone'
            THEN c.customer_id END) AS phone_customer_count,
        COUNT(CASE WHEN c.event_type = 'button_click' AND c.button_type = 'phone'
            THEN 1 END) AS phone_count
        FROM ({click_rows}) c JOIN ({roster}) r ON r.user_id = c.user_id
        GROUP BY {groups}, c.task_type"""
    return {name: bind(sql, values) for name, sql in query.items()}


def percentage(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return float(
        (Decimal(numerator) * 100 / Decimal(denominator)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


def assemble(results: dict[str, list[dict]], group_by: str) -> list[dict]:
    """只合并数据库聚合结果；绝不累加分组后的 DISTINCT 计数生成总行。"""
    rows = {}
    for dimension in results["permissions"]:
        for task_type in TASK_TYPES:
            key = (dimension["group_bbk"], dimension["group_org"], task_type)
            row = {field: 0 for field in COUNTS}
            row.update(
                first_bbk_id=key[0] if group_by != "overall" else None,
                org_id=key[1] if group_by == "org" else None,
                first_bbk_name=(
                    dimension["first_bbk_name"]
                    if group_by != "overall"
                    else None
                ),
                org_name=dimension["org_name"] if group_by == "org" else None,
                task_type=task_type,
                task_type_name=LABELS[task_type],
                permission_manager_count=int(
                    dimension["permission_manager_count"]
                ),
            )
            rows[key] = row
    for name, records in results.items():
        if name in ("permissions", "roster_conflicts"):
            continue
        for record in records:
            key = (
                record["group_bbk"],
                record["group_org"],
                record["task_type"],
            )
            if key not in rows:
                raise ValueError("roster changed during report query")
            for field in COUNTS:
                if field in record:
                    rows[key][field] = int(record[field] or 0)
    for row in rows.values():
        if row["task_type"] == "ask_plan":
            row["active_manager_count"] = None
        if row["task_type"] == "push_other":
            for field in (
                "recommended_customers",
                "read_customer_count",
                "insight_customer_count",
                "insight_count",
                "phone_customer_count",
                "phone_count",
            ):
                row[field] = None
        for field, (numerator, denominator) in RATIOS.items():
            row[field] = percentage(row[numerator], row[denominator])
    return [
        rows[key]
        for key in sorted(
            rows,
            key=lambda k: (k[0] or "", k[1] or "", TASK_TYPES.index(k[2])),
        )
    ]


async def query_core(db, scope: Scope) -> list[dict]:
    """db 使用现有 DatabaseConnection；空快照应在调用此函数之前处理。"""
    queries = build_queries(scope)
    conflicts = await db.fetch_all(*queries["roster_conflicts"])
    if conflicts:
        raise ValueError("jkh_roster_ambiguous")
    results = {}
    for name, (sql, params) in queries.items():
        if name != "roster_conflicts":
            results[name] = await db.fetch_all(sql, params)
    return assemble(results, scope.group_by)

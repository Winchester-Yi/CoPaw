# -*- coding: utf-8 -*-
"""独立的金葵花任务类型报表服务，保持现有报表方法不变。"""

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from ...database import get_db_connection
from ...models.task_type_report import (
    TaskTypeReportParams,
    TaskTypeReportResponse,
    ReportOptionsParams,
    ReportOptionsResponse,
)
from .query_service import QueryService
from .task_type_report_sql import Scope, build_queries

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
DATABASE_UNAVAILABLE = "报表数据库暂不可用。"


def date_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    """日期首尾均包含；数据库查询统一使用半开区间。"""
    return datetime.combine(start_date, time.min), datetime.combine(
        end_date + timedelta(days=1), time.min
    )


def percentage(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return float(
        (Decimal(numerator) * 100 / Decimal(denominator)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


def _empty_rows(dimensions: list[dict], group_by: str) -> dict:
    """以名单机构为骨架补齐三类任务。"""
    rows = {}
    for dimension in dimensions:
        for task_type in TASK_TYPES:
            key = (
                dimension["group_bbk"],
                dimension["group_org"],
                dimension.get("group_user", ""),
                task_type,
            )
            row = {field: 0 for field in COUNTS}
            row.update(
                first_bbk_id=key[0] if group_by != "overall" else None,
                org_id=key[1] if group_by in ("org", "manager") else None,
                first_bbk_name=(
                    dimension["first_bbk_name"]
                    if group_by != "overall"
                    else None
                ),
                org_name=(
                    dimension["org_name"]
                    if group_by in ("org", "manager")
                    else None
                ),
                user_id=key[2] if group_by == "manager" else None,
                user_name=dimension.get("user_name"),
                sapid=key[2] if group_by == "manager" else None,
                pst_lvl=dimension.get("pst_lvl"),
                skill_id=None,
                cn_name=None,
                task_type=task_type,
                task_type_name=LABELS[task_type],
                permission_manager_count=int(
                    dimension["permission_manager_count"]
                ),
            )
            rows[key] = row
    return rows


def _finish_row(row: dict) -> dict:
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
    return row


def assemble(
    results: dict[str, list[dict]], group_by: str, skill_detail: bool = False
) -> list[dict]:
    """只合并数据库聚合结果；绝不累加分组后的 DISTINCT 计数生成总行。"""
    base_rows = _empty_rows(results["permissions"], group_by)
    rows = {} if skill_detail else base_rows
    for name, records in results.items():
        if name in ("permissions", "roster_conflicts"):
            continue
        for record in records:
            key = (
                record["group_bbk"],
                record["group_org"],
                record.get("group_user", ""),
                record["task_type"],
            )
            if key not in base_rows:
                raise ValueError("roster changed during report query")
            if skill_detail:
                key = _expand_skill_rows(rows, base_rows, key, record)
            for field in COUNTS:
                if field in record:
                    rows[key][field] = int(record[field] or 0)
    return [
        _finish_row(rows[key])
        for key in sorted(
            rows,
            key=lambda k: (
                k[0] is not None,
                k[0] or "",
                k[1] is not None,
                k[1] or "",
                k[2],
                k[4] if skill_detail else "",
                TASK_TYPES.index(k[3]),
            ),
        )
    ]


def _expand_skill_rows(
    rows: dict, base_rows: dict, key: tuple, record: dict
) -> tuple:
    """只展开有事实关联的人员/机构与技能组合，不做名单×技能笛卡尔积。"""
    for task_type in TASK_TYPES:
        base_key = (*key[:3], task_type)
        skill_key = (*base_key, record["skill_id"])
        if skill_key not in rows:
            rows[skill_key] = {
                **base_rows[base_key],
                "skill_id": record["skill_id"],
                "cn_name": record["cn_name"],
            }
    return (*key, record["skill_id"])


async def query_core(db, scope: Scope) -> list[dict]:
    """db 使用现有 DatabaseConnection；空快照应在调用此函数之前处理。"""
    queries = build_queries(scope)
    conflicts = await db.fetch_all(*queries["roster_conflicts"])
    if conflicts:
        raise ReportError(
            503,
            "jkh_roster_ambiguous",
            "名单快照中存在同一用户的多个机构归属。",
        )
    results = {}
    for name, (sql, params) in queries.items():
        if name != "roster_conflicts":
            results[name] = await db.fetch_all(sql, params)
            if name == "permissions" and not results[name]:
                return []
    return assemble(results, scope.group_by, scope.skill_detail)


class ReportError(Exception):
    """只由新报表路由消费的稳定业务错误。"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


async def resolve_organization_filters(
    db, sync_date: str, params: TaskTypeReportParams
) -> tuple[bool, dict[str, str | None]]:
    """同一快照内精确解析名称，显式区分无匹配和未筛选。"""
    resolved = {"first_bbk_id": params.first_bbk_id, "org_id": params.org_id}
    # 只遍历两个固定层级，机构数量不会扩大查询次数。
    for name, name_column, id_columns in (
        (params.first_bbk_name, "first_bbk_nm", ("first_bbk_id",)),
        (params.org_name, "org_nm", ("first_bbk_id", "org_id")),
    ):
        if name is None:
            continue
        conditions = ["sync_date = %s", f"{name_column} = %s"]
        values = [sync_date, name]
        for column in id_columns:
            conditions.extend([f"{column} IS NOT NULL", f"{column} <> ''"])
            if resolved[column] is not None:
                conditions.append(f"{column} = %s")
                values.append(resolved[column])
        sql = (
            f"SELECT DISTINCT {', '.join(id_columns)} FROM jkh_user_inf "
            f"WHERE {' AND '.join(conditions)} LIMIT 2"
        )
        rows = await db.fetch_all(sql, tuple(values))
        if not rows:
            return False, resolved
        if len(rows) > 1:
            raise ReportError(
                422,
                "organization_name_ambiguous",
                "机构名称对应多个机构，请同时提供父分行或机构 ID。",
            )
        resolved.update(rows[0])
    return True, resolved


def enforce_branch(params, bbk_id: str):
    """使用现有可信网关传入的分行范围；客户端筛选只能收窄。"""
    if (
        not isinstance(bbk_id, str)
        or not bbk_id.strip()
        or len(bbk_id.strip()) > 64
    ):
        raise ReportError(
            422, "report_scope_required", "必须提供有效的 X-Bbk-Id。"
        )
    bbk_id = bbk_id.strip()
    if bbk_id != "100":
        if params.first_bbk_id is not None and params.first_bbk_id != bbk_id:
            raise ReportError(
                403, "report_scope_forbidden", "不能查询其他分行的数据。"
            )
        return params.model_copy(update={"first_bbk_id": bbk_id})
    return params


def report_db():
    try:
        db = get_db_connection()
    except RuntimeError as exc:
        raise ReportError(
            503, "report_database_unavailable", DATABASE_UNAVAILABLE
        ) from exc
    if not db.is_connected:
        raise ReportError(
            503, "report_database_unavailable", DATABASE_UNAVAILABLE
        )
    return db


async def validate_roster_scope(
    db, sync_date: str, params: TaskTypeReportParams
):
    """存在但不属于有效分行/网点的 ID 报 403；不存在的 ID 保留空结果。"""
    selectors = (
        ("org_id", params.org_id),
        ("user_id", params.user_id),
        ("first_bbk_nm", params.first_bbk_name),
        ("org_nm", params.org_name),
    )
    for column, value in selectors:
        if value is None:
            continue
        conditions, allowed_params = [], []
        if params.first_bbk_id is not None:
            conditions.append("first_bbk_id = %s")
            allowed_params.append(params.first_bbk_id)
        if column == "user_id" and params.org_id is not None:
            conditions.append("org_id = %s")
            allowed_params.append(params.org_id)
        if not conditions:
            continue
        allowed = " AND ".join(conditions)
        row = await db.fetch_one(
            f"SELECT COUNT(*) AS total, SUM(CASE WHEN {allowed} THEN 1 ELSE 0 END) AS allowed "
            f"FROM jkh_user_inf WHERE sync_date = %s AND {column} = %s",
            tuple(allowed_params + [sync_date, value]),
        )
        if row and row["total"] and not row["allowed"]:
            raise ReportError(
                403,
                "report_scope_forbidden",
                "所选机构或客户经理不属于允许的查询范围。",
            )


async def query_page(db, scope: Scope, params: TaskTypeReportParams):
    # 检查全快照冲突，空页也不能跳过该校验。
    conflicts = await db.fetch_all(*build_queries(scope)["roster_conflicts"])
    if conflicts:
        raise ReportError(
            503,
            "jkh_roster_ambiguous",
            "名单快照中存在同一用户的多个机构归属。",
        )
    key_sql, values = build_queries(scope, keys_only=True)["keys"]
    count = await db.fetch_one(
        f"SELECT COUNT(*) AS total FROM ({key_sql}) dimension_keys", values
    )
    total = int(count["total"] or 0)
    offset = (params.page - 1) * params.page_size
    if offset >= total:
        return [], total
    page_keys = await db.fetch_all(
        f"SELECT * FROM ({key_sql}) dimension_keys ORDER BY "
        "group_bbk IS NOT NULL, COALESCE(group_bbk, ''), "
        "group_org IS NOT NULL, COALESCE(group_org, ''), group_user, skill_id "
        "LIMIT %s OFFSET %s",
        values + (params.page_size, offset),
    )
    if not page_keys:
        return [], total
    scoped_page = replace(
        scope,
        manager_keys=tuple(
            (row["group_user"], row["skill_id"]) for row in page_keys
        ),
    )
    return await query_core(db, scoped_page), total


class TaskTypeReportService:
    """请求状态均为局部变量；统计、选项、导出共用快照和范围规则。"""

    async def get_report(
        self, params: TaskTypeReportParams, source_id: str, bbk_id: str
    ) -> TaskTypeReportResponse:
        params = enforce_branch(params, bbk_id)
        db = report_db()
        sync_date = await QueryService._resolve_jkh_sync_date(
            db, datetime.combine(params.end_date, time.max)
        )
        filters = {
            "first_bbk_id": params.first_bbk_id,
            "org_id": params.org_id,
        }
        warnings = ["period_ratios_not_cohort_conversion"]
        items, total = [], None
        if sync_date is None:
            warnings.append("empty_roster")
        else:
            await validate_roster_scope(db, sync_date, params)
            matched, filters = await resolve_organization_filters(
                db, sync_date, params
            )
            if matched:
                if (
                    filters["first_bbk_id"] != params.first_bbk_id
                    or filters["org_id"] != params.org_id
                ):
                    await validate_roster_scope(
                        db, sync_date, params.model_copy(update=filters)
                    )
                start, stop = date_bounds(params.start_date, params.end_date)
                scope = Scope(
                    source_id=source_id,
                    sync_date=sync_date,
                    start=start,
                    stop=stop,
                    group_by=params.group_by,
                    skill_detail=params.skill_detail,
                    user_id=params.user_id,
                    keyword=params.keyword,
                    **filters,
                )
                if params.page is not None:
                    items, total = await query_page(db, scope, params)
                else:
                    items = await query_core(db, scope)
            if not items and not total:
                warnings.append(
                    "no_matching_skills"
                    if matched and params.skill_detail
                    else "no_matching_organization"
                )
        if params.task_type is not None:
            items = [
                item for item in items if item["task_type"] == params.task_type
            ]
        if total is None:
            total = len(items)
        if params.skill_detail:
            warnings.append("skill_rows_not_additive")
        return TaskTypeReportResponse(
            start_date=params.start_date,
            end_date=params.end_date,
            source_id=source_id,
            sync_date=sync_date,
            group_by=params.group_by,
            skill_detail=params.skill_detail,
            resolved_filters=filters,
            warnings=warnings,
            items=items,
            page=params.page,
            page_size=params.page_size,
            total=total,
            has_more=params.page is not None
            and params.page * params.page_size < total,
        )

    async def get_options(
        self, params: ReportOptionsParams, source_id: str, bbk_id: str
    ) -> ReportOptionsResponse:
        params = enforce_branch(params, bbk_id)
        if params.kind == "orgs" and params.first_bbk_id is None:
            raise ReportError(
                422, "report_branch_required", "查询支行选项必须指定分行。"
            )
        db = report_db()
        sync_date = await QueryService._resolve_jkh_sync_date(
            db, datetime.combine(params.end_date, time.max)
        )
        if sync_date is None:
            return ReportOptionsResponse(sync_date=None, items=[])
        id_column, name_column = (
            ("first_bbk_id", "first_bbk_nm")
            if params.kind == "branches"
            else ("org_id", "org_nm")
        )
        branch_filter, values = "", [sync_date]
        if params.first_bbk_id is not None:
            branch_filter = " AND first_bbk_id = %s"
            values.append(params.first_bbk_id)
        rows = await db.fetch_all(
            f"SELECT {id_column} AS value, COALESCE(MIN(NULLIF({name_column}, '')), {id_column}) AS label "
            f"FROM jkh_user_inf WHERE sync_date = %s AND {id_column} IS NOT NULL "
            f"AND TRIM({id_column}) <> '' {branch_filter} GROUP BY {id_column} ORDER BY {id_column}",
            tuple(values),
        )
        return ReportOptionsResponse(sync_date=sync_date, items=rows)


def get_task_type_report_service() -> TaskTypeReportService:
    return TaskTypeReportService()

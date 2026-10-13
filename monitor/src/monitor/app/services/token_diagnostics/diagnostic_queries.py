# -*- coding: utf-8 -*-
"""Read-only B projections from saved facts, immutable history and current heads.

All multi-table responses share a TokenStore repeatable-read transaction. No
HTTP projection claims leases, runs analysis or compares raw prompt material.
"""

from dataclasses import replace
from datetime import datetime, timezone
from typing import TypedDict, NotRequired, cast

from .diagnostic_filters import (
    DiagnosticFilters,
    encode_cursor,
    normalize_scope,
    sql_time,
    utc_string,
    validate_cursor,
    validate_identity,
)
from .diagnostic_contract import RULE_IDS
from .recommendations import RECOMMENDATIONS
from .storage_schema import COLUMNS
from .store import decode_row


class UsageDTO(TypedDict):
    known_input_tokens: int
    known_output_tokens: int
    known_total_tokens: int
    actual_total_tokens: int | None
    observed_call_count: int
    reported_call_count: int
    partial_call_count: int
    unknown_call_count: int
    usage_complete: bool


class CompletenessDTO(TypedDict):
    branch_count: int
    expected_attempt_count: int | None
    observed_call_count: int
    missing_child_count: int
    gap_count: int
    gap_tracking_limited: bool
    capture_state: str
    usage_complete: bool


class MetadataDTO(TypedDict):
    scope: dict[str, str]
    window: dict[str, str]
    as_of: str


class PageDTO(MetadataDTO):
    items: list[dict]
    next_cursor: str | None


class TaskDTO(MetadataDTO):
    task_run_id: str
    root: dict
    branches: list[dict]
    branches_limited: bool
    lifecycle_usage: UsageDTO
    selected_contribution: UsageDTO
    window_attempt_usage: UsageDTO
    completeness: CompletenessDTO


class EvaluationLocatorDTO(TypedDict):
    evaluation_id: str
    task_run_id: str
    branch_run_id: str
    object_type: str
    object_id: str
    rule_id: str
    state: str
    input_revision: int
    current_data_revision: int
    snapshot_id: str
    rule_version: int
    threshold_version: int
    freshness: str
    freshness_reason: str | None
    analysis_state: str


class EvaluationLocatorsDTO(TypedDict):
    items: list[EvaluationLocatorDTO]
    limited: bool


class BranchDTO(TypedDict):
    branch_run_id: str
    task_run_id: str
    parent_branch_run_id: str | None
    agent_id: str | None
    execution_state: str
    data_revision: int
    analyzed_revision: int
    freshness: str
    evaluations: EvaluationLocatorsDTO


class CallDTO(TypedDict):
    call_id: str
    task_run_id: str
    branch_run_id: str
    agent_id: str | None
    model_name: str | None
    input_tokens: int | None
    output_tokens: int | None
    usage_status: str
    context_id: str | None
    evaluations: EvaluationLocatorsDTO


class OccurrenceDTO(TypedDict):
    context_id: str
    occurrence_index: int
    source_object_id: str | None
    source_ref: str | None
    producer_operation_id: str | None
    category: str | None
    attribution: str | None
    estimated_tokens: int | None
    text_start: int | None
    text_end: int | None


class OccurrencePageDTO(TypedDict):
    items: list[OccurrenceDTO]
    next_cursor: str | None


class ContextDTO(MetadataDTO):
    context: dict
    occurrences: OccurrencePageDTO
    composition: dict


class OperationComparisonDTO(TypedDict):
    unit: str
    before_estimated_tokens: int | None
    after_estimated_tokens: int | None
    saved_estimated_tokens: int | None
    reason_code: str | None


class PrefixCandidateDTO(TypedDict):
    historical_context_id: str
    historical_context_revision: int
    historical_call_id: str | None
    historical_ended_at: str | None
    lower_text_bytes: int | None
    upper_text_bytes: int | None
    first_difference_index: int | None


class IssueGroupDTO(TypedDict):
    group_id: str
    reliable_grouping: bool
    rule_id: str
    recommendation_code: str
    hit_count: int
    distinct_task_count: int
    first_seen: str
    latest_seen: str


class EvaluationDTO(MetadataDTO):
    evaluation: dict
    is_current: bool
    values: list[dict]
    gaps: list[dict]
    refs: list[dict]
    findings: list[dict]
    recommendation: dict | None
    values_limited: NotRequired[bool]
    gaps_limited: NotRequired[bool]
    refs_limited: NotRequired[bool]
    findings_limited: NotRequired[bool]


TERMINAL = "('completed','failed','cancelled')"
SAFE_EXCLUDED = frozenset(
    {
        "claim_token",
        "lease_until",
        "compatibility_digest",
        "content_digest",
        "structure_digest",
        "chained_digest",
        "fingerprint",
        "arguments_fingerprint",
        "result_fingerprint",
    },
)
USAGE_SQL = """
COALESCE(SUM(c.input_tokens),0) AS known_input_tokens,
COALESCE(SUM(c.output_tokens),0) AS known_output_tokens,
COUNT(c.call_id) AS observed_call_count,
COALESCE(SUM(CASE WHEN c.usage_status='reported' AND c.input_tokens IS NOT NULL AND c.output_tokens IS NOT NULL THEN 1 ELSE 0 END),0) AS reported_call_count,
COALESCE(SUM(CASE WHEN c.usage_status='partial' THEN 1 ELSE 0 END),0) AS partial_call_count,
COALESCE(SUM(CASE WHEN c.usage_status NOT IN ('reported','partial') OR c.usage_status IS NULL OR (c.usage_status='reported' AND (c.input_tokens IS NULL OR c.output_tokens IS NULL)) THEN 1 ELSE 0 END),0) AS unknown_call_count,
COALESCE(SUM(CASE WHEN c.execution_state NOT IN ('completed','error','cancelled') OR c.execution_state IS NULL THEN 1 ELSE 0 END),0) AS nonterminal_call_count
""".strip()


def columns(table, alias=None):
    return ",".join(
        f"{alias}.{key}" if alias else key for key in COLUMNS[table]
    )


def public_row(row):
    return {
        key: value for key, value in row.items() if key not in SAFE_EXCLUDED
    }


def usage_dto(row, complete=None):
    row = row or {}
    count = int(row.get("observed_call_count") or 0)
    reported = int(row.get("reported_call_count") or 0)
    complete = (
        (count == reported and not row.get("nonterminal_call_count"))
        if complete is None
        else complete
    )
    known_input = int(row.get("known_input_tokens") or 0)
    known_output = int(row.get("known_output_tokens") or 0)
    return {
        "known_input_tokens": known_input,
        "known_output_tokens": known_output,
        "known_total_tokens": known_input + known_output,
        "actual_total_tokens": (
            known_input + known_output if complete else None
        ),
        "observed_call_count": count,
        "reported_call_count": reported,
        "partial_call_count": int(row.get("partial_call_count") or 0),
        "unknown_call_count": int(row.get("unknown_call_count") or 0),
        "usage_complete": bool(complete),
    }


def completeness_dto(row):
    expected = row.get("expected_attempt_count")
    observed = int(row.get("observed_call_count") or 0)
    limited = bool(row.get("gap_tracking_limited"))
    capture = "complete"
    if expected is None or row.get("unknown_expected_count") or limited:
        capture = "unverified"
    elif (
        expected != observed
        or row.get("missing_child_count")
        or row.get("gap_count")
        or row.get("nonterminal_branch_count")
        or row.get("nonterminal_call_count")
    ):
        capture = "incomplete"
    complete = capture == "complete" and observed == row.get(
        "reported_call_count",
        0,
    )
    return {
        "branch_count": int(row.get("branch_count") or 0),
        "expected_attempt_count": (
            None if row.get("unknown_expected_count") else expected
        ),
        "observed_call_count": observed,
        "missing_child_count": int(row.get("missing_child_count") or 0),
        "gap_count": int(row.get("gap_count") or 0),
        "gap_tracking_limited": limited,
        "capture_state": capture,
        "usage_complete": bool(complete),
    }


def branch_dto(row):
    head_revision = row.pop("current_head_revision", None)
    head_count = row.pop("head_count", 0)
    freshness = "pending"
    if head_count:
        freshness = (
            "current" if head_revision == row["data_revision"] else "stale"
        )
    if row.get("analysis_state") == "failed":
        freshness = "failed"
    return {**public_row(row), "freshness": freshness}


class DiagnosticQueryService:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _request(scope, filters, resource):
        scope = normalize_scope(scope)
        filters = filters or DiagnosticFilters()
        position = validate_cursor(scope, filters, resource)
        metadata = {
            "scope": {
                "source_id": scope.source_id,
                "tenant_id": scope.tenant_id,
            },
            "window": {
                "start": utc_string(filters.start),
                "end": utc_string(filters.end),
                "semantics": "UTC half-open [start,end)",
            },
            "as_of": utc_string(datetime.now(timezone.utc)),
        }
        return scope, filters, position, metadata

    async def _rows(self, cursor, query, params=()):
        await cursor.execute(query, tuple(params))
        return await self.store._read_rows(cursor, query)

    @staticmethod
    def _scope(scope, alias):
        return f"{alias}.source_id=%s AND {alias}.tenant_id=%s", list(
            scope.pair(),
        )

    @staticmethod
    def _call_filters(where, params, filters, alias="c", window=True):
        if window:
            where += f" AND {alias}.started_at>=%s AND {alias}.started_at<%s"
            params.extend((sql_time(filters.start), sql_time(filters.end)))
        for key in ("agent_id", "model_name", "branch_run_id"):
            value = getattr(filters, key)
            if value is not None:
                where += f" AND {alias}.{key}=%s"
                params.append(value)
        if filters.state in {"reported", "partial", "unavailable", "pending"}:
            where += f" AND {alias}.usage_status=%s"
            params.append(filters.state)
        elif filters.state in {
            "completed",
            "failed",
            "error",
            "cancelled",
            "running",
            "prepared",
            "abandoned",
        }:
            where += f" AND {alias}.execution_state=%s"
            params.append(filters.state)
        return where, params

    def _root_filters(self, scope, filters, alias="r", window=True):
        where, params = self._scope(scope, alias)
        where += f" AND {alias}.parent_branch_run_id IS NULL"
        if filters.root_agent_id:
            where += f" AND {alias}.agent_id=%s"
            params.append(filters.root_agent_id)
        if window:
            where += f" AND {alias}.started_at>=%s AND {alias}.started_at<%s"
            params.extend((sql_time(filters.start), sql_time(filters.end)))
        if any((filters.agent_id, filters.model_name, filters.branch_run_id)):
            call_where, call_params = self._scope(scope, "selected")
            call_where, call_params = self._call_filters(
                call_where,
                call_params,
                replace(filters, state=None),
                "selected",
                window=False,
            )
            where += f" AND EXISTS (SELECT 1 FROM swe_token_model_calls selected WHERE {call_where} AND selected.task_run_id={alias}.task_run_id)"
            params.extend(call_params)
        where, params = self._root_state_filters(
            where,
            params,
            scope,
            filters,
            alias,
        )
        if filters.state in {"complete", "incomplete", "unverified"}:
            summary, bindings = self._task_summary_sql(scope)
            classification = "CASE WHEN unknown_expected_count>0 OR gap_tracking_limited=1 THEN 'unverified' WHEN expected_attempt_count<>observed_call_count OR missing_child_count>0 OR gap_count>0 OR nonterminal_branch_count>0 OR nonterminal_call_count>0 THEN 'incomplete' ELSE 'complete' END"
            where += f" AND {alias}.task_run_id IN (SELECT task_run_id FROM ({summary}) completeness_metrics WHERE {classification}=%s)"
            params.extend((*bindings, filters.state))
        if filters.rule_id or filters.state in {
            "candidate",
            "not_hit",
            "insufficient_evidence",
            "not_applicable",
        }:
            condition = f"h.task_run_id={alias}.task_run_id AND h.source_id={alias}.source_id AND h.tenant_id={alias}.tenant_id"
            if filters.rule_id:
                condition += " AND e.rule_id=%s"
                params.append(filters.rule_id)
            if filters.state in {
                "candidate",
                "not_hit",
                "insufficient_evidence",
                "not_applicable",
            }:
                condition += " AND e.state=%s"
                params.append(filters.state)
            where += f" AND EXISTS (SELECT 1 FROM swe_token_diagnostic_heads h JOIN swe_token_runs current_run ON current_run.branch_run_id=h.branch_run_id AND current_run.source_id=h.source_id AND current_run.tenant_id=h.tenant_id AND current_run.data_revision=h.input_revision JOIN swe_token_diagnostic_evaluations e ON e.evaluation_id=h.evaluation_id AND e.source_id=h.source_id AND e.tenant_id=h.tenant_id AND e.input_revision=current_run.data_revision WHERE {condition})"
        return where, params

    def _root_state_filters(self, where, params, scope, filters, alias):
        state = filters.state
        if state in {
            "completed",
            "failed",
            "cancelled",
            "running",
            "error",
            "prepared",
            "abandoned",
        }:
            where += f" AND {alias}.execution_state=%s"
            params.append(state)
        elif state in {"pending", "ready"}:
            where += f" AND {alias}.analysis_state=%s"
            params.append(state)
        elif state == "stale":
            where += f" AND {alias}.data_revision<>{alias}.analyzed_revision"
        elif state in {"reported", "partial", "unavailable"}:
            summary, bindings = self._task_summary_sql(scope)
            condition = {
                "reported": "reported_call_count=observed_call_count AND unknown_expected_count=0 AND expected_attempt_count=observed_call_count AND gap_tracking_limited=0 AND gap_count=0 AND missing_child_count=0 AND nonterminal_branch_count=0 AND nonterminal_call_count=0",
                "partial": "partial_call_count>0",
                "unavailable": "unknown_call_count>0",
            }[state]
            where += f" AND {alias}.task_run_id IN (SELECT task_run_id FROM ({summary}) usage_metrics WHERE {condition})"
            params.extend(bindings)
        return where, params

    def _task_summary_sql(self, scope, extra="", params=()):
        run_where, run_params = self._scope(scope, "r")
        call_where, call_params = self._scope(scope, "c")
        query = f"""SELECT lifecycle.task_run_id,lifecycle.branch_count,lifecycle.expected_attempt_count,
        lifecycle.unknown_expected_count,lifecycle.gap_count,lifecycle.gap_tracking_limited,
        lifecycle.nonterminal_branch_count,lifecycle.missing_child_count,
        COALESCE(calls.known_input_tokens,0) AS known_input_tokens,
        COALESCE(calls.known_output_tokens,0) AS known_output_tokens,
        COALESCE(calls.observed_call_count,0) AS observed_call_count,
        COALESCE(calls.reported_call_count,0) AS reported_call_count,
        COALESCE(calls.partial_call_count,0) AS partial_call_count,
        COALESCE(calls.unknown_call_count,0) AS unknown_call_count,
        COALESCE(calls.nonterminal_call_count,0) AS nonterminal_call_count
        FROM (SELECT r.task_run_id,COUNT(r.branch_run_id) AS branch_count,
        SUM(r.expected_attempt_count) AS expected_attempt_count,
        SUM(CASE WHEN r.expected_attempt_count IS NULL THEN 1 ELSE 0 END) AS unknown_expected_count,
        SUM(r.gap_count) AS gap_count,MAX(r.gap_tracking_limited) AS gap_tracking_limited,
        SUM(CASE WHEN r.execution_state NOT IN {TERMINAL} OR r.ended_at IS NULL THEN 1 ELSE 0 END) AS nonterminal_branch_count,
        SUM((SELECT COUNT(child.child_branch_run_id) FROM swe_token_expected_children child
          WHERE child.source_id=r.source_id AND child.tenant_id=r.tenant_id AND child.branch_run_id=r.branch_run_id
          AND NOT EXISTS (SELECT 1 FROM swe_token_runs observed WHERE observed.branch_run_id=child.child_branch_run_id AND observed.source_id=child.source_id AND observed.tenant_id=child.tenant_id AND observed.task_run_id=r.task_run_id))) AS missing_child_count
        FROM swe_token_runs r WHERE {run_where} {extra} GROUP BY r.task_run_id) lifecycle
        LEFT JOIN (SELECT c.task_run_id,{USAGE_SQL} FROM swe_token_model_calls c WHERE {call_where} GROUP BY c.task_run_id) calls ON calls.task_run_id=lifecycle.task_run_id"""
        return query, (*run_params, *params, *call_params)

    async def _usage(self, cursor, scope, filters, task=None, window=True):
        where, params = self._scope(scope, "c")
        where, params = self._call_filters(
            where,
            params,
            filters,
            window=window,
        )
        if task:
            where += " AND c.task_run_id=%s"
            params.append(task)
        if filters.root_agent_id:
            where += " AND EXISTS (SELECT 1 FROM swe_token_runs root WHERE root.source_id=c.source_id AND root.tenant_id=c.tenant_id AND root.task_run_id=c.task_run_id AND root.parent_branch_run_id IS NULL AND root.agent_id=%s)"
            params.append(filters.root_agent_id)
        rows = await self._rows(
            cursor,
            f"SELECT {USAGE_SQL} FROM swe_token_model_calls c WHERE {where}",
            params,
        )
        return usage_dto(rows[0])

    async def _page(
        self,
        cursor,
        query,
        params,
        scope,
        filters,
        resource,
        time_expr,
        id_expr,
        *,
        position=None,
        ascending=False,
        numeric_id=False,
    ):
        position = (
            position
            if position is not None
            else validate_cursor(scope, filters, resource)
        )
        comparison = ">" if ascending else "<"
        if position:
            identity = position["sort_id"]
            if numeric_id:
                try:
                    identity = int(identity)
                except ValueError as error:
                    raise ValueError("invalid_cursor") from error
            if numeric_id:
                query += f" AND {id_expr}{comparison}%s"
                params = (*params, identity)
            else:
                query += f" AND ({time_expr}{comparison}%s OR ({time_expr}=%s AND {id_expr}{comparison}%s))"
                params = (
                    *params,
                    position["sort_time"],
                    position["sort_time"],
                    identity,
                )
        direction = "ASC" if ascending else "DESC"
        query += (
            f" ORDER BY {time_expr} {direction},{id_expr} {direction} LIMIT %s"
        )
        rows = await self._rows(cursor, query, (*params, filters.limit + 1))
        more, rows = len(rows) > filters.limit, rows[: filters.limit]
        next_cursor = None
        if more and rows:
            last = rows[-1]
            next_cursor = encode_cursor(
                scope,
                filters,
                resource,
                last.pop("sort_time"),
                last.pop("sort_id"),
            )
        for row in rows:
            row.pop("sort_time", None)
            row.pop("sort_id", None)
        return {"items": rows, "next_cursor": next_cursor}

    async def dashboard(self, scope, filters=None):
        scope, filters, _, metadata = self._request(
            scope,
            filters,
            "dashboard",
        )
        async with self.store.transaction() as cursor:
            usage = await self._usage(cursor, scope, filters)
            root_where, root_params = self._root_filters(scope, filters)
            summary, params = self._task_summary_sql(
                scope,
                f"AND r.task_run_id IN (SELECT r.task_run_id FROM swe_token_runs r WHERE {root_where})",
                root_params,
            )
            complete = "unknown_expected_count=0 AND gap_tracking_limited=0 AND gap_count=0 AND missing_child_count=0 AND nonterminal_branch_count=0 AND nonterminal_call_count=0 AND expected_attempt_count=observed_call_count AND reported_call_count=observed_call_count"
            rows = await self._rows(
                cursor,
                f"SELECT COUNT(task_run_id) AS task_count,COALESCE(SUM(CASE WHEN {complete} THEN 1 ELSE 0 END),0) AS sample_count,AVG(CASE WHEN {complete} THEN known_input_tokens+known_output_tokens ELSE NULL END) AS mean_total_tokens,MIN(CASE WHEN {complete} THEN known_input_tokens+known_output_tokens ELSE NULL END) AS min_total_tokens,MAX(CASE WHEN {complete} THEN known_input_tokens+known_output_tokens ELSE NULL END) AS max_total_tokens FROM ({summary}) task_metrics",
                params,
            )
            rows[0]["quantiles"] = await self._quantiles(
                cursor,
                summary,
                params,
                complete,
                rows[0]["sample_count"],
            )
            attribution = await self._agent_attribution(cursor, scope, filters)
            coverage = await self._rule_coverage(cursor, scope, filters)
            prefix = await self._prefix_aggregate(cursor, scope, filters)
        distribution = rows[0]
        distribution["population"] = (
            "terminal tasks with complete lifecycle Usage"
        )
        return {
            **metadata,
            "window_attempt_usage": usage,
            "lifecycle_distribution": distribution,
            "projection_filters": {
                "window_attempt_usage": [
                    "start",
                    "end",
                    "agent_id",
                    "root_agent_id",
                    "model_name",
                    "branch_run_id",
                    "state",
                ],
                "agent_attribution": [
                    "start",
                    "end",
                    "agent_id",
                    "root_agent_id",
                    "model_name",
                    "branch_run_id",
                    "state",
                ],
                "prefix": [
                    "start",
                    "end",
                    "agent_id",
                    "root_agent_id",
                    "model_name",
                    "branch_run_id",
                    "state",
                ],
                "rule_coverage": [
                    "start",
                    "end",
                    "agent_id",
                    "root_agent_id",
                    "model_name",
                    "branch_run_id",
                    "rule_id",
                    "state",
                ],
            },
            "agent_attribution": attribution,
            "rule_coverage": coverage,
            "prefix": prefix,
            "limits": {
                "available": False,
                "reason_code": "deployment_limits_unavailable",
            },
        }

    async def _quantiles(
        self,
        cursor,
        summary,
        params,
        complete,
        sample_count,
    ):
        output = {
            "sample_count": sample_count,
            "p50": None,
            "p90": None,
            "p95": None,
            "method": "nearest_rank",
            "available": bool(sample_count),
            "reason_code": None if sample_count else "no_eligible_tasks",
        }
        if sample_count:
            for percentile in (50, 90, 95):
                rank = (int(sample_count) * percentile + 99) // 100
                rows = await self._rows(
                    cursor,
                    f"SELECT known_input_tokens+known_output_tokens AS total_tokens FROM ({summary}) eligible_tasks WHERE {complete} ORDER BY known_input_tokens+known_output_tokens,task_run_id LIMIT 1 OFFSET %s",
                    (*params, rank - 1),
                )
                output[f"p{percentile}"] = rows[0]["total_tokens"]
        return output

    async def _agent_attribution(self, cursor, scope, filters):
        where, params = self._scope(scope, "c")
        where, params = self._call_filters(where, params, filters)
        if filters.root_agent_id:
            where += " AND root.agent_id=%s"
            params.append(filters.root_agent_id)
        joins = "LEFT JOIN swe_token_runs root ON root.task_run_id=c.task_run_id AND root.source_id=c.source_id AND root.tenant_id=c.tenant_id AND root.parent_branch_run_id IS NULL"
        result = {}
        for key, expression in (
            ("executing_agents", "c.agent_id"),
            ("root_agents", "root.agent_id"),
        ):
            rows = await self._rows(
                cursor,
                f"SELECT {expression} AS attributed_agent_id,{USAGE_SQL} FROM swe_token_model_calls c {joins} WHERE {where} GROUP BY {expression} ORDER BY SUM(COALESCE(c.input_tokens,0)+COALESCE(c.output_tokens,0)) DESC,{expression} LIMIT 101",
                params,
            )
            result[key] = {
                "items": [
                    {
                        "agent_id": row["attributed_agent_id"],
                        "usage": usage_dto(row),
                    }
                    for row in rows[:100]
                ],
                "limited": len(rows) > 100,
                "population": "window attempts grouped by agent",
            }
        return result

    async def _rule_coverage(self, cursor, scope, filters):
        root_where, root_params = self._root_filters(scope, filters)
        where, params = self._scope(scope, "r")
        where += f" AND r.task_run_id IN (SELECT r.task_run_id FROM swe_token_runs r WHERE {root_where})"
        params.extend(root_params)
        if filters.branch_run_id:
            where += " AND r.branch_run_id=%s"
            params.append(filters.branch_run_id)
        output = []
        for rule in RULE_IDS:
            rows = await self._rows(
                cursor,
                f"""SELECT COUNT(DISTINCT r.branch_run_id) AS branch_count,
                COUNT(e.evaluation_id) AS evaluation_count,
                COALESCE(SUM(CASE WHEN e.state='candidate' THEN 1 ELSE 0 END),0) AS candidate,
                COALESCE(SUM(CASE WHEN e.state='not_hit' THEN 1 ELSE 0 END),0) AS not_hit,
                COALESCE(SUM(CASE WHEN e.state='insufficient_evidence' THEN 1 ELSE 0 END),0) AS insufficient_evidence,
                COALESCE(SUM(CASE WHEN e.state='not_applicable' THEN 1 ELSE 0 END),0) AS not_applicable,
                COUNT(DISTINCT CASE WHEN e.evaluation_id IS NULL AND r.analysis_state<>'failed' THEN r.branch_run_id ELSE NULL END) AS pending,
                COUNT(DISTINCT CASE WHEN e.evaluation_id IS NULL AND r.analysis_state='failed' THEN r.branch_run_id ELSE NULL END) AS failed
                FROM swe_token_runs r LEFT JOIN swe_token_diagnostic_heads h
                  ON h.branch_run_id=r.branch_run_id AND h.source_id=r.source_id AND h.tenant_id=r.tenant_id AND h.input_revision=r.data_revision AND h.rule_id=%s
                LEFT JOIN swe_token_diagnostic_evaluations e ON e.evaluation_id=h.evaluation_id AND e.source_id=h.source_id AND e.tenant_id=h.tenant_id AND e.input_revision=r.data_revision
                WHERE {where}""",
                (rule, *params),
            )
            output.append(
                {
                    "rule_id": rule,
                    **rows[0],
                    "population": "current evaluation objects; pending/failed count branches without a current head",
                },
            )
        return output

    async def _prefix_aggregate(self, cursor, scope, filters):
        where, params = self._scope(scope, "c")
        where, params = self._call_filters(where, params, filters)
        if filters.root_agent_id:
            where += " AND EXISTS (SELECT 1 FROM swe_token_runs root WHERE root.source_id=c.source_id AND root.tenant_id=c.tenant_id AND root.task_run_id=c.task_run_id AND root.parent_branch_run_id IS NULL AND root.agent_id=%s)"
            params.append(filters.root_agent_id)
        # The CASE order matches aggregate_prefix.exclusion_reason; each window
        # attempt receives exactly one participation or exclusion category.
        inner = f"""SELECT p.lower_text_bytes,ctx.text_bytes,
        CASE WHEN p.comparison_id IS NULL THEN CASE WHEN r.analysis_state='failed' THEN 'failed' WHEN r.analysis_state IN ('ready','insufficient_evidence') AND r.analyzed_revision=r.data_revision THEN 'missing_evidence' ELSE 'pending' END
          WHEN p.analysis_state IN ('pending','running') THEN 'pending'
          WHEN p.analysis_state IN ('failed','unavailable') THEN 'failed'
          WHEN p.candidate_limited=1 THEN 'candidate_limit'
          WHEN ctx.capture_state IS NULL OR ctx.capture_state<>'complete' THEN CASE WHEN ctx.capture_reason='media' THEN 'media' ELSE 'partial_context' END
          WHEN p.candidate_count IS NULL OR p.candidate_count=0 THEN 'no_history'
          WHEN p.analysis_state<>'ready' OR p.lower_text_bytes IS NULL THEN 'missing_evidence'
          WHEN ctx.text_bytes IS NULL OR ctx.text_bytes=0 THEN 'empty_text'
          WHEN p.lower_text_bytes<0 OR p.lower_text_bytes>ctx.text_bytes THEN 'missing_evidence'
          ELSE 'participating' END AS reason
        FROM swe_token_model_calls c
        JOIN swe_token_runs r ON r.branch_run_id=c.branch_run_id AND r.source_id=c.source_id AND r.tenant_id=c.tenant_id
        LEFT JOIN swe_token_contexts ctx ON ctx.call_id=c.call_id AND ctx.source_id=c.source_id AND ctx.tenant_id=c.tenant_id AND ctx.context_role='prepared'
        LEFT JOIN swe_token_prefix_comparisons p ON p.current_context_id=ctx.context_id AND p.current_context_revision=ctx.context_revision AND p.source_id=ctx.source_id AND p.tenant_id=ctx.tenant_id AND p.input_revision=r.data_revision
          AND NOT EXISTS (SELECT 1 FROM swe_token_prefix_comparisons newer WHERE newer.current_context_id=p.current_context_id AND newer.current_context_revision=p.current_context_revision AND newer.source_id=p.source_id AND newer.tenant_id=p.tenant_id AND newer.input_revision=p.input_revision AND (newer.created_at>p.created_at OR (newer.created_at=p.created_at AND newer.comparison_id>p.comparison_id)))
        WHERE {where}"""
        rows = await self._rows(
            cursor,
            f"SELECT reason,COUNT(reason) AS request_count,COALESCE(SUM(CASE WHEN reason='participating' THEN lower_text_bytes ELSE 0 END),0) AS matched_text_bytes,COALESCE(SUM(CASE WHEN reason='participating' THEN text_bytes ELSE 0 END),0) AS eligible_text_bytes FROM ({inner}) prefix_metrics GROUP BY reason",
            params,
        )
        excluded = {
            reason: 0
            for reason in (
                "pending",
                "failed",
                "candidate_limit",
                "media",
                "partial_context",
                "missing_evidence",
                "no_history",
                "empty_text",
            )
        }
        output = {
            "request_count": 0,
            "matched_text_bytes": 0,
            "eligible_text_bytes": 0,
            "excluded": excluded,
            "population": "window attempts with a current saved comparison",
            "limited": False,
        }
        for row in rows:
            if row["reason"] == "participating":
                for key in (
                    "request_count",
                    "matched_text_bytes",
                    "eligible_text_bytes",
                ):
                    output[key] = int(row[key])
            else:
                excluded[row["reason"]] = int(row["request_count"])
        output["ratio"] = (
            output["matched_text_bytes"] / output["eligible_text_bytes"]
            if output["eligible_text_bytes"]
            else None
        )
        return output

    async def tasks(self, scope, filters=None):
        scope, filters, position, metadata = self._request(
            scope,
            filters,
            "tasks",
        )
        where, params = self._root_filters(scope, filters)
        query = f"SELECT {columns('swe_token_runs', 'r')},r.started_at AS sort_time,r.task_run_id AS sort_id FROM swe_token_runs r WHERE {where}"
        async with self.store.transaction() as cursor:
            page = await self._page(
                cursor,
                query,
                params,
                scope,
                filters,
                "tasks",
                "r.started_at",
                "r.task_run_id",
                position=position,
            )
            if page["items"]:
                marks = ",".join(["%s"] * len(page["items"]))
                summary, bindings = self._task_summary_sql(
                    scope,
                    f"AND r.task_run_id IN ({marks})",
                    [r["task_run_id"] for r in page["items"]],
                )
                totals = {
                    row["task_run_id"]: row
                    for row in await self._rows(cursor, summary, bindings)
                }
                for row in page["items"]:
                    total = totals[row["task_run_id"]]
                    row["completeness"] = completeness_dto(total)
                    row["lifecycle_usage"] = usage_dto(
                        total,
                        row["completeness"]["usage_complete"],
                    )
                    row["window_attempt_usage"] = await self._usage(
                        cursor,
                        scope,
                        filters,
                        row["task_run_id"],
                    )
                    row["selected_contribution"] = await self._usage(
                        cursor,
                        scope,
                        filters,
                        row["task_run_id"],
                        window=False,
                    )
            page["items"] = [public_row(row) for row in page["items"]]
        return {**metadata, **page}

    async def _root(self, cursor, identity, scope, filters):
        where, params = self._root_filters(scope, filters, window=False)
        rows = await self._rows(
            cursor,
            f"SELECT {columns('swe_token_runs', 'r')} FROM swe_token_runs r WHERE {where} AND r.task_run_id=%s LIMIT 1",
            (*params, identity),
        )
        return public_row(rows[0]) if rows else None

    @staticmethod
    def _branch_query(scope, identity):
        return (
            f"SELECT {columns('swe_token_runs', 'r')},(SELECT COUNT(h.evaluation_id) FROM swe_token_diagnostic_heads h WHERE h.source_id=r.source_id AND h.tenant_id=r.tenant_id AND h.branch_run_id=r.branch_run_id) AS head_count,(SELECT MIN(h.input_revision) FROM swe_token_diagnostic_heads h WHERE h.source_id=r.source_id AND h.tenant_id=r.tenant_id AND h.branch_run_id=r.branch_run_id) AS current_head_revision,r.started_at AS sort_time,r.branch_run_id AS sort_id FROM swe_token_runs r WHERE r.source_id=%s AND r.tenant_id=%s AND r.task_run_id=%s",
            (*scope.pair(), identity),
        )

    async def task(self, identity, scope, filters=None) -> TaskDTO | None:
        validate_identity(identity)
        scope, filters, _, metadata = self._request(
            scope,
            filters,
            "task:" + identity,
        )
        async with self.store.transaction() as cursor:
            root = await self._root(cursor, identity, scope, filters)
            if root is None:
                return None
            query, params = self._task_summary_sql(
                scope,
                "AND r.task_run_id=%s",
                (identity,),
            )
            summary = (await self._rows(cursor, query, params))[0]
            completeness = completeness_dto(summary)
            query, params = self._branch_query(scope, identity)
            branches = await self._rows(
                cursor,
                query + " ORDER BY r.started_at,r.branch_run_id LIMIT 101",
                params,
            )
            for branch in branches:
                branch.pop("sort_time", None)
                branch.pop("sort_id", None)
            selected = await self._usage(
                cursor,
                scope,
                filters,
                identity,
                window=False,
            )
            window = await self._usage(cursor, scope, filters, identity)
            await self._evaluation_locators(
                cursor,
                branches[:100],
                scope,
                "branch",
            )
        response = {
            **metadata,
            "task_run_id": identity,
            "root": root,
            "branches": [branch_dto(row) for row in branches[:100]],
            "branches_limited": len(branches) > 100,
            "lifecycle_usage": usage_dto(
                summary,
                completeness["usage_complete"],
            ),
            "selected_contribution": selected,
            "window_attempt_usage": window,
            "completeness": completeness,
        }
        return cast(TaskDTO, response)

    async def branches(self, identity, scope, filters=None):
        validate_identity(identity)
        resource = "branches:" + identity
        scope, filters, position, metadata = self._request(
            scope,
            filters,
            resource,
        )
        async with self.store.transaction() as cursor:
            if await self._root(cursor, identity, scope, filters) is None:
                return None
            query, params = self._branch_query(scope, identity)
            if filters.branch_run_id:
                query += " AND r.branch_run_id=%s"
                params += (filters.branch_run_id,)
            page = await self._page(
                cursor,
                query,
                params,
                scope,
                filters,
                resource,
                "r.started_at",
                "r.branch_run_id",
                position=position,
            )
            page["items"] = [branch_dto(row) for row in page["items"]]
            for branch in page["items"]:
                branch["steps"] = await self._business_steps(
                    cursor,
                    branch["branch_run_id"],
                    scope,
                )
            await self._evaluation_locators(
                cursor,
                page["items"],
                scope,
                "branch",
            )
        return {**metadata, **page}

    async def calls(self, identity, scope, filters=None):
        validate_identity(identity)
        resource = "calls:" + identity
        scope, filters, position, metadata = self._request(
            scope,
            filters,
            resource,
        )
        async with self.store.transaction() as cursor:
            if await self._root(cursor, identity, scope, filters) is None:
                return None
            where, params = self._scope(scope, "c")
            where, params = self._call_filters(where, params, filters)
            query = f"SELECT {columns('swe_token_model_calls', 'c')},c.started_at AS sort_time,c.call_id AS sort_id FROM swe_token_model_calls c WHERE {where} AND c.task_run_id=%s"
            params.append(identity)
            if position:
                anchor = await self._object(
                    cursor,
                    "swe_token_model_calls",
                    "call_id",
                    position["sort_id"],
                    scope,
                )
                if not anchor or anchor["task_run_id"] != identity:
                    raise ValueError("invalid_cursor")
                query += " AND (c.branch_run_id,c.logical_seq,c.attempt_no,c.call_id)>(%s,%s,%s,%s)"
                params.extend(
                    anchor[key]
                    for key in (
                        "branch_run_id",
                        "logical_seq",
                        "attempt_no",
                        "call_id",
                    )
                )
            rows = await self._rows(
                cursor,
                query
                + " ORDER BY c.branch_run_id,c.logical_seq,c.attempt_no,c.call_id LIMIT %s",
                (*params, filters.limit + 1),
            )
            more, rows = len(rows) > filters.limit, rows[: filters.limit]
            next_cursor = (
                encode_cursor(
                    scope,
                    filters,
                    resource,
                    rows[-1]["started_at"],
                    rows[-1]["call_id"],
                )
                if more and rows
                else None
            )
            page = {
                "items": [
                    public_row(
                        {
                            key: value
                            for key, value in row.items()
                            if key not in {"sort_time", "sort_id"}
                        },
                    )
                    for row in rows
                ],
                "next_cursor": next_cursor,
            }
            await self._evaluation_locators(
                cursor,
                page["items"],
                scope,
                "call",
            )
        return {**metadata, **page}

    async def _evaluation_locators(self, cursor, rows, scope, object_type):
        targets = {}
        identity_key = "call_id" if object_type == "call" else "branch_run_id"
        for row in rows:
            row["evaluations"] = {"items": [], "limited": False}
            targets[(row["branch_run_id"], object_type, row[identity_key])] = (
                row["evaluations"]
            )
            if object_type == "branch":
                for step in row.get("steps", {}).get("items", []):
                    step["evaluations"] = {"items": [], "limited": False}
                    targets[
                        (row["branch_run_id"], "step", step["step_id"])
                    ] = step["evaluations"]
        if not targets:
            return
        marks = ",".join(["(%s,%s,%s)"] * len(targets))
        rules = ",".join(["%s"] * len(RULE_IDS))
        params = (
            *scope.pair(),
            *(value for key in targets for value in key),
            *RULE_IDS,
        )
        # The head PK and the eight allowlisted rules bound each object to eight
        # locators. The page-wide guard also bounds pages with many real Steps.
        locators = await self._rows(
            cursor,
            f"""SELECT e.evaluation_id,e.task_run_id,e.branch_run_id,e.object_type,e.object_id,e.rule_id,e.state,e.input_revision,e.snapshot_id,e.rule_version,e.threshold_version,
            h.input_revision AS head_input_revision,h.snapshot_id AS head_snapshot_id,h.rule_version AS head_rule_version,h.threshold_version AS head_threshold_version,r.data_revision AS current_data_revision,r.analysis_state
            FROM swe_token_diagnostic_heads h
            JOIN swe_token_diagnostic_evaluations e ON e.evaluation_id=h.evaluation_id AND e.source_id=h.source_id AND e.tenant_id=h.tenant_id AND e.branch_run_id=h.branch_run_id AND e.task_run_id=h.task_run_id AND e.object_type=h.object_type AND e.object_id=h.object_id AND e.rule_id=h.rule_id
            JOIN swe_token_runs r ON r.branch_run_id=h.branch_run_id AND r.source_id=h.source_id AND r.tenant_id=h.tenant_id AND r.task_run_id=h.task_run_id
            WHERE h.source_id=%s AND h.tenant_id=%s AND (h.branch_run_id,h.object_type,h.object_id) IN ({marks}) AND h.rule_id IN ({rules})
            ORDER BY h.branch_run_id,h.object_type,h.object_id,e.rule_id,e.evaluation_id LIMIT 10001""",
            params,
        )
        if len(locators) > 10000:
            for target in targets.values():
                target["limited"] = True
        for locator in locators[:10000]:
            current_fields = (
                "input_revision",
                "snapshot_id",
                "rule_version",
                "threshold_version",
            )
            saved_fields = tuple(locator[key] for key in current_fields)
            head_fields = tuple(
                locator.pop("head_" + key) for key in current_fields
            )
            current = (
                saved_fields == head_fields
                and locator["input_revision"]
                == locator["current_data_revision"]
            )
            locator["freshness"] = "current" if current else "stale"
            locator["freshness_reason"] = (
                "analysis_failed"
                if locator["analysis_state"] == "failed"
                else None if current else "revision_or_snapshot_changed"
            )
            targets[
                (
                    locator["branch_run_id"],
                    locator["object_type"],
                    locator["object_id"],
                )
            ]["items"].append(locator)

    async def _matches_object(self, cursor, row, scope, filters):
        if row is None:
            return False
        for key in ("agent_id", "model_name", "branch_run_id"):
            selected = getattr(filters, key)
            if selected is not None and key in row and row[key] != selected:
                return False
        if filters.root_agent_id:
            root = await self._root(cursor, row["task_run_id"], scope, filters)
            if root is None:
                return False
        return True

    async def _business_steps(self, cursor, identity, scope):
        rows = await self._rows(
            cursor,
            "SELECT step_id,MIN(started_at) AS started_at,MAX(ended_at) AS ended_at,COUNT(event_id) AS event_count FROM swe_token_execution_events WHERE source_id=%s AND tenant_id=%s AND branch_run_id=%s AND identity_source='business' AND step_id IS NOT NULL GROUP BY step_id ORDER BY MIN(started_at),step_id LIMIT 101",
            (*scope.pair(), identity),
        )
        return {
            "items": rows[:100],
            "limited": len(rows) > 100,
            "order_known": False,
            "reason_code": "step_order_not_recorded",
        }

    async def _object(self, cursor, table, column, identity, scope):
        rows = await self._rows(
            cursor,
            f"SELECT {columns(table)} FROM {table} WHERE {column}=%s AND source_id=%s AND tenant_id=%s LIMIT 1",
            (identity, *scope.pair()),
        )
        return rows[0] if rows else None

    async def _relations(
        self,
        cursor,
        table,
        column,
        identity,
        scope,
        filters,
        resource,
        index,
        position=None,
    ):
        epoch = "'1970-01-01 00:00:00'"
        query = f"SELECT {columns(table)},{epoch} AS sort_time,{index} AS sort_id FROM {table} WHERE {column}=%s AND source_id=%s AND tenant_id=%s"
        page = await self._page(
            cursor,
            query,
            (identity, *scope.pair()),
            scope,
            filters,
            resource,
            epoch,
            index,
            position=position,
            ascending=True,
            numeric_id=True,
        )
        page["items"] = [public_row(row) for row in page["items"]]
        return page

    async def context(
        self,
        identity,
        scope,
        filters=None,
    ) -> ContextDTO | None:
        validate_identity(identity)
        resource = "context:" + identity
        scope, filters, position, metadata = self._request(
            scope,
            filters,
            resource,
        )
        async with self.store.transaction() as cursor:
            context = await self._object(
                cursor,
                "swe_token_contexts",
                "context_id",
                identity,
                scope,
            )
            if not await self._matches_object(cursor, context, scope, filters):
                return None
            page = await self._relations(
                cursor,
                "swe_token_context_occurrences",
                "context_id",
                identity,
                scope,
                filters,
                resource,
                "occurrence_index",
                position,
            )
            categories = await self._rows(
                cursor,
                "SELECT category,COUNT(occurrence_index) AS occurrence_count,COALESCE(SUM(estimated_tokens),0) AS known_estimated_tokens,SUM(CASE WHEN estimated_tokens IS NULL THEN 1 ELSE 0 END) AS unknown_estimate_count,SUM(CASE WHEN category IS NULL OR category='unclassified' THEN 1 ELSE 0 END) AS unknown_category_count,SUM(CASE WHEN attribution IS NULL OR attribution NOT IN ('explicit','structure') THEN 1 ELSE 0 END) AS unknown_attribution_count,CASE WHEN SUM(CASE WHEN estimated_tokens IS NULL THEN 1 ELSE 0 END)=0 THEN SUM(estimated_tokens) ELSE NULL END AS estimated_tokens FROM swe_token_context_occurrences WHERE source_id=%s AND tenant_id=%s AND context_id=%s GROUP BY category ORDER BY category",
                (*scope.pair(), identity),
            )
            count = sum(row["occurrence_count"] for row in categories)
            known = sum(row["known_estimated_tokens"] for row in categories)
            complete = (
                context["capture_state"] == "complete"
                and context["expected_item_count"] == count
                and all(
                    row["unknown_estimate_count"] == 0
                    and row["unknown_category_count"] == 0
                    and row["unknown_attribution_count"] == 0
                    for row in categories
                )
            )
            composition = {
                "unit": "estimated_tokens",
                "categories": categories,
                "occurrence_count": count,
                "known_estimated_tokens": known,
                "estimated_tokens": known if complete else None,
                "complete": complete,
                "unknown_category_count": sum(
                    row["unknown_category_count"] for row in categories
                ),
                "unknown_attribution_count": sum(
                    row["unknown_attribution_count"] for row in categories
                ),
                "reason_code": None if complete else "untrusted_composition",
                "population": "all saved context occurrences",
            }
        response = {
            **metadata,
            "context": public_row(context),
            "occurrences": page,
            "composition": composition,
        }
        return cast(ContextDTO, response)

    async def prefix(self, identity, scope, filters=None):
        validate_identity(identity)
        resource = "prefix:" + identity
        scope, filters, position, metadata = self._request(
            scope,
            filters,
            resource,
        )
        async with self.store.transaction() as cursor:
            call = await self._object(
                cursor,
                "swe_token_model_calls",
                "call_id",
                identity,
                scope,
            )
            if not await self._matches_object(cursor, call, scope, filters):
                return None
            rows = await self._rows(
                cursor,
                f"SELECT {columns('swe_token_prefix_comparisons', 'p')} FROM swe_token_prefix_comparisons p WHERE p.source_id=%s AND p.tenant_id=%s AND p.call_id=%s ORDER BY p.created_at DESC,p.comparison_id DESC LIMIT 1",
                (*scope.pair(), identity),
            )
            if not rows:
                if position:
                    raise ValueError("cursor_snapshot_changed")
                branch = await self._object(
                    cursor,
                    "swe_token_runs",
                    "branch_run_id",
                    call["branch_run_id"],
                    scope,
                )
                reason = "analysis_pending"
                if branch and branch["analysis_state"] == "failed":
                    reason = "analysis_failed"
                elif (
                    branch
                    and branch["analysis_state"]
                    in {"ready", "insufficient_evidence"}
                    and branch["analyzed_revision"] == branch["data_revision"]
                ):
                    reason = "missing_evidence"
                return {
                    **metadata,
                    "call_id": identity,
                    "comparison": None,
                    "candidates": {"items": [], "next_cursor": None},
                    "is_current": False,
                    "reason_code": reason,
                }
            comparison = rows[0]
            if position:
                saved_comparison, index = position["sort_id"].rsplit(":", 1)
                if saved_comparison != comparison["comparison_id"]:
                    raise ValueError("cursor_snapshot_changed")
                position = {**position, "sort_id": index}
            context = await self._object(
                cursor,
                "swe_token_contexts",
                "context_id",
                comparison["current_context_id"],
                scope,
            )
            branch = await self._object(
                cursor,
                "swe_token_runs",
                "branch_run_id",
                comparison["branch_run_id"],
                scope,
            )
            is_current = bool(
                context
                and branch
                and context["context_revision"]
                == comparison["current_context_revision"]
                and branch["data_revision"] == comparison["input_revision"],
            )
            page = await self._relations(
                cursor,
                "swe_token_prefix_candidates",
                "comparison_id",
                comparison["comparison_id"],
                scope,
                filters,
                resource,
                "candidate_index",
                position,
            )
            if page["next_cursor"]:
                page["next_cursor"] = encode_cursor(
                    scope,
                    filters,
                    resource,
                    datetime(1970, 1, 1, tzinfo=timezone.utc),
                    f"{comparison['comparison_id']}:{page['items'][-1]['candidate_index']}",
                )
            for candidate in page["items"]:
                historical = await self._object(
                    cursor,
                    "swe_token_contexts",
                    "context_id",
                    candidate["historical_context_id"],
                    scope,
                )
                candidate["evidence_expired"] = (
                    not historical
                    or historical["context_revision"]
                    != candidate["historical_context_revision"]
                )
        return {
            **metadata,
            "call_id": identity,
            "comparison": public_row(comparison),
            "candidates": page,
            "is_current": is_current,
            "evidence_expired": any(
                row["evidence_expired"] for row in page["items"]
            ),
            "reason_code": (
                comparison["reason_code"] if is_current else "stale_analysis"
            ),
        }

    @staticmethod
    def _operation_comparison(operation, before, after):
        reason = None
        if operation["operation_state"] not in {"completed", "success"}:
            reason = "operation_not_successful"
        elif (
            operation["capture_state"] != "complete"
            or operation["missing_before"]
            or operation["missing_after"]
            or not before
            or not after
        ):
            reason = "missing_context_pair"
        elif any(
            row["task_run_id"] != operation["task_run_id"]
            or row["branch_run_id"] != operation["branch_run_id"]
            for row in (before, after)
        ):
            reason = "incompatible_context_pair"
        elif (
            before["capture_state"] != "complete"
            or after["capture_state"] != "complete"
        ):
            reason = "partial_context_pair"
        elif (
            not before["representation_version"]
            or before["representation_version"]
            != after["representation_version"]
        ):
            reason = "incompatible_representation"
        elif (
            not before["estimator_version"]
            or before["estimator_version"] != after["estimator_version"]
        ):
            reason = "incompatible_estimator"
        elif (
            before["estimated_tokens"] is None
            or after["estimated_tokens"] is None
        ):
            reason = "missing_estimated_tokens"
        return {
            "unit": "estimated_tokens",
            "before_estimated_tokens": (
                before["estimated_tokens"] if not reason else None
            ),
            "after_estimated_tokens": (
                after["estimated_tokens"] if not reason else None
            ),
            "saved_estimated_tokens": (
                before["estimated_tokens"] - after["estimated_tokens"]
                if not reason
                else None
            ),
            "reason_code": reason,
        }

    async def operations(
        self,
        identity,
        scope,
        filters=None,
        *,
        operation_id=None,
        relation_cursor=None,
    ):
        validate_identity(identity)
        if operation_id:
            validate_identity(operation_id)
        resource = "operations:" + identity
        if operation_id:
            resource += ":operation:" + operation_id
        scope, filters, position, metadata = self._request(
            scope,
            filters,
            resource,
        )
        relation_filters = replace(filters, cursor=None)
        if relation_cursor:
            if not operation_id:
                raise ValueError("operation_id_required_for_relation_cursor")
            validate_cursor(
                scope,
                relation_filters,
                "segments:" + operation_id,
                relation_cursor,
            )
        async with self.store.transaction() as cursor:
            if await self._root(cursor, identity, scope, filters) is None:
                return None
            where, params = self._scope(scope, "o")
            where += " AND o.task_run_id=%s"
            params.append(identity)
            if operation_id:
                where += " AND o.operation_id=%s"
                params.append(operation_id)
            if filters.branch_run_id:
                where += " AND o.branch_run_id=%s"
                params.append(filters.branch_run_id)
            query = f"SELECT {columns('swe_token_context_operations', 'o')},COALESCE(o.started_at,o.ended_at,'1970-01-01 00:00:00') AS sort_time,o.operation_id AS sort_id FROM swe_token_context_operations o WHERE {where}"
            page = await self._page(
                cursor,
                query,
                params,
                scope,
                filters,
                resource,
                "COALESCE(o.started_at,o.ended_at,'1970-01-01 00:00:00')",
                "o.operation_id",
                position=position,
            )
            for operation in page["items"]:
                before = await self._object(
                    cursor,
                    "swe_token_contexts",
                    "context_id",
                    operation["before_context_id"],
                    scope,
                )
                after = await self._object(
                    cursor,
                    "swe_token_contexts",
                    "context_id",
                    operation["after_context_id"],
                    scope,
                )
                operation["comparison"] = self._operation_comparison(
                    operation,
                    before,
                    after,
                )
                operation["before_context"] = (
                    public_row(before)
                    if before
                    and before["task_run_id"] == identity
                    and before["branch_run_id"] == operation["branch_run_id"]
                    else None
                )
                operation["after_context"] = (
                    public_row(after)
                    if after
                    and after["task_run_id"] == identity
                    and after["branch_run_id"] == operation["branch_run_id"]
                    else None
                )
                summary_call = await self._object(
                    cursor,
                    "swe_token_model_calls",
                    "call_id",
                    operation["summary_call_id"],
                    scope,
                )
                operation["evidence_expired"] = {
                    "before_context": bool(
                        operation["before_context_id"]
                        and before is None
                        and not operation["missing_before"],
                    ),
                    "after_context": bool(
                        operation["after_context_id"]
                        and after is None
                        and not operation["missing_after"],
                    ),
                    "summary_call": bool(
                        operation["summary_call_id"] and summary_call is None,
                    ),
                }
                operation["summary_call"] = (
                    public_row(summary_call)
                    if summary_call and summary_call["task_run_id"] == identity
                    else None
                )
                operation["segments"] = await self._relations(
                    cursor,
                    "swe_token_operation_segments",
                    "operation_id",
                    operation["operation_id"],
                    scope,
                    replace(relation_filters, cursor=relation_cursor),
                    "segments:" + operation["operation_id"],
                    "relation_index",
                )
        return {**metadata, **page}

    @staticmethod
    def _current_joins():
        return """FROM swe_token_diagnostic_findings f
        JOIN swe_token_diagnostic_evaluations e ON e.evaluation_id=f.evaluation_id AND e.source_id=f.source_id AND e.tenant_id=f.tenant_id
        JOIN swe_token_diagnostic_heads h ON h.evaluation_id=e.evaluation_id AND h.source_id=e.source_id AND h.tenant_id=e.tenant_id AND h.branch_run_id=e.branch_run_id
        JOIN swe_token_runs r ON r.branch_run_id=h.branch_run_id AND r.source_id=h.source_id AND r.tenant_id=h.tenant_id AND r.task_run_id=h.task_run_id AND r.data_revision=h.input_revision AND e.input_revision=r.data_revision"""

    def _finding_filters(self, scope, filters):
        where, params = self._scope(scope, "f")
        where += (
            " AND e.state='candidate' AND f.created_at>=%s AND f.created_at<%s"
        )
        params.extend((sql_time(filters.start), sql_time(filters.end)))
        for key in ("agent_id", "model_name", "rule_id", "branch_run_id"):
            value = getattr(filters, key)
            if value:
                where += f" AND f.{key}=%s"
                params.append(value)
        if filters.state:
            where += " AND e.state=%s"
            params.append(filters.state)
        if filters.root_agent_id:
            where += " AND EXISTS (SELECT 1 FROM swe_token_runs root WHERE root.source_id=f.source_id AND root.tenant_id=f.tenant_id AND root.task_run_id=f.task_run_id AND root.parent_branch_run_id IS NULL AND root.agent_id=%s)"
            params.append(filters.root_agent_id)
        return where, params

    async def issues(self, scope, filters=None):
        scope, filters, position, metadata = self._request(
            scope,
            filters,
            "issues",
        )
        where, params = self._finding_filters(scope, filters)
        reliable = "CASE WHEN f.reliable_grouping=1 AND f.signature IS NOT NULL THEN 1 ELSE 0 END"
        group = "CASE WHEN f.reliable_grouping=1 AND f.signature IS NOT NULL THEN f.signature ELSE f.finding_id END"
        grouped = f"SELECT {group} AS group_value,{reliable} AS reliable_grouping,MIN(f.rule_id) AS rule_id,MIN(f.recommendation_code) AS recommendation_code,COUNT(f.finding_id) AS hit_count,COUNT(DISTINCT f.task_run_id) AS distinct_task_count,MIN(f.created_at) AS first_seen,MAX(f.created_at) AS latest_seen {self._current_joins()} WHERE {where} GROUP BY {group},{reliable}"
        query = f"SELECT grouped.group_value,grouped.reliable_grouping,grouped.rule_id,grouped.recommendation_code,grouped.hit_count,grouped.distinct_task_count,grouped.first_seen,grouped.latest_seen,grouped.latest_seen AS sort_time,grouped.group_value AS sort_id FROM ({grouped}) grouped WHERE 1=1"
        async with self.store.transaction() as cursor:
            page = await self._page(
                cursor,
                query,
                params,
                scope,
                filters,
                "issues",
                "grouped.latest_seen",
                "grouped.group_value",
                position=position,
            )
        for row in page["items"]:
            row["group_id"] = (
                "s:" if row["reliable_grouping"] else "f:"
            ) + row.pop("group_value")
        return {**metadata, **page}

    async def instances(self, identity, scope, filters=None):
        validate_identity(identity, 128)
        if not identity.startswith(("s:", "f:")):
            raise ValueError("invalid_group_id")
        resource = "instances:" + identity
        scope, filters, position, metadata = self._request(
            scope,
            filters,
            resource,
        )
        where, params = self._finding_filters(scope, filters)
        where += (
            " AND f.signature=%s AND f.reliable_grouping=1"
            if identity.startswith("s:")
            else " AND f.finding_id=%s AND (f.reliable_grouping=0 OR f.signature IS NULL)"
        )
        params.append(identity[2:])
        query = f"SELECT {columns('swe_token_diagnostic_findings', 'f')},e.state AS evaluation_state,f.created_at AS sort_time,f.finding_id AS sort_id {self._current_joins()} WHERE {where}"
        async with self.store.transaction() as cursor:
            exists = await self._rows(
                cursor,
                f"SELECT f.finding_id {self._current_joins()} WHERE {where} LIMIT 1",
                params,
            )
            if not exists:
                return None
            page = await self._page(
                cursor,
                query,
                params,
                scope,
                filters,
                resource,
                "f.created_at",
                "f.finding_id",
                position=position,
            )
        return {**metadata, "group_id": identity, **page}

    async def _reference_expired(self, cursor, ref, scope):
        kind = ref["reference_type"]
        identity = ref["object_id"]
        table, column = {
            "task": ("swe_token_runs", "task_run_id"),
            "branch": ("swe_token_runs", "branch_run_id"),
            "call": ("swe_token_model_calls", "call_id"),
            "context": ("swe_token_contexts", "context_id"),
            "operation": ("swe_token_context_operations", "operation_id"),
        }.get(kind, (None, None))
        if table:
            return (
                await self._object(cursor, table, column, identity, scope)
                is None
            )
        if kind == "occurrence":
            rows = await self._rows(
                cursor,
                "SELECT occurrence_index FROM swe_token_context_occurrences WHERE source_id=%s AND tenant_id=%s AND context_id=%s AND occurrence_index=%s LIMIT 1",
                (*scope.pair(), ref["context_id"], ref["occurrence_index"]),
            )
            return not rows
        if kind == "step":
            if not ref.get("task_run_id") or not ref.get("branch_run_id"):
                return True
            rows = await self._rows(
                cursor,
                "SELECT event_id FROM swe_token_execution_events WHERE source_id=%s AND tenant_id=%s AND task_run_id=%s AND branch_run_id=%s AND step_id=%s AND identity_source='business' LIMIT 1",
                (
                    *scope.pair(),
                    ref["task_run_id"],
                    ref["branch_run_id"],
                    identity,
                ),
            )
            return not rows
        return True

    async def evaluation(
        self,
        identity,
        scope,
        filters=None,
    ) -> EvaluationDTO | None:
        validate_identity(identity)
        resource = "evaluation:" + identity
        scope, filters, _, metadata = self._request(scope, filters, resource)
        async with self.store.transaction() as cursor:
            evaluation = await self._object(
                cursor,
                "swe_token_diagnostic_evaluations",
                "evaluation_id",
                identity,
                scope,
            )
            if evaluation is None:
                return None
            current = await self._rows(
                cursor,
                "SELECT h.evaluation_id FROM swe_token_diagnostic_heads h JOIN swe_token_runs r ON r.branch_run_id=h.branch_run_id AND r.source_id=h.source_id AND r.tenant_id=h.tenant_id AND r.data_revision=h.input_revision WHERE h.source_id=%s AND h.tenant_id=%s AND h.evaluation_id=%s LIMIT 1",
                (*scope.pair(), identity),
            )
            details = {}
            for key, table, order in (
                ("values", "swe_token_diagnostic_values", "value_key"),
                ("gaps", "swe_token_diagnostic_gaps", "reason_code"),
                ("refs", "swe_token_diagnostic_refs", "reference_index"),
                ("findings", "swe_token_diagnostic_findings", "finding_id"),
            ):
                rows = await self._rows(
                    cursor,
                    f"SELECT {columns(table)} FROM {table} WHERE source_id=%s AND tenant_id=%s AND evaluation_id=%s ORDER BY {order} LIMIT 2049",
                    (*scope.pair(), identity),
                )
                details[key] = rows[:2048]
                if len(rows) > 2048:
                    details[key + "_limited"] = True
            for ref in details["refs"]:
                ref["evidence_expired"] = await self._reference_expired(
                    cursor,
                    ref,
                    scope,
                )
        response = {
            **metadata,
            "evaluation": evaluation,
            "is_current": bool(current),
            "recommendation": RECOMMENDATIONS.get(evaluation["rule_id"]),
            **details,
        }
        return cast(EvaluationDTO, response)

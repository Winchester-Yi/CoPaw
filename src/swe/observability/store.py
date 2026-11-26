# -*- coding: utf-8 -*-
"""Independent observability SQL store and schema readiness inspection."""

from __future__ import annotations

import json
from datetime import timezone
from typing import Any

from ..database import DatabaseConnection

REQUIRED_TABLES = frozenset(
    {
        "swe_agentobs_inbox",
        "swe_agentobs_events",
        "swe_agentobs_runs",
        "swe_agentobs_attempts",
        "swe_agentobs_spans",
    },
)
REQUIRED_COLUMNS = {
    "swe_agentobs_inbox": frozenset(
        {
            "ingest_id",
            "payload_text",
            "processing_state",
            "processed_at",
            "reason_code",
            "event_id",
        },
    ),
    "swe_agentobs_events": frozenset(
        {
            "event_id",
            "schema_version",
            "event_name",
            "entity_kind",
            "entity_id",
            "phase_key",
            "local_seq",
            "run_id",
            "attempt_id",
            "span_id",
            "occurred_at",
            "first_ingest_id",
            "producer_id",
            "metadata_text",
        },
    ),
    "swe_agentobs_runs": frozenset(
        {
            "run_id",
            "service_unit_key",
            "source_key",
            "tenant_key",
            "agent_key",
            "chat_key",
            "root_question_key",
            "session_key",
            "started_at",
            "ended_at",
            "status",
            "quality",
            "terminal_fact_source",
            "duration_us",
            "llm_calls",
            "tool_calls",
            "agent_loops",
            "input_tokens",
            "output_tokens",
            "span_count",
            "error_span_count",
            "projected_at",
        },
    ),
    "swe_agentobs_attempts": frozenset(
        {
            "attempt_id",
            "run_id",
            "attempt_index",
            "predecessor_attempt_id",
            "started_at",
            "ended_at",
            "status",
            "duration_us",
        },
    ),
    "swe_agentobs_spans": frozenset(
        {
            "span_id",
            "run_id",
            "attempt_id",
            "parent_span_id",
            "span_type",
            "operation",
            "kind",
            "name",
            "step_index",
            "tool_key",
            "model_key",
            "provider_key",
            "request_model_key",
            "response_model_key",
            "model_key_source",
            "streaming",
            "finish_reason",
            "started_at",
            "ended_at",
            "status",
            "technical_status",
            "has_error",
            "error_type",
            "quality",
            "duration_us",
            "time_to_first_chunk_us",
            "time_to_first_visible_text_us",
            "input_tokens",
            "output_tokens",
        },
    ),
}
REQUIRED_INDEXES = {
    "swe_agentobs_inbox": {
        "ix_agentobs_inbox_pending": (
            False,
            ("processing_state", "ingest_id"),
        ),
    },
    "swe_agentobs_events": {
        "uq_agentobs_event_slot": (
            True,
            ("entity_kind", "entity_id", "phase_key", "local_seq"),
        ),
        "ix_agentobs_events_run": (
            False,
            ("run_id", "occurred_at", "event_id"),
        ),
    },
    "swe_agentobs_runs": {
        "ix_agentobs_runs_source_time": (
            False,
            ("service_unit_key", "source_key", "started_at", "run_id"),
        ),
        "ix_agentobs_runs_scope_time": (
            False,
            (
                "service_unit_key",
                "source_key",
                "tenant_key",
                "agent_key",
                "started_at",
                "run_id",
            ),
        ),
    },
    "swe_agentobs_attempts": {
        "uq_agentobs_attempt_order": (True, ("run_id", "attempt_index")),
    },
    "swe_agentobs_spans": {
        "ix_agentobs_spans_run_time": (
            False,
            ("run_id", "started_at", "span_id"),
        ),
        "ix_agentobs_spans_parent": (
            False,
            ("run_id", "parent_span_id", "started_at", "span_id"),
        ),
        "ix_agentobs_spans_tool": (
            False,
            ("tool_key", "started_at", "run_id"),
        ),
    },
}


def _run_where_clause(
    service_unit_id: str,
    source_ids: list[str],
    start_at,
    end_at,
    filters: dict[str, Any],
    tenant_id: str | None = None,
) -> tuple[str, list[Any]]:
    placeholders = ",".join(["%s"] * len(source_ids))
    query = "WHERE service_unit_key=%s " f"AND source_key IN ({placeholders})"
    params: list[Any] = [
        service_unit_id.encode(),
        *[s.encode() for s in source_ids],
    ]
    if tenant_id is not None:
        query += " AND tenant_key=%s"
        params.append(tenant_id.encode())
    if filters.get("run_id") is not None:
        query += " AND run_id=%s"
        params.append(filters["run_id"])
    if filters.get("attempt_id") is not None:
        query += (
            " AND EXISTS (SELECT 1 FROM swe_agentobs_attempts a "
            "WHERE a.run_id=swe_agentobs_runs.run_id AND a.attempt_id=%s)"
        )
        params.append(filters["attempt_id"])
    if filters.get("session_id") is not None:
        query += " AND session_key=%s"
        params.append(filters["session_id"].encode())
    if filters.get("agent_id") is not None:
        query += " AND agent_key=%s"
        params.append(filters["agent_id"].encode())
    if filters.get("status") is not None:
        query += " AND status=%s"
        params.append(filters["status"])
    if filters.get("min_duration_us") is not None:
        query += " AND duration_us >= %s"
        params.append(filters["min_duration_us"])
    if filters.get("sort_by") == "duration_us":
        query += " AND ended_at IS NOT NULL AND duration_us IS NOT NULL"
    if start_at is not None:
        query += " AND started_at >= %s"
        params.append(start_at.astimezone(timezone.utc).replace(tzinfo=None))
    if end_at is not None:
        query += " AND started_at < %s"
        params.append(end_at.astimezone(timezone.utc).replace(tzinfo=None))
    return query, params


class ObservabilityStore:
    def __init__(self, connection: DatabaseConnection) -> None:
        self.connection = connection

    async def readiness(self) -> dict[str, Any]:
        async with self.connection.acquire() as session:
            rows = await session.fetch_all(
                "SELECT TABLE_NAME,COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME LIKE 'swe_agentobs_%'",
                (self.connection.config.database,),
            )
            index_rows = await session.fetch_all(
                "SELECT TABLE_NAME,INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME "
                "FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME LIKE 'swe_agentobs_%'",
                (self.connection.config.database,),
            )
        found: dict[str, set[str]] = {}
        for row in rows:
            found.setdefault(str(row["TABLE_NAME"]), set()).add(
                str(row["COLUMN_NAME"]),
            )
        missing_columns = {
            table: sorted(columns - found.get(table, set()))
            for table, columns in REQUIRED_COLUMNS.items()
            if columns - found.get(table, set())
        }
        missing_tables = sorted(REQUIRED_TABLES - found.keys())
        indexes: dict[str, dict[str, dict[str, Any]]] = {}
        for row in index_rows:
            table = str(row["TABLE_NAME"])
            name = str(row["INDEX_NAME"])
            index = indexes.setdefault(table, {}).setdefault(
                name,
                {"non_unique": bool(int(row["NON_UNIQUE"])), "columns": []},
            )
            index["columns"].append(
                (int(row["SEQ_IN_INDEX"]), str(row["COLUMN_NAME"])),
            )
        invalid_indexes: dict[str, list[str]] = {}
        for table, definitions in REQUIRED_INDEXES.items():
            for name, (unique, columns) in definitions.items():
                actual = indexes.get(table, {}).get(name)
                actual_columns = (
                    tuple(column for _, column in sorted(actual["columns"]))
                    if actual
                    else None
                )
                if (
                    actual is None
                    or actual["non_unique"] == unique
                    or actual_columns != columns
                ):
                    invalid_indexes.setdefault(table, []).append(name)
        return {
            "ready": not missing_tables
            and not missing_columns
            and not invalid_indexes,
            "missing_tables": missing_tables,
            "missing_columns": missing_columns,
            "invalid_indexes": invalid_indexes,
        }

    async def list_runs(
        self,
        *,
        service_unit_id: str,
        source_ids: list[str],
        limit: int,
        before: tuple[Any, str] | None = None,
        start_at=None,
        end_at=None,
        run_id: str | None = None,
        attempt_id: str | None = None,
        session_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
        min_duration_us: int | None = None,
        sort_by: str = "started_at",
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not source_ids:
            return []
        filters = {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "session_id": session_id,
            "agent_id": agent_id,
            "status": status,
            "min_duration_us": min_duration_us,
            "sort_by": sort_by,
        }
        where, params = _run_where_clause(
            service_unit_id,
            source_ids,
            start_at,
            end_at,
            filters,
            tenant_id,
        )
        query = (
            "SELECT /*+ MAX_EXECUTION_TIME(5000) */ run_id, service_unit_key, "
            "source_key, tenant_key, agent_key, chat_key, root_question_key, "
            "session_key, started_at, ended_at, status, quality, duration_us, "
            "llm_calls, tool_calls, agent_loops, input_tokens, output_tokens, span_count, "
            "error_span_count, projected_at FROM swe_agentobs_runs " + where
        )
        if before:
            cursor_column = (
                "duration_us" if sort_by == "duration_us" else "started_at"
            )
            query += (
                f" AND ({cursor_column} < %s OR "
                f"({cursor_column} = %s AND run_id < %s))"
            )
            params.extend([before[0], before[0], before[1]])
        sort_column = (
            "duration_us" if sort_by == "duration_us" else "started_at"
        )
        query += f" ORDER BY {sort_column} DESC, run_id DESC LIMIT %s"
        params.append(limit)
        async with self.connection.acquire() as session:
            rows = await session.fetch_all(query, tuple(params))
            await self._add_model_request_summary(session, rows)
            return rows

    async def count_run_candidates(
        self,
        *,
        service_unit_id: str,
        source_ids: list[str],
        start_at,
        end_at,
        run_id: str | None = None,
        attempt_id: str | None = None,
        session_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
        min_duration_us: int | None = None,
        sort_by: str = "started_at",
        max_candidates: int = 50_000,
        tenant_id: str | None = None,
    ) -> int:
        if not source_ids:
            return 0
        filters = {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "session_id": session_id,
            "agent_id": agent_id,
            "status": status,
            "min_duration_us": min_duration_us,
            "sort_by": sort_by,
        }
        where, params = _run_where_clause(
            service_unit_id,
            source_ids,
            start_at,
            end_at,
            filters,
            tenant_id,
        )
        query = (
            "SELECT /*+ MAX_EXECUTION_TIME(5000) */ COUNT(*) AS candidate_count "
            "FROM (SELECT run_id FROM swe_agentobs_runs "
            + where
            + " LIMIT %s) AS bounded_run_candidates"
        )
        async with self.connection.acquire() as session:
            row = await session.fetch_one(
                query,
                (*params, max_candidates + 1),
            )
        return int(row["candidate_count"])

    async def get_run(
        self,
        *,
        service_unit_id: str,
        run_id: str,
        source_ids: list[str],
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not source_ids:
            return None
        placeholders = ",".join(["%s"] * len(source_ids))
        query = (
            "SELECT run_id,service_unit_key,source_key,tenant_key,agent_key,"
            "chat_key,root_question_key,session_key,started_at,ended_at,status,quality,"
            "duration_us,active_duration_us,llm_calls,tool_calls,agent_loops,"
            "input_tokens,output_tokens,"
            "span_count,error_span_count,projected_at FROM swe_agentobs_runs "
            f"WHERE service_unit_key=%s AND run_id=%s "
            f"AND source_key IN ({placeholders})"
        )
        params = [
            service_unit_id.encode(),
            run_id,
            *[item.encode() for item in source_ids],
        ]
        if tenant_id is not None:
            query += " AND tenant_key=%s"
            params.append(tenant_id.encode())
        async with self.connection.acquire() as session:
            row = await session.fetch_one(query, tuple(params))
            if row is not None:
                await self._add_model_request_summary(session, [row])
            return row

    @staticmethod
    async def _add_model_request_summary(session, rows: list[dict]) -> None:
        if not rows:
            return
        # Aggregate only the selected authorized page using the unique spans.
        # The existing span metadata records when a logical call proves zero.
        placeholders = ",".join(["%s"] * len(rows))
        summaries = await session.fetch_all(
            "SELECT /*+ MAX_EXECUTION_TIME(5000) */ run_id,"
            "COUNT(CASE WHEN operation='chat' THEN 1 END) AS logical_calls,"
            "COUNT(CASE WHEN operation='chat' AND ended_at IS NOT NULL "
            "AND JSON_UNQUOTE(JSON_EXTRACT(metadata_text,'$.no_request_attempts'))='true' "
            "THEN 1 END) AS zero_request_calls,"
            "COUNT(CASE WHEN operation='request_attempt' THEN 1 END) AS request_count,"
            "COUNT(CASE WHEN operation='request_attempt' AND ended_at IS NOT NULL "
            "THEN input_tokens END) AS input_known,"
            "COUNT(CASE WHEN operation='request_attempt' AND ended_at IS NOT NULL "
            "THEN output_tokens END) AS output_known,"
            "SUM(CASE WHEN operation='request_attempt' AND ended_at IS NOT NULL "
            "THEN input_tokens END) AS request_input_tokens,"
            "SUM(CASE WHEN operation='request_attempt' AND ended_at IS NOT NULL "
            "THEN output_tokens END) AS request_output_tokens "
            "FROM swe_agentobs_spans WHERE run_id IN (" + placeholders + ") "
            "AND span_type='LLM' AND operation IN ('chat','request_attempt') GROUP BY run_id",
            tuple(row["run_id"] for row in rows),
        )
        by_run = {item["run_id"]: item for item in summaries}
        for row in rows:
            summary = by_run.get(row["run_id"], {})
            logical_calls = summary.get("logical_calls") or None
            request_count = summary.get("request_count") or None
            if logical_calls and logical_calls == summary.get(
                "zero_request_calls",
            ):
                request_count = request_count or 0
            input_total = summary.get("request_input_tokens")
            output_total = summary.get("request_output_tokens")
            row.update(
                llm_calls=logical_calls,
                request_attempt_count=request_count,
                input_tokens_known_requests=(
                    summary["input_known"]
                    if request_count is not None
                    else None
                ),
                output_tokens_known_requests=(
                    summary["output_known"]
                    if request_count is not None
                    else None
                ),
                input_tokens=(
                    int(input_total) if input_total is not None else None
                ),
                output_tokens=(
                    int(output_total) if output_total is not None else None
                ),
            )

    async def list_attempts(self, *, run_id: str) -> list[dict[str, Any]]:
        async with self.connection.acquire() as session:
            return await session.fetch_all(
                "SELECT attempt_id,run_id,attempt_index,predecessor_attempt_id,"
                "started_at,ended_at,status,duration_us FROM swe_agentobs_attempts "
                "WHERE run_id=%s ORDER BY attempt_index ASC,attempt_id ASC",
                (run_id,),
            )

    async def available_link_targets(
        self,
        *,
        run_id: str,
        span_ids: list[str],
        attempt_ids: list[str],
    ) -> tuple[set[str], set[str]]:
        span_ids = list(dict.fromkeys(span_ids))
        attempt_ids = list(dict.fromkeys(attempt_ids))
        async with self.connection.acquire() as session:
            spans = []
            attempts = []
            if span_ids:
                spans = await session.fetch_all(
                    "SELECT span_id FROM swe_agentobs_spans WHERE run_id=%s "
                    "AND span_id IN ("
                    + ",".join(["%s"] * len(span_ids))
                    + ")",
                    (run_id, *span_ids),
                )
            if attempt_ids:
                attempts = await session.fetch_all(
                    "SELECT attempt_id FROM swe_agentobs_attempts WHERE run_id=%s "
                    "AND attempt_id IN ("
                    + ",".join(["%s"] * len(attempt_ids))
                    + ")",
                    (run_id, *attempt_ids),
                )
        return (
            {str(row["span_id"]) for row in spans},
            {str(row["attempt_id"]) for row in attempts},
        )

    async def list_spans(
        self,
        *,
        run_id: str,
        attempt_id: str | None = None,
        limit: int = 200,
        after: tuple[Any, str] | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT span_id,run_id,attempt_id,parent_span_id,span_type,operation,"
            "kind,name,step_index,tool_key,started_at,ended_at,status,technical_status,"
            "has_error,error_type,quality,duration_us,time_to_first_chunk_us,"
            "time_to_first_visible_text_us,input_tokens,output_tokens,model_key,"
            "provider_key,request_model_key,response_model_key,model_key_source,"
            "streaming,finish_reason,metadata_text "
            "FROM swe_agentobs_spans WHERE run_id=%s"
        )
        params: list[Any] = [run_id]
        if attempt_id:
            query += " AND attempt_id=%s"
            params.append(attempt_id)
        if after:
            query += (
                " AND (started_at > %s OR (started_at = %s AND span_id > %s))"
            )
            params.extend([after[0], after[0], after[1]])
        query += " ORDER BY started_at ASC,span_id ASC LIMIT %s"
        params.append(limit)
        async with self.connection.acquire() as session:
            rows = await session.fetch_all(query, tuple(params))
        return [_span_for_api(row) for row in rows]

    async def count_span_search_candidates(
        self,
        *,
        run_id: str,
        attempt_id: str | None,
        query_text: str,
        errors_only: bool,
        max_candidates: int,
    ) -> int:
        where, params = _span_search_where(
            run_id,
            attempt_id,
            query_text,
            errors_only,
        )
        query = (
            "SELECT /*+ MAX_EXECUTION_TIME(5000) */ "
            "COUNT(*) AS candidate_count FROM (SELECT span_id "
            "FROM swe_agentobs_spans WHERE "
            + where
            + " LIMIT %s) AS bounded_span_candidates"
        )
        async with self.connection.acquire() as session:
            row = await session.fetch_one(
                query,
                (*params, max_candidates + 1),
            )
        return int(row["candidate_count"])

    async def search_spans(
        self,
        *,
        run_id: str,
        attempt_id: str | None,
        query_text: str,
        errors_only: bool,
        limit: int,
        after: tuple[Any, str] | None = None,
    ) -> list[dict[str, Any]]:
        where, params = _span_search_where(
            run_id,
            attempt_id,
            query_text,
            errors_only,
        )
        query = (
            "SELECT /*+ MAX_EXECUTION_TIME(5000) */ span_id,run_id,attempt_id,"
            "parent_span_id,span_type,operation,kind,name,step_index,tool_key,"
            "started_at,ended_at,status,technical_status,has_error,error_type,"
            "quality,duration_us,time_to_first_chunk_us,"
            "time_to_first_visible_text_us,input_tokens,output_tokens,model_key,"
            "provider_key,request_model_key,response_model_key,model_key_source,"
            "streaming,finish_reason,metadata_text FROM swe_agentobs_spans WHERE "
            + where
        )
        if after:
            query += (
                " AND (started_at > %s OR (started_at = %s AND span_id > %s))"
            )
            params.extend([after[0], after[0], after[1]])
        query += " ORDER BY started_at ASC,span_id ASC LIMIT %s"
        params.append(limit)
        async with self.connection.acquire() as session:
            rows = await session.fetch_all(query, tuple(params))
        return [_span_for_api(row) for row in rows]

    async def list_events(
        self,
        *,
        run_id: str,
        source_ids: list[str],
        limit: int,
        span_id: str | None = None,
        links_only: bool = False,
        before: tuple[Any, str] | None = None,
    ) -> list[dict[str, Any]]:
        if not source_ids:
            return []
        placeholders = ",".join(["%s"] * len(source_ids))
        query = (
            "SELECT event_id,event_name,entity_kind,entity_id,attempt_id,phase_key,"
            "local_seq,occurred_at,metadata_text FROM swe_agentobs_events "
            f"WHERE run_id=%s AND source_key IN ({placeholders})"
        )
        params: list[Any] = [run_id, *[item.encode() for item in source_ids]]
        if span_id:
            query += (
                " AND (span_id=%s OR (span_id IS NULL AND "
                "JSON_UNQUOTE(JSON_EXTRACT(metadata_text,'$.parent_obs_span_id'))=%s))"
            )
            params.extend([span_id, span_id])
        if links_only:
            query = _deduplicate_event_links(query)
        if before:
            query += " AND (occurred_at < %s OR (occurred_at = %s AND event_id < %s))"
            params.extend([before[0], before[0], before[1]])
        query += " ORDER BY occurred_at DESC,event_id DESC LIMIT %s"
        params.append(limit)
        async with self.connection.acquire() as session:
            return await session.fetch_all(query, tuple(params))

    async def list_span_evidence_refs(
        self,
        *,
        run_id: str,
        span_id: str,
        source_ids: list[str],
        limit: int = 32,
    ) -> list[dict[str, Any]]:
        """Return only bounded, metadata-only evidence locators for a Span."""
        if not source_ids:
            return []
        placeholders = ",".join(["%s"] * len(source_ids))
        candidate_occurrence = (
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,"
            "'$.content_ref.occurrence_id')) COLLATE utf8mb4_bin,"
            "JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,"
            "'$.occurrence_id')) COLLATE utf8mb4_bin,"
            "NULL)"
        )
        ranked_occurrence = (
            "COALESCE(" + candidate_occurrence + ","
            "IF(JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,"
            "'$.reference_id')) IS NOT NULL,"
            "CONVERT(CONCAT('reference:',JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,"
            "'$.reference_id'))) USING utf8mb4) COLLATE utf8mb4_bin,"
            "IF(JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,"
            "'$.event_type'))='evidence.pending',"
            "CONVERT(CONCAT('pending:',candidate.event_id) USING utf8mb4) "
            "COLLATE utf8mb4_bin,'0' COLLATE utf8mb4_bin)))"
        )
        linked_occurrence = (
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(linked.metadata_text,"
            "'$.content_ref.occurrence_id')) COLLATE utf8mb4_bin,"
            "JSON_UNQUOTE(JSON_EXTRACT(linked.metadata_text,"
            "'$.occurrence_id')) COLLATE utf8mb4_bin)"
        )
        legacy_linked_match = (
            "EXISTS (SELECT 1 FROM swe_agentobs_events legacy_linked "
            "WHERE legacy_linked.run_id=candidate.run_id "
            "AND legacy_linked.span_id=candidate.span_id "
            "AND legacy_linked.event_name='span.event' "
            "AND JSON_UNQUOTE(JSON_EXTRACT(legacy_linked.metadata_text,"
            "'$.event_type'))='evidence.linked' "
            "AND legacy_linked.source_key IN ("
            + placeholders
            + ") AND COALESCE(JSON_UNQUOTE(JSON_EXTRACT(legacy_linked.metadata_text,"
            "'$.content_ref.chat_id')) COLLATE utf8mb4_bin,"
            "JSON_UNQUOTE(JSON_EXTRACT(legacy_linked.metadata_text,"
            "'$.chat_id')) COLLATE utf8mb4_bin) <=> "
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,"
            "'$.content_ref.chat_id')) COLLATE utf8mb4_bin,"
            "JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,'$.chat_id')) "
            "COLLATE utf8mb4_bin) AND "
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(legacy_linked.metadata_text,"
            "'$.content_ref.message_id')) COLLATE utf8mb4_bin,"
            "JSON_UNQUOTE(JSON_EXTRACT(legacy_linked.metadata_text,'$.message_id')) "
            "COLLATE utf8mb4_bin) <=> "
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,"
            "'$.content_ref.message_id')) COLLATE utf8mb4_bin,"
            "JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,'$.message_id')) "
            "COLLATE utf8mb4_bin) AND "
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(legacy_linked.metadata_text,"
            "'$.content_ref.content_type')) COLLATE utf8mb4_bin,"
            "JSON_UNQUOTE(JSON_EXTRACT(legacy_linked.metadata_text,'$.content_type')) "
            "COLLATE utf8mb4_bin) <=> "
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,"
            "'$.content_ref.content_type')) COLLATE utf8mb4_bin,"
            "JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,'$.content_type')) "
            "COLLATE utf8mb4_bin))"
        )
        query = (
            "WITH ranked_evidence AS ("
            "SELECT event_id,occurred_at,metadata_text,"
            "IF(JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,'$.event_type'))="
            "'evidence.pending' AND "
            "JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,'$.reference_id')) "
            "IS NULL AND "
            + legacy_linked_match
            + ",1,0) AS legacy_pending_ambiguous,"
            "ROW_NUMBER() OVER (PARTITION BY "
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(metadata_text,'$.content_ref.chat_id')) COLLATE utf8mb4_bin,"
            "JSON_UNQUOTE(JSON_EXTRACT(metadata_text,'$.chat_id')) COLLATE utf8mb4_bin,"
            "CONVERT(CONCAT('event:',event_id) USING utf8mb4) COLLATE utf8mb4_bin),"
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(metadata_text,'$.content_ref.message_id')) COLLATE utf8mb4_bin,"
            "JSON_UNQUOTE(JSON_EXTRACT(metadata_text,'$.message_id')) COLLATE utf8mb4_bin,"
            "CONVERT(CONCAT('event:',event_id) USING utf8mb4) COLLATE utf8mb4_bin),"
            + ranked_occurrence
            + ","
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(metadata_text,'$.content_ref.content_type')) COLLATE utf8mb4_bin,"
            "JSON_UNQUOTE(JSON_EXTRACT(metadata_text,'$.content_type')) COLLATE utf8mb4_bin) "
            "ORDER BY IF(JSON_UNQUOTE(JSON_EXTRACT(metadata_text,'$.event_type'))="
            "'evidence.linked',0,1),occurred_at,event_id) AS locator_rank "
            "FROM swe_agentobs_events candidate WHERE candidate.run_id=%s "
            "AND candidate.span_id=%s AND candidate.event_name='span.event' "
            "AND JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,'$.event_type')) "
            "IN ('evidence.pending','evidence.linked') "
            "AND candidate.source_key IN (" + placeholders + ") AND ("
            "JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,'$.event_type'))="
            "'evidence.linked' OR NOT EXISTS (SELECT 1 FROM swe_agentobs_events linked "
            "WHERE linked.run_id=candidate.run_id AND linked.span_id=candidate.span_id "
            "AND linked.event_name='span.event' "
            "AND JSON_UNQUOTE(JSON_EXTRACT(linked.metadata_text,'$.event_type'))="
            "'evidence.linked' AND linked.source_key IN ("
            + placeholders
            + ") AND COALESCE(JSON_UNQUOTE(JSON_EXTRACT(linked.metadata_text,'$.content_ref.chat_id')) COLLATE utf8mb4_bin,"
            "JSON_UNQUOTE(JSON_EXTRACT(linked.metadata_text,'$.chat_id')) COLLATE utf8mb4_bin) <=> "
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,'$.content_ref.chat_id')) COLLATE utf8mb4_bin,"
            "JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,'$.chat_id')) COLLATE utf8mb4_bin) "
            "AND COALESCE(JSON_UNQUOTE(JSON_EXTRACT(linked.metadata_text,'$.content_ref.message_id')) COLLATE utf8mb4_bin,"
            "JSON_UNQUOTE(JSON_EXTRACT(linked.metadata_text,'$.message_id')) COLLATE utf8mb4_bin) <=> "
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,'$.content_ref.message_id')) COLLATE utf8mb4_bin,"
            "JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,'$.message_id')) COLLATE utf8mb4_bin) "
            "AND (("
            "("
            + candidate_occurrence
            + ") IS NOT NULL AND "
            + linked_occurrence
            + " <=> ("
            + candidate_occurrence
            + ")) OR ("
            "(" + candidate_occurrence + ") IS NULL AND "
            "JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,'$.reference_id')) IS NOT NULL AND "
            "JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,'$.reference_id')) COLLATE utf8mb4_bin = "
            "JSON_UNQUOTE(JSON_EXTRACT(linked.metadata_text,'$.reference_id')) COLLATE utf8mb4_bin)) "
            "AND COALESCE(JSON_UNQUOTE(JSON_EXTRACT(linked.metadata_text,'$.content_ref.content_type')) COLLATE utf8mb4_bin,"
            "JSON_UNQUOTE(JSON_EXTRACT(linked.metadata_text,'$.content_type')) COLLATE utf8mb4_bin) <=> "
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,'$.content_ref.content_type')) COLLATE utf8mb4_bin,"
            "JSON_UNQUOTE(JSON_EXTRACT(candidate.metadata_text,'$.content_type')) COLLATE utf8mb4_bin))) "
            ") SELECT event_id,occurred_at,metadata_text,"
            "legacy_pending_ambiguous "
            "FROM ranked_evidence WHERE locator_rank=1 "
            "ORDER BY occurred_at,event_id LIMIT %s"
        )
        async with self.connection.acquire() as session:
            rows = await session.fetch_all(
                query,
                (
                    *[item.encode() for item in source_ids],
                    run_id,
                    span_id,
                    *[item.encode() for item in source_ids],
                    *[item.encode() for item in source_ids],
                    limit,
                ),
            )
        refs_by_identity: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            raw = row.get("metadata_text")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                metadata = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(metadata, dict):
                continue
            reference = metadata.get("content_ref")
            if not isinstance(reference, dict):
                reference = {
                    key: metadata.get(key)
                    for key in (
                        "chat_id",
                        "message_id",
                        "occurrence_id",
                        "reference_id",
                        "content_type",
                        "saved_state",
                    )
                    if metadata.get(key) is not None
                }
            if not isinstance(reference, dict):
                continue
            if row.get("legacy_pending_ambiguous"):
                reference = {**reference, "saved_state": "unresolved"}
            safe = {
                key: reference[key]
                for key in (
                    "chat_id",
                    "message_id",
                    "occurrence_id",
                    "reference_id",
                    "content_type",
                    "saved_state",
                )
                if key in reference
                and (
                    isinstance(reference[key], str)
                    or type(reference[key]) is int
                )
            }
            if safe:
                safe["evidence_ref"] = f"event:{row['event_id']}"
                identity = tuple(
                    safe.get(key)
                    for key in (
                        "chat_id",
                        "message_id",
                        "occurrence_id",
                        "reference_id",
                        "content_type",
                    )
                )
                current = refs_by_identity.get(identity)
                if current is None or (
                    current.get("saved_state") == "pending"
                    and safe.get("saved_state") == "available"
                ):
                    refs_by_identity[identity] = safe
        return list(refs_by_identity.values())

    async def list_run_assets(
        self,
        *,
        run_id: str,
        source_ids: list[str],
        span_id: str | None = None,
        limit: int = 32,
        before: tuple[Any, str] | None = None,
    ) -> dict[str, Any]:
        """Read bounded asset.used metadata without resolving current assets."""
        if not source_ids:
            return {
                "items": [],
                "next_key": None,
                "has_more": False,
                "quality": "complete",
            }
        placeholders = ",".join(["%s"] * len(source_ids))
        query = (
            "SELECT event_id,span_id,occurred_at,metadata_text "
            "FROM swe_agentobs_events WHERE run_id=%s "
            "AND event_name='asset.used' AND source_key IN ("
            + placeholders
            + ")"
        )
        params: list[Any] = [run_id, *[item.encode() for item in source_ids]]
        if span_id:
            query += (
                " AND (span_id=%s OR (span_id IS NULL AND "
                "JSON_UNQUOTE(JSON_EXTRACT(metadata_text,'$.parent_obs_span_id'))=%s))"
            )
            params.extend([span_id, span_id])
        if before is not None:
            query += (
                " AND (occurred_at>%s OR (occurred_at=%s AND event_id>%s))"
            )
            params.extend([before[0], before[0], before[1]])
        query += " ORDER BY occurred_at ASC,event_id ASC LIMIT %s"
        params.append(limit + 1)
        async with self.connection.acquire() as session:
            rows = await session.fetch_all(query, tuple(params))
        assets: list[dict[str, Any]] = []
        for row in rows:
            raw = row.get("metadata_text")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                metadata = json.loads(raw)
            except (TypeError, ValueError):
                continue
            asset = (
                metadata.get("asset") if isinstance(metadata, dict) else None
            )
            if not isinstance(asset, dict):
                continue
            safe = {
                key: asset[key]
                for key in (
                    "asset_type",
                    "asset_id",
                    "asset_version",
                    "asset_digest",
                    "asset_source",
                )
                if isinstance(asset.get(key), str)
            }
            if not safe:
                continue
            safe["asset_ref"] = f"event:{row['event_id']}"
            safe["span_id"] = row.get("span_id") or metadata.get(
                "parent_obs_span_id",
            )
            assets.append(safe)
        has_more = len(rows) > limit
        if has_more:
            assets = assets[:limit]
        last_row = rows[limit - 1] if has_more else None
        return {
            "items": assets,
            "next_key": (
                (last_row["occurred_at"], str(last_row["event_id"]))
                if last_row is not None
                else None
            ),
            "has_more": has_more,
            "quality": "partial" if has_more else "complete",
        }

    async def business_run_population(
        self,
        *,
        service_unit_id: str,
        tenant_id: str,
        source_id: str,
        agent_id: str,
        start_at,
        end_at,
        limit: int = 50_000,
    ) -> list[dict[str, Any]]:
        """Read bounded Run identities for one exact business workspace."""
        query = (
            "SELECT /*+ MAX_EXECUTION_TIME(5000) */ run_id,chat_key,"
            "session_key,root_question_key,status,ended_at,"
            "terminal_fact_source,capture_enabled "
            "FROM swe_agentobs_runs WHERE service_unit_key=%s "
            "AND tenant_key=%s AND source_key=%s AND agent_key=%s "
            "AND started_at >= %s AND started_at < %s "
            "ORDER BY started_at ASC,run_id ASC LIMIT %s"
        )
        params = (
            service_unit_id.encode(),
            tenant_id.encode(),
            source_id.encode(),
            agent_id.encode(),
            start_at.astimezone(timezone.utc).replace(tzinfo=None),
            end_at.astimezone(timezone.utc).replace(tzinfo=None),
            limit + 1,
        )
        async with self.connection.acquire() as session:
            return await session.fetch_all(query, params)

    async def list_span_ancestors(
        self,
        *,
        run_id: str,
        span_id: str,
        limit: int = 64,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Walk parent links with a hard depth and cycle guard."""
        items: list[dict[str, Any]] = []
        visited: set[str] = set()
        current = cursor or span_id
        quality = "complete"
        async with self.connection.acquire() as session:
            while current and len(items) < limit:
                if current in visited:
                    quality = "cycle"
                    break
                visited.add(current)
                row = await session.fetch_one(
                    "SELECT span_id,run_id,attempt_id,parent_span_id,span_type,"
                    "operation,kind,name,step_index,tool_key,started_at,ended_at,"
                    "status,technical_status,has_error,error_type,quality,"
                    "duration_us,time_to_first_chunk_us,time_to_first_visible_text_us,"
                    "input_tokens,output_tokens,model_key,provider_key,"
                    "request_model_key,response_model_key,model_key_source,"
                    "streaming,finish_reason,metadata_text FROM swe_agentobs_spans "
                    "WHERE run_id=%s AND span_id=%s",
                    (run_id, current),
                )
                if row is None:
                    quality = "parent_missing"
                    break
                item = _span_for_api(row)
                items.append(item)
                current = item.get("parent_span_id")
        if current and len(items) >= limit and quality == "complete":
            quality = "depth_limited"
        return {
            "items": items,
            "next_cursor": (
                current if current and quality == "depth_limited" else None
            ),
            "quality": quality,
        }

    async def performance_samples(
        self,
        *,
        service_unit_id: str,
        source_ids: list[str],
        start_at,
        end_at,
        status: str | None = None,
        run_status: str | None = None,
        span_type: str | None = None,
        operation: str | None = None,
        max_samples: int = 50_000,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch a bounded ended-Span sample for reproducible aggregates."""
        if not source_ids:
            return []
        placeholders = ",".join(["%s"] * len(source_ids))
        query = (
            "SELECT /*+ MAX_EXECUTION_TIME(5000) */ s.span_id,s.span_type,"
            "s.operation,s.status,s.quality,s.has_error,s.technical_status,s.duration_us,"
            "s.time_to_first_chunk_us,s.time_to_first_visible_text_us,"
            "s.input_tokens,s.output_tokens,s.started_at,s.ended_at,"
            "r.run_id,r.duration_us AS run_duration_us,"
            "r.active_duration_us,r.agent_loops "
            "FROM swe_agentobs_spans s JOIN swe_agentobs_runs r ON r.run_id=s.run_id "
            "WHERE r.service_unit_key=%s AND r.source_key IN ("
            + placeholders
            + ") AND s.ended_at IS NOT NULL AND s.started_at >= %s "
            "AND s.started_at < %s"
        )
        params: list[Any] = [
            service_unit_id.encode(),
            *[item.encode() for item in source_ids],
            start_at.astimezone(timezone.utc).replace(tzinfo=None),
            end_at.astimezone(timezone.utc).replace(tzinfo=None),
        ]
        if tenant_id is not None:
            query += " AND r.tenant_key=%s"
            params.append(tenant_id.encode())
        if status:
            query += " AND s.status=%s"
            params.append(status)
        if run_status:
            query += " AND r.status=%s"
            params.append(run_status)
        if span_type:
            query += " AND s.span_type=%s"
            params.append(span_type)
        if operation:
            query += " AND s.operation=%s"
            params.append(operation)
        query += " ORDER BY s.started_at ASC,s.span_id ASC LIMIT %s"
        params.append(max_samples + 1)
        async with self.connection.acquire() as session:
            return await session.fetch_all(query, tuple(params))

    async def count_performance_candidates(
        self,
        *,
        service_unit_id: str,
        source_ids: list[str],
        start_at,
        end_at,
        status: str | None = None,
        run_status: str | None = None,
        span_type: str | None = None,
        operation: str | None = None,
        max_candidates: int = 50_000,
        tenant_id: str | None = None,
    ) -> int:
        """Count matching spans up to a hard ceiling before aggregation."""
        if not source_ids:
            return 0
        placeholders = ",".join(["%s"] * len(source_ids))
        query = (
            "SELECT /*+ MAX_EXECUTION_TIME(5000) */ COUNT(*) AS candidate_count "
            "FROM (SELECT s.span_id FROM swe_agentobs_spans s "
            "JOIN swe_agentobs_runs r ON r.run_id=s.run_id "
            "WHERE r.service_unit_key=%s AND r.source_key IN ("
            + placeholders
            + ") AND s.ended_at IS NOT NULL AND s.started_at >= %s "
            "AND s.started_at < %s"
        )
        params: list[Any] = [
            service_unit_id.encode(),
            *[item.encode() for item in source_ids],
            start_at.astimezone(timezone.utc).replace(tzinfo=None),
            end_at.astimezone(timezone.utc).replace(tzinfo=None),
        ]
        if tenant_id is not None:
            query += " AND r.tenant_key=%s"
            params.append(tenant_id.encode())
        for clause, value in (
            ("s.status", status),
            ("r.status", run_status),
            ("s.span_type", span_type),
            ("s.operation", operation),
        ):
            if value:
                query += f" AND {clause}=%s"
                params.append(value)
        query += " LIMIT %s) AS bounded_performance_candidates"
        params.append(max_candidates + 1)
        async with self.connection.acquire() as session:
            row = await session.fetch_one(query, tuple(params))
        return int(row["candidate_count"]) if row else 0


_TOOL_API_FIELDS = (
    "tool_call_id",
    "tool_execution_id",
    "executor_kind",
    "timeout_budget_ms",
    "retry_index",
    "exit_code",
)
_SUBAGENT_API_FIELDS = (
    "business_subagent_key",
    "mode",
    "execution_agent_key",
    "result_status",
)


def _deduplicate_event_links(scoped_events: str) -> str:
    # Choose one event per source/type/target before applying the page cursor.
    # Keep the original event envelope and only replace its links array.
    return (
        "WITH scoped_events AS ("
        + scoped_events
        + " AND JSON_LENGTH(JSON_EXTRACT(metadata_text,'$.links')) > 0), "
        "ranked_links AS ("
        "SELECT event_id, links.link_index, "
        "ROW_NUMBER() OVER ("
        "PARTITION BY entity_kind,entity_id,links.relation_type,"
        "links.target_span_id,links.target_attempt_id "
        "ORDER BY occurred_at DESC,event_id DESC,links.link_index ASC"
        ") AS edge_rank "
        "FROM scoped_events JOIN JSON_TABLE(CAST(metadata_text AS JSON), '$.links[*]' COLUMNS ("
        "link_index FOR ORDINALITY,"
        "relation_type VARCHAR(32) PATH '$.relation_type',"
        "target_span_id CHAR(36) PATH '$.obs_span_id',"
        "target_attempt_id CHAR(36) PATH '$.obs_attempt_id'"
        ")) AS links), "
        "unique_event_links AS ("
        "SELECT ranked_links.event_id, JSON_ARRAYAGG(JSON_EXTRACT(metadata_text,"
        "CONCAT('$.links[',link_index-1,']'))) AS links "
        "FROM ranked_links JOIN scoped_events USING (event_id) "
        "WHERE edge_rank=1 GROUP BY ranked_links.event_id) "
        "SELECT /*+ MAX_EXECUTION_TIME(5000) */ event_id,event_name,entity_kind,entity_id,"
        "attempt_id,phase_key,local_seq,occurred_at,"
        "JSON_SET(metadata_text,'$.links',unique_event_links.links) AS metadata_text "
        "FROM scoped_events JOIN unique_event_links USING (event_id) WHERE 1=1"
    )


def _span_search_where(
    run_id: str,
    attempt_id: str | None,
    query_text: str,
    errors_only: bool,
) -> tuple[str, list[Any]]:
    where = "run_id=%s"
    params: list[Any] = [run_id]
    if attempt_id:
        where += " AND attempt_id=%s"
        params.append(attempt_id)
    if query_text:
        escaped = (
            query_text.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        where += (
            " AND (name LIKE %s OR operation LIKE %s OR tool_key LIKE %s "
            "OR model_key LIKE %s OR request_model_key LIKE %s "
            "OR response_model_key LIKE %s)"
        )
        params.extend([pattern] * 6)
    if errors_only:
        where += (
            " AND (has_error=1 OR technical_status='ERROR' "
            "OR status IN ('failed','rejected'))"
        )
    return where, params


def _span_for_api(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    metadata_text = result.pop("metadata_text", None)
    if isinstance(metadata_text, bytes):
        metadata_text = metadata_text.decode("utf-8", errors="replace")
    span_type = result.get("span_type")
    if span_type == "LLM":
        streaming = result.get("streaming")
        result["streaming"] = (
            bool(streaming) if streaming is not None else None
        )
        input_tokens = result.get("input_tokens")
        output_tokens = result.get("output_tokens")
        result["usage_known"] = (
            input_tokens is not None and output_tokens is not None
        )
        result["total_tokens"] = (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        )
    if span_type not in {
        "LLM",
        "Tool",
        "Guard",
        "SubAgent",
        "Context",
        "Checkpoint",
    } or not isinstance(
        metadata_text,
        str,
    ):
        return result
    try:
        metadata = json.loads(metadata_text)
    except (TypeError, ValueError):
        return result
    if not isinstance(metadata, dict):
        return result
    if span_type == "Context":
        result["compaction_strategy"] = metadata.get("compaction_strategy")
        metrics = metadata.get("metrics")
        if isinstance(metrics, dict):
            for metric in ("context_before_tokens", "context_after_tokens"):
                result[metric] = metrics.get(metric)
        return result
    if span_type == "Checkpoint":
        result["checkpoint_id"] = metadata.get("checkpoint_id")
        return result
    if span_type == "LLM":
        if result.get("operation") == "request_attempt":
            ordinal = metadata.get("request_attempt_index")
            result["request_attempt_index"] = (
                ordinal
                if type(ordinal) is int and 0 <= ordinal <= 0xFFFFFFFF
                else None
            )
        return result
    if span_type in {"Tool", "Guard"}:
        tool = metadata.get("tool")
        if not isinstance(tool, dict):
            return result
        projected = {
            key: tool[key]
            for key in _TOOL_API_FIELDS
            if key in tool
            and (
                isinstance(tool[key], str)
                or (span_type == "Tool" and type(tool[key]) is int)
            )
        }
        tool_key = result.get("tool_key")
        if isinstance(tool_key, bytes):
            tool_key = tool_key.decode("utf-8")
        if not isinstance(tool_key, str):
            tool_key = tool.get("tool_key")
        if isinstance(tool_key, str):
            projected["tool_key"] = tool_key
        if projected:
            result["tool"] = projected
        return result

    subagent = metadata.get("subagent")
    if not isinstance(subagent, dict):
        return result
    projected = {
        key: subagent[key]
        for key in _SUBAGENT_API_FIELDS
        if key in subagent and isinstance(subagent[key], str)
    }
    if projected:
        result["subagent"] = projected
    return result

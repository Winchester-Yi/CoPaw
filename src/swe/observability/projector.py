# -*- coding: utf-8 -*-
"""Bounded inbox validation and idempotent event projection."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from .emitter import _project
from .store import ObservabilityStore

_MAX_BATCH = 32
_EVENT_NAMES = frozenset(
    {
        "run.accepted",
        "run.state",
        "run.ended",
        "attempt.started",
        "attempt.ended",
        "span.started",
        "span.ended",
        "span.event",
        "approval.requested",
        "approval.resolved",
        "subagent.started",
        "subagent.ended",
        "asset.used",
        "observer.health",
    },
)
_TOKEN = re.compile(r"^[A-Za-z0-9_.:@/-]{1,256}$")
logger = logging.getLogger(__name__)
_FIELD_BYTES = {
    "event_name": 48,
    "entity_kind": 16,
    "phase_key": 64,
    "producer_id": 128,
    "span_type": 32,
    "operation": 64,
    "kind": 16,
    "name": 128,
    "error_type": 128,
    "terminal_fact_source": 32,
    "model_key_source": 24,
    "finish_reason": 64,
    "provider_key": 256,
    "request_model_key": 256,
    "response_model_key": 256,
}
_UUID_FIELDS = (
    "obs_run_id",
    "obs_attempt_id",
    "obs_span_id",
    "parent_obs_span_id",
    "predecessor_attempt_id",
)
_REQUIRED_EVENT_FIELDS = {
    "run.accepted": ("started_at",),
    "run.state": ("status",),
    "run.ended": (
        "started_at",
        "ended_at",
        "status",
        "terminal_fact_source",
    ),
    "attempt.started": ("obs_attempt_id", "attempt_index", "started_at"),
    "attempt.ended": (
        "obs_attempt_id",
        "attempt_index",
        "started_at",
        "ended_at",
        "status",
    ),
    "span.started": (
        "obs_span_id",
        "started_at",
        "span_type",
        "operation",
        "kind",
        "name",
    ),
    "span.ended": (
        "obs_span_id",
        "started_at",
        "ended_at",
        "status",
        "span_type",
        "operation",
        "kind",
        "name",
        "metrics",
        "quality",
    ),
    "span.event": ("obs_span_id", "event_type"),
    "approval.requested": ("approval_request_id",),
    "approval.resolved": ("approval_request_id", "decision", "ended_at"),
    "subagent.started": ("obs_span_id", "subagent"),
    "subagent.ended": ("obs_span_id", "ended_at", "status"),
    "asset.used": ("asset",),
}
_RUN_TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "timed_out", "rejected"},
)
_APPROVAL_DECISIONS = frozenset(
    {"approved", "denied", "timed_out", "superseded"},
)
_TERMINAL_FACT_SOURCES = frozenset({"business_settlement"})

try:
    from mysql.connector.errors import Error as _MySQLError
except ImportError:  # 未安装 MySQL 驱动时只保留 Python 内建分类
    _MySQLError = ()


# Statement errors tied to the current row's values or constraints.
_ROW_SQL_ERRORS = {1048, 1062, 1264, 1364, 1366, 1406, 1451, 1452, 3819, 4025}


class _RowWriteError(Exception):
    def __init__(self, ingest_id: int, errno: int):
        super().__init__(ingest_id, errno)
        self.ingest_id = ingest_id
        self.errno = errno


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp timezone missing")
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _attempt_interval_union_us(rows: list[dict[str, Any]]) -> int | None:
    if not rows:
        return None
    intervals = []
    for row in rows:
        started = row.get("started_at")
        ended = row.get("ended_at")
        if (
            not isinstance(started, datetime)
            or not isinstance(ended, datetime)
            or ended < started
        ):
            return None
        intervals.append((started, ended))
    intervals.sort()

    def elapsed_us(started: datetime, ended: datetime) -> int:
        delta = ended - started
        return (
            delta.days * 86_400_000_000
            + delta.seconds * 1_000_000
            + delta.microseconds
        )

    total = 0
    current_start, current_end = intervals[0]
    for started, ended in intervals[1:]:
        if started <= current_end:
            current_end = max(current_end, ended)
            continue
        total += elapsed_us(current_start, current_end)
        current_start, current_end = started, ended
    total += elapsed_us(current_start, current_end)
    return total


async def _refresh_active_duration(session, run_id: str) -> None:
    run = await session.fetch_one(
        "SELECT ended_at FROM swe_agentobs_runs WHERE run_id=%s FOR UPDATE",
        (run_id,),
    )
    if run is None or run["ended_at"] is None:
        return
    attempts = await session.fetch_all(
        "SELECT started_at,ended_at FROM swe_agentobs_attempts "
        "WHERE run_id=%s ORDER BY started_at ASC,ended_at ASC,attempt_id ASC",
        (run_id,),
    )
    await session.execute(
        "UPDATE swe_agentobs_runs SET active_duration_us=%s WHERE run_id=%s",
        (_attempt_interval_union_us(attempts), run_id),
    )


def _validate_payload(raw: str) -> dict[str, Any]:
    event = _project_payload(raw)
    _validate_protocol(event)
    _validate_event_fields(event)
    if event["event_name"] == "observer.health":
        _validate_health_event(event)
        return event
    _validate_lifecycle_event(event)
    return event


def _validate_event_fields(event: dict[str, Any]) -> None:
    name = event["event_name"]
    required = _REQUIRED_EVENT_FIELDS.get(name, ())
    if any(event.get(key) is None for key in required):
        raise ValueError("event_missing_required_field")
    if name in {"span.started", "span.ended"} and (
        event["phase_key"] != name.split(".", 1)[1] or event["local_seq"] != 0
    ):
        raise ValueError("invalid_span_slot")
    if name == "run.ended":
        if event["status"] not in _RUN_TERMINAL_STATUSES:
            raise ValueError("invalid_run_terminal_status")
        if event["terminal_fact_source"] not in _TERMINAL_FACT_SOURCES:
            raise ValueError("invalid_terminal_fact_source")
    if name == "approval.resolved":
        if event["decision"] not in _APPROVAL_DECISIONS:
            raise ValueError("invalid_approval_decision")
    if name == "span.ended":
        quality = event.get("quality")
        if not isinstance(quality, dict) or any(
            key in quality and not isinstance(quality[key], bool)
            for key in ("start_observed", "end_observed")
        ):
            raise ValueError("invalid_span_quality")
    if name in {"attempt.ended", "span.ended", "subagent.ended"}:
        allowed_statuses = {
            "succeeded",
            "failed",
            "cancelled",
            "timed_out",
            "rejected",
            "waiting_approval",
        }
        if name == "span.ended":
            allowed_statuses.add("partial")
        if event["status"] not in allowed_statuses:
            raise ValueError("invalid_operation_terminal_status")


def _project_payload(raw: str) -> dict[str, Any]:
    if len(raw.encode("utf-8")) > 24 * 1024:
        raise ValueError("event_too_large")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("invalid_event")
    event = _project(parsed)
    required = {
        "schema_version",
        "event_id",
        "event_name",
        "occurred_at",
        "producer_id",
        "entity_kind",
        "entity_id",
        "phase_key",
        "local_seq",
    }
    if not isinstance(event, dict) or not required.issubset(event):
        raise ValueError("invalid_event")
    return event


def _validate_protocol(event: dict[str, Any]) -> None:
    if event["schema_version"] != 1:
        raise ValueError("unsupported_schema")
    if event["event_name"] not in _EVENT_NAMES:
        raise ValueError("unsupported_event")
    for key in (
        "event_id",
        "event_name",
        "producer_id",
        "entity_kind",
        "entity_id",
        "phase_key",
    ):
        if not isinstance(event.get(key), str) or not _TOKEN.fullmatch(
            event[key],
        ):
            raise ValueError("invalid_protocol_value")
    for key, limit in _FIELD_BYTES.items():
        value = event.get(key)
        if value is not None and (
            not isinstance(value, str) or len(value.encode("utf-8")) > limit
        ):
            raise ValueError("field_too_long")
    if (
        isinstance(event["local_seq"], bool)
        or not isinstance(event["local_seq"], int)
        or event["local_seq"] < 0
    ):
        raise ValueError("invalid_sequence")
    if event["local_seq"] > 0xFFFFFFFF:
        raise ValueError("invalid_sequence")
    try:
        _require_uuid(event, "event_id")
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("invalid_identity") from exc


def _require_uuid(event: dict[str, Any], key: str) -> None:
    if str(UUID(event[key])) != event[key]:
        raise ValueError("noncanonical")


def _validate_health_event(event: dict[str, Any]) -> None:
    if event.get("entity_kind") != "observer":
        raise ValueError("invalid_entity_kind")
    try:
        _require_uuid(event, "entity_id")
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("invalid_identity") from exc


def _validate_lifecycle_event(event: dict[str, Any]) -> None:
    for key in ("event_id", "obs_run_id", "entity_id"):
        try:
            _require_uuid(event, key)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("invalid_identity") from exc
    _validate_entity_identity(event)
    _validate_related_ids(event)
    _validate_scope(event)
    _validate_lifecycle_values(event)


def _validate_entity_identity(event: dict[str, Any]) -> None:
    expected_entity = None
    if event["event_name"].startswith("run."):
        expected_entity = ("run", "obs_run_id")
    elif event["event_name"].startswith("attempt."):
        expected_entity = ("attempt", "obs_attempt_id")
    elif event["event_name"].startswith("span."):
        expected_entity = ("span", "obs_span_id")
    elif event["event_name"].startswith("approval."):
        expected_entity = ("approval", "approval_request_id")
    elif event["event_name"].startswith("subagent."):
        expected_entity = ("span", "obs_span_id")
    elif event["event_name"] == "asset.used":
        expected_entity = ("asset", "entity_id")
    if (
        expected_entity is None
        or event.get("entity_kind") != expected_entity[0]
    ):
        raise ValueError("invalid_entity_kind")
    if expected_entity[1] != "entity_id" and event.get(
        "entity_id",
    ) != event.get(
        expected_entity[1],
    ):
        raise ValueError("invalid_entity_identity")


def _validate_related_ids(event: dict[str, Any]) -> None:
    for key in _UUID_FIELDS:
        if event.get(key) is None:
            continue
        try:
            if str(UUID(event[key])) != event[key]:
                raise ValueError("noncanonical")
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid_identity") from exc


def _validate_scope(event: dict[str, Any]) -> None:
    scope = event.get("scope")
    if not isinstance(scope, dict) or not all(
        isinstance(scope.get(key), str) and scope[key]
        for key in ("service_unit_id", "source_id", "tenant_id", "agent_id")
    ):
        raise ValueError("invalid_scope")
    if any(len(scope[key].encode("utf-8")) > 256 for key in scope):
        raise ValueError("invalid_scope")
    business_ref = event.get("business_ref") or {}
    if not isinstance(business_ref, dict):
        raise ValueError("invalid_business_reference")
    if any(
        not isinstance(business_ref.get(key, ""), str)
        or len(business_ref.get(key, "").encode("utf-8")) > limit
        for key, limit in {
            "chat_id": 128,
            "root_question_msgid": 256,
            "session_id": 1024,
            "current_question_msgid": 256,
        }.items()
    ):
        raise ValueError("invalid_business_reference")


def _validate_lifecycle_values(event: dict[str, Any]) -> None:
    if not isinstance(event.get("capture_enabled"), bool):
        raise ValueError("invalid_capture_snapshot")
    if event.get("no_request_attempts") is not None and (
        not isinstance(event["no_request_attempts"], bool)
        or event.get("event_name") != "span.ended"
        or event.get("span_type") != "LLM"
        or event.get("operation") != "chat"
    ):
        raise ValueError("invalid_request_attempt_coverage")
    if (
        "has_error" in event
        and event["has_error"] is not None
        and not isinstance(event["has_error"], bool)
    ):
        raise ValueError("invalid_error_flag")
    if (
        "streaming" in event
        and event["streaming"] is not None
        and not isinstance(event["streaming"], bool)
    ):
        raise ValueError("invalid_streaming_flag")
    if event.get("model_key_source") not in {
        None,
        "response",
        "request_fallback",
    }:
        raise ValueError("invalid_model_key_source")
    if event["event_name"] == "span.event":
        if event.get("phase_key") != "event":
            raise ValueError("invalid_event_slot")
        if event.get("event_type") not in {
            "model.retry_scheduled",
            "model.request_attempt_started",
            "tool.retry_scheduled",
            "tool.recovered",
            "evidence.pending",
            "evidence.linked",
            "link.added",
            "retry",
        }:
            raise ValueError("invalid_event_type")
    if event["event_name"] == "asset.used":
        asset = event.get("asset")
        if not isinstance(asset, dict) or not asset:
            raise ValueError("invalid_asset")
        for key in (
            "asset_type",
            "asset_id",
            "asset_version",
            "asset_digest",
            "asset_source",
        ):
            value = asset.get(key)
            if value is not None and (
                not isinstance(value, str) or not _TOKEN.fullmatch(value)
            ):
                raise ValueError("invalid_asset")
    tool = event.get("tool")
    if (
        event.get("span_type") == "Tool"
        and event.get("event_name") in {"span.started", "span.ended"}
        and tool is None
    ):
        raise ValueError("invalid_tool_metadata")
    if tool is not None:
        _validate_tool_fields(event, tool)
    _validate_links(event)
    _timestamp(event.get("occurred_at"))
    _timestamp(event.get("run_started_at"))
    # 可选时间字段必须在入库前解析，否则坏值会在投影事务中途抛错，
    # 导致整批回滚并让该行永久卡住。
    for key in ("started_at", "ended_at", "attempt_started_at"):
        value = event.get(key)
        if value is None:
            continue
        try:
            _timestamp(value)
        except ValueError as exc:
            raise ValueError(f"invalid_{key}") from exc
    for key in ("attempt_index", "step_index", "request_attempt_index"):
        value = event.get(key)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 0xFFFFFFFF
        ):
            raise ValueError("invalid_ordinal")
    metrics = event.get("metrics") or {}
    if not isinstance(metrics, dict):
        raise ValueError("invalid_metrics")
    for metric_key, value in metrics.items():
        if isinstance(value, bool) and metric_key.endswith("_known"):
            continue
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 0xFFFFFFFFFFFFFFFF
        ):
            raise ValueError("invalid_metric")
    if (
        len(
            json.dumps(
                event,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode(
                "utf-8",
            ),
        )
        > 24 * 1024
    ):
        raise ValueError("event_too_large")


def _validate_links(event: dict[str, Any]) -> None:
    links = event.get("links")
    if links is None:
        return
    if not isinstance(links, list) or len(links) > 32:
        raise ValueError("invalid_links")
    for link in links:
        if not isinstance(link, dict):
            raise ValueError("invalid_link")
        if link.get("relation_type") not in {
            "retry_of",
            "retry_next",
            "cause",
            "recovered_by",
        }:
            raise ValueError("invalid_link_relation")
        if link.get("obs_run_id", event["obs_run_id"]) != event["obs_run_id"]:
            raise ValueError("cross_run_link_not_supported")
        targets = [link.get("obs_attempt_id"), link.get("obs_span_id")]
        if sum(target is not None for target in targets) != 1:
            raise ValueError("invalid_link_target")
        for target in targets:
            if target is not None:
                try:
                    if str(UUID(target)) != target:
                        raise ValueError("invalid_link_target")
                except (ValueError, TypeError) as exc:
                    raise ValueError("invalid_link_target") from exc


def _validate_tool_fields(event: dict[str, Any], tool: Any) -> None:
    if not isinstance(tool, dict):
        raise ValueError("invalid_tool_metadata")
    if event.get("span_type") not in {"Tool", "Guard"}:
        raise ValueError("tool_metadata_on_non_tool_span")
    if not isinstance(tool.get("tool_key"), str) or not _TOKEN.fullmatch(
        tool["tool_key"],
    ):
        raise ValueError("invalid_tool_identity")
    if not isinstance(tool.get("tool_call_id"), str) or not _TOKEN.fullmatch(
        tool["tool_call_id"],
    ):
        raise ValueError("invalid_tool_identity")
    if event.get("span_type") == "Tool":
        try:
            if (
                str(UUID(tool["tool_execution_id"]))
                != tool["tool_execution_id"]
            ):
                raise ValueError("invalid_tool_identity")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid_tool_identity") from exc
    elif "tool_execution_id" in tool:
        raise ValueError("guard_must_not_claim_tool_execution")
    if tool.get("executor_kind") not in {
        "agent_tool",
        "mcp_tool",
        "unregistered",
    }:
        raise ValueError("invalid_tool_executor")
    for key, minimum, maximum in (
        ("timeout_budget_ms", 1, 0xFFFFFFFF),
        ("retry_index", 0, 0xFFFFFFFF),
        ("exit_code", -(2**31), 2**31 - 1),
    ):
        value = tool.get(key)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not minimum <= value <= maximum
        ):
            raise ValueError("invalid_tool_metadata")


def _semantic_json(event: dict[str, Any]) -> str:
    semantic = {
        k: v for k, v in event.items() if k not in {"event_id", "producer_id"}
    }
    return json.dumps(
        semantic,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _span_shape_conflicts(
    event: dict[str, Any],
    projected: dict[str, Any],
) -> bool:
    if event["event_name"] not in {"span.started", "span.ended"}:
        return False
    if any(
        event.get(key) != projected[key] for key in ("span_type", "operation")
    ):
        return True
    if event["span_type"] != "LLM" or event["operation"] != "request_attempt":
        return False
    previous_index = json.loads(projected["metadata_text"]).get(
        "request_attempt_index",
    )
    index = event.get("request_attempt_index")
    return (
        previous_index is not None
        and index is not None
        and previous_index != index
    )


class InboxProjector:
    def __init__(
        self,
        store: ObservabilityStore,
        *,
        poll_seconds: float = 1.0,
    ) -> None:
        self.store = store
        self.connection = store.connection
        self.poll_seconds = poll_seconds
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self._task is None:
            self._stopping.clear()
            self._task = asyncio.create_task(
                self._run(),
                name="agentobs-projector",
            )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping.set()
        self._task.cancel()
        try:
            await asyncio.wait_for(self._task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                count = await self.project_batch()
                if count:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Agent observability projector cycle failed: %s",
                    type(exc).__name__,
                )
                await asyncio.sleep(min(max(self.poll_seconds, 1.0) * 5, 30))
                continue
            await asyncio.sleep(self.poll_seconds)

    async def project_batch(self) -> int:
        try:
            return await self._project_pending_batch()
        except _RowWriteError as exc:
            # The database session invalidates a lease after a statement error.
            # The failed batch has rolled back. Reject only the failed row in a
            # fresh transaction and let the next batch retry its healthy peers.
            async with self.connection.transaction() as session:
                return await session.execute(
                    "UPDATE swe_agentobs_inbox SET processing_state=2, "
                    "processed_at=UTC_TIMESTAMP(6), reason_code=%s, payload_text=NULL "
                    "WHERE ingest_id=%s AND processing_state=0",
                    (f"projection_sql_error:{exc.errno}", exc.ingest_id),
                )

    async def _project_pending_batch(self) -> int:
        async with self.connection.acquire() as session:
            await session.execute(
                "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED",
            )
            async with self.connection.transaction() as tx:
                rows = await tx.fetch_all(
                    "SELECT ingest_id, payload_text FROM swe_agentobs_inbox "
                    "WHERE processing_state = 0 AND payload_text IS NOT NULL "
                    "ORDER BY ingest_id LIMIT %s FOR UPDATE SKIP LOCKED",
                    (_MAX_BATCH,),
                )
                for row in rows:
                    await self._project_one(tx, row)
                return len(rows)

    async def _project_one(self, session, row: dict[str, Any]) -> None:
        ingest_id = int(row["ingest_id"])
        try:
            event = _validate_payload(row["payload_text"] or "")
        except Exception as exc:  # noqa: BLE001 - 单行坏数据不得中断整批
            if isinstance(exc, ValueError):
                reason = str(exc)[:64]
            else:
                reason = f"invalid_payload:{type(exc).__name__}"
                logger.warning(
                    "Agent observability payload rejected: ingest_id=%s",
                    ingest_id,
                    exc_info=True,
                )
            state, event = 2, None
            await session.execute(
                "UPDATE swe_agentobs_inbox SET payload_text = NULL WHERE ingest_id = %s",
                (ingest_id,),
            )
        else:
            assert event is not None
            await session.execute("SAVEPOINT agentobs_row")
            reason = None
            try:
                state = await self._store_event(session, event, ingest_id)
            except _MySQLError as exc:
                if exc.errno in _ROW_SQL_ERRORS:
                    raise _RowWriteError(ingest_id, exc.errno) from exc
                raise
            except Exception as exc:  # noqa: BLE001 - 行级错误就地隔离
                await session.execute(
                    "ROLLBACK TO SAVEPOINT agentobs_row",
                )
                await session.execute("RELEASE SAVEPOINT agentobs_row")
                state = 2
                reason = f"projection_error:{type(exc).__name__}"
                logger.warning(
                    "Agent observability projection failed: ingest_id=%s",
                    ingest_id,
                    exc_info=True,
                )
            else:
                if state == 1:
                    await session.execute("RELEASE SAVEPOINT agentobs_row")
                else:
                    # 冲突/未插入时回滚本行已写入的附属行，避免半成品。
                    await session.execute(
                        "ROLLBACK TO SAVEPOINT agentobs_row",
                    )
                    await session.execute("RELEASE SAVEPOINT agentobs_row")
        if event is not None and state == 3:
            reason = "event_conflict"
        elif event is not None and state == 2 and reason is None:
            reason = "event_not_inserted"
        await session.execute(
            "UPDATE swe_agentobs_inbox SET processing_state = %s, processed_at = UTC_TIMESTAMP(6), "
            "reason_code = %s, event_id = %s, payload_text = NULL WHERE ingest_id = %s",
            (
                state,
                reason,
                event.get("event_id") if event else None,
                ingest_id,
            ),
        )

    async def _store_event(
        self,
        session,
        event: dict[str, Any],
        ingest_id: int,
    ) -> int:
        semantic = _semantic_json(event)
        slot = (
            event["entity_kind"],
            event["entity_id"],
            event["phase_key"],
            event["local_seq"],
        )
        existing = await session.fetch_one(
            "SELECT event_id, metadata_text FROM swe_agentobs_events WHERE event_id = %s",
            (event["event_id"],),
        )
        slot_row = await session.fetch_one(
            "SELECT event_id, metadata_text FROM swe_agentobs_events "
            "WHERE entity_kind = %s AND entity_id = %s AND phase_key = %s AND local_seq = %s",
            slot,
        )
        for duplicate in (existing, slot_row):
            if duplicate:
                prior = json.loads(duplicate["metadata_text"])
                if _semantic_json(prior) == semantic:
                    return 1
                return 3
        metadata = json.dumps(
            event,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        context_scope = event.get("scope") or {}
        run_id = event.get("obs_run_id")
        if not await self._ensure_run_row(session, event, metadata):
            return 3
        if event.get("obs_attempt_id"):
            existing_attempt = await session.fetch_one(
                "SELECT run_id FROM swe_agentobs_attempts WHERE attempt_id=%s FOR UPDATE",
                (event["obs_attempt_id"],),
            )
            if existing_attempt and existing_attempt["run_id"] != run_id:
                return 3
            if run_id:
                attempt_index = event.get("attempt_index") or 1
                index_row = await session.fetch_one(
                    "SELECT attempt_id FROM swe_agentobs_attempts "
                    "WHERE run_id=%s AND attempt_index=%s FOR UPDATE",
                    (run_id, attempt_index),
                )
                if (
                    index_row is not None
                    and index_row["attempt_id"] != event["obs_attempt_id"]
                ):
                    return 3
        if event.get("obs_span_id"):
            existing_span = await session.fetch_one(
                "SELECT run_id,attempt_id,parent_span_id,span_type,operation,metadata_text "
                "FROM swe_agentobs_spans WHERE span_id=%s FOR UPDATE",
                (event["obs_span_id"],),
            )
            if existing_span and (
                existing_span["run_id"] != run_id
                or existing_span["attempt_id"] != event.get("obs_attempt_id")
                or existing_span["parent_span_id"]
                != event.get("parent_obs_span_id")
                or _span_shape_conflicts(event, existing_span)
            ):
                return 3
        attempt_id = event.get("obs_attempt_id")
        span_id = event.get("obs_span_id")
        occurred_at = _timestamp(event["occurred_at"])
        inserted = await session.execute(
            "INSERT IGNORE INTO swe_agentobs_events "
            "(event_id,schema_version,event_name,entity_kind,entity_id,phase_key,local_seq,run_id,attempt_id,span_id,"
            "service_unit_key,source_key,tenant_key,agent_key,occurred_at,first_ingest_id,producer_id,metadata_text) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                event["event_id"],
                1,
                event["event_name"],
                slot[0],
                slot[1],
                slot[2],
                slot[3],
                run_id,
                attempt_id,
                span_id,
                context_scope.get("service_unit_id", "").encode() or None,
                context_scope.get("source_id", "").encode() or None,
                context_scope.get("tenant_id", "").encode() or None,
                context_scope.get("agent_id", "").encode() or None,
                occurred_at,
                ingest_id,
                event["producer_id"],
                metadata,
            ),
        )
        if not inserted:
            duplicate = await session.fetch_one(
                "SELECT event_id,metadata_text FROM swe_agentobs_events WHERE event_id=%s",
                (event["event_id"],),
            )
            if duplicate is None:
                duplicate = await session.fetch_one(
                    "SELECT event_id,metadata_text FROM swe_agentobs_events "
                    "WHERE entity_kind=%s AND entity_id=%s AND phase_key=%s AND local_seq=%s",
                    slot,
                )
            if duplicate is None:
                return 2
            prior = json.loads(duplicate["metadata_text"])
            return 1 if _semantic_json(prior) == semantic else 3
        await self._project_entity(session, event, metadata)
        return 1

    async def _ensure_run_row(
        self,
        session,
        event: dict[str, Any],
        metadata: str,
    ) -> bool:
        run_id = event.get("obs_run_id")
        if not run_id:
            return event["event_name"] == "observer.health"
        scope = event["scope"]
        business = event.get("business_ref") or {}
        name = event["event_name"]
        is_run_fact = name.startswith("run.")
        await session.execute(
            "INSERT IGNORE INTO swe_agentobs_runs "
            "(run_id,service_unit_key,source_key,tenant_key,agent_key,chat_key,root_question_key,session_key,capture_enabled,"
            "started_at,ended_at,status,quality,terminal_fact_source,metadata_text) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                run_id,
                scope["service_unit_id"].encode(),
                scope["source_id"].encode(),
                scope["tenant_id"].encode(),
                scope["agent_id"].encode(),
                business.get("chat_id", "").encode(),
                business.get("root_question_msgid", "").encode(),
                business.get("session_id", "").encode() or None,
                int(bool(event["capture_enabled"])),
                _timestamp(event["run_started_at"]),
                (
                    _timestamp(event["ended_at"])
                    if name == "run.ended" and event.get("ended_at")
                    else None
                ),
                event.get("status") if is_run_fact else None,
                "observed" if name == "run.ended" else "partial",
                event.get("terminal_fact_source") if is_run_fact else None,
                metadata,
            ),
        )
        stored = await session.fetch_one(
            "SELECT service_unit_key,source_key,tenant_key,agent_key,chat_key,root_question_key,session_key,"
            "capture_enabled,ended_at,status FROM swe_agentobs_runs WHERE run_id=%s FOR UPDATE",
            (run_id,),
        )
        if stored is None:
            return False
        if stored["capture_enabled"] != int(bool(event["capture_enabled"])):
            return False
        for column, field in (
            ("service_unit_key", "service_unit_id"),
            ("source_key", "source_id"),
            ("tenant_key", "tenant_id"),
            ("agent_key", "agent_id"),
        ):
            if stored[column] != scope[field].encode():
                return False
        for column, field in (
            ("chat_key", "chat_id"),
            ("root_question_key", "root_question_msgid"),
            ("session_key", "session_id"),
        ):
            value = business.get(field)
            if value and stored[column] != value.encode():
                return False
        if (
            name == "run.ended"
            and stored["ended_at"] is not None
            and stored["status"] != event.get("status")
        ):
            return False
        return True

    async def _project_entity(
        self,
        session,
        event: dict[str, Any],
        metadata: str,
    ) -> None:
        name = event["event_name"]
        scope = event.get("scope") or {}
        run_id = event.get("obs_run_id")
        started = _timestamp(
            event.get("run_started_at")
            or event.get("started_at")
            or event["occurred_at"],
        )
        if run_id:
            business = event.get("business_ref") or {}
            is_run_fact = name.startswith("run.")
            await session.execute(
                "INSERT INTO swe_agentobs_runs "
                "(run_id,service_unit_key,source_key,tenant_key,agent_key,chat_key,root_question_key,session_key,capture_enabled,"
                "started_at,ended_at,status,quality,terminal_fact_source,duration_us,metadata_text) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE status=IF(VALUES(ended_at) IS NOT NULL,COALESCE(VALUES(status),status),"
                "IF(ended_at IS NULL AND VALUES(status) IS NOT NULL AND "
                "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(VALUES(metadata_text),'$.occurred_at')),'') > "
                "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(metadata_text,'$.occurred_at')),''),"
                "VALUES(status),status)),"
                "quality=IF(VALUES(ended_at) IS NOT NULL,VALUES(quality),quality),"
                "terminal_fact_source=IF(VALUES(ended_at) IS NOT NULL,COALESCE(VALUES(terminal_fact_source),terminal_fact_source),terminal_fact_source),"
                "ended_at=COALESCE(ended_at,VALUES(ended_at)),"
                "duration_us=COALESCE(VALUES(duration_us),duration_us),"
                "metadata_text=IF(VALUES(status) IS NOT NULL AND "
                "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(VALUES(metadata_text),'$.occurred_at')),'') > "
                "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(metadata_text,'$.occurred_at')),''),"
                "VALUES(metadata_text),metadata_text)",
                (
                    run_id,
                    scope["service_unit_id"].encode(),
                    scope["source_id"].encode(),
                    scope["tenant_id"].encode(),
                    scope["agent_id"].encode(),
                    business.get("chat_id", "").encode(),
                    business.get("root_question_msgid", "").encode(),
                    business.get("session_id", "").encode() or None,
                    int(bool(event["capture_enabled"])),
                    started,
                    (
                        _timestamp(event["ended_at"])
                        if name == "run.ended" and event.get("ended_at")
                        else None
                    ),
                    event.get("status") if is_run_fact else None,
                    "observed" if name == "run.ended" else "partial",
                    event.get("terminal_fact_source") if is_run_fact else None,
                    (
                        (event.get("metrics") or {}).get("duration_us")
                        if name == "run.ended"
                        else None
                    ),
                    metadata,
                ),
            )
        if (
            name in {"attempt.started", "attempt.ended"}
            and run_id
            and event.get("obs_attempt_id")
        ):
            is_attempt_end = name == "attempt.ended"
            await session.execute(
                "INSERT INTO swe_agentobs_attempts (attempt_id,run_id,attempt_index,predecessor_attempt_id,started_at,ended_at,status,metadata_text) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                "status=IF(VALUES(ended_at) IS NOT NULL,COALESCE(VALUES(status),status),"
                "IF(ended_at IS NULL,COALESCE(VALUES(status),status),status)),"
                "ended_at=COALESCE(ended_at,VALUES(ended_at)),metadata_text=VALUES(metadata_text)",
                (
                    event["obs_attempt_id"],
                    run_id,
                    event.get("attempt_index") or 1,
                    event.get("predecessor_attempt_id"),
                    _timestamp(
                        event.get("started_at")
                        or event.get("attempt_started_at")
                        or event["occurred_at"],
                    ),
                    (
                        _timestamp(event["ended_at"])
                        if is_attempt_end and event.get("ended_at")
                        else None
                    ),
                    event.get("status") if is_attempt_end else "running",
                    metadata,
                ),
            )
        if (
            name in {"span.started", "span.ended"}
            and run_id
            and event.get("obs_span_id")
        ):
            is_end = name == "span.ended"
            has_start = is_end and await session.fetch_one(
                "SELECT 1 FROM swe_agentobs_events "
                "WHERE entity_kind='span' AND entity_id=%s "
                "AND phase_key='started' AND local_seq=0 AND event_name='span.started'",
                (event["obs_span_id"],),
            )
            await session.execute(
                "INSERT INTO swe_agentobs_spans (span_id,run_id,attempt_id,parent_span_id,span_type,operation,kind,name,step_index,"
                "started_at,ended_at,status,technical_status,has_error,error_type,quality,duration_us,time_to_first_chunk_us,"
                "time_to_first_visible_text_us,input_tokens,output_tokens,model_key,provider_key,request_model_key,"
                "response_model_key,model_key_source,streaming,finish_reason,tool_key,metadata_text) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE status=IF(VALUES(ended_at) IS NOT NULL,COALESCE(VALUES(status),status),"
                "IF(ended_at IS NULL,COALESCE(VALUES(status),status),status)),"
                "ended_at=COALESCE(ended_at,VALUES(ended_at)),"
                "technical_status=COALESCE(VALUES(technical_status),technical_status),has_error=COALESCE(VALUES(has_error),has_error),"
                "error_type=COALESCE(VALUES(error_type),error_type),"
                "quality=CASE WHEN VALUES(ended_at) IS NOT NULL THEN VALUES(quality) "
                "WHEN ended_at IS NOT NULL THEN 'observed' ELSE quality END,"
                "duration_us=COALESCE(VALUES(duration_us),duration_us),"
                "time_to_first_chunk_us=COALESCE(VALUES(time_to_first_chunk_us),time_to_first_chunk_us),"
                "time_to_first_visible_text_us=COALESCE(VALUES(time_to_first_visible_text_us),time_to_first_visible_text_us),"
                "input_tokens=COALESCE(VALUES(input_tokens),input_tokens),"
                "output_tokens=COALESCE(VALUES(output_tokens),output_tokens),"
                "model_key=IF(VALUES(ended_at) IS NOT NULL,COALESCE(VALUES(model_key),model_key),model_key),"
                "provider_key=COALESCE(VALUES(provider_key),provider_key),"
                "request_model_key=COALESCE(VALUES(request_model_key),request_model_key),"
                "response_model_key=COALESCE(VALUES(response_model_key),response_model_key),"
                "model_key_source=IF(VALUES(ended_at) IS NOT NULL OR ended_at IS NULL,"
                "COALESCE(VALUES(model_key_source),model_key_source),model_key_source),"
                "streaming=COALESCE(VALUES(streaming),streaming),"
                "finish_reason=COALESCE(VALUES(finish_reason),finish_reason),"
                "tool_key=COALESCE(VALUES(tool_key),tool_key),"
                "metadata_text=IF(VALUES(ended_at) IS NOT NULL OR ended_at IS NULL,"
                "VALUES(metadata_text),metadata_text)",
                (
                    event["obs_span_id"],
                    run_id,
                    event.get("obs_attempt_id"),
                    event.get("parent_obs_span_id"),
                    event.get("span_type", "Custom"),
                    event.get("operation") or "unknown",
                    event.get("kind") or "INTERNAL",
                    event.get("name") or "unknown",
                    event.get("step_index"),
                    _timestamp(
                        event.get("started_at") or event["occurred_at"],
                    ),
                    (
                        _timestamp(event["ended_at"])
                        if is_end and event.get("ended_at")
                        else None
                    ),
                    event.get("status") if is_end else "running",
                    event.get("technical_status"),
                    (
                        int(bool(event["has_error"]))
                        if "has_error" in event
                        else None
                    ),
                    event.get("error_type"),
                    "observed" if has_start else "partial",
                    (event.get("metrics") or {}).get("duration_us"),
                    (event.get("metrics") or {}).get("first_chunk_us"),
                    (event.get("metrics") or {}).get("first_visible_text_us"),
                    (event.get("metrics") or {}).get("input_tokens"),
                    (event.get("metrics") or {}).get("output_tokens"),
                    event.get("response_model_key")
                    or event.get("request_model_key"),
                    event.get("provider_key"),
                    event.get("request_model_key"),
                    event.get("response_model_key"),
                    event.get("model_key_source"),
                    (
                        int(event["streaming"])
                        if isinstance(event.get("streaming"), bool)
                        else None
                    ),
                    event.get("finish_reason"),
                    (event.get("tool") or {}).get("tool_key"),
                    metadata,
                ),
            )
            if is_end:
                await session.execute(
                    "UPDATE swe_agentobs_runs SET "
                    "span_count=(SELECT COUNT(*) FROM swe_agentobs_spans "
                    "WHERE run_id=%s), "
                    "error_span_count=(SELECT COUNT(*) FROM swe_agentobs_spans "
                    "WHERE run_id=%s AND (has_error=1 OR "
                    "technical_status='ERROR')), "
                    "tool_calls=(SELECT COUNT(*) FROM swe_agentobs_spans "
                    "WHERE run_id=%s AND span_type='Tool' "
                    "AND ended_at IS NOT NULL) "
                    "WHERE run_id=%s",
                    (run_id, run_id, run_id, run_id),
                )
            if event.get("span_type") == "LLM" and event.get("operation") in {
                "chat",
                "request_attempt",
            }:
                await session.execute(
                    "UPDATE swe_agentobs_runs SET "
                    "llm_calls=(SELECT NULLIF(COUNT(*),0) FROM swe_agentobs_spans "
                    "WHERE run_id=%s AND span_type='LLM' AND operation='chat'), "
                    "input_tokens=(SELECT SUM(input_tokens) "
                    "FROM swe_agentobs_spans WHERE run_id=%s AND span_type='LLM' "
                    "AND operation='request_attempt' AND ended_at IS NOT NULL), "
                    "output_tokens=(SELECT SUM(output_tokens) "
                    "FROM swe_agentobs_spans WHERE run_id=%s AND span_type='LLM' "
                    "AND operation='request_attempt' AND ended_at IS NOT NULL), "
                    "agent_loops=(SELECT CASE "
                    "WHEN COUNT(*)>0 AND COUNT(*)=COUNT(step_index) "
                    "THEN COUNT(DISTINCT CONCAT("
                    "COALESCE(parent_span_id,attempt_id), ':', step_index)) "
                    "ELSE NULL END FROM swe_agentobs_spans "
                    "WHERE run_id=%s AND span_type='LLM' AND operation='chat' "
                    "AND ended_at IS NOT NULL) "
                    "WHERE run_id=%s",
                    (run_id, run_id, run_id, run_id, run_id),
                )
        if run_id and (
            name == "run.ended"
            or name == "attempt.started"
            or (name == "attempt.ended" and event.get("ended_at"))
        ):
            await _refresh_active_duration(session, run_id)

# -*- coding: utf-8 -*-
"""Bounded, metadata-only JSONL output to stdout."""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from collections import deque
from typing import Any, TextIO
from uuid import uuid4

from .config import EmitterConfig, project_resource
from .models import MetadataEvent, new_id

_HEALTH_INTERVAL_SECONDS = 30.0
_ALLOWED_KEYS = frozenset(
    "schema_version event_id event_name occurred_at producer_id obs_run_id obs_attempt_id obs_span_id parent_obs_span_id entity_kind entity_id phase_key local_seq scope business_ref service_unit_id source_id tenant_id agent_id chat_id session_id channel root_question_msgid current_question_msgid run_started_at attempt_started_at capture_enabled span_type operation kind name step_index started_at ended_at status technical_status has_error reason_code metrics delay_ms quality model tool asset asset_type asset_id asset_version asset_digest asset_source approval_request_id subagent links events predecessor_attempt_id attempt_index terminal_fact_source decision content_ref content_kind saved_state metadata_truncated error_type phase cause_id recovery_id event_kind event_type relation_type duration_us usage_known input_tokens output_tokens total_tokens first_chunk_us first_visible_text_us start_observed end_observed truncated provider_key request_model_key response_model_key model_key_source finish_reason streaming tool_key tool_call_id tool_execution_id executor_kind timeout_budget_ms retry_index exit_code request_attempt_index skill_key agent_key prompt_id prompt_version occurrence_id message_id content_type reference_id business_subagent_key result_status mode execution_agent_key attempt_ordinal retries loop_count tool_count".split(),
) | {"dropped_events", "no_request_attempts", "resource"}
_FORBIDDEN_KEYS = frozenset(
    "prompt messages input output content body text query parameters arguments result exception traceback api_key password secret".split(),
)
_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "event_name",
        "occurred_at",
        "producer_id",
        "entity_kind",
        "entity_id",
        "phase_key",
        "local_seq",
    },
)
_TOKEN_FIELDS = frozenset(
    "event_name producer_id obs_run_id obs_attempt_id obs_span_id parent_obs_span_id entity_kind entity_id phase_key service_unit_id source_id tenant_id agent_id chat_id session_id channel root_question_msgid current_question_msgid span_type operation kind name status technical_status reason_code decision error_type phase provider_key request_model_key response_model_key model_key_source finish_reason tool_key tool_call_id tool_execution_id executor_kind event_type relation_type skill_key agent_key prompt_id prompt_version occurrence_id message_id content_type reference_id business_subagent_key result_status mode execution_agent_key attempt_ordinal terminal_fact_source saved_state asset_type asset_id asset_version asset_digest asset_source".split(),
)
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_.:@/-]{1,256}$")
_ENUM_FIELDS = {
    "span_type": frozenset(
        {
            "Agent",
            "LLM",
            "Tool",
            "Guard",
            "Approval",
            "SubAgent",
            "Skill",
            "Memory",
            "Retrieval",
            "Workflow",
            "Cron",
            "Custom",
        },
    ),
    "status": frozenset(
        {
            "accepted",
            "running",
            "waiting_approval",
            "succeeded",
            "failed",
            "cancelled",
            "timed_out",
            "rejected",
            "partial",
            "unknown",
        },
    ),
    "result_status": frozenset(
        {"completed", "partial", "blocked", "failed", "cancelled"},
    ),
    "technical_status": frozenset({"UNSET", "OK", "ERROR"}),
    "kind": frozenset(
        {"CLIENT", "INTERNAL", "SERVER", "PRODUCER", "CONSUMER"},
    ),
    "executor_kind": frozenset(
        {"agent_tool", "mcp_tool", "unregistered"},
    ),
    "event_kind": frozenset(
        {
            "model.retry_scheduled",
            "model.request_attempt_started",
            "tool.retry_scheduled",
            "tool.recovered",
            "evidence.pending",
            "evidence.linked",
            "link.added",
            "retry",
        },
    ),
    "event_type": frozenset(
        {
            "model.retry_scheduled",
            "model.request_attempt_started",
            "tool.retry_scheduled",
            "tool.recovered",
            "evidence.pending",
            "evidence.linked",
            "link.added",
            "retry",
        },
    ),
    "relation_type": frozenset(
        {"retry_of", "retry_next", "cause", "recovered_by"},
    ),
}
_REGISTERED_LABELS = {
    "agent.execute",
    "approved",
    "chat",
    "denied",
    "superseded",
    "execute",
    "model.call",
    "registered.tool",
    "request_attempt",
    "tool",
    "timed_out",
    "model.request_attempt",
    "shell_policy_denied",
    "policy_denied",
    "hook_denied",
    "hook_stop",
    "hook_waiting_approval",
}


def register_metadata_label(label: str) -> None:
    """Register a trusted code/config label; never pass request text here."""
    if not _TOKEN_PATTERN.fullmatch(label):
        raise ValueError("metadata labels must be short identifier tokens")
    _REGISTERED_LABELS.add(label)


def _project(value: Any, depth: int = 0, key: str | None = None) -> Any:
    if depth > 4:
        return None
    if isinstance(value, dict):
        result = {}
        for child_key, item in value.items():
            normalized = str(child_key).lower()
            if normalized in _FORBIDDEN_KEYS or child_key not in _ALLOWED_KEYS:
                continue
            if child_key == "resource":
                if depth == 0 and (resource := project_resource(item)):
                    result["resource"] = resource
                continue
            result[child_key] = _project(item, depth + 1, str(child_key))
        return result
    if isinstance(value, (list, tuple)):
        if any(not isinstance(item, dict) for item in value[:32]):
            return None
        return [_project(item, depth + 1) for item in value[:32]]
    if isinstance(value, str):
        if key in _TOKEN_FIELDS and not _TOKEN_PATTERN.fullmatch(value):
            return None
        if key in _ENUM_FIELDS and value not in _ENUM_FIELDS[key]:
            return None
        if (
            key in {"name", "operation", "reason_code", "decision"}
            and value not in _REGISTERED_LABELS
        ):
            return None
        return value[:256]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return None


class JsonlEmitter:
    """Single-writer emitter with event and byte limits including in-flight data."""

    def __init__(
        self,
        config: EmitterConfig | None = None,
        *,
        stream: TextIO | None = None,
    ) -> None:
        self.config = config or EmitterConfig.from_environment()
        self._stream = stream or sys.stdout
        self._condition = threading.Condition()
        self._queue: deque[bytes] = deque()
        self._outstanding_bytes = 0
        self._outstanding_events = 0
        self._dropped = 0
        self._observer_id = new_id()
        self._health_seq = 0
        self._closed = False
        self._thread = threading.Thread(
            target=self._write_loop,
            name="agentobs-jsonl",
            daemon=True,
        )
        self._thread.start()

    @property
    def dropped_count(self) -> int:
        with self._condition:
            return self._dropped

    def emit(self, event: MetadataEvent | dict[str, Any]) -> bool:
        try:
            raw = (
                event.to_dict() if isinstance(event, MetadataEvent) else event
            )
            projected = _project(raw)
            if not isinstance(projected, dict):
                raise ValueError("event must be an object")
            if not _REQUIRED_FIELDS.issubset(projected):
                raise ValueError("event is missing required protocol fields")
            if projected["schema_version"] != 1:
                raise ValueError("unsupported event schema")
            if projected["event_name"] != "observer.health":
                if not {
                    "obs_run_id",
                    "run_started_at",
                    "capture_enabled",
                    "scope",
                }.issubset(projected):
                    raise ValueError("run events require identity and scope")
                scope = projected["scope"]
                if not isinstance(scope, dict) or not all(
                    scope.get(key)
                    for key in (
                        "service_unit_id",
                        "source_id",
                        "tenant_id",
                        "agent_id",
                    )
                ):
                    raise ValueError("run event scope is incomplete")
            payload = (
                json.dumps(
                    projected,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
            if len(payload) > self.config.max_event_bytes:
                projected["metadata_truncated"] = True
                for key in (
                    "links",
                    "events",
                    "resource",
                    "asset",
                    "model",
                    "tool",
                    "subagent",
                    "metrics",
                    "quality",
                    "business_ref",
                ):
                    if key in _REQUIRED_FIELDS:
                        continue
                    projected.pop(key, None)
                    payload = (
                        json.dumps(
                            projected,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode("utf-8")
                        + b"\n"
                    )
                    if len(payload) <= self.config.max_event_bytes:
                        break
            if len(payload) > self.config.max_event_bytes:
                self._drop()
                return False
        except Exception:
            self._drop()
            return False
        with self._condition:
            if (
                self._closed
                or self._outstanding_events >= self.config.max_queue_events
                or self._outstanding_bytes + len(payload)
                > self.config.max_queue_bytes
            ):
                self._dropped += 1
                self._condition.notify()
                return False
            self._queue.append(payload)
            self._outstanding_events += 1
            self._outstanding_bytes += len(payload)
            self._condition.notify()
            return True

    def emit_raw(self, line: bytes) -> bool:
        """Validate and forward one child event through the metadata emitter."""
        if (
            not isinstance(line, (bytes, bytearray))
            or not line
            or len(line) > self.config.max_event_bytes
        ):
            self._drop()
            return False
        try:
            from .projector import _validate_payload

            raw = line.decode("utf-8")
            if "\n" in raw.removesuffix("\n") or "\r" in raw:
                raise ValueError("worker event is not a single JSONL record")
            event = _validate_payload(raw)
        except (TypeError, ValueError, RecursionError):
            self._drop()
            return False
        return self.emit(event)

    def _drop(self) -> None:
        with self._condition:
            self._dropped += 1
            self._condition.notify()

    def _health_payload(self, dropped: int) -> bytes:
        try:
            event = MetadataEvent(
                event_name="observer.health",
                obs_context=None,
                entity_kind="observer",
                entity_id=self._observer_id,
                phase_key="health",
                local_seq=self._health_seq,
                attributes={"metrics": {"dropped_events": dropped}},
            )
            projected = _project(event.to_dict())
            limit = min(
                self.config.max_event_bytes,
                self.config.max_queue_bytes,
            )
            for _ in range(2):
                payload = (
                    json.dumps(projected, separators=(",", ":")).encode(
                        "utf-8",
                    )
                    + b"\n"
                )
                if len(payload) <= limit:
                    self._health_seq += 1
                    return payload
                if projected.pop("resource", None) is None:
                    break
        except Exception:
            return b""
        return b""

    def _write_loop(self) -> None:
        payload = b""
        health_count = None
        final_health = False
        written = False
        reported = 0
        next_health_at = 0.0
        while True:
            with self._condition:
                if payload:
                    self._outstanding_events -= 1
                    self._outstanding_bytes -= len(payload)
                    self._condition.notify_all()
                    if health_count is not None:
                        if written:
                            reported = health_count
                        next_health_at = (
                            time.monotonic() + _HEALTH_INTERVAL_SECONDS
                        )
                    elif not written:
                        self._dropped += 1
                if final_health:
                    return
                payload, health_count = self._next_write(
                    reported,
                    next_health_at,
                )
                if not payload:
                    return
                final_health = self._closed and health_count is not None
            written = False
            try:
                self._stream.write(payload.decode("utf-8"))
                self._stream.flush()
            except Exception:
                pass
            else:
                written = True

    def _next_write(
        self,
        reported: int,
        next_health_at: float,
    ) -> tuple[bytes, int | None]:
        """Select the next record with the condition lock held."""
        while True:
            delay = None
            if self._dropped > reported:
                delay = next_health_at - time.monotonic()
                final = self._closed and not self._queue
                due = not self._closed and delay <= 0
                if (final or due) and (
                    self._outstanding_events < self.config.max_queue_events
                ):
                    health = self._health_payload(self._dropped)
                    if health and (
                        self._outstanding_bytes + len(health)
                        <= self.config.max_queue_bytes
                    ):
                        self._outstanding_events += 1
                        self._outstanding_bytes += len(health)
                        return health, self._dropped
                    delay = _HEALTH_INTERVAL_SECONDS
            if self._queue:
                return self._queue.popleft(), None
            if self._closed:
                return b"", None
            self._condition.wait(delay)

    def close(self, timeout: float | None = None) -> int:
        wait_for = (
            self.config.shutdown_timeout
            if timeout is None
            else min(timeout, 1.0)
        )
        deadline = time.monotonic() + min(wait_for, 1.0)
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            while self._outstanding_events and time.monotonic() < deadline:
                self._condition.wait(deadline - time.monotonic())
            remaining = self._outstanding_events
        self._thread.join(max(0.0, deadline - time.monotonic()))
        return remaining


_DEFAULT_EMITTER: JsonlEmitter | None = None
_DEFAULT_LOCK = threading.Lock()


def default_emitter() -> JsonlEmitter:
    global _DEFAULT_EMITTER
    with _DEFAULT_LOCK:
        if _DEFAULT_EMITTER is None:
            _DEFAULT_EMITTER = JsonlEmitter()
        return _DEFAULT_EMITTER


def shutdown_default_emitter() -> int:
    """Drain the lazily-created writer during application shutdown."""
    with _DEFAULT_LOCK:
        emitter = _DEFAULT_EMITTER
    return emitter.close() if emitter is not None else 0

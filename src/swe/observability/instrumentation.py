# -*- coding: utf-8 -*-
"""Small business-boundary instrumentation facade."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal

from ..utils.close_aware_stream import CloseAwareStream
from .config import capture_enabled
from .context import bind_context, current_context
from .emitter import JsonlEmitter, _TOKEN_PATTERN, default_emitter
from .models import (
    MetadataEvent,
    ObservabilityContext,
    new_id,
    producer_id,
    utc_now,
)


class SpanHandle:
    def __init__(
        self,
        *,
        emitter: JsonlEmitter,
        context: ObservabilityContext,
        span_id: str,
        parent_span_id: str | None,
        span_type: str,
        operation: str,
        name: str,
        attributes: dict[str, Any],
    ) -> None:
        self._emitter = emitter
        self._context = context.with_updates(parent_obs_span_id=parent_span_id)
        self.span_id = span_id
        self._started_at = utc_now()
        self._started_clock = time.monotonic_ns()
        self._base = {
            "obs_span_id": span_id,
            "span_type": span_type,
            "operation": operation,
            "name": name,
            "kind": "INTERNAL",
            **{
                key: value
                for key, value in attributes.items()
                if key
                not in {
                    "obs_run_id",
                    "obs_attempt_id",
                    "obs_span_id",
                    "parent_obs_span_id",
                    "event_name",
                    "entity_id",
                    "entity_kind",
                    "phase_key",
                    "local_seq",
                    "span_type",
                    "operation",
                    "name",
                }
            },
        }
        self._ended = False
        self._context_binding = None
        self._event_sequence = 0
        self._compacted_tool_output_ids: set[str] = set()

    def event(self, name: str, **attributes: Any) -> bool:
        if self._ended and name not in {"evidence.pending", "evidence.linked"}:
            return False
        local_seq = self._event_sequence
        self._event_sequence += 1
        return self._emitter.emit(
            MetadataEvent(
                event_name="span.event",
                obs_context=self._context,
                entity_kind="span",
                entity_id=self.span_id,
                phase_key="event",
                local_seq=local_seq,
                attributes={
                    **self._base,
                    **attributes,
                    "event_type": name[:64],
                },
            ),
        )

    def mark_tool_output_compacted(self, message_id: str) -> bool:
        if self._ended or not isinstance(message_id, str) or not message_id:
            return False
        self._compacted_tool_output_ids.add(message_id)
        return True

    def tool_output_was_compacted(self, message_id: str) -> bool:
        return message_id in self._compacted_tool_output_ids

    def asset_used(
        self,
        asset_type: str,
        asset_id: str,
        *,
        source: str,
        version: str | None = None,
        digest: str | None = None,
    ) -> bool:
        if self._ended:
            return False
        asset = {
            "asset_type": asset_type,
            "asset_id": asset_id,
            "asset_source": source,
        }
        if version:
            asset["asset_version"] = version
        if digest:
            asset["asset_digest"] = digest
        return self._emitter.emit(
            MetadataEvent(
                event_name="asset.used",
                obs_context=self._context.with_updates(
                    parent_obs_span_id=self.span_id,
                ),
                entity_kind="asset",
                entity_id=new_id(),
                phase_key="used",
                local_seq=0,
                attributes={"asset": asset},
            ),
        )

    def end(self, *, status: str, **attributes: Any) -> bool:
        if self._ended:
            return False
        self._ended = True
        ended_at = utc_now()
        elapsed_us = max(
            0,
            (time.monotonic_ns() - self._started_clock) // 1000,
        )
        supplied_metrics = attributes.get("metrics")
        metrics = {"duration_us": elapsed_us}
        if isinstance(supplied_metrics, dict):
            metrics.update(supplied_metrics)
        metrics["duration_us"] = elapsed_us
        optional = {
            key: value
            for key, value in attributes.items()
            if key
            not in {
                "obs_run_id",
                "obs_attempt_id",
                "obs_span_id",
                "parent_obs_span_id",
                "event_name",
                "entity_id",
                "entity_kind",
                "phase_key",
                "local_seq",
                "started_at",
                "ended_at",
                "status",
                "metrics",
                "quality",
            }
        }
        return self._emitter.emit(
            MetadataEvent(
                event_name="span.ended",
                obs_context=self._context,
                entity_kind="span",
                entity_id=self.span_id,
                phase_key="ended",
                local_seq=0,
                attributes={
                    **self._base,
                    "started_at": self._started_at,
                    "ended_at": ended_at,
                    "status": status,
                    "metrics": metrics,
                    "quality": {"start_observed": True, "end_observed": True},
                    **optional,
                },
            ),
        )

    def start(self) -> bool:
        return self._emitter.emit(
            MetadataEvent(
                event_name="span.started",
                obs_context=self._context,
                entity_kind="span",
                entity_id=self.span_id,
                phase_key="started",
                local_seq=0,
                attributes={**self._base, "started_at": self._started_at},
            ),
        )

    def to_carrier(self) -> dict[str, Any]:
        """Transfer this started Span's end ownership across a worker boundary."""
        return {
            "schema_version": 1,
            "context": self._context.to_carrier(),
            "obs_span_id": self.span_id,
            "parent_obs_span_id": self._context.parent_obs_span_id,
            "started_at": self._started_at,
            "attributes": dict(self._base),
        }

    def __enter__(self) -> "SpanHandle":
        self._context_binding = bind_context(
            self._context.with_updates(parent_obs_span_id=self.span_id),
        )
        self._context_binding.__enter__()
        return self

    def __exit__(self, exc_type, exc, _tb) -> Literal[False]:
        if not self._ended:
            self.end(
                status=(
                    "cancelled"
                    if exc_type
                    and issubclass(
                        exc_type,
                        (asyncio.CancelledError, KeyboardInterrupt),
                    )
                    else "failed" if exc_type else "succeeded"
                ),
                has_error=bool(exc_type),
                error_type=exc_type.__name__[:96] if exc_type else None,
            )
        if self._context_binding is not None:
            self._context_binding.__exit__(exc_type, exc, _tb)
            self._context_binding = None
        return False


_current_model_call: ContextVar[_ModelCall | None] = ContextVar(
    "agentobs_current_model_call",
    default=None,
)
_current_tool_span: ContextVar[SpanHandle | None] = ContextVar(
    "agentobs_current_tool_span",
    default=None,
)
_pending_model_response_spans: ContextVar[
    dict[tuple[str, str], SpanHandle | None] | None
] = ContextVar(
    "agentobs_pending_model_response_spans",
    default=None,
)


def attach_evidence_capture(memory: Any) -> None:
    """Observe message additions on a default memory without adding an archive."""
    if memory is None or getattr(memory, "_chat_checkpoint_chat_id", None):
        return
    if getattr(memory, "_agentobs_evidence_capture", False):
        return
    original_add = getattr(memory, "add", None)
    if not callable(original_add):
        return

    async def add(*args: Any, **kwargs: Any) -> Any:
        messages = (
            args[0] if args else kwargs.get("memories", kwargs.get("message"))
        )
        for message in messages if isinstance(messages, list) else [messages]:
            try:
                mark_pending_message_evidence(message)
            except Exception:
                pass
        result = original_add(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    memory.add = add
    memory._agentobs_evidence_capture = True


@contextmanager
def bind_tool_span(span: SpanHandle | None):
    """Expose the active Tool Span to durable message publication hooks."""
    token = _current_tool_span.set(span)
    try:
        yield
    finally:
        _current_tool_span.reset(token)


def emit_tool_output_evidence(
    *,
    chat_id: str,
    message_id: str,
    saved_state: str = "available",
    occurrence_id: int | None = None,
    reference_id: str | None = None,
) -> bool:
    """Record pending or committed tool-result evidence on its active Span."""
    span = _current_tool_span.get()
    if span is None:
        return False
    try:
        attributes: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "content_type": "tool_output",
            "saved_state": saved_state,
        }
        if type(occurrence_id) is int and occurrence_id >= 0:
            attributes["occurrence_id"] = occurrence_id
        if reference_id:
            attributes["reference_id"] = reference_id
        return span.event(
            (
                "evidence.pending"
                if saved_state == "pending"
                else "evidence.linked"
            ),
            **attributes,
        )
    except Exception:
        return False


def mark_tool_output_compacted(message_id: str) -> bool:
    """Mark a durable Tool result whose saved display was compacted."""
    span = _current_tool_span.get()
    if span is None:
        return False
    try:
        return span.mark_tool_output_compacted(message_id)
    except Exception:
        return False


def tool_output_was_compacted(message_id: str) -> bool:
    span = _current_tool_span.get()
    return bool(span and span.tool_output_was_compacted(message_id))


def emit_tool_asset_used(
    *,
    asset_type: str,
    asset_id: str,
    source: str,
    version: str | None = None,
    digest: str | None = None,
) -> bool:
    """Attribute an immutable asset snapshot to its active Tool Span."""
    span = _current_tool_span.get()
    if span is None:
        return False
    try:
        return span.asset_used(
            asset_type,
            asset_id,
            source=source,
            version=version,
            digest=digest,
        )
    except Exception:
        return False


def _remember_model_response_span(
    span: SpanHandle,
    response_id: Any,
) -> None:
    if not isinstance(response_id, str) or not response_id:
        return
    key = (span._context.obs_run_id, response_id)
    pending = dict(_pending_model_response_spans.get() or {})
    if key in pending:
        pending[key] = None
    else:
        pending[key] = span
    while len(pending) > 32:
        pending.pop(next(iter(pending)))
    _pending_model_response_spans.set(pending)


def emit_assistant_answer_evidence(
    *,
    chat_id: str,
    message_id: str,
    invocation_id: str,
    has_tool_use: bool,
    has_visible_answer: bool,
    saved_state: str = "available",
    occurrence_id: int | None = None,
    reference_id: str | None = None,
) -> bool:
    """Record answer evidence on its exact invocation before or after commit."""
    context = current_context()
    if context is None or not invocation_id:
        return False
    key = (context.obs_run_id, invocation_id)
    pending = dict(_pending_model_response_spans.get() or {})
    if key not in pending:
        return False
    span = pending.get(key)
    if (
        span is None
        or has_tool_use
        or not has_visible_answer
        or span._context.business_ref.get("chat_id") != chat_id
    ):
        return False
    if saved_state == "available":
        pending.pop(key)
        _pending_model_response_spans.set(pending)
    try:
        attributes: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "content_type": "assistant_answer",
            "saved_state": saved_state,
        }
        if type(occurrence_id) is int and occurrence_id >= 0:
            attributes["occurrence_id"] = occurrence_id
        if reference_id:
            attributes["reference_id"] = reference_id
        return span.event(
            (
                "evidence.pending"
                if saved_state == "pending"
                else "evidence.linked"
            ),
            **attributes,
        )
    except Exception:
        return False


def mark_pending_message_evidence(message: Any) -> None:
    """Attach a unique locator to the concrete message before memory accepts it."""
    context = current_context()
    if context is None or not context.capture_enabled:
        return
    chat_id = context.business_ref.get("chat_id")
    message_id = getattr(message, "id", None)
    if not isinstance(chat_id, str) or not isinstance(message_id, str):
        return
    blocks = getattr(message, "content", None)
    if not isinstance(blocks, list):
        return
    existing_metadata = getattr(message, "metadata", None)
    if (
        isinstance(existing_metadata, dict)
        and "agent_observability_evidence_ref" in existing_metadata
    ):
        return
    reference_id = new_id()
    role = getattr(message, "role", None)
    if role == "system" and any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in blocks
    ):
        span = _current_tool_span.get()
        content_type = "tool_output"
    elif role == "assistant":
        if any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in blocks
        ) or not any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and bool(block.get("text"))
            for block in blocks
        ):
            return
        invocation = getattr(message, "invocation_id", "") or ""
        span = (_pending_model_response_spans.get() or {}).get(
            (context.obs_run_id, invocation),
        )
        content_type = "assistant_answer"
    else:
        return
    if span is None or span._context.business_ref.get("chat_id") != chat_id:
        return
    from .emitter import _project
    from .outbox import sign_event
    from .outbox_recovery import scope_registered

    def not_saved() -> None:
        span.event(
            "evidence.linked",
            chat_id=chat_id,
            message_id=message_id,
            content_type=content_type,
            saved_state="not_saved",
            reference_id=reference_id,
        )

    if not scope_registered(
        context.scope.to_dict(),
        context.workspace_binding,
    ):
        not_saved()
        return

    sequence = span._event_sequence
    attributes = {
        **span._base,
        "chat_id": chat_id,
        "message_id": message_id,
        "content_type": content_type,
        "reference_id": reference_id,
    }
    pending = MetadataEvent(
        event_name="span.event",
        obs_context=span._context,
        entity_kind="span",
        entity_id=span.span_id,
        phase_key="event",
        local_seq=sequence,
        attributes={
            **attributes,
            "event_type": "evidence.pending",
            "saved_state": "pending",
        },
    )
    linked = MetadataEvent(
        event_name="span.event",
        obs_context=span._context,
        entity_kind="span",
        entity_id=span.span_id,
        phase_key="event",
        local_seq=sequence + 1,
        attributes={
            **attributes,
            "event_type": "evidence.linked",
            "saved_state": "available",
        },
    )
    try:
        record = sign_event(_project(linked.to_dict()))
    except (ValueError, TypeError):
        not_saved()
        return
    metadata = getattr(message, "metadata", None)
    if metadata is None:
        metadata = {}
        message.metadata = metadata
    if not isinstance(metadata, dict):
        not_saved()
        return
    metadata["agent_observability_evidence_ref"] = reference_id
    metadata["agent_observability_outbox"] = record
    span._event_sequence += 2
    span._emitter.emit(_project(pending.to_dict()))


_RUN_TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "timed_out", "rejected"},
)
_run_state_sequences: dict[str, int] = {}
_run_state_sequence_lock = threading.Lock()


def _next_run_state_sequence(run_id: str) -> int:
    """为同一 Run 的状态事件分配单调槽位，避免重复状态被判冲突丢弃。"""
    with _run_state_sequence_lock:
        sequence = _run_state_sequences.get(run_id, 0) + 1
        _run_state_sequences[run_id] = sequence
        return sequence


def _clear_run_state_sequence(run_id: str) -> None:
    with _run_state_sequence_lock:
        _run_state_sequences.pop(run_id, None)


_current_agent_step_index: ContextVar[int | None] = ContextVar(
    "agentobs_current_agent_step_index",
    default=None,
)


@contextmanager
def bind_agent_step_index(step_index: int):
    token = _current_agent_step_index.set(step_index)
    try:
        yield
    finally:
        _current_agent_step_index.reset(token)


def current_agent_step_index() -> int | None:
    return _current_agent_step_index.get()


class _ModelRequest:
    def __init__(self, span: SpanHandle) -> None:
        self.span = span
        self.streaming = bool(span._base["streaming"])
        self.metrics: dict[str, Any] = {}
        self.response: dict[str, Any] = {}
        self.saw_openai_response = False
        self.output_frame = True

    def observe_output(self, value: Any) -> None:
        if not self.streaming or not self.output_frame:
            return
        if "first_visible_text_us" in self.metrics:
            return
        visible_text = _has_visible_model_text(value)
        if not visible_text and not _has_model_output(value):
            return
        elapsed = max(
            0, (time.monotonic_ns() - self.span._started_clock) // 1000
        )
        self.metrics.setdefault("first_chunk_us", elapsed)
        if visible_text:
            self.metrics.setdefault("first_visible_text_us", elapsed)

    def observe_usage(self, usage: dict[str, int]) -> None:
        self.metrics.update(usage)
        if usage:
            self.metrics["usage_known"] = all(
                name in self.metrics
                for name in ("input_tokens", "output_tokens")
            )

    def end(self, **attributes: Any) -> None:
        self.span.end(
            metrics=self.metrics,
            **{"model_key_source": "request_fallback", **self.response},
            **attributes,
        )


class _ModelCall:
    def __init__(self, handle: SpanHandle, count_requests: bool) -> None:
        self.handle = handle
        self.count_requests = count_requests
        self.request: _ModelRequest | None = None
        self.started_requests = 0
        self.metrics: dict[str, Any] = {}

    @contextmanager
    def bind(self):
        token = _current_model_call.set(self)
        try:
            yield
        finally:
            _current_model_call.reset(token)

    def observe_response(self, value: Any) -> None:
        try:
            usage = _usage_metrics(value)
            if self.request is not None and not self.request.span._ended:
                if not self.request.saw_openai_response:
                    self.request.observe_usage(usage)
                self.request.observe_output(value)
            elif not self.count_requests:
                self.metrics.update(usage)
                if usage:
                    self.metrics["usage_known"] = all(
                        name in self.metrics
                        for name in ("input_tokens", "output_tokens")
                    )
        except Exception:
            # Evidence capture must not affect the model response.
            pass

    def end(self, **attributes: Any) -> None:
        if self.request is not None:
            self.request.end(**attributes)
            self.metrics.update(
                {
                    name: self.request.metrics[name]
                    for name in (
                        "input_tokens",
                        "output_tokens",
                        "usage_known",
                    )
                    if name in self.request.metrics
                },
            )
        elif self.count_requests and self.started_requests == 0:
            attributes["no_request_attempts"] = True
        self.handle.end(metrics=self.metrics, **attributes)


def record_model_retry(
    attempt: int,
    error_type: str,
    delay_seconds: float,
) -> None:
    call = _current_model_call.get()
    if call is None:
        return
    if call.request is not None:
        call.request.end(
            status="failed",
            technical_status="ERROR",
            has_error=True,
            error_type=error_type,
        )
    call.handle.event(
        "model.retry_scheduled",
        retries=attempt,
        error_type=error_type,
        metrics={"delay_ms": max(0, int(delay_seconds * 1000))},
    )


def record_model_request_attempt(
    attempt: int | None,
    *,
    streaming: bool,
    request_model: str | None = None,
) -> None:
    call = _current_model_call.get()
    if call is None:
        return
    call.started_requests += 1
    logical_span = call.handle
    previous = call.request
    attributes: dict[str, Any] = {
        "request_attempt_index": (
            attempt if attempt is not None else call.started_requests
        ),
        "streaming": streaming,
    }
    if previous is not None:
        previous.end(
            status="failed",
            technical_status="ERROR",
            has_error=True,
            error_type="model_retry",
            phase="model_request",
        )
        attributes["links"] = [
            {
                "relation_type": "retry_of",
                "obs_span_id": previous.span.span_id,
            },
        ]
    for key in ("provider_key", "request_model_key", "step_index"):
        value = logical_span._base.get(key)
        if value is not None:
            attributes[key] = value
    if request_model is not None:
        attributes["request_model_key"] = request_model
    with bind_context(logical_span._context):
        span = Observability().span(
            "LLM",
            "request_attempt",
            name="model.request_attempt",
            kind="CLIENT",
            parent_span_id=logical_span.span_id,
            **attributes,
        )
    if span is not None:
        call.request = _ModelRequest(span)


def record_openai_request(*, streaming: bool, request_model: str) -> None:
    record_model_request_attempt(
        None,
        streaming=streaming,
        request_model=request_model,
    )


def observe_openai_response(value: Any) -> None:
    """Read safe request facts before the SDK normalizes the response."""
    call = _current_model_call.get()
    request = call.request if call is not None else None
    if request is None or request.span._ended:
        return
    try:
        request.saw_openai_response = True
        model = getattr(value, "model", None)
        if isinstance(model, str) and _TOKEN_PATTERN.fullmatch(model):
            request.response["response_model_key"] = model
            request.response["model_key_source"] = "response"
        usage = getattr(value, "usage", None)
        request.observe_usage(
            _token_counts(usage, "prompt_tokens", "completion_tokens"),
        )
        choices = getattr(value, "choices", None)
        request.output_frame = bool(choices)
        if request.output_frame:
            choice = choices[0]
            delta = getattr(choice, "delta", None)
            if delta is not None:
                request.output_frame = bool(
                    getattr(delta, "content", None)
                    or getattr(delta, "reasoning_content", None)
                    or getattr(delta, "reasoning", None)
                    or getattr(delta, "tool_calls", None)
                    or getattr(delta, "audio", None)
                )
            finish_reason = getattr(choice, "finish_reason", None)
            if isinstance(finish_reason, str) and finish_reason in {
                "stop",
                "length",
                "tool_calls",
                "content_filter",
                "function_call",
            }:
                request.response["finish_reason"] = finish_reason
    except Exception:
        pass


def record_model_response_observation(value: Any) -> None:
    """Observe the response before retry policy can reject or replace it."""
    call = _current_model_call.get()
    if call is not None:
        call.observe_response(value)


class ObservabilityModelWrapper:
    """Record one logical model call without retaining model payloads."""

    def __init__(
        self,
        model: Any,
        *,
        provider_key: str,
        model_key: str,
        count_requests: bool = False,
    ) -> None:
        self._count_requests = count_requests
        self._model = model
        self._provider_key = provider_key
        self._model_key = model_key

    def __getattr__(self, name: str) -> Any:
        return getattr(self._model, name)

    @property
    def stream(self):
        return self._model.stream

    @stream.setter
    def stream(self, value):
        self._model.stream = value

    async def __call__(self, messages, *args, **kwargs):
        context = current_context()
        if context is None or not context.capture_enabled:
            return await self._model(messages, *args, **kwargs)
        attributes = {
            "provider_key": self._provider_key,
            "request_model_key": self._model_key,
            "streaming": bool(getattr(self._model, "stream", False)),
        }
        step_index = current_agent_step_index()
        if step_index is not None:
            attributes["step_index"] = step_index
        handle = Observability().span(
            "LLM",
            "chat",
            name="model.call",
            kind="CLIENT",
            **attributes,
        )
        if handle is None:
            return await self._model(messages, *args, **kwargs)
        handle.asset_used(
            "model",
            f"{self._provider_key}/{self._model_key}",
            source="runtime_resolved",
            version="unknown",
        )
        try:
            prompt_digest = _system_prompt_digest(messages)
        except Exception:
            prompt_digest = None
        if prompt_digest is not None:
            handle.asset_used(
                "prompt",
                "system_prompt",
                source="runtime_model_request",
                version="unknown",
                digest=prompt_digest,
            )
        chat_id = context.business_ref.get("chat_id")
        question_id = context.business_ref.get("root_question_msgid")
        if isinstance(chat_id, str) and isinstance(question_id, str):
            handle.event(
                "evidence.linked",
                chat_id=chat_id,
                message_id=question_id,
                content_type="user_question",
                saved_state="available",
            )
        if isinstance(chat_id, str):
            handle.event(
                "evidence.linked",
                chat_id=chat_id,
                content_type="model_input",
                saved_state="not_saved",
                reference_id=handle.span_id,
            )
        call = _ModelCall(handle, self._count_requests)
        try:
            with call.bind():
                result = await self._model(messages, *args, **kwargs)
            if _is_async_iterable(result):

                async def close_unstarted() -> None:
                    call.end(status="cancelled")
                    await self._close_stream(result)

                return CloseAwareStream(
                    self._consume_stream(result, call),
                    close_unstarted,
                )
            call.observe_response(result)
            _remember_model_response_span(handle, getattr(result, "id", None))
            call.end(status="succeeded")
            return result
        except asyncio.CancelledError:
            call.end(status="cancelled")
            raise
        except Exception as exc:
            call.end(
                status="failed",
                technical_status="ERROR",
                has_error=True,
                error_type=type(exc).__name__,
            )
            raise

    async def _consume_stream(self, stream, call: _ModelCall):
        response_id = None
        try:
            iterator = aiter(stream)
            while True:
                try:
                    with call.bind():
                        chunk = await anext(iterator)
                except StopAsyncIteration:
                    break
                response_id = getattr(chunk, "id", None) or response_id
                call.observe_response(chunk)
                self._observe_logical_latency(call, chunk)
                yield chunk
            _remember_model_response_span(call.handle, response_id)
            call.end(status="succeeded")
        except (asyncio.CancelledError, GeneratorExit):
            call.end(status="cancelled")
            raise
        except Exception as exc:
            call.end(
                status="failed",
                technical_status="ERROR",
                has_error=True,
                error_type=type(exc).__name__,
            )
            raise
        finally:
            with call.bind():
                await self._close_stream(stream)

    @staticmethod
    async def _close_stream(stream: Any) -> None:
        close = getattr(stream, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass

    @staticmethod
    def _observe_logical_latency(call: _ModelCall, chunk: Any) -> None:
        try:
            if not _has_model_output(chunk):
                return
            elapsed = max(
                0,
                (time.monotonic_ns() - call.handle._started_clock) // 1000,
            )
            call.metrics.setdefault("first_chunk_us", elapsed)
            if _has_visible_model_text(chunk):
                call.metrics.setdefault("first_visible_text_us", elapsed)
        except Exception:
            pass


def _has_model_output(value: Any) -> bool:
    content = _get_field(value, "content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, (list, tuple)):
        return False
    for block in content:
        block_type = _get_field(block, "type")
        fields = {
            "text": ("text",),
            "thinking": ("thinking",),
            "reasoning": ("reasoning", "text"),
            "tool_use": ("name", "raw_input", "input"),
            "audio": ("source", "data"),
        }.get(block_type, ())
        if any(
            field.strip() if isinstance(field, str) else bool(field)
            for name in fields
            if (field := _get_field(block, name)) is not None
        ):
            return True
    return False


def _has_visible_model_text(value: Any) -> bool:
    content = _get_field(value, "content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, (list, tuple)):
        return False
    return any(
        _get_field(block, "type") == "text"
        and isinstance(text := _get_field(block, "text"), str)
        and bool(text.strip())
        for block in content
    )


def _is_async_iterable(value: Any) -> bool:
    try:
        return hasattr(value, "__aiter__")
    except Exception:
        return False


def _get_field(value: Any, name: str) -> Any:
    return (
        value.get(name)
        if isinstance(value, dict)
        else getattr(value, name, None)
    )


def _token_counts(
    usage: Any,
    input_field: str,
    output_field: str,
) -> dict[str, int]:
    metrics = {}
    for field, name in (
        (input_field, "input_tokens"),
        (output_field, "output_tokens"),
    ):
        count = _get_field(usage, field)
        if type(count) is int and count >= 0:
            metrics[name] = count
    return metrics


def _usage_metrics(value: Any) -> dict[str, int]:
    return _token_counts(
        _get_field(value, "usage"), "input_tokens", "output_tokens"
    )


def _system_prompt_digest(messages: Any) -> str | None:
    if not isinstance(messages, (list, tuple)):
        return None
    digest = hashlib.sha256()
    found = False
    for message in messages:
        role = (
            message.get("role")
            if isinstance(message, dict)
            else getattr(message, "role", None)
        )
        if role != "system":
            continue
        content = (
            message.get("content")
            if isinstance(message, dict)
            else getattr(message, "content", None)
        )
        if not isinstance(content, str):
            return None
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError:
            return None
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        found = True
    return f"sha256:{digest.hexdigest()}" if found else None


class Observability:
    """Independent facade; disabled calls avoid copying attributes or payloads."""

    def __init__(self, emitter: JsonlEmitter | None = None) -> None:
        self._provided_emitter = emitter

    def _emitter_for(
        self,
        context: ObservabilityContext,
    ) -> JsonlEmitter | None:
        if not context.capture_enabled:
            return None
        try:
            return self._provided_emitter or default_emitter()
        except Exception:
            return None

    def asset_used(
        self,
        asset_type: str,
        asset_id: str,
        *,
        source: str,
        digest: str | None = None,
    ) -> bool:
        context = current_context()
        if context is None:
            return False
        emitter = self._emitter_for(context)
        if emitter is None:
            return False
        asset = {
            "asset_type": asset_type,
            "asset_id": asset_id,
            "asset_source": source,
        }
        if digest:
            asset["asset_digest"] = digest
        return emitter.emit(
            MetadataEvent(
                event_name="asset.used",
                obs_context=context,
                entity_kind="asset",
                entity_id=new_id(),
                phase_key="used",
                local_seq=0,
                attributes={"asset": asset},
            ),
        )

    def admit_run(
        self,
        *,
        scope,
        business_ref: dict[str, str] | None = None,
        enabled: bool | None = None,
    ) -> ObservabilityContext:
        context = self.prepare_run(
            scope=scope,
            business_ref=business_ref,
            enabled=enabled,
        )
        self.accept_run(context)
        return context

    def prepare_run(
        self,
        *,
        scope,
        business_ref: dict[str, str] | None = None,
        enabled: bool | None = None,
    ) -> ObservabilityContext:
        return ObservabilityContext.new_run(
            capture_enabled=capture_enabled() if enabled is None else enabled,
            scope=scope,
            business_ref=business_ref,
        )

    def accept_run(self, context: ObservabilityContext) -> bool:
        emitter = self._emitter_for(context)
        if emitter is not None:
            accepted = emitter.emit(
                MetadataEvent(
                    event_name="run.accepted",
                    obs_context=context,
                    entity_kind="run",
                    entity_id=context.obs_run_id,
                    phase_key="accepted",
                    local_seq=0,
                    attributes={
                        "started_at": context.run_started_at,
                        "status": "accepted",
                    },
                ),
            )
            emitter.emit(
                MetadataEvent(
                    event_name="asset.used",
                    obs_context=context,
                    entity_kind="asset",
                    entity_id=new_id(),
                    phase_key="used",
                    local_seq=0,
                    attributes={
                        "asset": {
                            "asset_type": "agent",
                            "asset_id": context.scope.agent_id,
                            "asset_version": "unknown",
                            "asset_source": "runtime_identity",
                        },
                    },
                ),
            )
            return accepted
        return False

    def start_attempt(
        self,
        context: ObservabilityContext,
    ) -> ObservabilityContext:
        attempt = context.new_attempt()
        emitter = self._emitter_for(attempt)
        if emitter is not None:
            emitter.emit(
                MetadataEvent(
                    event_name="attempt.started",
                    obs_context=attempt,
                    entity_kind="attempt",
                    entity_id=attempt.obs_attempt_id or "",
                    phase_key="started",
                    local_seq=0,
                    attributes={"started_at": attempt.attempt_started_at},
                ),
            )
        return attempt

    def span(
        self,
        span_type: str,
        operation: str,
        *,
        name: str | None = None,
        parent_span_id: str | None = None,
        **attributes: Any,
    ) -> SpanHandle | None:
        context = current_context()
        if context is None:
            return None
        emitter = self._emitter_for(context)
        if emitter is None:
            return None
        span_id = new_id()
        handle = SpanHandle(
            emitter=emitter,
            context=context,
            span_id=span_id,
            parent_span_id=parent_span_id or context.parent_obs_span_id,
            span_type=span_type,
            operation=operation,
            name=name or operation,
            attributes=attributes,
        )
        handle.start()
        return handle

    def end_carried_span(
        self,
        carrier: dict[str, Any],
        *,
        status: str,
        **attributes: Any,
    ) -> bool:
        """End a Span whose started slot was transferred to this process."""
        try:
            if carrier.get("schema_version") != 1:
                return False
            context_data = carrier.get("context")
            base = carrier.get("attributes")
            span_id = carrier.get("obs_span_id")
            started_at = carrier.get("started_at")
            if (
                not isinstance(context_data, dict)
                or not isinstance(base, dict)
                or not isinstance(span_id, str)
                or not isinstance(started_at, str)
            ):
                return False
            context = ObservabilityContext.from_carrier(context_data)
            if not context.capture_enabled:
                return False
            emitter = self._emitter_for(context)
            if emitter is None:
                return False
            reserved = {
                "obs_run_id",
                "obs_attempt_id",
                "obs_span_id",
                "parent_obs_span_id",
                "event_name",
                "entity_id",
                "entity_kind",
                "phase_key",
                "local_seq",
                "started_at",
                "ended_at",
                "status",
                "metrics",
                "quality",
            }
            optional = {
                key: value
                for key, value in attributes.items()
                if key not in reserved
            }
            return emitter.emit(
                MetadataEvent(
                    event_name="span.ended",
                    obs_context=context,
                    entity_kind="span",
                    entity_id=span_id,
                    phase_key="ended",
                    local_seq=0,
                    attributes={
                        **{
                            key: value
                            for key, value in base.items()
                            if key not in reserved
                        },
                        "started_at": started_at,
                        "ended_at": utc_now(),
                        "status": status,
                        "metrics": {},
                        "quality": {
                            "start_observed": True,
                            "end_observed": True,
                        },
                        **optional,
                    },
                ),
            )
        except Exception:
            return False

    def end_attempt(
        self,
        context: ObservabilityContext,
        *,
        status: str,
    ) -> bool:
        emitter = self._emitter_for(context)
        if emitter is None or context.obs_attempt_id is None:
            return False
        return emitter.emit(
            MetadataEvent(
                event_name="attempt.ended",
                obs_context=context,
                entity_kind="attempt",
                entity_id=context.obs_attempt_id,
                phase_key="ended",
                local_seq=0,
                attributes={
                    "status": status,
                    "started_at": context.attempt_started_at,
                    "ended_at": utc_now(),
                },
            ),
        )

    def end_run(self, context: ObservabilityContext, *, status: str) -> bool:
        emitter = self._emitter_for(context)
        if emitter is None:
            return False
        _clear_run_state_sequence(context.obs_run_id)
        metrics = {}
        if (
            context.run_started_monotonic_ns is not None
            and context.run_clock_producer_id == producer_id()
        ):
            metrics["duration_us"] = max(
                0,
                (time.monotonic_ns() - context.run_started_monotonic_ns)
                // 1000,
            )
        return emitter.emit(
            MetadataEvent(
                event_name="run.ended",
                obs_context=context,
                entity_kind="run",
                entity_id=context.obs_run_id,
                phase_key="ended",
                local_seq=0,
                attributes={
                    "started_at": context.run_started_at,
                    "ended_at": utc_now(),
                    "status": status,
                    "terminal_fact_source": "business_settlement",
                    **({"metrics": metrics} if metrics else {}),
                },
            ),
        )

    def run_state(self, context: ObservabilityContext, *, status: str) -> bool:
        emitter = self._emitter_for(context)
        if emitter is None:
            return False
        sequence = _next_run_state_sequence(context.obs_run_id)
        if status in _RUN_TERMINAL_STATUSES:
            _clear_run_state_sequence(context.obs_run_id)
        return emitter.emit(
            MetadataEvent(
                event_name="run.state",
                obs_context=context,
                entity_kind="run",
                entity_id=context.obs_run_id,
                phase_key=f"state:{status}",
                local_seq=sequence,
                attributes={"status": status},
            ),
        )

    def approval_requested(
        self,
        context: ObservabilityContext,
        approval_request_id: str,
        *,
        started_at: str | None = None,
    ) -> bool:
        emitter = self._emitter_for(context)
        if emitter is None:
            return False
        occurred_at = started_at or utc_now()
        return emitter.emit(
            MetadataEvent(
                event_name="approval.requested",
                obs_context=context,
                entity_kind="approval",
                entity_id=approval_request_id,
                phase_key="requested",
                local_seq=0,
                occurred_at=occurred_at,
                attributes={
                    "approval_request_id": approval_request_id,
                    "started_at": occurred_at,
                },
            ),
        )

    def approval_resolved(
        self,
        context: ObservabilityContext,
        approval_request_id: str,
        *,
        decision: str,
        started_at: str | None = None,
    ) -> bool:
        emitter = self._emitter_for(context)
        if emitter is None:
            return False
        ended_at = utc_now()
        return emitter.emit(
            MetadataEvent(
                event_name="approval.resolved",
                obs_context=context,
                entity_kind="approval",
                entity_id=approval_request_id,
                phase_key="resolved",
                local_seq=0,
                occurred_at=ended_at,
                attributes={
                    "approval_request_id": approval_request_id,
                    "decision": decision,
                    "started_at": started_at or ended_at,
                    "ended_at": ended_at,
                },
            ),
        )

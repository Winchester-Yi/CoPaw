# -*- coding: utf-8 -*-
"""Independent logical/attempt observation around the existing model chain."""

import json
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from uuid import uuid4

from .context import (
    bind_branch,
    child_snapshot,
    create_branch,
    current_branch,
    current_logical,
    logical_call,
    now,
)
from .input_capture import capture_inputs
from .contract import context_capture_enabled, schema_version
from .context_capture import capture_context
from .context_operations import (
    associate_summary_attempt,
    bound_context_operation,
)
from .runtime import safe_call
from .context_contract import REPRESENTATION_VERSION
from .models import UsageSnapshot
from .usage import extract_usage, token_count

logger = logging.getLogger(__name__)
LEGACY_PROJECTION_UNAVAILABLE = (
    "bounded_context_v1_legacy_projection_unavailable"
)


def unavailable_input():
    return {
        "items": [],
        "capture_state": "unavailable",
        "expected_item_count": None,
        "estimated_input_tokens": None,
        "assembly_stage": "messages_only",
        "prompt_format_confidence": "message_approx",
        "context_builder_version": "prepared-messages-v1",
        "prompt_modality": "unknown",
    }


def prepared_snapshot(branch, call, messages, tools):
    config = branch.collector.config
    try:
        context = capture_context(
            messages,
            tools,
            (call["source_id"], call["tenant_id"]),
            config,
            context_id=call["context_id"],
            context_role="prepared",
        )
    except Exception:
        context = {
            "context_id": call["context_id"],
            "context_role": "prepared",
            "capture_state": "unavailable",
            "capture_reason": "capture_error",
            "occurrences": [],
            "blocks": [],
            "text_bytes": None,
            "encoded_bytes": None,
            "expected_item_count": None,
            "representation_version": REPRESENTATION_VERSION,
            "estimator_version": "unavailable",
            "key_version": config.key_version,
        }
    occurrences = context["occurrences"]
    operation = bound_context_operation(branch)
    return {
        **branch.fields,
        **context,
        "schema_version": schema_version(config),
        "call_id": call["call_id"],
        "operation_id": (
            operation.operation_id if operation is not None else None
        ),
        "context_revision": 1,
        "revision": 1,
        "request_started_at": call["started_at"],
        "request_ended_at": None,
        "assembly_stage": call["assembly_stage"],
        "execution_state": "running",
        "provider_id": call["provider_id"],
        "model_name": call["model_name"],
        "estimated_tokens": (
            occurrences[-1]["token_end"]
            if occurrences
            else (0 if context["capture_state"] == "complete" else None)
        ),
    }


def observe_usage(result):
    try:
        return extract_usage(result)
    except Exception as exc:
        logger.warning("Token usage unavailable: %s", type(exc).__name__)
        return UsageSnapshot()


async def close_iterator(iterator):
    close = getattr(iterator, "aclose", None)
    if close:
        try:
            await close()
        except Exception as exc:
            logger.warning(
                "Token provider stream close failed: %s",
                type(exc).__name__,
            )


@dataclass
class LogicalState:
    branch: object
    call_id: str = field(default_factory=lambda: str(uuid4()))
    sequence: int = 0
    attempt_no: int = 0
    active: dict = field(default_factory=dict)


class ModelProxy:
    def __init__(self, model):
        self._model = model

    def __getattr__(self, name):
        return getattr(self._model, name)


class ManagedStream(AsyncGenerator):
    """Close the request even when the consumer never starts iteration."""

    def __init__(self, iterator, cleanup):
        self.iterator, self.cleanup = iterator, cleanup

    async def __anext__(self):
        return await anext(self.iterator)

    async def asend(self, value):
        return await self.iterator.asend(value)

    async def athrow(self, *args):
        return await self.iterator.athrow(*args)

    async def aclose(self):
        try:
            await self.iterator.aclose()
        finally:
            await self.cleanup()


class AttemptObserver(ModelProxy):
    def __init__(self, model, provider_id, metadata):
        super().__init__(model)
        self.provider_id, self.metadata = provider_id, metadata

    async def __call__(self, messages, **kwargs):
        logical = current_logical()
        if logical is None or logical.branch is None:
            return await self._model(messages, **kwargs)
        branch = logical.branch
        logical.attempt_no += 1
        branch.attempts += 1
        b_enabled = context_capture_enabled(branch.collector.config)
        try:
            capture = (
                unavailable_input()
                if b_enabled
                else capture_inputs(
                    messages,
                    kwargs.get("tools", []),
                    branch.scope_key,
                    branch.collector.config,
                )
            )
        except Exception:
            capture = unavailable_input()
        if b_enabled:
            # B's representation and A's five legacy input rules are not
            # interchangeable. Keep the old fields explicitly unavailable.
            capture.update(
                items=[],
                expected_item_count=None,
                estimated_input_tokens=None,
                capture_state="unavailable",
                capture_reason=LEGACY_PROJECTION_UNAVAILABLE,
                fingerprint_available=False,
                context_builder_version=REPRESENTATION_VERSION,
            )
        call = {
            **branch.fields,
            **capture,
            "call_id": str(uuid4()),
            "step_id": str(uuid4()),
            "parent_step_id": branch.fields["branch_run_id"],
            "context_id": str(uuid4()),
            "assembled_at": now(),
            "logical_call_id": logical.call_id,
            "logical_seq": logical.sequence,
            "attempt_no": logical.attempt_no,
            "compaction_epoch": branch.epoch,
            "provider_id": self.provider_id,
            "model_name": str(getattr(self._model, "model_name", "unknown"))[
                :128
            ],
            "max_output_tokens": token_count(
                kwargs.get("max_tokens")
                or kwargs.get("max_output_tokens")
                or self.metadata.get("max_output_tokens"),
            ),
            "max_input_tokens": token_count(
                branch.fields.get("max_input_tokens")
                or self.metadata.get("max_input_tokens"),
            ),
            "revision": 1,
            "execution_state": "running",
            "usage_status": "pending",
            "usage_origin": "unknown",
            "usage_accuracy": "unavailable",
            "input_tokens": None,
            "output_tokens": None,
            "started_at": now(),
        }
        prepared_context = (
            safe_call(
                prepared_snapshot,
                branch,
                call,
                messages,
                kwargs.get("tools", []),
            )
            if b_enabled
            else None
        )
        count = 2 if prepared_context is not None else 1
        size = len(
            json.dumps(
                {"call": call, "context": prepared_context},
                ensure_ascii=False,
            ).encode(),
        )
        if not branch.collector.reserve(size, count=count):
            # Preserve A's bounded fallback: drop the input items, retain
            # model/usage facts, and reserve only the reduced call record.
            call.update(
                items=[],
                capture_state="partial",
                expected_item_count=None,
                estimated_input_tokens=None,
            )
            prepared_context = None
            count = 1
            size = len(json.dumps({"call": call}, ensure_ascii=False).encode())
            if not branch.collector.reserve(size, count=count):
                branch.collector.writer.gaps[
                    branch.fields["branch_run_id"]
                ] += 1
                return await self._model(messages, **kwargs)
        attempt = {
            "call": call,
            "usage": UsageSnapshot(),
            "iterator": None,
            "size": size,
            "count": count,
            "context": prepared_context,
        }
        logical.active[call["call_id"]] = attempt
        if prepared_context is not None:
            safe_call(associate_summary_attempt, branch, call["call_id"])
        branch.collector.emit("call", call)
        if prepared_context is not None:
            branch.collector.emit("context", prepared_context)
        try:
            result = await self._model(messages, **kwargs)
            if hasattr(result, "__aiter__"):
                attempt["iterator"] = result
                return self._stream(result, logical, attempt)
            attempt["usage"] = observe_usage(result)
            self.finish(logical, attempt, "completed")
            return result
        except BaseException as exc:
            self.finish(
                logical,
                attempt,
                (
                    "cancelled"
                    if type(exc).__name__
                    in {"CancelledError", "GeneratorExit"}
                    else "error"
                ),
                type(exc).__name__,
            )
            raise

    def finish(self, logical, attempt, state, error_code=None):
        identity = attempt["call"]["call_id"]
        if identity not in logical.active:
            return
        logical.active.pop(identity)
        logical.branch.collector.release(
            attempt["size"],
            count=attempt["count"],
        )
        data = {
            **attempt["call"],
            **attempt["usage"].as_dict(),
            "revision": 2,
            "execution_state": state,
            "ended_at": now(),
            "error_code": error_code,
        }
        if state != "completed" and data["usage_status"] == "reported":
            data["usage_status"] = "partial"
        logical.branch.collector.emit("call", data)
        context = attempt.get("context")
        if context is not None:
            context.update(
                context_revision=2,
                revision=2,
                request_ended_at=data["ended_at"],
                execution_state=state,
            )
            logical.branch.collector.emit("context", context)

    async def _stream(self, iterator, logical, attempt):
        state, error = "cancelled", None
        try:
            async for chunk in iterator:
                usage = observe_usage(chunk)
                previous = attempt["usage"]
                attempt["usage"] = UsageSnapshot(
                    (
                        usage.input_tokens
                        if usage.input_tokens is not None
                        else previous.input_tokens
                    ),
                    (
                        usage.output_tokens
                        if usage.output_tokens is not None
                        else previous.output_tokens
                    ),
                    {**previous.details, **usage.details},
                    usage.finish_reason or previous.finish_reason,
                )
                yield chunk
            state = "completed"
        except BaseException as exc:
            error = type(exc).__name__
            state = (
                "cancelled"
                if error in {"CancelledError", "GeneratorExit"}
                else "error"
            )
            raise
        finally:
            self.finish(logical, attempt, state, error)
            await close_iterator(iterator)


class LogicalObserver(ModelProxy):
    def __init__(self, model, metadata=None):
        super().__init__(model)
        self.metadata = metadata or {}

    async def __call__(self, messages, **kwargs):
        branch = current_branch()
        owned = branch is None
        purpose = self.metadata.get("purpose")
        if branch and purpose and purpose != branch.fields["purpose"]:
            parent = child_snapshot(purpose, "auxiliary")
            branch = create_branch(
                {**branch.fields, "purpose": purpose, "role": "auxiliary"},
                branch.collector,
                parent,
            )
            owned = True
        branch = branch or create_branch(self.metadata)
        if branch is None:
            return await self._model(messages, **kwargs)
        branch.logical_seq += 1
        logical = LogicalState(branch, sequence=branch.logical_seq)
        try:
            with bind_branch(branch), logical_call(logical):
                result = await self._model(messages, **kwargs)
            if hasattr(result, "__aiter__"):
                return ManagedStream(
                    self._stream(result, logical, owned),
                    lambda: self._finish_stream(
                        result,
                        logical,
                        owned,
                        "cancelled",
                    ),
                )
            if owned:
                branch.finish()
            return result
        except BaseException as exc:
            if owned:
                branch.finish(
                    (
                        "cancelled"
                        if type(exc).__name__
                        in {"CancelledError", "GeneratorExit"}
                        else "error"
                    ),
                )
            raise

    async def _stream(self, iterator, logical, owned):
        state = "cancelled"
        try:
            while True:
                with bind_branch(logical.branch), logical_call(logical):
                    try:
                        chunk = await anext(iterator)
                    except StopAsyncIteration:
                        state = "completed"
                        break
                yield chunk
        except BaseException as exc:
            state = (
                "cancelled"
                if type(exc).__name__ in {"CancelledError", "GeneratorExit"}
                else "error"
            )
            raise
        finally:
            await self._finish_stream(iterator, logical, owned, state)

    async def _finish_stream(self, iterator, logical, owned, state):
        with bind_branch(logical.branch), logical_call(logical):
            await close_iterator(iterator)
            # Provider streams can be hidden under the legacy wrappers.
            for attempt in list(logical.active.values()):
                await close_iterator(attempt["iterator"])
                observer = AttemptObserver(None, "", {})
                observer.finish(logical, attempt, state)
            if owned:
                logical.branch.finish(state)

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
from .models import UsageSnapshot
from .usage import extract_usage, token_count

logger = logging.getLogger(__name__)


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
        try:
            capture = capture_inputs(
                messages,
                kwargs.get("tools", []),
                branch.scope_key,
                branch.collector.config,
            )
        except Exception:
            capture = {
                "items": [],
                "capture_state": "unavailable",
                "expected_item_count": None,
                "estimated_input_tokens": None,
                "assembly_stage": "messages_only",
                "prompt_format_confidence": "message_approx",
                "context_builder_version": "prepared-messages-v1",
                "prompt_modality": "unknown",
            }
        size = len(json.dumps(capture).encode())
        if not branch.collector.reserve(size):
            capture["items"] = []
            capture["capture_state"] = "partial"
            size = 0
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
        attempt = {
            "call": call,
            "usage": UsageSnapshot(),
            "iterator": None,
            "size": size,
        }
        logical.active[call["call_id"]] = attempt
        branch.collector.emit("call", call)
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
        logical.branch.collector.release(attempt["size"])
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

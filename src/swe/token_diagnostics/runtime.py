# -*- coding: utf-8 -*-
"""Small runtime adapters; diagnostic failures never alter execution."""

import logging
from contextlib import contextmanager
from functools import wraps
from uuid import uuid4

from .context import (
    accounting_run,
    bind_branch,
    create_branch,
    child_snapshot,
    compaction_completed,
    current_branch,
    now,
    tool_completed,
    tool_started,
)
from .provenance import register_origin

logger = logging.getLogger(__name__)


def safe_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Exception as exc:
        logger.warning(
            "Token diagnostic adapter skipped: %s",
            type(exc).__name__,
        )
        branch = current_branch()
        if branch:
            branch.collector.writer.gaps[branch.fields["branch_run_id"]] += 1
        return None


def accounting_query(function):
    @wraps(function)
    async def wrapped(self, msgs, request=None, **kwargs):
        channel_meta = getattr(request, "channel_meta", None) or {}
        metadata = {
            "source_id": getattr(request, "source_id", None)
            or channel_meta.get("source_id"),
            "tenant_id": getattr(request, "user_id", None),
            "user_id": getattr(request, "user_id", None),
            "agent_id": getattr(self, "agent_id", None),
            "session_id": getattr(request, "session_id", None),
            "chat_id": channel_meta.get("chat_id"),
            "trace_id": getattr(request, "trace_id", None),
            "answer_turn_identity": channel_meta.get("answer_turn_identity"),
            "execution_origin": getattr(request, "execution_origin", None),
            "cron_persistence_key": getattr(
                request,
                "cron_persistence_key",
                None,
            ),
        }
        branch = safe_call(create_branch, metadata)
        iterator = function(self, msgs, request=request, **kwargs)
        state = "cancelled"
        try:
            while True:
                with bind_branch(branch):
                    try:
                        frame = await anext(iterator)
                    except StopAsyncIteration:
                        state = "completed"
                        break
                yield frame
        except BaseException as exc:
            state = (
                "cancelled"
                if type(exc).__name__ in {"CancelledError", "GeneratorExit"}
                else "error"
            )
            raise
        finally:
            with bind_branch(branch):
                await iterator.aclose()
                if branch:
                    safe_call(branch.finish, state)

    return wrapped


def accounting_subagent(function):
    @wraps(function)
    async def wrapped(self, *args, **kwargs):
        metadata = dict(kwargs.get("request_context") or {})
        parent = metadata.get("_token_scope")
        if parent is None and current_branch():
            parent = safe_child_snapshot()
        metadata.update(role="subagent", purpose="research")
        with accounting_run(metadata, parent=parent):
            return await function(self, *args, **kwargs)

    return wrapped


def safe_child_snapshot():
    return safe_call(child_snapshot)


def notify_tool_started(tool_call):
    return safe_call(tool_started, tool_call)


def notify_tool_completed(metadata, output, error_code=None):
    return safe_call(tool_completed, metadata, output, error_code)


def notify_compaction():
    safe_call(compaction_completed)


def configure_agent(running, system_prompt):
    branch = current_branch()
    if branch:
        branch.fields["max_input_tokens"] = getattr(
            running,
            "max_input_length",
            None,
        )
        safe_call(
            register_origin,
            system_prompt,
            branch.scope_key,
            branch.collector.config,
            "system",
            "agent-system-prompt",
        )


@contextmanager
def auxiliary_run(purpose):
    branch = current_branch()
    if branch is None:
        yield None
        return
    snapshot = safe_call(child_snapshot, purpose, "auxiliary")
    with accounting_run(
        {**branch.fields, "purpose": purpose, "role": "auxiliary"},
        collector=branch.collector,
        parent=snapshot,
    ) as auxiliary:
        yield auxiliary


@contextmanager
def external_call(purpose):
    """A remote service attempt is visible, its internal model usage is not."""
    with auxiliary_run(purpose) as branch:
        if branch is None:
            yield
            return
        branch.attempts += 1
        branch.logical_seq += 1
        data = {
            **branch.fields,
            "call_id": str(uuid4()),
            "step_id": str(uuid4()),
            "parent_step_id": branch.fields["branch_run_id"],
            "context_id": None,
            "logical_call_id": str(uuid4()),
            "logical_seq": branch.logical_seq,
            "attempt_no": 1,
            "compaction_epoch": branch.epoch,
            "provider_id": "external-service",
            "model_name": "unobservable",
            "revision": 1,
            "started_at": now(),
            "execution_state": "running",
            "usage_status": "pending",
            "usage_origin": "unknown",
            "usage_accuracy": "unavailable",
            "capture_state": "unavailable",
            "input_tokens": None,
            "output_tokens": None,
            "external_usage_unavailable": True,
            "items": [],
        }
        branch.collector.emit("call", data)
        state = "completed"
        try:
            yield
        except BaseException as exc:
            state = (
                "cancelled"
                if type(exc).__name__ in {"CancelledError", "GeneratorExit"}
                else "error"
            )
            raise
        finally:
            branch.collector.emit(
                "call",
                {
                    **data,
                    "revision": 2,
                    "execution_state": state,
                    "usage_status": "unavailable",
                    "ended_at": now(),
                },
            )

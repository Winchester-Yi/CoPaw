# -*- coding: utf-8 -*-
"""Bounded operation snapshots and explicit, scoped summary association."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
from uuid import uuid4

from .context import current_branch, now
from .context_capture import capture_context
from .context_contract import CONTEXT_MAX_ITEMS
from .contract import context_capture_enabled, schema_version

_operation = ContextVar("token_context_operation", default=None)


def snapshot_messages(messages, fixed=()):
    """Borrow business payloads one at a time; never clone whole memory."""
    for role, content in fixed:
        if content:
            yield {"role": role, "content": content}
    for message in messages:
        if type(message) is dict:
            yield message
        else:
            yield {"role": message.role, "content": message.content}


def capture_snapshot(
    branch,
    messages,
    role,
    *,
    context_id=None,
    metadata=None,
):
    """A failed hash still produces a typed missing-evidence record."""
    identity = context_id or str(uuid4())
    config = branch.collector.config
    scope = (branch.fields["source_id"], branch.fields["tenant_id"])
    try:
        captured = capture_context(
            snapshot_messages(messages),
            [],
            scope,
            config,
            context_id=identity,
            context_role=role,
        )
    except Exception:
        from .context_contract import REPRESENTATION_VERSION

        captured = {
            "context_id": identity,
            "context_role": role,
            "representation_version": REPRESENTATION_VERSION,
            "estimator_version": f"utf8-density-v1:{config.divisor}",
            "key_version": config.key_version,
            "capture_state": "unavailable",
            "capture_reason": "capture_error",
            "text_bytes": None,
            "encoded_bytes": None,
            "expected_item_count": None,
            "occurrences": [],
            "blocks": [],
        }
    result = {
        **branch.fields,
        **captured,
        "schema_version": schema_version(config),
        "call_id": None,
        "operation_id": None,
        "context_revision": 1,
        "revision": 1,
        "request_started_at": None,
        "request_ended_at": None,
        "assembly_stage": "messages_only",
        "execution_state": "completed",
        **(metadata or {}),
    }
    occurrences = result["occurrences"]
    result["estimated_tokens"] = (
        occurrences[-1]["token_end"]
        if occurrences
        else (0 if result["capture_state"] == "complete" else None)
    )
    return result


@dataclass
class ContextOperation:
    branch: object
    data: dict
    origins: tuple = ()
    finished: bool = False
    reserved_bytes: int = 0

    @property
    def operation_id(self):
        return self.data["operation_id"]


def begin_context_operation(before, operation_type, *, trigger_reason=None):
    branch = current_branch()
    if branch is None or not context_capture_enabled(branch.collector.config):
        return None
    identity = str(uuid4())
    captured = (
        capture_snapshot(
            branch,
            before,
            "before",
            metadata={"operation_id": identity},
        )
        if before is not None
        else None
    )
    accepted = captured is not None and branch.collector.emit(
        "context",
        captured,
    )
    complete = accepted and captured["capture_state"] == "complete"
    data = {
        **branch.fields,
        "operation_id": identity,
        "operation_type": operation_type,
        "operation_state": "running",
        "trigger_reason": trigger_reason,
        "started_at": now(),
        "ended_at": None,
        "before_context_id": captured["context_id"] if accepted else None,
        "after_context_id": None,
        "summary_call_id": None,
        "missing_before": not complete,
        "missing_after": True,
        "capture_state": "partial",
        "revision": 1,
        "segments": [],
    }
    origins = (
        tuple(
            (o["occurrence_index"], o["source_object_id"], o["source_version"])
            for o in captured["occurrences"]
            if o["source_object_id"] and o["source_version"]
        )
        if complete
        else ()
    )
    reserved = len(
        json.dumps(
            {"data": data, "origins": origins},
            ensure_ascii=False,
        ).encode(),
    )
    if not branch.collector.reserve(reserved):
        branch.collector.writer.gaps[branch.fields["branch_run_id"]] += 1
        return None
    branch.collector.emit("context_operation", data)
    return ContextOperation(branch, data, origins, reserved_bytes=reserved)


def _relations(operation, captured, segments):
    """Only explicit facts referencing reliable source/version survive."""
    if (
        operation.data["missing_before"]
        or captured is None
        or captured["capture_state"] != "complete"
    ):
        return []
    before = {
        index: (source, version)
        for index, source, version in operation.origins
    }
    after = {
        o["occurrence_index"]: (o["source_object_id"], o["source_version"])
        for o in captured["occurrences"]
        if o["source_object_id"] and o["source_version"]
    }
    result = []
    limit = min(CONTEXT_MAX_ITEMS, len(operation.origins) + len(after))
    for fact in segments[:limit]:
        relation = fact.get("relation_type")
        bi, ai = fact.get("before_occurrence_index"), fact.get(
            "after_occurrence_index",
        )
        source = (fact.get("source_object_id"), fact.get("source_version"))
        if relation == "removed" and before.get(bi) == source and ai is None:
            valid = True
        elif relation == "created" and after.get(ai) == source and bi is None:
            valid = True
        elif (
            relation == "replaced" and before.get(bi) == source and ai in after
        ):
            valid = True
        else:
            valid = False
        if valid:
            result.append(
                {
                    "relation_index": len(result),
                    "relation_type": relation,
                    "before_context_id": (
                        operation.data["before_context_id"]
                        if bi is not None
                        else None
                    ),
                    "before_occurrence_index": bi,
                    "after_context_id": (
                        captured["context_id"] if ai is not None else None
                    ),
                    "after_occurrence_index": ai,
                    "source_object_id": source[0],
                    "source_version": source[1],
                },
            )
    return result


def finish_context_operation(
    operation,
    after,
    state="completed",
    *,
    segments=(),
):
    if operation is None or operation.finished:
        return
    operation.finished = True
    branch = operation.branch
    branch.collector.release(operation.reserved_bytes)
    operation.reserved_bytes = 0
    captured = (
        capture_snapshot(
            branch,
            after,
            "after",
            metadata={"operation_id": operation.operation_id},
        )
        if after is not None and state == "completed"
        else None
    )
    accepted = captured is not None and branch.collector.emit(
        "context",
        captured,
    )
    complete = accepted and captured["capture_state"] == "complete"
    data = {
        **operation.data,
        "revision": 2,
        "operation_state": state,
        "ended_at": now(),
        "after_context_id": captured["context_id"] if accepted else None,
        "missing_after": not complete,
        "capture_state": (
            "complete"
            if complete and not operation.data["missing_before"]
            else "partial"
        ),
        "segments": (
            _relations(operation, captured, segments) if complete else []
        ),
    }
    branch.collector.emit("context_operation", data)
    operation.origins = ()


@contextmanager
def bind_context_operation(operation):
    token = _operation.set(operation)
    try:
        yield operation
    finally:
        _operation.reset(token)


def bound_context_operation(branch):
    operation = _operation.get()
    if (
        operation is None
        or operation.finished
        or any(
            operation.branch.fields[k] != branch.fields[k]
            for k in ("source_id", "tenant_id", "task_run_id")
        )
    ):
        return None
    return operation


def associate_summary_attempt(branch, call_id):
    operation = bound_context_operation(branch)
    if operation is None:
        return None
    # Every prepared Context has the binding; the head retains the first
    # attempt, not a last-retry guess at the complete summary call set.
    if operation.data["summary_call_id"] is None:
        operation.data["summary_call_id"] = call_id
    return operation.operation_id

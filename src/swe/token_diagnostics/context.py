# -*- coding: utf-8 -*-
"""Task/branch accounting context with optional external Trace references."""

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from swe.__version__ import __version__

from .collector import get_collector
from .contract import (
    SCHEMA_VERSION,
    TELEMETRY_SOURCE,
    execution_identity,
    scoped_run_id,
)
from .provenance import bind_origins, fingerprint, reset_origins

_scope = ContextVar("token_diagnostic_scope", default=None)
_logical = ContextVar("token_logical_call", default=None)


def now():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Branch:
    fields: dict
    collector: object
    revision: int = 1
    attempts: int = 0
    logical_seq: int = 0
    event_seq: int = 0
    epoch: int = 0
    children: set = field(default_factory=set)
    origins: dict = field(default_factory=dict)
    finished: bool = False

    @property
    def scope_key(self):
        return json.dumps(
            [self.fields["source_id"], self.fields["tenant_id"]],
            separators=(",", ":"),
        )

    def snapshot(self, state=None):
        state = state or self.fields.get("execution_state", "running")
        return {
            **self.fields,
            "revision": self.revision,
            "execution_state": state,
            "expected_attempt_count": (
                self.attempts if state != "running" else None
            ),
            "expected_child_branches": sorted(self.children),
            "ended_at": (
                self.fields.get("ended_at") if state != "running" else None
            ),
        }

    def finish(self, state="completed"):
        if not self.finished:
            self.finished = True
            self.fields["execution_state"] = state
            self.fields["ended_at"] = now()
            self.revision += 1
            self.collector.emit("run", self.snapshot(state))
            self.publish_fragment(state)

    def publish_fragment(self, state="running"):
        self.collector.emit(
            "event",
            {
                **self.snapshot(state),
                "event_id": self.fields["capture_fragment_id"],
                "event_type": "accounting_fragment",
                "revision": 1 if state == "running" else 2,
                "event_seq": 0,
                "created_at": now(),
            },
        )


def current_branch():
    return _scope.get()


def current_logical():
    return _logical.get()


def create_branch(metadata, collector=None, parent=None):
    collector = collector or get_collector()
    if not collector.config.enabled:
        return None
    fields = {
        key: str(metadata.get(key) or "")[:128]
        for key in (
            "user_id",
            "agent_id",
            "chat_id",
            "session_id",
        )
    }
    fields.update(
        source_id=str(metadata.get("source_id") or "default")[:64],
        tenant_id=str(
            metadata.get("tenant_id") or metadata.get("user_id") or "default",
        )[:128],
        trace_id=str(metadata.get("trace_id") or "")[:64] or None,
        role=str(metadata.get("role") or metadata.get("agent_role") or "main")[
            :32
        ],
        purpose=str(metadata.get("purpose") or "main")[:32],
        started_at=now(),
        parent_branch_run_id=None,
    )
    execution_id, identity_source = execution_identity(metadata)
    execution_origin = str(metadata.get("execution_origin") or "interactive")[
        :32
    ]
    run_id = scoped_run_id(
        fields["source_id"],
        fields["tenant_id"],
        identity_source,
        execution_id,
        execution_origin,
    )
    fields.update(
        task_run_id=run_id,
        branch_run_id=run_id,
        schema_version=SCHEMA_VERSION,
        telemetry_source=TELEMETRY_SOURCE,
        execution_origin=execution_origin,
        execution_id=execution_id,
        identity_source=identity_source,
        agent_version=__version__,
    )
    if parent and all(
        parent.get(k) == fields[k] for k in ("source_id", "tenant_id")
    ):
        for key in (
            "task_run_id",
            "branch_run_id",
            "parent_branch_run_id",
            "execution_id",
            "identity_source",
            "execution_origin",
            "schema_version",
            "telemetry_source",
            "agent_version",
        ):
            fields[key] = parent[key]
    branch = Branch(fields, collector)
    fields["capture_fragment_id"] = str(uuid4())
    collector.emit("run", branch.snapshot())
    branch.publish_fragment()
    return branch


@contextmanager
def bind_branch(branch):
    same = current_branch() is branch
    token = _scope.set(branch)
    provenance = (
        None if same else bind_origins(branch.origins if branch else {})
    )
    try:
        yield branch
    finally:
        if provenance is not None:
            reset_origins(provenance)
        _scope.reset(token)


@contextmanager
def accounting_run(metadata, collector=None, parent=None):
    try:
        branch = create_branch(metadata, collector, parent)
    except Exception:
        branch = None
    state = "completed"
    with bind_branch(branch):
        try:
            yield branch
        except BaseException as exc:
            state = (
                "cancelled"
                if type(exc).__name__ in {"CancelledError", "GeneratorExit"}
                else "error"
            )
            raise
        finally:
            if branch:
                try:
                    branch.finish(state)
                except Exception:
                    pass


def bind_trace(trace_id):
    branch = current_branch()
    if branch and trace_id:
        branch.fields["trace_id"] = str(trace_id)[:64]


def child_snapshot(purpose="research", role="subagent"):
    parent = current_branch()
    if parent is None:
        return None
    identity = str(uuid4())
    parent.children.add(identity)
    parent.revision += 1
    parent.collector.emit("run", parent.snapshot())
    return {
        **parent.fields,
        "branch_run_id": identity,
        "parent_branch_run_id": parent.fields["branch_run_id"],
        "role": role,
        "purpose": purpose,
        "capture_enabled": parent.collector.config.enabled,
    }


@contextmanager
def logical_call(logical):
    token = _logical.set(logical)
    try:
        yield logical
    finally:
        _logical.reset(token)


def record_event(event_type, **values):
    branch = current_branch()
    if branch is None:
        return
    branch.event_seq += 1
    branch.collector.emit(
        "event",
        {
            **branch.fields,
            "event_id": str(uuid4()),
            "event_type": event_type,
            "event_seq": branch.event_seq,
            "compaction_epoch": branch.epoch,
            "created_at": now(),
            "started_at": None,
            "ended_at": None,
            "revision": 1,
            "step_id": str(uuid4()),
            "parent_step_id": branch.fields["branch_run_id"],
            **values,
        },
    )


def tool_started(tool_call):
    branch = current_branch()
    if branch is None:
        return None
    metadata = {
        "step_id": str(uuid4()),
        "started_at": now(),
        "tool_kind": "unknown",
        "idempotency_class": "unknown",
        "is_cacheable": False,
        "data_version": None,
        "producer_operation_id": str(tool_call.get("id") or uuid4())[:64],
        "tool_name": str(tool_call.get("name") or "unknown")[:128],
        "key_version": branch.collector.config.key_version,
        "polling": (tool_call.get("metadata") or {}).get("polling") is True,
        "arguments_fingerprint": fingerprint(
            tool_call.get("input", tool_call.get("arguments", {})),
            branch.scope_key,
            branch.collector.config,
        ),
    }
    record_event("tool_started", **metadata)
    return metadata


def tool_completed(metadata, output, error_code=None):
    branch = current_branch()
    if branch and metadata:
        record_event(
            "tool_completed",
            **metadata,
            ended_at=now(),
            result_fingerprint=fingerprint(
                output,
                branch.scope_key,
                branch.collector.config,
            ),
            error_code=error_code,
            result_status="error" if error_code else "success",
        )


def compaction_completed():
    branch = current_branch()
    if branch:
        branch.epoch += 1
        record_event("compaction", ended_at=now())

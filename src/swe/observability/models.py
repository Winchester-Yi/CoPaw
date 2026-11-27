# -*- coding: utf-8 -*-
"""Typed identity and event models for independent agent observability."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
import socket
import time
from typing import Any
from uuid import uuid4

from .config import deployment_resource

_PRODUCER_PID = os.getpid()
_PRODUCER_ID = f"{socket.gethostname()[:32]}:{_PRODUCER_PID}:{uuid4()}"


def producer_id() -> str:
    global _PRODUCER_PID, _PRODUCER_ID
    if _PRODUCER_PID != os.getpid():
        _PRODUCER_PID = os.getpid()
        _PRODUCER_ID = f"{socket.gethostname()[:32]}:{_PRODUCER_PID}:{uuid4()}"
    return _PRODUCER_ID


def new_id() -> str:
    return str(uuid4())


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class Scope:
    service_unit_id: str
    source_id: str
    tenant_id: str
    agent_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "service_unit_id": self.service_unit_id,
            "source_id": self.source_id,
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
        }


@dataclass(frozen=True)
class ObservabilityContext:
    obs_run_id: str
    run_started_at: str
    capture_enabled: bool
    scope: Scope
    business_ref: dict[str, str] = field(default_factory=dict)
    obs_attempt_id: str | None = None
    parent_obs_span_id: str | None = None
    attempt_index: int = 0
    predecessor_attempt_id: str | None = None
    attempt_started_at: str | None = None
    run_started_monotonic_ns: int | None = field(default=None, repr=False)
    run_clock_producer_id: str | None = field(default=None, repr=False)
    workspace_binding: tuple[str, str] | None = field(default=None, repr=False)

    @classmethod
    def new_run(
        cls,
        *,
        capture_enabled: bool,
        scope: Scope,
        business_ref: dict[str, str] | None = None,
    ) -> "ObservabilityContext":
        return cls(
            obs_run_id=new_id(),
            run_started_at=utc_now(),
            capture_enabled=capture_enabled,
            scope=scope,
            business_ref=dict(business_ref or {}),
            run_started_monotonic_ns=time.monotonic_ns(),
            run_clock_producer_id=producer_id(),
        )

    def new_attempt(self) -> "ObservabilityContext":
        return self.with_updates(
            obs_attempt_id=new_id(),
            parent_obs_span_id=None,
            attempt_index=self.attempt_index + 1,
            predecessor_attempt_id=self.obs_attempt_id,
            attempt_started_at=utc_now(),
        )

    def with_updates(self, **updates: Any) -> "ObservabilityContext":
        return ObservabilityContext(
            obs_run_id=updates.get("obs_run_id", self.obs_run_id),
            run_started_at=updates.get("run_started_at", self.run_started_at),
            capture_enabled=updates.get(
                "capture_enabled",
                self.capture_enabled,
            ),
            scope=updates.get("scope", self.scope),
            business_ref=updates.get("business_ref", dict(self.business_ref)),
            obs_attempt_id=updates.get("obs_attempt_id", self.obs_attempt_id),
            parent_obs_span_id=updates.get(
                "parent_obs_span_id",
                self.parent_obs_span_id,
            ),
            attempt_index=updates.get("attempt_index", self.attempt_index),
            predecessor_attempt_id=updates.get(
                "predecessor_attempt_id",
                self.predecessor_attempt_id,
            ),
            attempt_started_at=updates.get(
                "attempt_started_at",
                self.attempt_started_at,
            ),
            run_started_monotonic_ns=updates.get(
                "run_started_monotonic_ns",
                self.run_started_monotonic_ns,
            ),
            run_clock_producer_id=updates.get(
                "run_clock_producer_id",
                self.run_clock_producer_id,
            ),
            workspace_binding=updates.get(
                "workspace_binding",
                self.workspace_binding,
            ),
        )

    def to_carrier(self) -> dict[str, Any]:
        """Return the explicit metadata-only cross-process carrier."""
        return {
            "schema_version": 1,
            "obs_run_id": self.obs_run_id,
            "run_started_at": self.run_started_at,
            "capture_enabled": self.capture_enabled,
            "scope": self.scope.to_dict(),
            "business_ref": dict(self.business_ref),
            "obs_attempt_id": self.obs_attempt_id,
            "parent_obs_span_id": self.parent_obs_span_id,
            "attempt_index": self.attempt_index,
            "predecessor_attempt_id": self.predecessor_attempt_id,
            "attempt_started_at": self.attempt_started_at,
            "run_started_monotonic_ns": self.run_started_monotonic_ns,
            "run_clock_producer_id": self.run_clock_producer_id,
        }

    @classmethod
    def from_carrier(cls, carrier: dict[str, Any]) -> "ObservabilityContext":
        if carrier.get("schema_version") != 1:
            raise ValueError("Unsupported observability context schema")
        if not isinstance(carrier.get("capture_enabled"), bool):
            raise ValueError("capture_enabled must be a boolean")
        scope = carrier["scope"]
        started_monotonic_ns = carrier.get("run_started_monotonic_ns")
        clock_producer_id = carrier.get("run_clock_producer_id")
        if (
            not isinstance(started_monotonic_ns, int)
            or isinstance(started_monotonic_ns, bool)
            or clock_producer_id != producer_id()
        ):
            started_monotonic_ns = None
            clock_producer_id = None
        return cls(
            obs_run_id=str(carrier["obs_run_id"]),
            run_started_at=str(carrier["run_started_at"]),
            capture_enabled=bool(carrier["capture_enabled"]),
            scope=Scope(
                service_unit_id=str(scope["service_unit_id"]),
                source_id=str(scope["source_id"]),
                tenant_id=str(scope["tenant_id"]),
                agent_id=str(scope["agent_id"]),
            ),
            business_ref={
                str(k): str(v)
                for k, v in carrier.get("business_ref", {}).items()
            },
            obs_attempt_id=carrier.get("obs_attempt_id"),
            parent_obs_span_id=carrier.get("parent_obs_span_id"),
            attempt_index=int(carrier.get("attempt_index", 0)),
            predecessor_attempt_id=carrier.get("predecessor_attempt_id"),
            attempt_started_at=carrier.get("attempt_started_at"),
            run_started_monotonic_ns=started_monotonic_ns,
            run_clock_producer_id=clock_producer_id,
        )


@dataclass(frozen=True)
class MetadataEvent:
    event_name: str
    obs_context: ObservabilityContext | None
    entity_kind: str
    entity_id: str
    phase_key: str
    local_seq: int
    attributes: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=new_id)
    occurred_at: str = field(default_factory=utc_now)
    producer_id: str = field(default_factory=producer_id)
    _resource: dict[str, str] = field(
        default_factory=deployment_resource,
        init=False,
        repr=False,
    )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": 1,
            "event_id": self.event_id,
            "event_name": self.event_name,
            "occurred_at": self.occurred_at,
            "producer_id": self.producer_id,
            "entity_kind": self.entity_kind,
            "entity_id": self.entity_id,
            "phase_key": self.phase_key,
            "local_seq": self.local_seq,
            "resource": dict(self._resource),
        }
        context = self.obs_context
        if context is not None:
            result.update(
                {
                    "obs_run_id": context.obs_run_id,
                    "run_started_at": context.run_started_at,
                    "capture_enabled": context.capture_enabled,
                    "scope": context.scope.to_dict(),
                    "business_ref": dict(context.business_ref),
                },
            )
            if context.obs_attempt_id:
                result["obs_attempt_id"] = context.obs_attempt_id
            if context.parent_obs_span_id:
                result["parent_obs_span_id"] = context.parent_obs_span_id
            if context.attempt_index:
                result["attempt_index"] = context.attempt_index
            if context.predecessor_attempt_id:
                result["predecessor_attempt_id"] = (
                    context.predecessor_attempt_id
                )
            if context.attempt_started_at:
                result["attempt_started_at"] = context.attempt_started_at
        reserved = _REQUIRED_EVENT_KEYS | {
            "obs_run_id",
            "obs_attempt_id",
            "obs_span_id",
            "parent_obs_span_id",
            "run_started_at",
            "capture_enabled",
            "scope",
            "business_ref",
            "resource",
        }
        result.update(
            (key, value)
            for key, value in self.attributes.items()
            if key not in reserved
        )
        if self.entity_kind == "span":
            result["obs_span_id"] = self.entity_id
        return result


_REQUIRED_EVENT_KEYS = frozenset(
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

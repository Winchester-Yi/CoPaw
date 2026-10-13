# -*- coding: utf-8 -*-
"""Immutable, UTC query scope and filter-bound keyset cursors.

``agent_id`` is the actual executing agent; ``root_agent_id`` selects the
root branch owner. Clients must retain the response window when paging.
"""

import base64
import binascii
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

from .diagnostic_contract import RULE_IDS, EVALUATION_STATES

QUERY_STATES = EVALUATION_STATES | frozenset(
    {
        "pending",
        "failed",
        "running",
        "ready",
        "stale",
        "completed",
        "cancelled",
        "error",
        "prepared",
        "abandoned",
        "reported",
        "partial",
        "unavailable",
        "complete",
        "unverified",
        "incomplete",
    },
)


MAX_CURSOR_BYTES = 2048
MAX_ID_LENGTH = 64


def validate_identity(value, maximum=MAX_ID_LENGTH):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError("invalid_identity")
    if any(ord(char) < 32 for char in value):
        raise ValueError("invalid_identity")
    return value


def utc_datetime(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("invalid_window") from error
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("window_requires_timezone")
    try:
        return value.astimezone(timezone.utc)
    except OverflowError as error:
        raise ValueError("invalid_window") from error


def utc_string(value):
    return utc_datetime(value).isoformat().replace("+00:00", "Z")


def sql_time(value):
    return utc_datetime(value).replace(tzinfo=None)


@dataclass(frozen=True)
class DiagnosticScope:
    source_id: str
    tenant_id: str

    def __post_init__(self):
        validate_identity(self.source_id)
        validate_identity(self.tenant_id, 128)

    def pair(self):
        return self.source_id, self.tenant_id


@dataclass(frozen=True)
class DiagnosticFilters:
    start: datetime | str | None = None
    end: datetime | str | None = None
    agent_id: str | None = None
    root_agent_id: str | None = None
    model_name: str | None = None
    rule_id: str | None = None
    state: str | None = None
    branch_run_id: str | None = None
    limit: int = 50
    cursor: str | None = None

    def __post_init__(self):
        end = (
            utc_datetime(self.end)
            if self.end is not None
            else datetime.now(timezone.utc)
        )
        try:
            start = (
                utc_datetime(self.start)
                if self.start is not None
                else end - timedelta(hours=24)
            )
        except OverflowError as error:
            raise ValueError("invalid_window") from error
        if start >= end or end - start > timedelta(days=30):
            raise ValueError("invalid_window")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        if type(self.limit) is not int or not 1 <= self.limit <= 100:
            raise ValueError("invalid_limit")
        for field in (
            "agent_id",
            "root_agent_id",
            "branch_run_id",
            "rule_id",
            "state",
        ):
            value = getattr(self, field)
            if value is not None:
                validate_identity(value)
        if self.rule_id is not None and self.rule_id not in RULE_IDS:
            raise ValueError("invalid_rule_id")
        if self.state is not None and self.state not in QUERY_STATES:
            raise ValueError("invalid_state")
        if self.model_name is not None:
            validate_identity(self.model_name, 256)
        if self.cursor is not None and (
            not isinstance(self.cursor, str)
            or len(self.cursor) > MAX_CURSOR_BYTES
        ):
            raise ValueError("invalid_cursor")

    def canonical(self):
        return {
            field.name: (
                utc_string(getattr(self, field.name))
                if field.name in {"start", "end"}
                else getattr(self, field.name)
            )
            for field in fields(self)
            if field.name != "cursor"
        }


def normalize_scope(scope):
    if isinstance(scope, DiagnosticScope):
        return scope
    if not isinstance(scope, (tuple, list)) or len(scope) != 2:
        raise ValueError("invalid_scope")
    return DiagnosticScope(*scope)


def canonical_hash(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()


def cursor_hashes(scope, filters, resource):
    scope = normalize_scope(scope)
    return canonical_hash(scope.pair()), canonical_hash(
        {"filters": filters.canonical(), "resource": resource},
    )


def validate_cursor(scope, filters, resource, cursor=None):
    """Validate before database reads; authorization comes solely from scope args."""
    token = filters.cursor if cursor is None else cursor
    if token is None:
        return None
    if (
        not isinstance(token, str)
        or not token
        or len(token) > MAX_CURSOR_BYTES
    ):
        raise ValueError("invalid_cursor")
    try:
        raw = base64.b64decode(
            token.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        value = json.loads(raw)
    except (ValueError, UnicodeError, binascii.Error) as error:
        raise ValueError("invalid_cursor") from error
    required = {"scope_hash", "filter_hash", "sort_time", "sort_id"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("invalid_cursor")
    if any(not isinstance(value[key], str) for key in required):
        raise ValueError("invalid_cursor")
    if (value["scope_hash"], value["filter_hash"]) != cursor_hashes(
        scope,
        filters,
        resource,
    ):
        raise ValueError("cursor_scope_or_filter_mismatch")
    try:
        value["sort_time"] = sql_time(value["sort_time"])
        validate_identity(value["sort_id"], 128)
        if resource.startswith(("context:", "segments:")):
            if (
                not value["sort_id"].isdigit()
                or int(value["sort_id"]) > 2147483647
            ):
                raise ValueError("invalid_cursor")
        if resource.startswith("prefix:"):
            comparison, index = value["sort_id"].rsplit(":", 1)
            validate_identity(comparison)
            if not index.isdigit() or int(index) > 2147483647:
                raise ValueError("invalid_cursor")
    except ValueError as error:
        raise ValueError("invalid_cursor") from error
    return value


def encode_cursor(scope, filters, resource, sort_time, sort_id):
    if isinstance(sort_time, str):
        sort_time = datetime.fromisoformat(sort_time.replace("Z", "+00:00"))
    if sort_time.tzinfo is None:
        sort_time = sort_time.replace(tzinfo=timezone.utc)
    scope_hash, filter_hash = cursor_hashes(scope, filters, resource)
    value = {
        "scope_hash": scope_hash,
        "filter_hash": filter_hash,
        "sort_time": utc_string(sort_time),
        "sort_id": str(sort_id),
    }
    return base64.urlsafe_b64encode(
        json.dumps(value, separators=(",", ":")).encode(),
    ).decode()

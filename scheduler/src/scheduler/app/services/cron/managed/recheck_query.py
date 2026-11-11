# -*- coding: utf-8 -*-
"""Read-only, scoped views of active rechecks and terminal receipts."""

import base64
from datetime import datetime, timezone
import json

from .dispatch_events import EVENTS
from .identity import identity_value
from .repository import _identity_terms, _cursor_context
from .contracts import ManagedCronUnavailable

TABLE = "swe_cron_recheck_tasks"
TERMINAL_EVENT = "recheck_terminal"
KEYS = ("tenant_id", "source_id", "agent_id", "job_id")
ACTIVE = "('queued','dispatching','accepted','running')"


def identity_join(left, right):
    return " AND ".join(
        f"{left}.{key}={right}.{key} AND "
        f"{left}.{key} COLLATE utf8mb4_bin={right}.{key}"
        for key in KEYS
    )


def invalidated():
    return (
        "(d.deleted_at IS NOT NULL OR d.enabled=0 OR s.job_id IS NULL "
        "OR s.pause_reason IS NOT NULL OR r.enqueued_state_version "
        "<COALESCE(s.queue_invalidation_version,0))"
    )


def sql_time(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _stamp(value):
    parsed = sql_time(value)
    return parsed.replace(tzinfo=timezone.utc).isoformat() if parsed else None


def _decode_position(cursor, context, history):
    if cursor is None:
        return None
    try:
        raw = base64.b64decode(
            cursor + "=" * (-len(cursor) % 4),
            altchars=b"-_",
            validate=True,
        )
        value = json.loads(raw)
        if value["context"] != context:
            raise ValueError("cursor_context")
        stamp, key = value["after"]
        position = sql_time(stamp)
        if position is None or (history and (type(key) is not int or key < 1)):
            raise ValueError("cursor_position")
        return position, key if history else identity_value(key)
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RecursionError,
    ) as exc:
        raise ValueError("invalid_cron_cursor") from exc


def _encode_position(row, context, history):
    stamp = _stamp(row["finished_at"] if history else row["run_at"])
    key = row["event_order"] if history else row["recheck_id"]
    raw = json.dumps(
        {"context": context, "after": [stamp, key]},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _page_item(row, caller, history):
    details = json.loads(row["details_json"]) if history else row
    result = {
        "recheck_id": row["recheck_id"],
        "parent_execution_id": details["parent_execution_id"],
        "job_id": row["job_id"],
        "job_name": row["job_name"],
        "run_at": _stamp(details.get("run_at")),
        "started_at": _stamp(details.get("started_at")),
        "status": details["status"],
    }
    if history:
        result.update(
            finished_at=_stamp(row["finished_at"]),
            reason_code=details.get("reason_code"),
            chat_id=(
                row.get("task_chat_id")
                if not row["deleted_at"]
                and row["creator_user_id"] == caller.actor_id
                else None
            ),
        )
    return result


async def page_rechecks(db, caller, *, view, limit, cursor=None):
    if view not in {"active", "history"} or not 1 <= limit <= 200:
        raise ValueError("invalid_cron_query")
    history = view == "history"
    context = {
        **_cursor_context(caller, history, None, ()),
        "view": view,
        "limit": limit,
    }
    after = _decode_position(cursor, context, history)
    names = list(KEYS[:3])
    scope = [caller.tenant_id, caller.source_id, caller.agent_id]
    if not caller.is_manager:
        names.append("creator_user_id")
        scope.append(caller.actor_id)
    clauses, params = _identity_terms(names, scope)
    clauses.append("d.management_owner='scheduler'")
    if history:
        source = EVENTS
        fields = (
            "r.execution_id AS recheck_id,r.job_id,r.details_json,"
            "r.occurred_at AS finished_at,r.event_order"
        )
        clauses.append("r.event_type='recheck_terminal'")
        position = ("r.occurred_at", "r.event_order")
    else:
        source = TABLE
        fields = "r.recheck_id,r.parent_execution_id,r.job_id,r.run_at,r.started_at,r.status"
        clauses.append(f"r.status IN {ACTIVE}")
        clauses.append(f"(r.status='running' OR NOT {invalidated()})")
        position = ("r.run_at", "r.recheck_id")
    if after:
        stamp, key = after
        operator = "<" if history else ">"
        first, second = position
        clauses.append(
            f"({first}{operator}%s OR ({first}=%s AND {second}{operator}%s))",
        )
        params.extend((stamp, stamp, key))
    direction = "DESC" if history else "ASC"
    ordering = ",".join(f"{field} {direction}" for field in position)
    try:
        rows = await db.fetch_all(
            f"SELECT {fields},d.name AS job_name,d.creator_user_id,d.deleted_at,"
            f"s.task_chat_id FROM {source} r "
            f"JOIN swe_cron_jobs_v2 d ON {identity_join('d', 'r')} "
            f"LEFT JOIN swe_cron_job_states s ON {identity_join('d', 's')} "
            f"WHERE {' AND '.join(clauses)} ORDER BY {ordering} LIMIT %s",
            (*params, limit + 1),
        )
        return {
            "items": [
                _page_item(row, caller, history) for row in rows[:limit]
            ],
            "next_cursor": (
                _encode_position(rows[limit - 1], context, history)
                if len(rows) > limit
                else None
            ),
        }
    except Exception as exc:
        raise ManagedCronUnavailable("cron_recheck_query_unavailable") from exc

# -*- coding: utf-8 -*-
"""复用执行事件保存逐项决策；调用前缀利用既有唯一索引分页。"""

import hashlib
import json
from uuid import UUID

from .daily_retry_filters import (
    FILTERED_QUEUED,
    FILTERED_RUNNING,
    DailyRetryFilters,
)
from .daily_retry_policy import json_object, retry_marker
from .dispatch_events import EVENTS
from .repository import IDENTITY_FIELDS
from .storage import insert

SCOPE_EVENT = "daily_retry_scope"


def event_prefix(call_id):
    return UUID(call_id).hex


def _scope_key(call_id):
    return hashlib.sha256(f"daily-retry-scope:{call_id}".encode()).hexdigest()


async def record_filters(tx, call, filters):
    key = _scope_key(call["call_id"])
    await insert(
        tx,
        EVENTS,
        {
            "event_id": key,
            "event_key": key,
            "source_id": call["source_id"],
            # 此事件记录来源级筛选，不承载单条任务身份。
            "tenant_id": "",
            "agent_id": "",
            "job_id": "",
            "actor_id": "",
            "event_type": SCOPE_EVENT,
            "occurred_at": call["created_at"],
            "details_json": filters.model_dump_json(),
        },
    )


async def read_filters(db, call):
    if call["status"] not in {FILTERED_QUEUED, FILTERED_RUNNING}:
        return None
    row = await db.fetch_one(
        f"SELECT details_json FROM {EVENTS} WHERE event_key=%s "
        "AND source_id COLLATE utf8mb4_bin=%s AND event_type=%s",
        (_scope_key(call["call_id"]), call["source_id"], SCOPE_EVENT),
    )
    if row is None:
        raise RuntimeError("cron_daily_retry_filters_missing")
    return DailyRetryFilters.model_validate_json(row["details_json"])


async def record_decision(tx, call, candidate, result, decision, reason, now):
    suffix = hashlib.sha256(candidate["execution_id"].encode()).hexdigest()[
        :32
    ]
    key = event_prefix(call["call_id"]) + suffix
    marker = retry_marker(result)
    details = {
        "call_id": call["call_id"],
        "decision": decision,
        "reason": reason,
        "previous_execution_id": candidate["execution_id"],
        "root_execution_id": marker.get("root_execution_id"),
        "retry_count": marker.get("retry_count"),
    }
    meta = json_object(result.get("dispatch_meta_json"))
    await insert(
        tx,
        EVENTS,
        {
            "event_id": hashlib.sha256(
                ("daily-retry:" + key).encode(),
            ).hexdigest(),
            "event_key": key,
            **{name: candidate[name] for name in IDENTITY_FIELDS},
            "execution_id": result["execution_id"],
            "batch_id": meta.get("batch_id"),
            "member_id": meta.get("batch_member_id"),
            "event_type": (
                "retry_accepted" if decision == "queued" else "retry_skipped"
            ),
            "execution_version": result["version"],
            "actor_id": candidate["actor_id"],
            "occurred_at": now,
            "details_json": json.dumps(details, ensure_ascii=False),
        },
    )


async def decision_items(db, source, call_id, *, after=0, limit=50):
    rows = await db.fetch_all(
        "SELECT e.*,r.status AS run_status,r.settled_status AS run_settled_status,"
        "r.error_code AS run_error_code,r.settled_error_code AS run_settled_error_code,"
        "r.history_execution_id FROM swe_cron_managed_dispatch_events e "
        "LEFT JOIN swe_cron_execution_runs r ON r.execution_id=e.execution_id "
        "AND r.tenant_id=e.tenant_id AND r.source_id=e.source_id AND r.agent_id=e.agent_id AND r.job_id=e.job_id "
        "WHERE e.event_key LIKE %s AND e.source_id=%s AND e.source_id COLLATE utf8mb4_bin=%s "
        "AND e.event_type IN ('retry_accepted','retry_skipped') "
        "AND e.event_order>%s ORDER BY e.event_order LIMIT %s",
        (event_prefix(call_id) + "%", source, source, after, limit + 1),
    )
    selected = rows[:limit]
    items = []
    for row in selected:
        detail = json_object(row["details_json"])
        execution = None
        if detail["decision"] == "queued":
            execution = {
                "execution_id": row["execution_id"],
                "status": row["run_status"],
                "settled_status": row["run_settled_status"],
                "error_code": row["run_error_code"],
                "settled_error_code": row["run_settled_error_code"],
                "history_execution_id": row["history_execution_id"],
            }
        items.append(
            {
                **{name: row[name] for name in IDENTITY_FIELDS},
                **detail,
                "event_order": row["event_order"],
                "execution": execution,
            },
        )
    return items, selected[-1]["event_order"] if len(rows) > limit else None

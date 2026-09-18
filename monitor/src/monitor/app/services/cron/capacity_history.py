"""Bounded keyset pages over an immutable capacity-history ID boundary."""

import base64
import binascii
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from ...database import DatabaseConnection

PAGE_SIZE = 100
CAPACITY_COLUMNS = """
    id, worker_id, source_id, provider_id, model_id, strategy_id,
    previous_workers, baseline_workers, min_workers, max_workers,
    effective_workers, pending_count, claimed_count, running_count,
    success_count, failure_count, error_rate, matched_rule,
    avg_latency_ms, decision_reason, created_at
"""


def _db_time(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is not None:
        return value.astimezone(timezone(timedelta(hours=8))).replace(
            tzinfo=None
        )
    return value


def _decode_cursor(
    token: str, scope: list[str | None]
) -> tuple[int, datetime, int]:
    try:
        if len(token) > 2048:
            raise ValueError
        data = json.loads(
            base64.b64decode(token, altchars=b"-_", validate=True)
        )
        if data["scope"] != scope:
            raise ValueError
        if any(
            type(data[k]) is not int or not 0 < data[k] <= 9223372036854775807
            for k in ("snapshot", "id")
        ):
            raise ValueError
        if data["id"] > data["snapshot"]:
            raise ValueError
        timestamp = datetime.fromisoformat(data["time"])
        if timestamp.tzinfo is not None:
            raise ValueError
        return data["snapshot"], timestamp, data["id"]
    except (ValueError, TypeError, KeyError, binascii.Error) as exc:
        raise ValueError("无效的 worker 历史游标，请重新查询") from exc


async def read_capacity_page(
    db: DatabaseConnection,
    source: str,
    start: datetime | None = None,
    end: datetime | None = None,
    cursor: str | None = None,
) -> tuple[list[dict], str | None]:
    start, end = _db_time(start), _db_time(end)
    if start and end and start > end:
        raise ValueError("结束时间不能早于开始时间")
    scope = [
        source,
        start.isoformat() if start else None,
        end.isoformat() if end else None,
    ]
    conditions = ["source_id = %s", "created_at IS NOT NULL"]
    params: list[Any] = [source]
    if start:
        conditions.append("created_at >= %s")
        params.append(start)
    if end:
        conditions.append("created_at <= %s")
        params.append(end)
    if cursor:
        snapshot, last_time, last_id = _decode_cursor(cursor, scope)
    else:
        maximum = await db.fetch_one(
            "SELECT MAX(id) AS snapshot_id "
            "FROM swe_cron_dispatch_worker_capacity "
            f"WHERE {' AND '.join(conditions)}",
            tuple(params),
        )
        snapshot = int((maximum or {}).get("snapshot_id") or 0)
    conditions.append("id <= %s")
    params.append(snapshot)
    if cursor:
        conditions.append("(created_at < %s OR (created_at = %s AND id < %s))")
        params.extend((last_time, last_time, last_id))
    rows = await db.fetch_all(
        f"SELECT {CAPACITY_COLUMNS} FROM swe_cron_dispatch_worker_capacity "
        f"WHERE {' AND '.join(conditions)} "
        "ORDER BY created_at DESC, id DESC LIMIT %s",
        (*params, PAGE_SIZE + 1),
    )
    next_cursor = None
    if len(rows) > PAGE_SIZE:
        last = rows[PAGE_SIZE - 1]
        timestamp = last["created_at"]
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)
        next_cursor = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "scope": scope,
                    "snapshot": snapshot,
                    "time": timestamp.isoformat(),
                    "id": int(last["id"]),
                },
                separators=(",", ":"),
            ).encode()
        ).decode()
    return rows[:PAGE_SIZE], next_cursor

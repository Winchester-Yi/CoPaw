# -*- coding: utf-8 -*-
"""Claim one-shot rechecks independently from ordinary Cron and batch capacity."""

from datetime import timedelta, timezone
import hashlib
import json
import logging
from uuid import uuid4

from .dispatch_events import EVENTS
from .dispatcher import DISPATCH_RETRY_DELAYS_SECONDS
from .identity import CallerIdentity
from .operations import utc_now
from .recheck_query import (
    KEYS,
    TABLE,
    TERMINAL_EVENT,
    identity_join,
    invalidated,
    sql_time,
)
from .storage import insert, transaction, update

logger = logging.getLogger(__name__)
WHERE_JOB = " AND ".join(f"`{name}`=%s" for name in KEYS)
RETRY_LIMIT = len(DISPATCH_RETRY_DELAYS_SECONDS) + 1
READINESS_COLUMNS = (
    "recheck_id,parent_execution_id,tenant_id,source_id,agent_id,job_id,actor_id,"
    "enqueued_state_version,payload_json,run_at,next_dispatch_at,created_at,"
    "started_at,status,version,dispatch_attempt,dispatch_owner,dispatch_lease_until,"
    "run_owner,run_lease_until,updated_at"
)
storage_ready = False


async def prepare_recheck_storage(db):
    """Probe optional storage at startup without blocking ordinary Cron."""
    global storage_ready
    storage_ready = False
    try:
        await db.fetch_all(f"SELECT {READINESS_COLUMNS} FROM {TABLE} LIMIT 0")
    except Exception as exc:
        logger.info("回检存储暂不可用 error_type=%s", type(exc).__name__)
        return False
    storage_ready = True
    return True


def _not_after(value, now):
    return value is None or sql_time(value) <= now


def _iso(value):
    parsed = sql_time(value)
    return parsed.replace(tzinfo=timezone.utc).isoformat() if parsed else None


async def _terminal(tx, row, now, status, reason):
    event_id = hashlib.sha256(
        ("cron_recheck_terminal:" + row["recheck_id"]).encode(),
    ).hexdigest()
    details = {
        "parent_execution_id": row["parent_execution_id"],
        "status": status,
        "run_at": _iso(row["run_at"]),
        "started_at": _iso(row["started_at"]),
        "reason_code": reason,
    }
    await insert(
        tx,
        EVENTS,
        {
            "event_id": event_id,
            "event_key": event_id,
            **{key: row[key] for key in KEYS},
            "execution_id": row["recheck_id"],
            "event_type": TERMINAL_EVENT,
            "actor_id": row["actor_id"],
            "occurred_at": now,
            "details_json": json.dumps(details, separators=(",", ":")),
        },
    )
    await tx.execute(
        f"DELETE FROM {TABLE} WHERE recheck_id=%s",
        (row["recheck_id"],),
    )


class RecheckWorker:
    def __init__(self, db, client, limit, *, clock=utc_now):
        self.db = db
        self.client = client
        self.limit = limit
        self.clock = clock

    async def tick(self):
        now = self.clock()
        rows = await self.db.fetch_all(
            f"SELECT r.recheck_id,r.tenant_id,r.source_id,r.agent_id,r.job_id "
            f"FROM {TABLE} r JOIN swe_cron_jobs_v2 d ON {identity_join('d', 'r')} "
            f"LEFT JOIN swe_cron_job_states s ON {identity_join('d', 's')} "
            f"WHERE (r.status<>'running' AND {invalidated()}) "
            "OR (r.status='queued' AND r.next_dispatch_at<=%s) "
            "OR (r.status IN ('dispatching','accepted') AND r.dispatch_lease_until<=%s) "
            "OR (r.status='running' AND r.run_lease_until<=%s AND r.next_dispatch_at<=%s) "
            "ORDER BY r.next_dispatch_at,r.recheck_id LIMIT %s",
            (now, now, now, now, self.limit),
        )
        for item in rows:
            try:
                await self._process(item)
            except Exception as exc:
                logger.warning(
                    "回检派发暂未完成 recheck_id=%s error_type=%s",
                    item["recheck_id"],
                    type(exc).__name__,
                )
        return len(rows)

    async def _process(self, item):
        async with transaction(self.db) as tx:
            key = tuple(item[field] for field in KEYS)
            definition = await tx.fetch_one(
                f"SELECT enabled,deleted_at FROM swe_cron_jobs_v2 "
                f"WHERE {WHERE_JOB} FOR UPDATE",
                key,
            )
            state = await tx.fetch_one(
                f"SELECT pause_reason,queue_invalidation_version "
                f"FROM swe_cron_job_states WHERE {WHERE_JOB} FOR UPDATE",
                key,
            )
            row = await tx.fetch_one(
                f"SELECT * FROM {TABLE} WHERE recheck_id=%s FOR UPDATE",
                (item["recheck_id"],),
            )
            if not row or tuple(row[field] for field in KEYS) != key:
                return
            action = await self._claim(
                tx,
                row,
                definition,
                state,
                self.clock(),
            )
        if action:
            caller = CallerIdentity(*key[:3], row["actor_id"])
            await self.client.recheck(action, row, caller)

    async def _claim(self, tx, row, definition, state, now):
        status = row["status"]
        if status == "running":
            if not (
                _not_after(row["run_lease_until"], now)
                and _not_after(row["next_dispatch_at"], now)
            ):
                return None
            await self._write(
                tx,
                row,
                now,
                next_dispatch_at=now + timedelta(seconds=30),
            )
            return "recover"
        if _recheck_invalidated(row, definition, state):
            await _terminal(
                tx,
                row,
                now,
                "cancelled",
                "cron_job_disabled_before_start",
            )
            return None
        if status in {"dispatching", "accepted"}:
            if not _not_after(row["dispatch_lease_until"], now):
                return None
            return await self._retry(tx, row, now)
        if status != "queued" or not _not_after(row["next_dispatch_at"], now):
            return None
        owner = str(uuid4())
        await self._write(
            tx,
            row,
            now,
            status="dispatching",
            dispatch_owner=owner,
            dispatch_lease_until=now + timedelta(seconds=30),
            dispatch_attempt=row["dispatch_attempt"] + 1,
        )
        row["dispatch_owner"] = owner
        return "execute"

    async def _retry(self, tx, row, now):
        attempt = row["dispatch_attempt"]
        if attempt >= RETRY_LIMIT:
            await _terminal(tx, row, now, "error", "cron_dispatch_unconfirmed")
        else:
            delay = DISPATCH_RETRY_DELAYS_SECONDS[max(attempt - 1, 0)]
            await self._write(
                tx,
                row,
                now,
                status="queued",
                dispatch_owner="",
                dispatch_lease_until=None,
                next_dispatch_at=now + timedelta(seconds=delay),
            )
        return None

    async def _write(self, tx, row, now, **values):
        await update(
            tx,
            TABLE,
            {**values, "version": row["version"] + 1, "updated_at": now},
            "recheck_id=%s",
            (row["recheck_id"],),
        )


def _recheck_invalidated(row, definition, state):
    return (
        not definition
        or definition["deleted_at"]
        or not definition["enabled"]
        or not state
        or state["pause_reason"]
        or row["enqueued_state_version"]
        < (state["queue_invalidation_version"] or 0)
    )

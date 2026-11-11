# -*- coding: utf-8 -*-
"""SWE writes the durable lifecycle of one-shot Cron rechecks."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError

from .models import CronRecheckSpec

TABLE = "swe_cron_recheck_tasks"
EVENTS = "swe_cron_managed_dispatch_events"
KEYS = ("tenant_id", "source_id", "agent_id", "job_id")
WHERE = " AND ".join(f"{name}=%s" for name in KEYS)
MAX_PARENT_OUTPUT_BYTES = 64 * 1024


class RecheckNotFound(LookupError):
    pass


class RecheckLeaseLost(RuntimeError):
    pass


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_text(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def identity(row):
    return tuple(row[name] for name in KEYS)


def invalidated(definition, state, version):
    return (
        not definition["enabled"]
        or definition["deleted_at"] is not None
        or state is None
        or bool(state.get("pause_reason"))
        or version < int(state.get("queue_invalidation_version") or 0)
    )


async def write_terminal(db, row, status, now, reason=None):
    event_key = hashlib.sha256(
        f"cron_recheck_terminal:{row['recheck_id']}".encode(),
    ).hexdigest()
    details = json.dumps(
        {
            "parent_execution_id": row["parent_execution_id"],
            "status": status,
            "run_at": utc_text(row.get("run_at")),
            "started_at": utc_text(row.get("started_at")),
            "reason_code": reason,
        },
    )
    await db.execute(
        f"INSERT INTO {EVENTS} (event_id,event_key,{','.join(KEYS)},"
        "execution_id,event_type,actor_id,occurred_at,details_json) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,'recheck_terminal',%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE event_key=event_key",
        (
            event_key,
            event_key,
            *identity(row),
            row["recheck_id"],
            row["actor_id"],
            now,
            details,
        ),
    )


def _recheck_spec(spec, rule):
    meta = spec["meta"]
    session, user, chat_id = (
        meta["task_session_id"],
        meta["creator_user_id"],
        meta["task_chat_id"],
    )
    if not all(
        isinstance(item, str) and item for item in (session, user, chat_id)
    ):
        raise ValueError("cron_recheck_binding_unavailable")
    return {
        **{
            name: spec[name]
            for name in (
                "id",
                "name",
                "schedule",
                "runtime",
                "tenant_id",
                "source_id",
                "scope_id",
                "model_slot",
            )
            if name in spec
        },
        "request": {
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": rule.instruction}],
                },
            ],
            "session_id": session,
            "user_id": user,
        },
        "dispatch": {
            "type": "channel",
            "channel": "console",
            "target": {"session_id": session, "user_id": user},
        },
        "meta": {
            "task_session_id": session,
            "creator_user_id": user,
            "task_chat_id": chat_id,
        },
    }


async def register_recheck(db, definition, run, outcome, now):
    """Use the parent's frozen rule while holding its existing result transaction."""
    if (
        run["kind"] not in {"ordinary", "batch"}
        or not definition["enabled"]
        or definition["deleted_at"]
    ):
        return
    spec = json.loads(run["spec_json"])
    config = spec.get("recheck")
    if config is None or spec.get("task_type", "agent") != "agent":
        return
    key = identity(run)
    state = await db.fetch_one(
        f"SELECT * FROM swe_cron_job_states WHERE {WHERE}",
        key,
    )
    if invalidated(definition, state, run["enqueued_state_version"]):
        return
    row = {
        **dict(zip(KEYS, key)),
        "recheck_id": str(
            uuid5(NAMESPACE_URL, f"cron-recheck:{run['execution_id']}"),
        ),
        "parent_execution_id": run["execution_id"],
        "actor_id": run["actor_id"],
    }
    try:
        rule = CronRecheckSpec.model_validate(config)
        row["run_at"] = now + timedelta(seconds=rule.delay_seconds)
        frozen_spec = _recheck_spec(spec, rule)
    except (ValidationError, ValueError, OverflowError, KeyError):
        await write_terminal(
            db,
            row,
            "error",
            now,
            "cron_recheck_invalid_snapshot",
        )
        return
    output = outcome.recheck_output
    if not output or len(output.encode("utf-8")) > MAX_PARENT_OUTPUT_BYTES:
        await write_terminal(
            db,
            row,
            "error",
            now,
            "cron_recheck_parent_output_unavailable",
        )
        return
    payload = json.dumps(
        {"spec": frozen_spec, "parent_output": output},
        ensure_ascii=False,
    )
    await db.execute(
        f"INSERT INTO {TABLE} (recheck_id,parent_execution_id,{','.join(KEYS)},"
        "actor_id,enqueued_state_version,payload_json,run_at,next_dispatch_at,"
        "created_at,updated_at,status) VALUES "
        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'queued') "
        "ON DUPLICATE KEY UPDATE recheck_id=recheck_id",
        (
            row["recheck_id"],
            run["execution_id"],
            *key,
            run["actor_id"],
            run["enqueued_state_version"],
            payload,
            row["run_at"],
            row["run_at"],
            now,
            now,
        ),
    )


class RecheckStore:
    def __init__(self, db):
        self.db = db

    async def definition(self, key, *, lock=False):
        row = await self.db.fetch_one(
            f"SELECT * FROM swe_cron_jobs_v2 WHERE {WHERE}"
            + (" FOR UPDATE" if lock else ""),
            key,
        )
        if (
            row is None
            or identity(row) != key
            or row["management_owner"] != "scheduler"
        ):
            raise RecheckNotFound()
        return row

    async def get(self, recheck_id, key, *, lock=False):
        row = await self.db.fetch_one(
            f"SELECT * FROM {TABLE} WHERE recheck_id=%s AND {WHERE}"
            + (" FOR UPDATE" if lock else ""),
            (recheck_id, *key),
        )
        if row is not None and identity(row) == key:
            return row
        event = await self.db.fetch_one(
            f"SELECT * FROM {EVENTS} WHERE execution_id=%s AND {WHERE} "
            "AND event_type='recheck_terminal'",
            (recheck_id, *key),
        )
        if event is None or identity(event) != key:
            raise RecheckNotFound()
        return {
            **event,
            "recheck_id": recheck_id,
            "status": json.loads(event["details_json"])["status"],
        }

    async def _change(self, row, now, **values):
        params = {**values, "updated_at": now}
        setters = ", ".join(f"{name}=%s" for name in params)
        await self.db.execute(
            f"UPDATE {TABLE} SET {setters}, version=version+1 "
            f"WHERE recheck_id=%s AND {WHERE} AND version=%s",
            (
                *params.values(),
                row["recheck_id"],
                *identity(row),
                row["version"],
            ),
        )
        return {**row, **params, "version": row["version"] + 1}

    async def _finish_locked(self, row, status, now, reason):
        await write_terminal(self.db, row, status, now, reason)
        await self.db.execute(
            f"DELETE FROM {TABLE} WHERE recheck_id=%s AND {WHERE} AND version=%s",
            (row["recheck_id"], *identity(row), row["version"]),
        )
        return {**row, "status": status}

    async def accept(self, recheck_id, key, token):
        async with self.db.transaction():
            await self.definition(key, lock=True)
            row = await self.get(recheck_id, key, lock=True)
            if row["status"] in {"running", "success", "error", "cancelled"}:
                return row
            now = now_utc()
            if (
                row["status"] not in {"dispatching", "accepted"}
                or row["dispatch_owner"] != token
                or row["dispatch_lease_until"] is None
                or row["dispatch_lease_until"] <= now
            ):
                raise RecheckLeaseLost()
            if row["status"] == "accepted":
                return row
            return await self._change(row, now, status="accepted")

    async def start(self, recheck_id, key, token, owner, timeout):
        async with self.db.transaction():
            definition = await self.definition(key, lock=True)
            row = await self.get(recheck_id, key, lock=True)
            now = now_utc()
            if row["status"] != "accepted" or row["dispatch_owner"] != token:
                return False
            if (
                row["dispatch_lease_until"] is None
                or row["dispatch_lease_until"] <= now
            ):
                return False
            if row["run_at"] > now:
                return False
            state = await self.db.fetch_one(
                f"SELECT * FROM swe_cron_job_states WHERE {WHERE}",
                key,
            )
            if invalidated(definition, state, row["enqueued_state_version"]):
                await self._finish_locked(
                    row,
                    "cancelled",
                    now,
                    "cron_job_disabled_before_start",
                )
                return False
            await self._change(
                row,
                now,
                status="running",
                started_at=now,
                run_owner=owner,
                run_lease_until=now + timedelta(seconds=timeout + 120),
                dispatch_owner="",
                dispatch_lease_until=None,
            )
            return True

    async def release(self, recheck_id, key, token):
        async with self.db.transaction():
            await self.definition(key, lock=True)
            row = await self.get(recheck_id, key, lock=True)
            if row["status"] != "accepted" or row["dispatch_owner"] != token:
                return
            now = now_utc()
            await self._change(
                row,
                now,
                status="queued",
                dispatch_owner="",
                dispatch_lease_until=None,
                next_dispatch_at=now + timedelta(seconds=5),
                dispatch_attempt=max(0, row["dispatch_attempt"] - 1),
            )

    async def finish(
        self,
        recheck_id,
        key,
        owner,
        status,
        reason,
        *,
        expired=False,
    ):
        async with self.db.transaction():
            await self.definition(key, lock=True)
            row = await self.get(recheck_id, key, lock=True)
            if row["status"] != "running" or row["run_owner"] != owner:
                return row
            now = now_utc()
            if expired and (
                row["run_lease_until"] is None or row["run_lease_until"] > now
            ):
                return row
            return await self._finish_locked(row, status, now, reason)

    async def reject(self, recheck_id, key, token, reason):
        async with self.db.transaction():
            await self.definition(key, lock=True)
            row = await self.get(recheck_id, key, lock=True)
            if row["status"] == "accepted" and row["dispatch_owner"] == token:
                return await self._finish_locked(
                    row,
                    "error",
                    now_utc(),
                    reason,
                )
            return row

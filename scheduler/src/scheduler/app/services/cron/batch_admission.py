"""Atomic parent callback admission under the same control lock as pause."""

import json
from datetime import datetime

from .batch_run_state import (
    CONTROL_COLUMNS,
    IDENTITY_WHERE,
    JOB_COLUMNS,
    RunStateNotReady,
    cursor_rows,
    is_batch_parent_definition,
)

BATCH_COLUMNS = (
    "batch_id,parent_job_id,parent_external_job_id,tenant_id,source_id,"
    "provider_id,model_id,agent_id,scheduled_fire_at,callback_received_at"
)
INTENT_COLUMNS = (
    "batch_id,intent_role,source_id,provider_id,model_id,tenant_id,agent_id,"
    "job_id,parent_job_id,scheduled_fire_at,due_at,dispatch_order,"
    "viewer_heat_score,max_attempts,payload"
)


async def _admit_locked(cur, batch, rows, physical_fire_at):
    identity = tuple(
        batch[k] for k in ("source_id", "tenant_id", "parent_job_id")
    )
    controls = await cursor_rows(
        cur,
        f"SELECT {CONTROL_COLUMNS} FROM swe_cron_dispatch_controls "
        f"WHERE {IDENTITY_WHERE} FOR UPDATE",
        identity,
        CONTROL_COLUMNS,
    )
    if not controls:
        raise RunStateNotReady("批调度控制状态尚未初始化")
    control = controls[0]
    if control["paused"]:
        return {"skipped": "batch_dispatch_paused", "intent_ids": []}
    parents = await cursor_rows(
        cur,
        f"SELECT {JOB_COLUMNS} FROM swe_cron_jobs "
        "WHERE source_id=%s AND tenant_id=%s AND id=%s",
        identity,
        JOB_COLUMNS,
    )
    if not parents or not is_batch_parent_definition(parents[0]):
        return {"skipped": "dispatch_mode_disabled", "intent_ids": []}
    existing = await cursor_rows(
        cur,
        "SELECT batch_id FROM swe_cron_dispatch_batches WHERE batch_id=%s",
        (batch["batch_id"],),
        "batch_id",
    )
    if not existing:
        resumed_at = control.get("resumed_at")
        if isinstance(resumed_at, str):
            resumed_at = datetime.fromisoformat(resumed_at)
        if resumed_at and physical_fire_at < resumed_at:
            return {"skipped": "before_batch_resume", "intent_ids": []}
        await cur.execute(
            "INSERT INTO swe_cron_dispatch_batches "
            f"({BATCH_COLUMNS},status,callback_metadata) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'received',%s)",
            (
                *[batch[k] for k in BATCH_COLUMNS.split(",")],
                json.dumps(batch.get("callback_metadata", {}), default=str),
            ),
        )
        parameters = []
        for row in rows:
            values = {
                **row,
                "batch_id": batch["batch_id"],
                "scheduled_fire_at": batch["scheduled_fire_at"],
                "max_attempts": 3,
                "payload": json.dumps(row.get("payload", {}), default=str),
            }
            parameters.append(
                tuple(values[k] for k in INTENT_COLUMNS.split(","))
            )
        if parameters:
            await cur.executemany(
                "INSERT INTO swe_cron_dispatch_intents "
                f"({INTENT_COLUMNS},status,attempt_count) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "%s,%s,%s,%s,%s,'pending',0)",
                parameters,
            )
    intents = await cursor_rows(
        cur,
        "SELECT id FROM swe_cron_dispatch_intents WHERE batch_id=%s "
        "AND source_id=%s ORDER BY dispatch_order,id",
        (batch["batch_id"], batch["source_id"]),
        "id",
    )
    if not existing:
        await _record_admission(cur, batch, rows, intents)
    return {"intent_ids": [int(row["id"]) for row in intents]}


async def _record_admission(cur, batch, rows, intents):
    sql = (
        "INSERT INTO swe_cron_dispatch_events "
        "(batch_id,intent_id,event_type,source_id,tenant_id,job_id,details) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)"
    )
    parameters = [
        (
            batch["batch_id"],
            None,
            "batch_callback_received",
            batch["source_id"],
            batch["tenant_id"],
            batch["parent_job_id"],
            json.dumps({"count": len(rows)}),
        )
    ]
    for intent, row in zip(intents, rows, strict=True):
        parameters.append(
            (
                batch["batch_id"],
                intent["id"],
                f"{row['intent_role']}_execution_intent_queued",
                row["source_id"],
                row["tenant_id"],
                row["job_id"],
                json.dumps(
                    {
                        "dispatch_order": row["dispatch_order"],
                        "provider_id": row["provider_id"],
                        "model_id": row["model_id"],
                    }
                ),
            )
        )
    await cur.executemany(sql, parameters)


async def admit_batch(db, batch, rows, *, physical_fire_at):
    async with db.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                result = await _admit_locked(
                    cur, batch, rows, physical_fire_at
                )
            await conn.commit()
            return result
        except BaseException:
            await conn.rollback()
            raise

"""Short transactions separating reversible claims from callback handoff."""

import json

from .batch_run_state import (
    CONTROL_COLUMNS,
    IDENTITY_WHERE,
    JOB_COLUMNS,
    RunStateNotReady,
    cursor_rows,
    enabled_value,
    is_batch_parent_definition,
)

SKIP_MESSAGES = {
    "job_disabled": "任务已关闭，跳过执行",
    "job_unavailable": "任务已删除或不存在，跳过执行",
    "dispatch_mode_disabled": "父任务已退出批调度或已删除，跳过执行",
}

BATCH_IDENTITY_SQL = (
    "SELECT source_id,tenant_id,parent_job_id "
    "FROM swe_cron_dispatch_batches WHERE batch_id=%s AND source_id=%s"
)


async def read_batch_identity(db, batch_id: str, source: str) -> tuple:
    """Read immutable batch ownership before starting a gated transaction."""
    batch = await db.fetch_one(BATCH_IDENTITY_SQL, (batch_id, source))
    if not batch:
        raise LookupError("Batch not found")
    return tuple(batch[k] for k in ("source_id", "tenant_id", "parent_job_id"))


async def lock_batch_control(
    cur, batch_id: str, source: str, *, skip_locked=False, parent_identity=None
):
    if parent_identity is None:
        batches = await cursor_rows(
            cur,
            BATCH_IDENTITY_SQL,
            (batch_id, source),
            "source_id,tenant_id,parent_job_id",
        )
        if not batches:
            return None
        parent_identity = tuple(
            batches[0][k] for k in ("source_id", "tenant_id", "parent_job_id")
        )
    controls = await cursor_rows(
        cur,
        f"SELECT {CONTROL_COLUMNS} FROM swe_cron_dispatch_controls "
        f"WHERE {IDENTITY_WHERE} FOR UPDATE"
        + (" SKIP LOCKED" if skip_locked else ""),
        parent_identity,
        CONTROL_COLUMNS,
    )
    return controls[0] if controls else None


async def skip_locked_intent(cur, row: dict, reason: str, now) -> None:
    await cur.execute(
        "UPDATE swe_cron_dispatch_intents SET status='skipped', "
        "completed_at=%s,updated_at=%s,locked_at=NULL,"
        "lock_owner='',error_message=%s "
        "WHERE id=%s AND claim_token=%s AND attempt_count=%s "
        "AND status IN ('claimed','acknowledged')",
        (
            now,
            now,
            SKIP_MESSAGES[reason],
            row["id"],
            row["claim_token"],
            row["attempt_count"],
        ),
    )
    await cur.execute(
        "INSERT INTO swe_cron_dispatch_events "
        "(batch_id,intent_id,event_type,source_id,tenant_id,job_id,details) "
        "VALUES (%s,%s,'intent_skipped',%s,%s,%s,%s)",
        (
            row["batch_id"],
            row["id"],
            row["source_id"],
            row["tenant_id"],
            row["job_id"],
            json.dumps({"reason": reason, "attempt": row["attempt_count"]}),
        ),
    )


async def _check_claim(cur, row: dict, owner: str) -> bool:
    # Lock the batch before the intent, consistently with manual recovery.
    await cursor_rows(
        cur,
        "SELECT batch_id FROM swe_cron_dispatch_batches "
        "WHERE batch_id=%s AND source_id=%s FOR UPDATE",
        (row["batch_id"], row["source_id"]),
        "batch_id",
    )
    records = await cursor_rows(
        cur,
        "SELECT status FROM swe_cron_dispatch_intents WHERE id=%s "
        "AND batch_id=%s AND source_id=%s AND job_id=%s AND tenant_id=%s "
        "AND lock_owner=%s AND attempt_count=%s AND claim_token=%s FOR UPDATE",
        (
            row["id"],
            row["batch_id"],
            row["source_id"],
            row["job_id"],
            row["tenant_id"],
            owner,
            row["attempt_count"],
            row["claim_token"],
        ),
        "status",
    )
    return bool(records and records[0]["status"] == "claimed")


async def _prepare_locked(cur, row: dict, owner: str, now, identity) -> str:
    control = await lock_batch_control(
        cur,
        row["batch_id"],
        row["source_id"],
        parent_identity=identity,
    )
    if control is None:
        raise RunStateNotReady("批调度控制状态缺失，停止派发")
    if not await _check_claim(cur, row, owner):
        return "stale"
    if control["paused"]:
        await cur.execute(
            "UPDATE swe_cron_dispatch_intents SET status='pending', "
            "attempt_count=CASE WHEN attempt_count>0 "
            "THEN attempt_count-1 ELSE 0 END, "
            "claim_token='',lock_owner='',locked_at=NULL,"
            "updated_at=%s WHERE id=%s",
            (now, row["id"]),
        )
        return "paused"
    parents = await cursor_rows(
        cur,
        f"SELECT {JOB_COLUMNS} FROM swe_cron_jobs "
        "WHERE source_id=%s AND tenant_id=%s AND id=%s",
        tuple(control[k] for k in ("source_id", "tenant_id", "parent_job_id")),
        JOB_COLUMNS,
    )
    reason = ""
    if not parents or not is_batch_parent_definition(parents[0]):
        reason = "dispatch_mode_disabled"
    else:
        jobs = await cursor_rows(
            cur,
            f"SELECT {JOB_COLUMNS} FROM swe_cron_jobs "
            "WHERE source_id=%s AND tenant_id=%s AND id=%s",
            (row["source_id"], row["tenant_id"], row["job_id"]),
            JOB_COLUMNS,
        )
        if not jobs or jobs[0]["deleted_at"]:
            reason = "job_unavailable"
        elif (
            not enabled_value(jobs[0]["enabled"])
            or jobs[0]["status"] != "active"
        ):
            reason = "job_disabled"
    if reason:
        await skip_locked_intent(cur, row, reason, now)
        return "skipped"
    await cur.execute(
        "UPDATE swe_cron_dispatch_intents "
        "SET status='acknowledged',acked_at=%s,locked_at=%s "
        "WHERE id=%s AND status='claimed' AND claim_token=%s",
        (now, now, row["id"], row["claim_token"]),
    )
    return "ready"


async def prepare_handoff(db, row, owner: str, now) -> str:
    row = row.model_dump() if hasattr(row, "model_dump") else dict(row)
    if not row.get("claim_token"):
        return "stale"
    # A plain SELECT inside BEGIN before the control lock would establish an
    # RR snapshot that could outlive a concurrent job-disable operation.
    identity = await read_batch_identity(db, row["batch_id"], row["source_id"])
    async with db.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                outcome = await _prepare_locked(cur, row, owner, now, identity)
            await conn.commit()
            return outcome
        except BaseException:
            await conn.rollback()
            raise


async def settle_callback_skip(db, row, owner: str, now) -> bool:
    row = row.model_dump() if hasattr(row, "model_dump") else dict(row)
    async with db.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                records = await cursor_rows(
                    cur,
                    "SELECT id FROM swe_cron_dispatch_intents WHERE id=%s "
                    "AND status='acknowledged' "
                    "AND lock_owner=%s AND claim_token=%s "
                    "AND attempt_count=%s FOR UPDATE",
                    (
                        row["id"],
                        owner,
                        row["claim_token"],
                        row["attempt_count"],
                    ),
                    "id",
                )
                if records:
                    await skip_locked_intent(cur, row, "job_disabled", now)
            await conn.commit()
            return bool(records)
        except BaseException:
            await conn.rollback()
            raise

"""Source-scoped failure previews and audited single-attempt manual retries."""

import json
from datetime import datetime, timedelta, timezone
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field

from scheduler.app.database import get_db_connection
from .dispatch_gate import lock_batch_control, read_batch_identity
from .batch_run_state import RunStateNotReady

# Ordered, narrow signatures shared by Python classification and SQL counts.
FAILURE_RULES = (
    ("auth_expired", "鉴权过期", ("cron auth user_info is expired",)),
    ("subtask_timeout", "子任务状态超时", ("获取子任务状态超时",)),
    ("subtask_failed", "子任务执行失败", ("子任务执行失败",)),
    (
        "configuration",
        "配置或请求校验错误",
        (
            "channel not found",
            "validation error for agentrequest",
            "illegal argument",
            "callback base url is not configured",
        ),
    ),
    (
        "model_rate_limit",
        "模型限流",
        (
            "rate limit",
            "rate-limited",
            "lailgw0429",
            "lailgw0433",
            "过多的访问请求",
            "too many requests",
            "每分钟上限",
        ),
    ),
    (
        "outcome_unknown",
        "执行结果未知",
        (
            "dispatch outcome unknown",
            "no execution record",
            "callback outcome unknown",
        ),
    ),
    ("execution_timeout", "执行超时", ("timeout", "timed out", "超时")),
    (
        "service_error",
        "网络或服务异常",
        (
            "connection",
            "connecterror",
            "network",
            "502",
            "503",
            "504",
        ),
    ),
)
FAILURE_LABELS = {key: label for key, label, _ in FAILURE_RULES}
FAILURE_LABELS["other"] = "其他错误"
RESOLUTION_TYPES = {"auth_expired", "configuration"}
STOP_TYPES = {
    "outcome_unknown",
    "execution_timeout",
    "subtask_timeout",
    "other",
}
PREVIEW_LIMIT = 200


def classify_failure(error: str) -> str:
    text = (error or "").lower()
    return next(
        (
            key
            for key, _, patterns in FAILURE_RULES
            if any(p in text for p in patterns)
        ),
        "other",
    )


def failure_case_sql() -> str:
    clauses = []
    for key, _, patterns in FAILURE_RULES:
        conditions = " OR ".join(
            "INSTR(LOWER(COALESCE(error_message, '')), '"
            + p.replace("'", "''")
            + "') > 0"
            for p in patterns
        )
        clauses.append(f"WHEN {conditions} THEN '{key}'")
    return "CASE " + " ".join(clauses) + " ELSE 'other' END"


class RetryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(gt=0)
    attempt_count: int = Field(ge=0)


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[RetryCandidate] = Field(min_length=1, max_length=200)
    confirm_resolved: bool = False
    confirm_stopped: bool = False


async def failure_preview(source: str, batch: str, types: list[str]) -> dict:
    if any(t not in FAILURE_LABELS for t in types):
        raise ValueError("未知失败类型")
    db = get_db_connection()
    if not await db.fetch_one(
        "SELECT batch_id FROM swe_cron_dispatch_batches "
        "WHERE source_id=%s AND batch_id=%s",
        (source, batch),
    ):
        raise LookupError("Batch not found")
    case = failure_case_sql()
    where = "source_id=%s AND batch_id=%s AND status='failed'"
    counts = await db.fetch_all(
        f"SELECT {case} AS failure_type, COUNT(*) AS count "
        f"FROM swe_cron_dispatch_intents WHERE {where} GROUP BY failure_type",
        (source, batch),
    )
    params = [source, batch]
    if types:
        where += f" AND ({case}) IN ({','.join(['%s'] * len(types))})"
        params.extend(types)
    items = await db.fetch_all(
        "SELECT id, attempt_count, tenant_id, error_message, "
        f"{case} AS failure_type FROM swe_cron_dispatch_intents "
        f"WHERE {where} ORDER BY dispatch_order, id LIMIT %s",
        (*params, PREVIEW_LIMIT + 1),
    )
    return {
        "counts": [
            dict(row, label=FAILURE_LABELS[row["failure_type"]])
            for row in counts
        ],
        "items": items[:PREVIEW_LIMIT],
        "has_more": len(items) > PREVIEW_LIMIT,
    }


async def retry_failed(
    source: str, batch: str, actor: str, body: RetryRequest
) -> dict:
    db = get_db_connection()
    identity = await read_batch_identity(db, batch, source)
    queued = 0
    now = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
    # Batch lock serializes manual operations, including duplicate submissions.
    async with db.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                control = await lock_batch_control(
                    cur,
                    batch,
                    source,
                    parent_identity=identity,
                )
                if control is None:
                    raise RunStateNotReady("批调度运行状态未就绪，不能重试")
                dispatch_paused = bool(control["paused"])
                await cur.execute(
                    "SELECT batch_id FROM swe_cron_dispatch_batches "
                    "WHERE source_id=%s AND batch_id=%s FOR UPDATE",
                    (source, batch),
                )
                if not await cur.fetchall():
                    raise LookupError("Batch not found")
                for candidate in {c.id: c for c in body.candidates}.values():
                    queued += await _retry_candidate(
                        cur,
                        source,
                        batch,
                        actor,
                        candidate,
                        body,
                        now,
                    )
                if queued:
                    await _refresh_batch(cur, source, batch, now)
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
    return {
        "queued": queued,
        "skipped": len(body.candidates) - queued,
        "dispatch_paused": dispatch_paused,
    }


async def _retry_candidate(cur, source, batch, actor, candidate, body, now):
    await cur.execute(
        "SELECT error_message FROM swe_cron_dispatch_intents "
        "WHERE id=%s AND source_id=%s AND batch_id=%s "
        "AND status='failed' AND attempt_count=%s "
        "AND EXISTS (SELECT 1 FROM swe_cron_jobs j "
        "WHERE j.id=swe_cron_dispatch_intents.job_id "
        "AND j.tenant_id=swe_cron_dispatch_intents.tenant_id "
        "AND j.source_id=swe_cron_dispatch_intents.source_id "
        "AND j.enabled=1 AND j.status='active') FOR UPDATE",
        (candidate.id, source, batch, candidate.attempt_count),
    )
    rows = await cur.fetchall()
    if not rows:
        return 0
    error = (
        rows[0].get("error_message")
        if isinstance(rows[0], Mapping)
        else rows[0][0]
    ) or ""
    category = classify_failure(error)
    if category in RESOLUTION_TYPES and not body.confirm_resolved:
        raise ValueError("请先修复鉴权或配置，并确认已修复")
    if category in STOP_TYPES and not body.confirm_stopped:
        raise ValueError("请核对旧执行，并确认旧执行已停止")
    await cur.execute(
        "INSERT INTO swe_cron_dispatch_events "
        "(batch_id, intent_id, source_id, event_type, details, created_at) "
        "VALUES (%s,%s,%s,'manual_retry_requested',%s,%s)",
        (
            batch,
            candidate.id,
            source,
            json.dumps(
                {
                    "actor": actor,
                    "previous_attempt": candidate.attempt_count,
                    "error": error,
                    "failure_type": category,
                    "confirm_resolved": body.confirm_resolved,
                    "confirm_stopped": body.confirm_stopped,
                },
                ensure_ascii=False,
            ),
            now,
        ),
    )
    await cur.execute(
        "UPDATE swe_cron_dispatch_intents SET status='pending', "
        "max_attempts=attempt_count+1, due_at=%s, completed_at=NULL, "
        "locked_at=NULL, lock_owner='', error_message='', updated_at=%s "
        "WHERE id=%s AND source_id=%s AND batch_id=%s "
        "AND status='failed' AND attempt_count=%s",
        (now, now, candidate.id, source, batch, candidate.attempt_count),
    )
    return 1


async def _refresh_batch(cur, source, batch, now):
    await cur.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS done, "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
        "SUM(CASE WHEN status IN ('skipped','cancelled') THEN 1 ELSE 0 END) AS skipped, "
        "SUM(CASE WHEN status IN ('claimed','acknowledged','dispatched') "
        "THEN 1 ELSE 0 END) AS running, "
        "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending "
        "FROM swe_cron_dispatch_intents WHERE source_id=%s AND batch_id=%s",
        (source, batch),
    )
    rows = await cur.fetchall()
    row = rows[0]
    values = (
        [
            row[k]
            for k in (
                "total",
                "done",
                "failed",
                "skipped",
                "running",
                "pending",
            )
        ]
        if isinstance(row, Mapping)
        else row
    )
    total, done, failed, skipped, running, pending = (
        int(v or 0) for v in values
    )
    status = (
        "running"
        if running
        else "pending" if pending else "failed" if failed else "completed"
    )
    if total == 0:
        status = "received"
    elif not running and not pending and done + failed + skipped < total:
        status = "received"
    terminal = status in {"failed", "completed"}
    await cur.execute(
        "UPDATE swe_cron_dispatch_batches SET total_count=%s, "
        "completed_count=%s, failed_count=%s, skipped_count=%s, "
        "status=%s, completed_at=CASE WHEN %s THEN COALESCE(completed_at,%s) ELSE NULL END, updated_at=%s, "
        "lock_owner=CASE WHEN %s THEN '' ELSE lock_owner END, "
        "locked_at=CASE WHEN %s THEN NULL ELSE locked_at END "
        "WHERE source_id=%s AND batch_id=%s",
        (
            total,
            done,
            failed,
            skipped,
            status,
            terminal,
            now,
            now,
            terminal,
            terminal,
            source,
            batch,
        ),
    )


async def refresh_batch_counts(batch: str, now: datetime, *, db=None) -> None:
    """Serialize result projection with manual retries using the batch row."""
    db = db if db is not None else get_db_connection()
    async with db.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT source_id FROM swe_cron_dispatch_batches "
                    "WHERE batch_id=%s FOR UPDATE",
                    (batch,),
                )
                rows = await cur.fetchall()
                if rows:
                    source = (
                        rows[0]["source_id"]
                        if isinstance(rows[0], Mapping)
                        else rows[0][0]
                    )
                    await _refresh_batch(cur, source, batch, now)
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise

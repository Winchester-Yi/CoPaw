"""Scheduler-owned batch controls, separate from replicated definitions."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from ...database.connection import get_db_connection

BEIJING = timezone(timedelta(hours=8))
MODE_KEY = "broadcast_dispatch_intents_enabled"
PARENT_KEY = "broadcast_source_job_id"
JOB_COLUMNS = "id,tenant_id,source_id,enabled,status,deleted_at,meta"
CONTROL_COLUMNS = (
    "source_id,tenant_id,parent_job_id,paused,version,resumed_at,"
    "updated_by,created_at,updated_at"
)
IDENTITY_WHERE = "source_id=%s AND tenant_id=%s AND parent_job_id=%s"


class RunStateNotReady(RuntimeError):
    """Schema or the eligible parent's control has not been initialized."""


class RunStateConflict(ValueError):
    """An operator edited a stale control version."""


def parse_meta(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def enabled_value(value: Any) -> bool:
    return (
        value is True
        or value == 1
        or (isinstance(value, str) and value.strip().lower() in {"true", "1"})
    )


def is_batch_parent_definition(row: Mapping[str, Any]) -> bool:
    meta = parse_meta(row.get("meta"))
    return (
        row.get("status") in {"active", "paused"}
        and not row.get("deleted_at")
        and enabled_value(meta.get(MODE_KEY))
        and not meta.get(PARENT_KEY)
    )


def initial_paused(row: Mapping[str, Any]) -> bool:
    return not enabled_value(row.get("enabled"))


def now_local() -> datetime:
    return datetime.now(BEIJING).replace(tzinfo=None)


async def cursor_rows(
    cur, sql: str, params=(), columns: str = ""
) -> list[dict]:
    await cur.execute(sql, params)
    rows = await cur.fetchall()
    names = columns.split(",")
    return [
        dict(row) if isinstance(row, Mapping) else dict(zip(names, row))
        for row in rows
    ]


async def control_event(cur, identity, event: str, details: dict) -> None:
    source, tenant, parent = identity
    await cur.execute(
        "INSERT INTO swe_cron_dispatch_events "
        "(batch_id,intent_id,event_type,job_id,tenant_id,source_id,details) "
        "VALUES ('',NULL,%s,%s,%s,%s,%s)",
        (
            event,
            parent,
            tenant,
            source,
            json.dumps(details, ensure_ascii=False),
        ),
    )


async def initialize_parent(db, row: Mapping[str, Any]) -> bool:
    """Serialize first insertion on the definition; never overwrite state."""
    identity = tuple(
        str(row.get(k) or "").strip() for k in ("source_id", "tenant_id", "id")
    )
    if not all(identity) or not is_batch_parent_definition(row):
        return False
    async with db.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                definitions = await cursor_rows(
                    cur,
                    f"SELECT {JOB_COLUMNS} FROM swe_cron_jobs "
                    "WHERE source_id=%s AND tenant_id=%s AND id=%s FOR UPDATE",
                    identity,
                    JOB_COLUMNS,
                )
                if not definitions or not is_batch_parent_definition(
                    definitions[0]
                ):
                    await conn.commit()
                    return False
                controls = await cursor_rows(
                    cur,
                    f"SELECT {CONTROL_COLUMNS} "
                    "FROM swe_cron_dispatch_controls "
                    f"WHERE {IDENTITY_WHERE}",
                    identity,
                    CONTROL_COLUMNS,
                )
                # The job row serializes initialization of this parent.
                # Locking a missing control instead would gap-lock unrelated
                # parents and deadlock concurrent inserts into the empty range.
                if controls:
                    await conn.commit()
                    return False
                paused = initial_paused(definitions[0])
                await cur.execute(
                    "INSERT INTO swe_cron_dispatch_controls "
                    "(source_id,tenant_id,parent_job_id,paused,updated_by) "
                    "VALUES (%s,%s,%s,%s,'compatibility') "
                    "ON DUPLICATE KEY UPDATE parent_job_id = parent_job_id",
                    (*identity, int(paused)),
                )
                await control_event(
                    cur,
                    identity,
                    "batch_dispatch_initialized",
                    {"paused": paused, "version": 1},
                )
            await conn.commit()
            return True
        except BaseException:
            await conn.rollback()
            raise


async def initialize_missing_controls(
    db, *, dry_run: bool = False
) -> dict[str, int]:
    report = dict(
        scanned=0, eligible=0, initialized=0, existing=0, ignored=0, invalid=0
    )
    after = ""
    while True:
        rows = await db.fetch_all(
            f"SELECT {JOB_COLUMNS} FROM swe_cron_jobs "
            "WHERE id>%s ORDER BY id LIMIT 500",
            (after,),
        )
        if not rows:
            return report
        for row in rows:
            report["scanned"] += 1
            if not _metadata_is_valid(row.get("meta")):
                report["invalid"] += 1
                continue
            if not is_batch_parent_definition(row):
                report["ignored"] += 1
                continue
            if not all(
                str(row.get(k) or "").strip()
                for k in ("id", "tenant_id", "source_id")
            ):
                report["invalid"] += 1
                continue
            report["eligible"] += 1
            if not dry_run:
                inserted = await initialize_parent(db, row)
                report["initialized" if inserted else "existing"] += 1
        after = str(rows[-1]["id"])


def _metadata_is_valid(raw: Any) -> bool:
    try:
        meta = (
            json.loads(raw or "{}")
            if isinstance(raw, str) or raw is None
            else raw
        )
    except (TypeError, ValueError):
        return False
    if not isinstance(meta, dict):
        return False
    value = meta.get(MODE_KEY)
    return (
        value is None
        or isinstance(value, bool)
        or (type(value) is int and value in (0, 1))
        or (
            isinstance(value, str)
            and value.strip().lower() in {"true", "false", "1", "0"}
        )
    )


async def assert_run_state_schema_ready(db) -> None:
    try:
        await db.fetch_all(
            f"SELECT {CONTROL_COLUMNS} FROM swe_cron_dispatch_controls LIMIT 0"
        )
        await db.fetch_all(
            "SELECT claim_token FROM swe_cron_dispatch_intents LIMIT 0"
        )
        await db.fetch_all(
            "SELECT skipped_count FROM swe_cron_dispatch_batches LIMIT 0"
        )
    except Exception as exc:
        raise RunStateNotReady(
            "批调度状态迁移未就绪，请先执行迁移并检查数据库权限"
        ) from exc


async def validated_parent(db, identity) -> dict:
    row = await db.fetch_one(
        f"SELECT {JOB_COLUMNS} FROM swe_cron_jobs "
        "WHERE source_id=%s AND tenant_id=%s AND id=%s",
        identity,
    )
    if not row or row.get("deleted_at"):
        raise LookupError("batch parent not found")
    if not is_batch_parent_definition(row):
        raise ValueError("仅批调度父任务可操作整批运行状态")
    return row


def serialize_control(row: dict) -> dict:
    result = dict(row)
    result["paused"] = bool(row["paused"])
    for key in ("created_at", "updated_at", "resumed_at"):
        value = result.get(key)
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if isinstance(value, datetime):
            result[key] = value.replace(tzinfo=BEIJING).isoformat()
    return result


async def get_run_state(source: str, tenant: str, parent: str) -> dict:
    db = get_db_connection()
    identity = (source, tenant, parent)
    await validated_parent(db, identity)
    row = await db.fetch_one(
        f"SELECT {CONTROL_COLUMNS} FROM swe_cron_dispatch_controls "
        f"WHERE {IDENTITY_WHERE}",
        identity,
    )
    if not row:
        raise RunStateNotReady("批调度运行状态尚未初始化，请先执行兼容初始化")
    counts = await db.fetch_one(
        "SELECT SUM(CASE WHEN i.status='claimed' THEN 1 ELSE 0 END) "
        "AS claimed_count, SUM(CASE WHEN i.status IN "
        "('acknowledged','dispatched') THEN 1 ELSE 0 END) AS inflight_count "
        "FROM swe_cron_dispatch_intents i JOIN swe_cron_dispatch_batches b "
        "ON b.batch_id=i.batch_id AND b.source_id=i.source_id "
        "WHERE b.source_id=%s AND b.tenant_id=%s AND b.parent_job_id=%s",
        identity,
    )
    return {
        **serialize_control(row),
        **{k: int(v or 0) for k, v in (counts or {}).items()},
    }


async def refund_unstarted_claims(cur, identity, now: datetime) -> None:
    batches = await cursor_rows(
        cur,
        "SELECT batch_id FROM swe_cron_dispatch_batches "
        "WHERE source_id=%s AND tenant_id=%s AND parent_job_id=%s "
        "ORDER BY batch_id FOR UPDATE",
        identity,
        "batch_id",
    )
    for batch in batches:
        await cur.execute(
            "UPDATE swe_cron_dispatch_intents SET status='pending', "
            "attempt_count=CASE WHEN attempt_count>0 "
            "THEN attempt_count-1 ELSE 0 END, "
            "claim_token='',lock_owner='',locked_at=NULL,updated_at=%s "
            "WHERE source_id=%s AND batch_id=%s AND status='claimed' "
            "AND claim_token<>''",
            (now, identity[0], batch["batch_id"]),
        )
        await cur.execute(
            "UPDATE swe_cron_dispatch_batches "
            "SET lock_owner='',locked_at=NULL, status=CASE WHEN EXISTS "
            "(SELECT 1 FROM swe_cron_dispatch_intents i "
            "WHERE i.batch_id=%s AND i.source_id=%s "
            "AND i.status IN ('claimed','acknowledged','dispatched')) "
            "THEN 'running' "
            "WHEN EXISTS (SELECT 1 FROM swe_cron_dispatch_intents i "
            "WHERE i.batch_id=%s AND i.source_id=%s AND i.status='pending') "
            "THEN 'pending' "
            "ELSE status END WHERE batch_id=%s AND source_id=%s",
            (batch["batch_id"], identity[0]) * 3,
        )


async def set_run_state(
    source: str,
    tenant: str,
    parent: str,
    actor: str,
    paused: bool,
    expected_version: int,
) -> dict:
    db = get_db_connection()
    identity = (source, tenant, parent)
    await validated_parent(db, identity)
    async with db.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                rows = await cursor_rows(
                    cur,
                    f"SELECT {CONTROL_COLUMNS} "
                    "FROM swe_cron_dispatch_controls "
                    f"WHERE {IDENTITY_WHERE} FOR UPDATE",
                    identity,
                    CONTROL_COLUMNS,
                )
                if not rows:
                    raise RunStateNotReady("批调度运行状态尚未初始化")
                current = rows[0]
                same = bool(current["paused"]) == paused
                version = int(current["version"])
                if version != expected_version and not (
                    same and version == expected_version + 1
                ):
                    raise RunStateConflict("批调度状态已被修改，请刷新后重试")
                if not same:
                    now = now_local()
                    await cur.execute(
                        "UPDATE swe_cron_dispatch_controls "
                        "SET paused=%s,version=version+1, "
                        "resumed_at=CASE WHEN %s=0 THEN %s "
                        "ELSE resumed_at END, "
                        f"updated_at=%s,updated_by=%s WHERE {IDENTITY_WHERE}",
                        (int(paused), int(paused), now, now, actor, *identity),
                    )
                    if paused:
                        await refund_unstarted_claims(cur, identity, now)
                    await control_event(
                        cur,
                        identity,
                        "batch_dispatch_state_changed",
                        {
                            "paused": paused,
                            "version": version + 1,
                            "actor": actor,
                        },
                    )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
    return await get_run_state(source, tenant, parent)

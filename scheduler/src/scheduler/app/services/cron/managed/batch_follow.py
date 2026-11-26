# -*- coding: utf-8 -*-
"""按批次父任务保存唯一的关注等级。"""

from .batch_failures import read_batch, require_manager
from .batch_members import MEMBERS
from .contracts import ManagedCronNotFound
from .executions import JOB_WHERE
from .operations import utc_now
from .repository import IDENTITY_FIELDS
from .storage import transaction

RECEIPTS = "swe_cron_managed_trigger_receipts"
FOLLOWED_LEVEL = 1
MAX_FOLLOW_LEVEL = 5
BATCH_MODES = {"batch_enabled", "batch_paused"}

PARENT_DEFINITION_JOIN = " AND ".join(
    f"p.{name}=s.{name}" for name in IDENTITY_FIELDS
)
FOLLOWED_PARENT = (
    f"s.batch_follow_level>={FOLLOWED_LEVEL} "
    "AND p.management_owner='scheduler' "
    "AND p.deleted_at IS NULL"
)


def checked_follow_level(level):
    """关注等级只接受 0-5 的严格整数，0 表示取消关注。"""
    if type(level) is not int or not 0 <= level <= MAX_FOLLOW_LEVEL:
        raise ValueError("invalid_cron_batch_follow_level")
    return level


async def _queued_execution_ids(tx, key):
    """取该父任务下仍在排队、会被重新排序的成员执行。"""
    marks = " AND ".join(f"c.{name}=%s" for name in IDENTITY_FIELDS)
    rows = await tx.fetch_all(
        "SELECT r.execution_id FROM swe_cron_execution_runs r "
        f"JOIN {MEMBERS} m ON m.current_execution_id=r.execution_id "
        "AND m.source_id=r.source_id "
        f"JOIN {RECEIPTS} c ON c.batch_id=m.batch_id "
        "AND c.source_id=m.source_id "
        "AND c.kind='batch' AND c.status='accepted' "
        "WHERE r.status IN ('queued','dispatch_failed') AND "
        f"{marks}",
        key,
    )
    return [row["execution_id"] for row in rows]


async def _refresh_queued_level(tx, key, level):
    """只把新等级写入未派发成员；已派发与运行中的执行保持原样。"""
    ids = await _queued_execution_ids(tx, key)
    if not ids:
        return 0
    marks = ",".join("%s" for _ in ids)
    await tx.execute(
        "UPDATE swe_cron_execution_runs SET dispatch_meta_json=JSON_SET("
        "COALESCE(dispatch_meta_json,'{}'),'$.batch_follow_level',%s) "
        f"WHERE execution_id IN ({marks})",
        (level, *ids),
    )
    return len(ids)


async def set_batch_follow_level(db, caller, batch_id, level):
    level = checked_follow_level(level)
    receipt = await read_batch(db, caller, batch_id)
    key = tuple(receipt[name] for name in IDENTITY_FIELDS)
    async with transaction(db) as tx:
        parent = await tx.fetch_one(
            f"SELECT management_owner,deleted_at FROM swe_cron_jobs_v2 WHERE {JOB_WHERE} FOR UPDATE",
            key,
        )
        current = await read_batch(tx, caller, batch_id, lock=True)
        if (
            parent is None
            or parent["management_owner"] != "scheduler"
            or parent["deleted_at"] is not None
            or tuple(current[name] for name in IDENTITY_FIELDS) != key
        ):
            raise LookupError("cron_batch_parent_not_found")
        await _persist_parent_level(tx, key, level, caller.actor_id)
    return {"batch_id": batch_id, "batch_follow_level": level}


async def set_task_follow_level(db, caller, job_id, level):
    """通过父任务身份设置与批次看板相同的等级。"""
    require_manager(caller)
    level = checked_follow_level(level)
    key = caller.job_key(job_id)
    async with transaction(db) as tx:
        parent = await tx.fetch_one(
            "SELECT tenant_id,source_id,agent_id,job_id,management_owner,"
            f"deleted_at,parent_job_id FROM swe_cron_jobs_v2 WHERE {JOB_WHERE} FOR UPDATE",
            key,
        )
        if (
            parent is None
            or tuple(parent[name] for name in IDENTITY_FIELDS) != key
            or parent["management_owner"] != "scheduler"
            or parent["deleted_at"] is not None
            or parent["parent_job_id"]
        ):
            raise ManagedCronNotFound("cron_batch_parent_not_found")
        await _persist_parent_level(
            tx,
            key,
            level,
            caller.actor_id,
            batch_mode_required=True,
        )
    return {"batch_follow_level": level}


async def _persist_parent_level(
    tx,
    key,
    level,
    actor_id,
    *,
    batch_mode_required=False,
):
    state = await tx.fetch_one(
        "SELECT batch_follow_level,dispatch_state FROM swe_cron_job_states "
        f"WHERE {JOB_WHERE} FOR UPDATE",
        key,
    )
    if state is None:
        raise RuntimeError("cron_state_record_missing")
    if batch_mode_required and state["dispatch_state"] not in BATCH_MODES:
        raise ManagedCronNotFound("cron_batch_parent_not_found")
    if state["batch_follow_level"] == level:
        return
    await tx.execute(
        "UPDATE swe_cron_job_states SET batch_follow_level=%s,"
        "batch_follow_updated_at=%s,batch_follow_updated_by=%s "
        f"WHERE {JOB_WHERE}",
        (level, utc_now(), actor_id, *key),
    )
    # 同一事务内刷新排队成员，改级对已排队批次立即生效。
    await _refresh_queued_level(tx, key, level)


async def read_batch_follow_level(tx, receipt):
    state = await tx.fetch_one(
        f"SELECT batch_follow_level FROM swe_cron_job_states WHERE {JOB_WHERE} FOR UPDATE",
        tuple(receipt[name] for name in IDENTITY_FIELDS),
    )
    return state["batch_follow_level"] if state else 0


async def followed_batch_reason(tx, run, receipt):
    if receipt is None:
        return "cron_daily_retry_batch_relation_missing"
    # 调用方已持有父任务定义锁并校验过回执，这里只复核批次关系与等级。
    members = await tx.fetch_all(
        f"SELECT batch_id FROM {MEMBERS} WHERE current_execution_id=%s "
        f"AND {JOB_WHERE} LIMIT 2 FOR UPDATE",
        (run["execution_id"], *(run[name] for name in IDENTITY_FIELDS)),
    )
    if len(members) != 1 or members[0]["batch_id"] != receipt["batch_id"]:
        return "cron_daily_retry_batch_not_current"
    parent_key = " AND ".join(f"s.{name}=%s" for name in IDENTITY_FIELDS)
    followed = await tx.fetch_one(
        "SELECT s.batch_follow_level FROM swe_cron_job_states s "
        f"JOIN swe_cron_jobs_v2 p ON {PARENT_DEFINITION_JOIN} "
        f"WHERE {parent_key} AND {FOLLOWED_PARENT} FOR UPDATE",
        tuple(receipt[name] for name in IDENTITY_FIELDS),
    )
    return None if followed else "cron_daily_retry_batch_not_followed"

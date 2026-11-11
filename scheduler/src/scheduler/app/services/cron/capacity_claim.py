# -*- coding: utf-8 -*-
"""Capacity reservation on the existing model-scope lease row."""

from typing import Mapping
from datetime import datetime, timezone
from .managed.run_accounting import occupied_run_sql

# 仅由新版后台在完整表结构校验后启用，不是新增运维配置。
# 旧部署没有新表时沿用原统计，不探测或自动创建业务表。
MANAGED_CAPACITY_ACCOUNTING = False
# 仅后台就绪时为待运行任务让位；启动失败仍必须统计已有执行的真实占用。
MANAGED_PRIORITY_ENABLED = False


def enable_managed_capacity_accounting():
    """新运行时已就绪后，将其持久在途占位纳入共用模型池。"""
    global MANAGED_CAPACITY_ACCOUNTING
    MANAGED_CAPACITY_ACCOUNTING = True


def set_managed_priority(enabled):
    global MANAGED_PRIORITY_ENABLED
    MANAGED_PRIORITY_ENABLED = enabled


async def _managed_occupants(cur, scope, exclude_execution_id=None):
    await cur.execute(
        "SELECT COUNT(*) AS count FROM swe_cron_execution_runs "
        "WHERE source_id=%s AND provider_id=%s AND model_id=%s "
        f"AND {occupied_run_sql()} "
        "AND execution_id<>%s "
        "AND JSON_UNQUOTE(JSON_EXTRACT(dispatch_meta_json,'$.protocol'))="
        "'scheduler_managed_v1'",
        (*scope, exclude_execution_id or ""),
    )
    rows = await cur.fetchall()
    return int(row_values(rows[0], ("count",))[0])


def row_values(row, names):
    return [row[name] for name in names] if isinstance(row, Mapping) else row


async def available_capacity(
    cur,
    scope,
    owner,
    now,
    policy,
    *,
    exclude_execution_id=None,
):
    await cur.execute(
        "SELECT lock_owner, lease_expires_at "
        "FROM swe_cron_dispatch_scope_leases "
        "WHERE source_id=%s AND provider_id=%s AND model_id=%s FOR UPDATE",
        scope,
    )
    rows = await cur.fetchall()
    if not rows:
        return 0
    lock_owner, expires = row_values(
        rows[0],
        ("lock_owner", "lease_expires_at"),
    )
    if isinstance(expires, str):
        expires = datetime.fromisoformat(expires)
    if lock_owner != owner or not expires or expires < now:
        return 0
    await cur.execute(
        "SELECT effective_workers FROM swe_cron_dispatch_worker_capacity "
        "WHERE source_id=%s AND provider_id=%s AND model_id=%s "
        "AND strategy_id=%s "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (*scope, policy["strategy_id"]),
    )
    rows = await cur.fetchall()
    capacity = (
        int(row_values(rows[0], ("effective_workers",))[0])
        if rows
        else policy["effective_workers"]
    )
    capacity = max(policy["min_workers"], min(policy["max_workers"], capacity))
    # First non-locking reads occur after the scope lock. Competing claimers
    # cannot add occupants until this transaction commits. Do not lock intents
    # before batches: manual retry holds batch -> intent in that order.
    await cur.execute(
        "SELECT id FROM swe_cron_dispatch_intents "
        "WHERE COALESCE(source_id,'')=%s "
        "AND COALESCE(NULLIF(provider_id,''),'default')=%s "
        "AND COALESCE(NULLIF(model_id,''),'default')=%s "
        "AND status IN ('claimed','acknowledged','dispatched')",
        scope,
    )
    legacy = len(await cur.fetchall())
    # 与旧领取事务共用同一 scope 行锁；统计失败必须阻止领取，不能当作零占用。
    managed = (
        await _managed_occupants(cur, scope, exclude_execution_id)
        if MANAGED_CAPACITY_ACCOUNTING
        else 0
    )
    return max(0, capacity - legacy - managed)


async def legacy_available_capacity(cur, scope, owner, now, policy):
    """同一 scope 锁内先给可执行新版留位，旧队列只领取剩余容量。"""
    available = await available_capacity(cur, scope, owner, now, policy)
    if (
        available <= 0
        or not MANAGED_CAPACITY_ACCOUNTING
        or not MANAGED_PRIORITY_ENABLED
    ):
        return available
    from .managed.date_policy import BEIJING
    from .managed.operations import IDENTITY_FIELDS
    from .managed.work_query import priority_candidates

    moment = (
        now.replace(tzinfo=BEIJING)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )
    await cur.execute(*priority_candidates(scope, moment))
    rows = await cur.fetchall()
    reserved = {}
    remaining = available
    for row in rows:
        values = row_values(
            row,
            (*IDENTITY_FIELDS, "occupied_count", "max_concurrency"),
        )
        key = tuple(values[:4])
        used = reserved.get(key, 0)
        if int(values[4]) + used < int(values[5]):
            reserved[key] = used + 1
            remaining -= 1
            if remaining == 0:
                break
    return remaining


async def write_capacity(db, sql, params):
    """Publish a capacity decision under the same lock used by claimers."""
    owner, source, provider, model = params[:4]
    async with db.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT lock_owner, lease_expires_at "
                    "FROM swe_cron_dispatch_scope_leases "
                    "WHERE source_id=%s AND provider_id=%s AND model_id=%s "
                    "FOR UPDATE",
                    (source, provider, model),
                )
                rows = await cur.fetchall()
                lease = (
                    row_values(rows[0], ("lock_owner", "lease_expires_at"))
                    if rows
                    else (None, None)
                )
                if lease[0] != owner or not lease[1] or lease[1] < params[-1]:
                    raise RuntimeError("Capacity scope lease lost")
                await cur.execute(sql, params)
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise

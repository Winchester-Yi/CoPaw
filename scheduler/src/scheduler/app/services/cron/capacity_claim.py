"""Capacity reservation on the existing model-scope lease row."""

from typing import Mapping
from datetime import datetime


def row_values(row, names):
    return [row[name] for name in names] if isinstance(row, Mapping) else row


async def available_capacity(cur, scope, owner, now, policy):
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
        rows[0], ("lock_owner", "lease_expires_at")
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
    return max(0, capacity - len(await cur.fetchall()))


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

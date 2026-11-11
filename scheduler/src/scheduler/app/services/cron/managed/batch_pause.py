# -*- coding: utf-8 -*-
"""Pause one accepted batch without changing job definitions or timers."""

from .batch_failures import read_batch
from .batch_members import MEMBERS, RECEIPTS
from .executions import JOB_WHERE
from .operations import IDENTITY_FIELDS
from .storage import transaction


async def batch_for_execution(db, row):
    # Ordinary callbacks may own a run subsequently reused by a real batch.
    return await db.fetch_one(
        f"SELECT c.* FROM {RECEIPTS} c JOIN {MEMBERS} m "
        "ON m.batch_id=c.batch_id AND m.source_id=c.source_id "
        "WHERE c.source_id=%s "
        "AND c.kind='batch' AND c.status='accepted' "
        "AND m.current_execution_id=%s "
        "ORDER BY c.batch_paused DESC,c.batch_id LIMIT 1",
        (row["source_id"], row["execution_id"]),
    )


async def lock_batch(tx, receipt):
    # Same parent-definition -> receipt -> target-definition order as retries.
    await tx.fetch_one(
        f"SELECT job_id FROM swe_cron_jobs_v2 WHERE {JOB_WHERE} FOR UPDATE",
        tuple(receipt[name] for name in IDENTITY_FIELDS),
    )
    return await tx.fetch_one(
        f"SELECT * FROM {RECEIPTS} WHERE receipt_id=%s FOR UPDATE",
        (receipt["receipt_id"],),
    )


async def set_batch_paused(db, caller, batch_id, paused):
    receipt = await read_batch(db, caller, batch_id)
    async with transaction(db) as tx:
        await lock_batch(tx, receipt)
        # Recheck ownership and receipt state after the lock.
        current = await read_batch(tx, caller, batch_id, lock=True)
        await tx.execute(
            f"UPDATE {RECEIPTS} SET batch_paused=%s WHERE receipt_id=%s",
            (int(paused), current["receipt_id"]),
        )
    return {"batch_id": batch_id, "batch_paused": paused}

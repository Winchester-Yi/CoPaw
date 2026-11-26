# -*- coding: utf-8 -*-
"""Verify followed membership and admission against the migrated MySQL schema."""

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from monitor.app.services.cron.batch_status_report import report_snapshot
from scheduler.app.services.cron.managed.batch_failures import read_batch
from scheduler.app.services.cron.managed.batch_follow import (
    followed_batch_reason,
    set_batch_follow_level,
)
from scheduler.app.services.cron.managed.batch_pause import lock_batch
from scheduler.app.services.cron.managed.daily_retry_filters import (
    DailyRetryFilters,
)
from scheduler.app.services.cron.managed.daily_retry_scan import candidates
from scheduler.app.services.cron.managed.identity import CallerIdentity
from scheduler.app.services.cron.managed.storage import transaction
from tests.integrated.scheduler.test_batch_status_report_mysql import (
    report_mysql,
    seed_batch,
)


@pytest.mark.asyncio
async def test_mysql_followed_batch_candidates_and_cancelled_admission(
    report_mysql,
):
    db = report_mysql
    source = "follow_" + uuid4().hex[:12]
    caller = CallerIdentity("tenant", source, "default", "manager", True)
    now = datetime.now(timezone.utc)
    when = now.replace(tzinfo=None, microsecond=0)
    business_date = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    try:
        await seed_batch(db, source, "selected", when, "error")
        await seed_batch(db, source, "ignored", when, "error")
        await db.execute(
            "UPDATE swe_cron_execution_runs SET finished_at=%s WHERE source_id=%s",
            (when, source),
        )
        filters = DailyRetryFilters(task_scope="followed_batch")
        call = {
            "source_id": source,
            "queue_cursor": 0,
            "max_queue_order": 2**63 - 1,
            "window_start": when - timedelta(seconds=1),
            "cutoff_at": when,
        }
        assert await candidates(db, call, 10, filters) == []
        batch_id = source + ":selected"
        await set_batch_follow_level(db, caller, batch_id, 3)
        [run] = await candidates(db, call, 10, filters)
        assert run["job_id"] == "selected"
        snapshot = await report_snapshot(db, source, business_date)
        assert [batch["batch_id"] for batch in snapshot["batches"]] == [
            batch_id
        ]
        receipt = await read_batch(db, caller, batch_id)
        async with transaction(db) as tx:
            locked = await lock_batch(tx, receipt)
            assert (await followed_batch_reason(tx, run, locked)) is None
            cancel = asyncio.create_task(
                set_batch_follow_level(db, caller, batch_id, 0),
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(cancel), timeout=0.1)
        await asyncio.wait_for(cancel, timeout=5)
        async with transaction(db) as tx:
            locked = await lock_batch(tx, receipt)
            assert (
                await followed_batch_reason(tx, run, locked)
                == "cron_daily_retry_batch_not_followed"
            )
        assert await candidates(db, call, 10, filters) == []
        snapshot = await report_snapshot(db, source, business_date)
        assert snapshot["batches"] == []
    finally:
        for table in (
            "swe_cron_managed_batch_members",
            "swe_cron_managed_trigger_receipts",
            "swe_cron_execution_runs",
            "swe_cron_job_states",
            "swe_cron_jobs_v2",
        ):
            await db.execute(
                f"DELETE FROM {table} WHERE source_id=%s", (source,)
            )

# -*- coding: utf-8 -*-
"""持久分页补跑工作域，异常不阻断其他调用或已有任务派发。"""

import asyncio
import logging
from datetime import timedelta
from uuid import uuid4

from .batch_retry_policy import BatchRetryPolicyUnavailable, max_batch_attempts
from .daily_retry_audit import decision_items, read_filters, record_decision
from .daily_retry_enqueue import (
    eligible_reason,
    enqueue_retry,
    lock_retry,
    prepare_retry,
)
from .daily_retry_scan import candidates, latest_reason
from .daily_retry_filters import matches_locked_scope
from .daily_retry_store import CALLS, DailyRetryCalls
from .diagnostics import format_management_error
from .executions import ExecutionConflict
from .operations import utc_now
from .storage import transaction

logger = logging.getLogger(__name__)


class DailyFailureRetry:

    def __init__(
        self,
        db,
        *,
        clock=None,
        page_size=100,
        dispatch_ordinary=None,
        retry_overrides=None,
    ):
        if type(page_size) is not int or not 1 <= page_size <= 200:
            raise ValueError("invalid_cron_daily_retry_page_size")
        self.db = db
        self.clock = clock or utc_now
        self.calls = DailyRetryCalls(db, clock=self.clock)
        self.page_size = page_size
        self.dispatch_ordinary = dispatch_ordinary
        self.retry_overrides = retry_overrides
        self.next_probe = None

    async def detail(self, source, call_id, *, after=0, limit=50):
        if (
            type(after) is not int
            or after < 0
            or type(limit) is not int
            or not 1 <= limit <= 200
        ):
            raise ValueError("invalid_cron_daily_retry_query")
        call = await self.calls.get(source, call_id)
        items, next_order = await decision_items(
            self.db,
            source,
            call_id,
            after=after,
            limit=limit,
        )
        return {"call": call, "items": items, "next_event_order": next_order}

    async def tick(self):
        if self.next_probe is not None and self.clock() < self.next_probe:
            return 0
        try:
            await self.calls.ready()
        except RuntimeError as error:
            self.next_probe = self.clock() + timedelta(seconds=60)
            logger.warning(
                "当日失败补跑表未就绪，仅停用新工作域\n%s",
                format_management_error(error),
            )
            return 0
        self.next_probe = None
        processed = 0
        for call in await self.calls.pending():
            try:
                processed += await self.process(call["call_id"])
            except Exception as error:
                logger.error(
                    "当日失败补跑批次暂未完成 call_id=%s\n%s",
                    call["call_id"],
                    format_management_error(error),
                )
        return processed

    async def process(self, call_id):
        owner = str(uuid4())
        if not await self.calls.claim(call_id, owner):
            return 0
        try:
            return await self._process_page(call_id, owner)
        except Exception:
            try:
                await self.calls.defer(call_id, owner)
            except Exception as error:
                logger.error(
                    "补跑扫描故障未能持久记录，等待租约恢复 call_id=%s\n%s",
                    call_id,
                    format_management_error(error),
                )
            raise

    async def _process_page(self, call_id, owner):
        row = await self.db.fetch_one(
            f"SELECT * FROM {CALLS} WHERE call_id=%s",
            (call_id,),
        )
        filters = await read_filters(self.db, row)
        page = await candidates(self.db, row, self.page_size, filters)
        ordinary = []
        for candidate in page:
            execution_id = await self._process_one(
                call_id,
                owner,
                candidate,
                filters,
            )
            if execution_id is not None:
                ordinary.append(execution_id)
        await self.calls.release(
            call_id,
            owner,
            complete=len(page) < self.page_size,
        )
        if self.dispatch_ordinary and ordinary:
            results = await asyncio.gather(
                *(
                    self.dispatch_ordinary(execution_id)
                    for execution_id in ordinary
                ),
                return_exceptions=True,
            )
            for execution_id, result in zip(ordinary, results):
                if isinstance(result, Exception):
                    logger.warning(
                        "普通补跑派发待恢复 execution_id=%s error_type=%s",
                        execution_id,
                        type(result).__name__,
                    )
        return len(page)

    async def _process_one(self, call_id, owner, row, filters):
        try:
            early = await latest_reason(self.db, row)
            if early:
                await self._decision(call_id, owner, row, "skipped", early)
                return
            prepared = await prepare_retry(self.db, row)
            overrides = (
                await self.retry_overrides()
                if prepared.member and self.retry_overrides
                else {}
            )
            max_attempts = max_batch_attempts(overrides, row["source_id"])
            async with transaction(self.db) as tx:
                call = await self.calls.locked(tx, call_id, owner)
                current, locked, member, receipt = await lock_retry(
                    tx,
                    row,
                    prepared,
                    verify_membership=filters is not None,
                )
                if locked is None:
                    reason = "cron_daily_retry_execution_missing"
                elif not matches_locked_scope(
                    filters,
                    locked,
                    member,
                    receipt,
                ):
                    reason = "cron_daily_retry_outside_task_scope"
                else:
                    reason = await eligible_reason(
                        tx,
                        call,
                        locked,
                        current,
                        member,
                        receipt,
                        prepared,
                        max_attempts=max_attempts,
                    )
                result, decision = row, "skipped"
                if reason is None:
                    result, decision, reason = await enqueue_retry(
                        tx,
                        call,
                        locked,
                        current,
                        member,
                        prepared,
                        self.clock(),
                        max_attempts=max_attempts,
                    )
                await self._commit_decision(
                    tx,
                    call,
                    owner,
                    row,
                    result,
                    decision,
                    reason,
                )
            if decision == "queued" and result["kind"] == "ordinary":
                return result["execution_id"]
        except BatchRetryPolicyUnavailable:
            raise
        except ExecutionConflict as error:
            await self._decision(call_id, owner, row, "skipped", str(error))
        except Exception as error:
            if str(error) == "cron_daily_retry_lease_lost":
                raise
            logger.error(
                "当日失败补跑单项未受理 call_id=%s job_id=%s\n%s",
                call_id,
                row["job_id"],
                format_management_error(error),
            )
            await self._decision(
                call_id,
                owner,
                row,
                "failed",
                "cron_daily_retry_item_failed",
            )

    async def _decision(self, call_id, owner, candidate, decision, reason):
        async with transaction(self.db) as tx:
            call = await self.calls.locked(tx, call_id, owner)
            await self._commit_decision(
                tx,
                call,
                owner,
                candidate,
                candidate,
                decision,
                reason,
            )

    async def _commit_decision(
        self,
        tx,
        call,
        owner,
        candidate,
        result,
        decision,
        reason,
    ):
        if candidate["queue_order"] <= call["queue_cursor"]:
            raise RuntimeError("cron_daily_retry_lease_lost")
        await record_decision(
            tx,
            call,
            candidate,
            result,
            decision,
            reason,
            self.clock(),
        )
        await self.calls.advance(tx, call, owner, candidate, decision)

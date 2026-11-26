# -*- coding: utf-8 -*-
"""仅对已登记的新版批成员续跑失败尝试，不扫描定义补生触发。"""

import logging
from datetime import datetime, timedelta, timezone

from .batch_failures import display_error_messages, member_rows
from .batch_follow import read_batch_follow_level
from .batch_members import MEMBERS, RECEIPTS, BatchRelationUnavailable
from .batch_retry import BatchRetryGrant, BatchRetryService, _utc
from .batch_retry_policy import BatchRetryPolicyUnavailable, max_batch_attempts
from .definition import request_hash
from .dispatch_events import append_run_event
from .failure_policy import is_model_failure
from .executions import ExecutionConflict, JOB_WHERE, RUN_TABLE
from .identity import CallerIdentity
from .operations import IDENTITY_FIELDS, utc_now
from .settlement import PROTOCOL, _history_matches
from .storage import transaction, update

logger = logging.getLogger(__name__)
RETRY_DELAY_SECONDS = 300
SUBTASK_TIMEOUT_SECONDS = 7800


def _naive(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return _utc(parsed).replace(tzinfo=None)


class AutoBatchRetry:
    def __init__(self, db, *, limit=32, clock=None, retry_overrides=None):
        self.db = db
        self.limit = limit
        self.clock = clock or utc_now
        self.retry_overrides = retry_overrides
        self.after = 0

    async def tick(self):
        try:
            overrides = (
                await self.retry_overrides() if self.retry_overrides else {}
            )
        except BatchRetryPolicyUnavailable:
            # 无法确认上限时只处理仍需结算的超时执行。
            logger.warning("批调度自动重试暂缓，来源配置读取失败")
            overrides = None
        rows = await self._candidates(self.after, overrides)
        if not rows and self.after:
            rows = await self._candidates(0, overrides)
        if rows:
            self.after = rows[-1]["queue_order"]
        count = 0
        for row in rows:
            try:
                count += await self._retry(
                    row,
                    max_batch_attempts(overrides, row["source_id"]),
                )
            except (ExecutionConflict, BatchRelationUnavailable):
                # 执行条件或批关联不满足时静默跳过，后续巡检仍可重新检查。
                continue
            except Exception as exc:
                logger.warning(
                    "批成员自动重试本轮未完成 batch_id=%s error_type=%s",
                    row["batch_id"],
                    type(exc).__name__,
                )
        return count

    async def _candidates(self, after, overrides):
        attempt_limit, params = "%s", (max_batch_attempts(overrides, None),)
        if overrides:
            cases = " WHEN %s THEN %s" * len(overrides)
            attempt_limit = (
                f"CASE m.source_id COLLATE utf8mb4_bin{cases} ELSE %s END"
            )
            params = (
                tuple(value for item in overrides.items() for value in item)
                + params
            )
        return await self.db.fetch_all(
            f"SELECT m.*,r.queue_order FROM {MEMBERS} m "
            f"JOIN {RUN_TABLE} r ON r.execution_id=m.current_execution_id "
            f"WHERE r.queue_order>%s AND {PROTOCOL} "
            f"AND (m.business_attempt<{attempt_limit} OR (r.status='success' AND r.settled_status IS NULL)) "
            "AND (r.status='error' OR r.settled_status='error' OR "
            "(r.status='success' AND r.settled_status IS NULL AND "
            "COALESCE(r.started_at,r.accepted_at,r.created_at)<=%s)) "
            "ORDER BY r.queue_order LIMIT %s",
            (
                after,
                *params,
                self.clock() - timedelta(seconds=SUBTASK_TIMEOUT_SECONDS),
                self.limit,
            ),
        )

    async def _receipt(self, db, batch_id, *, lock=False):
        return await db.fetch_one(
            f"SELECT * FROM {RECEIPTS} WHERE batch_id=%s AND kind='batch' "
            "AND status='accepted'" + (" FOR UPDATE" if lock else ""),
            (batch_id,),
        )

    async def _retry(self, candidate, max_attempts):
        receipt = await self._receipt(self.db, candidate["batch_id"])
        if receipt is None:
            return False
        async with transaction(self.db) as tx:
            # 与人工重试保持父定义→回执→目标定义→执行→成员的锁顺序。
            await self._lock_definition(tx, receipt)
            receipt = await self._receipt(tx, receipt["batch_id"], lock=True)
            if receipt is None or receipt["batch_paused"]:
                return False
            await self._lock_definition(tx, candidate)
            await tx.fetch_one(
                f"SELECT execution_id FROM {RUN_TABLE} WHERE execution_id=%s FOR UPDATE",
                (candidate["current_execution_id"],),
            )
            rows = await member_rows(
                tx,
                receipt,
                lock=True,
                member_ids=(candidate["member_id"],),
            )
            row = rows[0]
            if not self._current(row, candidate):
                return False
            # 单任务暂停也会作废队列版本，先记录本次重试被拦截的原因。
            pause_reason = row["current_pause_reason"]
            if pause_reason in {"auto_unread_threshold", "manual"}:
                if await self._eligible(tx, row, max_attempts):
                    await append_run_event(
                        tx,
                        {**row, "updated_at": self.clock()},
                        "auto_retry_blocked",
                        {
                            "reason": pause_reason,
                            "business_attempt": row["business_attempt"],
                        },
                    )
                return False
            if row["enqueued_state_version"] < (
                row.get("current_queue_invalidation_version") or 0
            ):
                return False
            timed_out = await self._expire_subtasks(tx, row)
            if not await self._eligible(tx, row, max_attempts):
                return False
            return await self._enqueue(tx, receipt, row, timed_out)

    @staticmethod
    async def _lock_definition(tx, row):
        await tx.fetch_one(
            f"SELECT job_id FROM swe_cron_jobs_v2 WHERE {JOB_WHERE} FOR UPDATE",
            tuple(row[name] for name in IDENTITY_FIELDS),
        )

    @staticmethod
    def _current(row, candidate):
        return (
            row["execution_id"] == candidate["current_execution_id"]
            and row["definition_version"] == row["current_definition_version"]
        )

    async def _expire_subtasks(self, tx, row):
        if row["status"] != "success" or row["settled_status"] is not None:
            return False
        history = await tx.fetch_one(
            "SELECT id,job_id,tenant_id,status,async_status,error_type,error_message,meta "
            "FROM swe_cron_executions WHERE id=%s FOR UPDATE",
            (row["history_execution_id"],),
        )
        if not _history_matches(row, history) or history["async_status"] in {
            "success",
            "error",
        }:
            return False
        started = row["started_at"] or row["accepted_at"]
        if started is None or self.clock() < _naive(started) + timedelta(
            seconds=SUBTASK_TIMEOUT_SECONDS,
        ):
            return False
        values = {
            "settled_status": "error",
            "settled_error_code": "cron_async_subtasks_timeout",
            "settled_at": self.clock(),
            "updated_at": self.clock(),
            "version": row["version"] + 1,
        }
        await update(
            tx,
            RUN_TABLE,
            values,
            "execution_id=%s AND version=%s AND settled_status IS NULL",
            (row["execution_id"], row["version"]),
        )
        row.update(values)
        await append_run_event(tx, row, "settled")
        return True

    @staticmethod
    async def _eligible(tx, row, max_attempts):
        failed = row["status"] == "error" or (
            row["status"] == "success" and row["settled_status"] == "error"
        )
        if not failed or row["business_attempt"] >= max_attempts:
            return False
        await display_error_messages(tx, [row])
        return is_model_failure(row, row.get("_history"))

    async def _enqueue(self, tx, receipt, row, timed_out):
        key = tuple(row[name] for name in IDENTITY_FIELDS)
        caller = CallerIdentity(*key[:3], row["actor_id"])
        digest = request_hash(
            [
                "auto_retry",
                receipt["batch_id"],
                row["member_id"],
                row["execution_id"],
            ],
        )
        finished = row["settled_at"] or row["finished_at"] or self.clock()
        due = (
            self.clock()
            if timed_out
            or row.get("settled_error_code") == "cron_async_subtasks_timeout"
            else _naive(finished) + timedelta(seconds=RETRY_DELAY_SECONDS)
        )
        run = await BatchRetryService._enqueue(
            tx,
            caller,
            row,
            receipt["batch_id"],
            digest,
            follow_level=await read_batch_follow_level(tx, receipt),
            grant=BatchRetryGrant(key, require_manager=False),
            retry_at=due.replace(tzinfo=timezone.utc),
        )
        await append_run_event(
            tx,
            run,
            "auto_retry_scheduled",
            {
                "business_attempt": row["business_attempt"] + 1,
                "previous_execution_id": row["execution_id"],
            },
        )
        return True

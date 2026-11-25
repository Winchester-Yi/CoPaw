# -*- coding: utf-8 -*-
"""只读共库执行历史，幂等保存 Scheduler 自己的派发收敛事实。"""

import json
import logging

from .executions import RUN_TABLE
from .operations import utc_now
from .storage import transaction, update
from .dispatch_events import append_run_event
from .run_accounting import direct_dispatch_sql

logger = logging.getLogger(__name__)
TERMINAL = ("success", "error", "skipped", "cancelled")
PROTOCOL = (
    "JSON_UNQUOTE(JSON_EXTRACT(r.dispatch_meta_json,'$.protocol'))="
    "'scheduler_managed_v1'"
)
WORKER_RUN = f"{PROTOCOL} AND NOT {direct_dispatch_sql('r')}"
SUBTASK_TERMINAL = "('SUC','FAIL','PART_SUC','TIMEOUT')"


def _history_matches(run, history):
    if history is None:
        return False
    try:
        meta = json.loads(history.get("meta") or "{}")
    except (TypeError, ValueError):
        return False
    expected = {
        "management_owner": "scheduler",
        "managed_execution_id": run["execution_id"],
        "source_id": run["source_id"],
        "agent_id": run["agent_id"],
    }
    return (
        isinstance(meta, dict)
        and history["job_id"] == run["job_id"]
        and history["tenant_id"] == run["tenant_id"]
        and history["status"] == "success"
        and all(meta.get(name) == value for name, value in expected.items())
    )


class ExecutionSettlement:
    def __init__(self, db, *, limit=32, clock=None, worker_only=False):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("invalid_cron_settlement_limit")
        self.db = db
        self.limit = limit
        self.clock = clock or utc_now
        self.run_filter = WORKER_RUN if worker_only else PROTOCOL
        # 仅是巡检位置；不缓存权威状态，重启仍从共库重新观察。
        self.after = 0

    async def tick(self):
        rows = await self._candidates(self.after)
        if not rows and self.after:
            rows = await self._candidates(0)
        if rows:
            self.after = rows[-1]["queue_order"]
        count = 0
        for row in rows:
            count += await self._settle(row["execution_id"])
        return count

    async def _candidates(self, after):
        # 正在等待的正常异步执行不占扫描限额；不一致历史通过轮转避免挡住后续行。
        return await self.db.fetch_all(
            f"SELECT r.execution_id,r.queue_order FROM {RUN_TABLE} r "
            "LEFT JOIN swe_cron_executions e ON e.id=r.history_execution_id "
            "WHERE r.settled_status IS NULL AND r.queue_order>%s "
            "AND (r.status IN ('error','skipped','cancelled') OR "
            "(r.status='success' AND (e.id IS NULL OR e.async_status IN ('success','error')))) "
            f"AND {self.run_filter} ORDER BY r.queue_order LIMIT %s",
            (after, self.limit),
        )

    async def _outcome(self, tx, run):
        if run["status"] != "success":
            return {
                "settled_status": run["status"],
                "settled_error_code": run.get("error_code"),
            }
        history = await tx.fetch_one(
            "SELECT job_id,tenant_id,status,async_status,trace_id,meta "
            "FROM swe_cron_executions WHERE id=%s",
            (run["history_execution_id"],),
        )
        if not _history_matches(run, history):
            logger.warning(
                "执行历史关联未确认，保留异步占位 execution_id=%s",
                run["execution_id"],
            )
            return None
        status = history["async_status"]
        if status not in ("success", "error"):
            return None
        outcome = {
            "settled_status": status,
            "settled_error_code": (
                "cron_async_subtasks_failed" if status == "error" else None
            ),
        }
        if history["trace_id"]:
            counts = await tx.fetch_one(
                "SELECT COUNT(*) AS total, "
                "COUNT(CASE WHEN status<>'SUC' AND "
                "INSTR(COALESCE(error_type,'') COLLATE utf8mb4_bin,'Prompt')>0 "
                "THEN 1 END) AS failures FROM swe_cron_subtasks "
                f"WHERE trace_id=%s AND status IN {SUBTASK_TERMINAL}",
                (history["trace_id"],),
            )
            if counts["total"]:
                outcome["subtask_success_count"] = (
                    counts["total"] - counts["failures"]
                )
                outcome["subtask_failure_count"] = counts["failures"]
        return outcome

    async def _settle(self, execution_id):
        async with transaction(self.db) as tx:
            run = await tx.fetch_one(
                f"SELECT r.* FROM {RUN_TABLE} r WHERE execution_id=%s AND {self.run_filter} FOR UPDATE",
                (execution_id,),
            )
            if run is None or run["settled_status"] is not None:
                return False
            if run["status"] not in TERMINAL:
                return False
            outcome = await self._outcome(tx, run)
            if outcome is None:
                return False
            observed = self.clock()
            values = {
                **outcome,
                "settled_at": observed,
                "updated_at": observed,
                "version": run["version"] + 1,
            }
            changed = await update(
                tx,
                RUN_TABLE,
                values,
                "execution_id=%s AND version=%s AND settled_status IS NULL",
                (execution_id, run["version"]),
            )
            if changed:
                await append_run_event(tx, {**run, **values}, "settled")
        if changed:
            logger.info(
                "执行派发已收敛 execution_id=%s settled_status=%s",
                execution_id,
                outcome["settled_status"],
            )
        return bool(changed)

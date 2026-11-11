# -*- coding: utf-8 -*-
"""SWE只写系统执行事实，配置与平台同步仍由Scheduler负责。"""

from datetime import datetime, timedelta, timezone
import json
import re

JOBS = "swe_cron_system_jobs"
RUNS = "swe_cron_system_executions"
IDENTITY = ("scope_kind", "tenant_id", "source_id", "agent_id", "task_type")
TERMINAL = frozenset({"success", "error", "partial", "skipped", "unknown"})
COUNTERS = frozenset(
    {
        "scopes_seen",
        "scopes_failed",
        "sessions_seen",
        "sessions_cleaned",
        "sessions_skipped_locked",
        "runs_removed",
        "messages_removed",
        "tenants_seen",
        "workspaces_seen",
        "workspaces_processed",
        "workspaces_failed",
        "workspaces_scanned",
        "files_seen",
        "files_scanned",
        "files_archived",
        "archived_size_bytes",
        "candidates_seen",
        "files_skipped_limit",
        "files_failed",
        "files_skipped",
        "errors_count",
        "records_created",
        "records_written",
    },
)


class SystemExecutionLeaseLost(RuntimeError):
    """迟到派发或过期执行者不能开始、提交或清除当前执行。"""


def _date(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value is not None and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _error(code):
    if code is None:
        return None
    return (
        code
        if isinstance(code, str)
        and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code)
        else "system_execution_failed"
    )


def safe_result(value):
    """只保存必要计数，不复制会话正文、凭据、文件内容或原始异常。"""
    if not isinstance(value, dict):
        return {}
    return {
        key: number
        for key, number in value.items()
        if key in COUNTERS and type(number) is int and 0 <= number < 2**63
    }


class SystemResultWriter:
    """与Scheduler使用同一库，始终先锁系统身份行再锁执行行。"""

    def __init__(self, db, *, clock=None):
        self.db = db
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def now(self):
        """数据库时间统一为UTC，不能受实例本地时区影响。"""
        return _date(self.clock())

    async def _locked(self, execution_id, job_id):
        job = await self.db.fetch_one(
            f"SELECT * FROM {JOBS} WHERE job_id=%s FOR UPDATE",
            (job_id,),
        )
        run = await self.db.fetch_one(
            f"SELECT * FROM {RUNS} WHERE execution_id=%s AND job_id=%s FOR UPDATE",
            (execution_id, job_id),
        )
        if (
            job is None
            or run is None
            or tuple(job[key] for key in IDENTITY)
            != tuple(run[key] for key in IDENTITY)
        ):
            raise LookupError("cron_system_execution_not_found")
        return job, run

    @staticmethod
    def _active(job, run):
        return (job["active_execution_id"], job["execution_fence"]) == (
            run["execution_id"],
            run["fence"],
        )

    async def _change(self, table, row, values, key):
        values = {
            **values,
            "version": row["version"] + 1,
            "updated_at": self.now(),
        }
        fields = ",".join(f"`{name}`=%s" for name in values)
        await self.db.execute(
            f"UPDATE {table} SET {fields} WHERE {key}=%s",
            (*values.values(), row[key]),
        )
        return {**row, **values}

    async def accept(self, execution_id, job_id, dispatch_token):
        """只受理有效派发围栏；重复身份不开始第二次业务。"""
        async with self.db.transaction():
            job, run = await self._locked(execution_id, job_id)
            if run["status"] in TERMINAL or run["status"] == "running":
                return run
            deadline = _date(run["dispatch_lease_until"])
            if (
                not self._active(job, run)
                or run["dispatch_owner"] != dispatch_token
                or deadline is None
                or deadline <= self.now()
            ):
                raise SystemExecutionLeaseLost("system_dispatch_lease_lost")
            if run["status"] == "accepted":
                return run
            if run["status"] != "dispatching":
                raise SystemExecutionLeaseLost("system_dispatch_lease_lost")
            result = await self._change(
                RUNS,
                run,
                {"status": "accepted", "accepted_at": self.now()},
                "execution_id",
            )
            await self._change(
                JOBS,
                job,
                {"last_status": "accepted"},
                "job_id",
            )
            return result

    async def start(self, execution_id, job_id, owner, current_config):
        """开始前同时核对数据库门控和SWE刚读取的当前启用配置。"""
        async with self.db.transaction():
            job, run = await self._locked(execution_id, job_id)
            if run["status"] != "accepted" or not self._active(job, run):
                return None
            if not job["enabled"] or current_config.get("enabled") is not True:
                await self._settle(
                    job,
                    run,
                    "skipped",
                    "system_task_disabled_before_start",
                    {},
                )
                return None
            timeout = current_config["timeout_seconds"]
            if type(timeout) is not int or not 1 <= timeout <= 86400:
                raise ValueError("invalid_system_timeout")
            now = self.now()
            result = await self._change(
                RUNS,
                run,
                {
                    "status": "running",
                    "run_owner": owner,
                    "started_at": now,
                    "run_lease_until": now + timedelta(seconds=timeout + 120),
                },
                "execution_id",
            )
            await self._change(
                JOBS,
                job,
                {"last_status": "running", "last_run_at": now},
                "job_id",
            )
            return result

    async def _settle(self, job, run, status, code, result):
        changed = await self._change(
            RUNS,
            run,
            {
                "status": status,
                "error_code": _error(code),
                "result_json": json.dumps(
                    safe_result(result),
                    separators=(",", ":"),
                ),
                "finished_at": self.now(),
                "run_lease_until": None,
                "dispatch_owner": None,
                "dispatch_lease_until": None,
            },
            "execution_id",
        )
        await self._change(
            JOBS,
            job,
            {"active_execution_id": None, "last_status": status},
            "job_id",
        )
        return changed

    async def finish(
        self,
        execution_id,
        job_id,
        owner,
        status,
        *,
        result=None,
        error_code=None,
    ):
        """正常结束直接落共库；未知、已核实或新围栏不能被迟到结果覆盖。"""
        if status not in {"success", "error", "partial", "skipped"}:
            raise ValueError("invalid_system_execution_status")
        async with self.db.transaction():
            job, run = await self._locked(execution_id, job_id)
            if (
                run["status"] != "running"
                or run["run_owner"] != owner
                or not self._active(job, run)
            ):
                return False
            await self._settle(job, run, status, error_code, result)
            return True

    async def unknown(self, execution_id, job_id, owner, error_code):
        """超时或取消后不能确认副作用结束，保留在途引用等待管理员核实。"""
        async with self.db.transaction():
            job, run = await self._locked(execution_id, job_id)
            if (
                run["status"] != "running"
                or run["run_owner"] != owner
                or not self._active(job, run)
            ):
                return False
            await self._change(
                RUNS,
                run,
                {"status": "unknown", "error_code": _error(error_code)},
                "execution_id",
            )
            await self._change(JOBS, job, {"last_status": "unknown"}, "job_id")
            return True

    async def reject_not_started(self, execution_id, job_id, error_code):
        """资源准备失败且尚未开始的动作可以结束，不影响其他实例已开始的同一执行。"""
        async with self.db.transaction():
            job, run = await self._locked(execution_id, job_id)
            if run["status"] != "accepted" or not self._active(job, run):
                return False
            await self._settle(job, run, "error", error_code, {})
            return True

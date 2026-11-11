# -*- coding: utf-8 -*-
"""登记平台实际触发的独立系统执行，未知结果阻塞同身份且不自动重跑。"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from ..managed.identity import identity_value
from ..managed.storage import insert, transaction, update
from .callback_binding import binding_config
from .configuration import canonical
from .identity import authorize
from .management_store import JOBS, _date

RUNS = "swe_cron_system_executions"
DISPATCH_LEASE_SECONDS = 60
MAX_DISPATCH_ATTEMPTS = 3
STARTUP_GRACE_SECONDS = 300


class SystemExecutionStore:
    """系统工作不进入用户批队列或共享模型池，同身份最多一个阻塞执行。"""

    def __init__(self, db, *, clock=None):
        self.db = db
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def now(self):
        """所有持久时间统一为UTC。"""
        return _date(self.clock())

    async def get(self, execution_id):
        """内部读取既有执行，公开查询另校验完整Header身份。"""
        return await self.db.fetch_one(
            f"SELECT * FROM {RUNS} WHERE execution_id=%s",
            (execution_id,),
        )

    async def pending(self, *, limit=20):
        """只扫已登记执行的传输待办，不从定义补造周期。"""
        return await self.db.fetch_all(
            f"SELECT execution_id FROM {RUNS} WHERE status IN ('pending','dispatching','accepted') AND (dispatch_lease_until IS NULL OR dispatch_lease_until<=%s) ORDER BY received_at,execution_id LIMIT %s",
            (self.now(), limit),
        )

    async def expired_running(self, *, limit=20):
        """读取已开始但超过核实期限的执行，后续仍需在行锁内复核。"""
        return await self.db.fetch_all(
            f"SELECT execution_id FROM {RUNS} WHERE status='running' AND run_lease_until<=%s ORDER BY run_lease_until,execution_id LIMIT %s",
            (self.now(), limit),
        )

    async def _job(self, tx, job_id):
        return await tx.fetch_one(
            f"SELECT * FROM {JOBS} WHERE job_id=%s FOR UPDATE",
            (job_id,),
        )

    async def _pair(self, tx, execution_id):
        hint = await tx.fetch_one(
            f"SELECT job_id FROM {RUNS} WHERE execution_id=%s",
            (execution_id,),
        )
        if hint is None:
            raise LookupError("cron_system_execution_not_found")
        job = await self._job(tx, hint["job_id"])
        run = await tx.fetch_one(
            f"SELECT * FROM {RUNS} WHERE execution_id=%s AND job_id=%s FOR UPDATE",
            (execution_id, hint["job_id"]),
        )
        if job is None or run is None:
            raise LookupError("cron_system_execution_not_found")
        identity = (
            "scope_kind",
            "tenant_id",
            "source_id",
            "agent_id",
            "task_type",
        )
        if tuple(job[key] for key in identity) != tuple(
            run[key] for key in identity
        ):
            raise LookupError("cron_system_execution_not_found")
        return job, run

    async def _change(self, tx, table, row, values, key):
        values = {
            **values,
            "version": row["version"] + 1,
            "updated_at": self.now(),
        }
        await update(tx, table, values, f"{key}=%s", (row[key],))
        return {**row, **values}

    async def _blocked(self, tx, job, config):
        if not job["enabled"] or not config["enabled"]:
            return "system_task_disabled"
        if not job["active_execution_id"]:
            return None
        active = await tx.fetch_one(
            f"SELECT status FROM {RUNS} WHERE execution_id=%s",
            (job["active_execution_id"],),
        )
        if active is None:
            raise RuntimeError("system_active_execution_unavailable")
        return (
            "system_result_unconfirmed"
            if active["status"] == "unknown"
            else "system_task_in_flight"
        )

    async def accept(self, caller, body):
        """每次有效请求独立登记；忙碌或未知时只记跳过，不排队、不取消在途。"""
        job_id = identity_value(str(body.get("job_id") or ""))
        async with transaction(self.db) as tx:
            job = await self._job(tx, job_id)
            config, generation = binding_config(caller, body, job)
            reason = await self._blocked(tx, job, config)
            execution_id = str(uuid4())
            fence = job["execution_fence"] + (reason is None)
            now = self.now()
            values = {
                "execution_id": execution_id,
                "job_id": job_id,
                **{
                    key: job[key]
                    for key in (
                        "scope_kind",
                        "tenant_id",
                        "source_id",
                        "agent_id",
                        "task_type",
                    )
                },
                "operation_id": body["operation_id"],
                "config_generation": generation,
                "config_json": canonical(config),
                "fence": fence,
                "status": "skipped" if reason else "pending",
                "received_at": now,
                "dispatch_attempt": 0,
                "error_code": reason,
                "finished_at": now if reason else None,
                "version": 1,
                "created_at": now,
                "updated_at": now,
            }
            await insert(tx, RUNS, values)
            if reason is None:
                await self._change(
                    tx,
                    JOBS,
                    job,
                    {
                        "active_execution_id": execution_id,
                        "execution_fence": fence,
                        "last_status": "pending",
                    },
                    "job_id",
                )
            return {
                "job_id": job_id,
                "execution_id": execution_id,
                "accepted": reason is None,
                "reused": False,
                "status": values["status"],
                "reason": reason,
            }

    async def mark_unknown(self, execution_id):
        """运行失联只能进入待核实，不能把期限到期解释为业务已结束。"""
        async with transaction(self.db) as tx:
            job, run = await self._pair(tx, execution_id)
            deadline = _date(run["run_lease_until"])
            if (
                run["status"] != "running"
                or deadline is None
                or deadline > self.now()
            ):
                return False
            if (job["active_execution_id"], job["execution_fence"]) != (
                execution_id,
                run["fence"],
            ):
                return False
            await self._change(
                tx,
                RUNS,
                run,
                {
                    "status": "unknown",
                    "error_code": "system_execution_unconfirmed",
                },
                "execution_id",
            )
            await self._change(
                tx,
                JOBS,
                job,
                {"last_status": "unknown"},
                "job_id",
            )
            return True

    def _dispatch_terminal_reason(self, run):
        if run["status"] == "accepted":
            accepted = _date(run["accepted_at"])
            if accepted is not None and self.now() - accepted >= timedelta(
                seconds=STARTUP_GRACE_SECONDS,
            ):
                return "system_execution_not_started"
            return None
        return (
            "system_dispatch_failed"
            if run["dispatch_attempt"] >= MAX_DISPATCH_ATTEMPTS
            else None
        )

    async def _not_started(self, tx, job, run, code, status="error"):
        await self._change(
            tx,
            RUNS,
            run,
            {
                "status": status,
                "error_code": code,
                "finished_at": self.now(),
                "dispatch_owner": None,
                "dispatch_lease_until": None,
            },
            "execution_id",
        )
        await self._change(
            tx,
            JOBS,
            job,
            {"active_execution_id": None, "last_status": status},
            "job_id",
        )

    async def claim_dispatch(self, execution_id, owner):
        """仅重投同一未开始身份，业务运行中或未知状态绝不重新派发。"""
        async with transaction(self.db) as tx:
            job, run = await self._pair(tx, execution_id)
            if run["status"] not in {"pending", "dispatching", "accepted"}:
                return None
            if (job["active_execution_id"], job["execution_fence"]) != (
                execution_id,
                run["fence"],
            ):
                return None
            if not job["enabled"]:
                await self._not_started(
                    tx,
                    job,
                    run,
                    "system_task_disabled_before_start",
                    "skipped",
                )
                return None
            deadline = _date(run["dispatch_lease_until"])
            if deadline is not None and deadline > self.now():
                return None
            reason = self._dispatch_terminal_reason(run)
            if reason:
                await self._not_started(tx, job, run, reason)
                return None
            if run["dispatch_attempt"] >= MAX_DISPATCH_ATTEMPTS:
                return None
            return await self._change(
                tx,
                RUNS,
                run,
                {
                    "status": (
                        "accepted"
                        if run["status"] == "accepted"
                        else "dispatching"
                    ),
                    "dispatch_owner": owner,
                    "dispatch_lease_until": self.now()
                    + timedelta(seconds=DISPATCH_LEASE_SECONDS),
                    "dispatch_attempt": run["dispatch_attempt"] + 1,
                },
                "execution_id",
            )

    async def dispatch_failed(self, execution_id, owner):
        """HTTP丢失不能覆盖已经由SWE写入的运行或终态。"""
        async with transaction(self.db) as tx:
            job, run = await self._pair(tx, execution_id)
            if run["dispatch_owner"] != owner or run["status"] not in {
                "pending",
                "dispatching",
                "accepted",
            }:
                return False
            if (
                run["dispatch_attempt"] >= MAX_DISPATCH_ATTEMPTS
                and run["status"] != "accepted"
            ):
                await self._not_started(tx, job, run, "system_dispatch_failed")
            else:
                await self._change(
                    tx,
                    RUNS,
                    run,
                    {
                        "dispatch_lease_until": self.now()
                        + timedelta(seconds=1),
                    },
                    "execution_id",
                )
            return True

    async def confirm_ended(self, caller, job_id, execution_id, command):
        """管理员真实核实后解除阻塞，不伪造成功、不自动创建下一次执行。"""
        if not caller.is_manager:
            raise PermissionError("cron_system_scope_forbidden")
        request_id = identity_value(command["request_id"])
        reason = command.get("reason", "")
        if (
            command.get("confirmed_ended") is not True
            or not isinstance(reason, str)
            or not 1 <= len(reason.strip()) <= 512
        ):
            raise ValueError("system_resolution_requires_confirmation")
        async with transaction(self.db) as tx:
            job, run = await self._pair(tx, execution_id)
            authorize(caller, job)
            if job["job_id"] != job_id:
                raise LookupError("cron_system_execution_not_found")
            if (
                run["resolution_request_id"] == request_id
                and run["resolved_at"] is not None
            ):
                if (
                    run["resolved_by"] != caller.actor_id
                    or run["resolution_reason"] != reason.strip()
                ):
                    raise ValueError("system_resolution_request_conflict")
                return run
            return await self._confirm_locked(
                tx,
                caller,
                job,
                run,
                {
                    **command,
                    "request_id": request_id,
                    "reason": reason.strip(),
                },
            )

    async def _confirm_locked(self, tx, caller, job, run, command):
        if (
            type(command.get("expected_version")) is not int
            or command["expected_version"] != run["version"]
        ):
            raise ValueError("system_execution_version_conflict")
        if run["status"] != "unknown" or run["resolved_at"] is not None:
            raise ValueError("system_execution_not_unconfirmed")
        if (job["active_execution_id"], job["execution_fence"]) != (
            run["execution_id"],
            run["fence"],
        ):
            raise ValueError("system_active_execution_changed")
        result = await self._change(
            tx,
            RUNS,
            run,
            {
                "resolution_request_id": command["request_id"],
                "resolved_by": caller.actor_id,
                "resolved_at": self.now(),
                "resolution_reason": command["reason"],
            },
            "execution_id",
        )
        await self._change(
            tx,
            JOBS,
            job,
            {
                "active_execution_id": None,
                "execution_fence": job["execution_fence"] + 1,
            },
            "job_id",
        )
        return result

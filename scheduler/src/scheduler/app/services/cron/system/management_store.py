# -*- coding: utf-8 -*-
"""仅持久化已受理的系统刷新意图，以行锁和租约串行同步同一身份。"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from ..managed.identity import identity_value
from ..managed.storage import transaction, update
from .configuration import canonical, normalize_config
from .identity import SystemIdentity, authorize

JOBS = "swe_cron_system_jobs"
SYNC_LEASE_SECONDS = 60


def _date(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value is not None and value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _previous_binding(row):
    """保留最近可能已发送给平台的引用，不接纳尚未进入平台阶段的操作。"""
    if not row["external_job_id"]:
        return {}
    operation, config = (
        row["effective_operation_id"],
        row["effective_config_json"],
    )
    attempted = (
        row["sync_stage"] in {"updating", "enabling", "pausing", "completed"}
        or row["previous_operation_id"] == row["operation_id"]
    )
    if attempted and row["operation_id"] != row["effective_operation_id"]:
        operation, config = row["operation_id"], row["desired_config_json"]
    if not operation:
        return {}
    return {"previous_operation_id": operation, "previous_config_json": config}


class SystemManagementStore:
    """只管理新系统表，不扫描工作区、旧任务文件或旧绑定表。"""

    def __init__(self, db, *, clock=None):
        self.db = db
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def now(self):
        """统一采用 UTC 无时区值写入数据库。"""
        return _date(self.clock())

    async def get(self, job_id):
        """内部按稳定标识读取；公开入口另校验完整网关身份。"""
        return await self.db.fetch_one(
            f"SELECT * FROM {JOBS} WHERE job_id=%s",
            (job_id,),
        )

    async def pending(self, *, limit=20):
        """只返回已经受理的待办身份，失败操作须显式请求重试。"""
        return await self.db.fetch_all(
            f"SELECT job_id FROM {JOBS} WHERE sync_status='pending' OR "
            "(sync_status='running' AND (sync_lease_until IS NULL OR sync_lease_until<=%s)) "
            "ORDER BY updated_at,job_id LIMIT %s",
            (self.now(), limit),
        )

    async def _locked(self, tx, job_id):
        return await tx.fetch_one(
            f"SELECT * FROM {JOBS} WHERE job_id=%s FOR UPDATE",
            (job_id,),
        )

    async def _apply(self, tx, row, values):
        values = {
            **values,
            "version": row["version"] + 1,
            "updated_at": self.now(),
        }
        await update(tx, JOBS, values, "job_id=%s", (row["job_id"],))
        return {**row, **values}

    async def refresh(self, caller, task_type, request_id):
        """先受理刷新水位，当前配置由后台重新读取，不信任旧请求快照。"""
        identity = SystemIdentity.from_caller(caller, task_type)
        return await self._refresh(caller, identity, request_id)

    async def refresh_workspace(self, caller, request_id):
        """受信工作区启动按需登记固定任务；不对外放宽来源管理权限。"""
        identities = SystemIdentity.for_workspace(caller)
        return [
            await self._refresh(caller, identity, request_id)
            for identity in identities
        ]

    async def _refresh(self, caller, identity, request_id):
        request_id = identity_value(request_id)
        values = {
            **identity.fields(),
            "route_tenant_id": identity_value(caller.tenant_id),
            "route_agent_id": identity_value(caller.agent_id),
            "enabled": False,
            "desired_config_json": "{}",
            "generation": 1,
            "sync_generation": 0,
            "synced_generation": 0,
            "operation_id": str(uuid4()),
            "sync_status": "pending",
            "sync_stage": "refresh",
            "last_request_id": request_id,
            "version": 1,
            "created_at": self.now(),
            "updated_at": self.now(),
        }
        async with transaction(self.db) as tx:
            # 缺行时先取得插入/唯一键锁，避免两个FOR UPDATE间隙锁互等。
            columns = ",".join(f"`{key}`" for key in values)
            marks = ",".join("%s" for _ in values)
            await tx.execute(
                f"INSERT INTO {JOBS} ({columns}) VALUES ({marks}) ON DUPLICATE KEY UPDATE job_id=job_id",
                tuple(values.values()),
            )
            row = await self._locked(tx, identity.job_id)
            if row is None:
                raise ValueError("system_identity_conflict")
            if row["last_request_id"] == request_id:
                return row
            return await self._apply(
                tx,
                row,
                {
                    **(
                        _previous_binding(row)
                        if row["sync_status"] != "running"
                        else {}
                    ),
                    "generation": row["generation"] + 1,
                    "last_request_id": request_id,
                    "route_tenant_id": caller.tenant_id,
                    "route_agent_id": caller.agent_id,
                    "sync_status": (
                        "running"
                        if row["sync_status"] == "running"
                        else "pending"
                    ),
                    "sync_stage": (
                        row["sync_stage"]
                        if row["sync_status"] == "running"
                        else "refresh"
                    ),
                },
            )

    async def claim(self, job_id, owner):
        """只领取已受理任务；无ID注册失联时不自动重复创建定时器。"""
        async with transaction(self.db) as tx:
            row = await self._locked(tx, job_id)
            if row is None or row["sync_status"] not in {"pending", "running"}:
                return None
            deadline = _date(row["sync_lease_until"])
            if (
                row["sync_owner"]
                and deadline is not None
                and deadline > self.now()
            ):
                return None
            if (
                row["sync_status"] == "running"
                and not row["external_job_id"]
                and row["sync_stage"] == "registering"
            ):
                await self._apply(
                    tx,
                    row,
                    {
                        "sync_status": "failed",
                        "sync_error_code": "system_platform_registration_unknown",
                        "sync_owner": None,
                        "sync_lease_until": None,
                    },
                )
                return None
            previous = _previous_binding(row)
            return await self._apply(
                tx,
                row,
                {
                    **previous,
                    "operation_id": str(uuid4()),
                    "sync_generation": row["generation"],
                    "sync_status": "running",
                    "sync_stage": "config",
                    "sync_owner": owner,
                    "sync_lease_until": self.now()
                    + timedelta(seconds=SYNC_LEASE_SECONDS),
                },
            )

    async def _fenced(self, tx, claimed):
        row = await self._locked(tx, claimed["job_id"])
        if row is None:
            return None
        same = (
            row["sync_owner"],
            row["operation_id"],
            row["sync_generation"],
        ) == (
            claimed["sync_owner"],
            claimed["operation_id"],
            claimed["sync_generation"],
        )
        if (
            not same
            or _date(row["sync_lease_until"]) is None
            or _date(row["sync_lease_until"]) <= self.now()
        ):
            return None
        return row

    async def prepare(self, claimed, raw_config):
        """HTTP前固定本次配置；迟到读取不覆盖更新后的刷新水位。"""
        config = normalize_config(raw_config)
        async with transaction(self.db) as tx:
            row = await self._fenced(tx, claimed)
            if row is None:
                return False
            if row["generation"] != claimed["sync_generation"]:
                await self._apply(
                    tx,
                    row,
                    {
                        "sync_status": "pending",
                        "sync_owner": None,
                        "sync_lease_until": None,
                    },
                )
                return False
            await self._apply(
                tx,
                row,
                {
                    "desired_config_json": canonical(
                        {**config, "_generation": claimed["sync_generation"]},
                    ),
                    "enabled": config["enabled"],
                    "sync_stage": "prepared",
                },
            )
            return True

    async def stage(self, claimed, stage):
        """在事务外平台调用前持久标明当前阶段。"""
        if stage not in {
            "prepared",
            "registering",
            "updating",
            "enabling",
            "pausing",
        }:
            raise ValueError("invalid_system_sync_stage")
        async with transaction(self.db) as tx:
            row = await self._fenced(tx, claimed)
            if row is None:
                return False
            await self._apply(
                tx,
                row,
                {
                    "sync_stage": stage,
                    "sync_lease_until": self.now()
                    + timedelta(seconds=SYNC_LEASE_SECONDS),
                },
            )
            return True

    async def bind(self, claimed, external_id):
        """取得平台ID后立即保存，后续失败仍可复用已知绑定。"""
        async with transaction(self.db) as tx:
            row = await self._fenced(tx, claimed)
            if row is None:
                return False
            if row["external_job_id"] not in (None, external_id):
                raise ValueError("system_platform_binding_conflict")
            await self._apply(
                tx,
                row,
                {"external_job_id": external_id, "sync_stage": "enabling"},
            )
            return True

    async def finish(self, claimed, external_id, raw_config):
        """发布本水位的已知生效状态，不能把新水位伪标为已完成。"""
        config = normalize_config(raw_config)
        async with transaction(self.db) as tx:
            row = await self._fenced(tx, claimed)
            if row is None:
                return False
            await self._apply(
                tx,
                row,
                {
                    "external_job_id": external_id,
                    "previous_operation_id": row["previous_operation_id"]
                    or row["effective_operation_id"],
                    "previous_config_json": row["previous_config_json"]
                    or row["effective_config_json"],
                    "effective_operation_id": claimed["operation_id"],
                    "effective_config_json": canonical(
                        {**config, "_generation": claimed["sync_generation"]},
                    ),
                    "synced_generation": claimed["sync_generation"],
                    "sync_status": (
                        "synced"
                        if row["generation"] == claimed["sync_generation"]
                        else "pending"
                    ),
                    "sync_stage": "completed",
                    "sync_error_code": None,
                    "sync_owner": None,
                    "sync_lease_until": None,
                },
            )
            return True

    async def fail(self, claimed, code):
        """保留已知绑定及生效配置；较新请求仍待同步，不被旧失败吞掉。"""
        async with transaction(self.db) as tx:
            row = await self._fenced(tx, claimed)
            if row is None:
                return False
            await self._apply(
                tx,
                row,
                {
                    "sync_status": (
                        "pending"
                        if row["generation"] > claimed["sync_generation"]
                        else "failed"
                    ),
                    "sync_error_code": code,
                    "sync_owner": None,
                    "sync_lease_until": None,
                },
            )
            return True

    async def retry(self, caller, job_id, expected_version, request_id):
        """仅续办平台同步，不重跑维护动作，也不吞并其他管理员的新操作。"""
        request_id = identity_value(request_id)
        async with transaction(self.db) as tx:
            row = authorize(caller, await self._locked(tx, job_id))
            if row["last_request_id"] == request_id:
                return row
            deadline = _date(row["sync_lease_until"])
            if (
                row["sync_status"] == "running"
                and deadline is not None
                and deadline > self.now()
            ):
                raise ValueError("system_sync_busy")
            if (
                type(expected_version) is not int
                or row["version"] != expected_version
            ):
                raise ValueError("system_version_conflict")
            return await self._apply(
                tx,
                row,
                {
                    **_previous_binding(row),
                    "sync_status": "pending",
                    "sync_stage": "refresh",
                    "sync_owner": None,
                    "sync_lease_until": None,
                    "last_request_id": request_id,
                },
            )

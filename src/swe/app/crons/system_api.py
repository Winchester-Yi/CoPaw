# -*- coding: utf-8 -*-
"""SWE系统任务内部边界：先核对共库操作，再只读当前业务配置。"""

import asyncio
from datetime import datetime, timezone
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .managed_api import _identity
from .system_config import SystemConfigReader
from .system_actions import SystemBusinessActions, TASK_SCOPES
from .system_execution import SystemExecutionService
from .system_results import SystemExecutionLeaseLost, SystemResultWriter

router = APIRouter(
    prefix="/internal/cron/system",
    tags=["system-cron-internal"],
)
logger = logging.getLogger(__name__)


def system_database(request):
    """复用应用现有共库连接，不另建库、表或初始化用户状态。"""
    db = getattr(request.app.state, "db_connection", None)
    if db is None or not getattr(db, "is_connected", False):
        raise HTTPException(503, "cron_system_database_unavailable")
    return db


async def authorized_job(request, job_id):
    """完整路由身份必须吻合，操作引用不能成为跨scope配置读取凭据。"""
    actor, tenant, source, agent = _identity(request)
    row = await system_database(request).fetch_one(
        "SELECT * FROM swe_cron_system_jobs WHERE job_id=%s AND source_id=%s",
        (job_id, source),
    )
    if (
        row is None
        or row["source_id"] != source
        or actor != tenant
        or (tenant, agent) != (row["route_tenant_id"], row["route_agent_id"])
    ):
        raise HTTPException(404, "cron_system_job_not_found")
    if TASK_SCOPES.get(row["task_type"]) != row["scope_kind"]:
        raise HTTPException(404, "cron_system_job_not_found")
    if row["scope_kind"] == "source" and (
        row["tenant_id"],
        row["agent_id"],
    ) != ("", ""):
        raise HTTPException(404, "cron_system_job_not_found")
    if row["scope_kind"] == "agent" and (tenant, agent) != (
        row["tenant_id"],
        row["agent_id"],
    ):
        raise HTTPException(404, "cron_system_job_not_found")
    return row


def _current_operation(row, operation_id):
    deadline = row["sync_lease_until"]
    if isinstance(deadline, str):
        deadline = datetime.fromisoformat(deadline)
    if deadline is not None and deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    valid = (
        row["operation_id"] == operation_id
        and row["sync_owner"]
        and row["sync_status"] == "running"
    )
    if not valid or deadline is None or deadline <= datetime.now(timezone.utc):
        raise HTTPException(409, "cron_system_operation_not_current")


@router.get("/config", summary="只读当前系统业务配置")
async def current_config(
    request: Request,
    job_id: str = Query(min_length=1, max_length=128),
    operation_id: str = Query(min_length=1, max_length=128),
):
    """Scheduler只提交共库引用；不初始化工作区、不修复文件、不读旧绑定。"""
    try:
        row = await authorized_job(request, job_id)
        _current_operation(row, operation_id)
        reader = SystemConfigReader(
            getattr(request.app.state, "source_system_config_service", None),
        )
        return await reader.read(row)
    except HTTPException:
        raise
    except Exception as error:
        logger.warning(
            "系统配置只读查询失败 job_id=%s error_type=%s",
            job_id,
            type(error).__name__,
        )
        raise HTTPException(503, "cron_system_config_unavailable") from error


class ExecuteRequest(BaseModel):
    """仅共库引用和本次传输围栏，不接受业务配置或维护参数。"""

    model_config = ConfigDict(extra="forbid")
    execution_id: str = Field(min_length=1, max_length=128)
    job_id: str = Field(min_length=1, max_length=128)
    dispatch_token: str = Field(min_length=1, max_length=64)


async def get_system_execution_service(request):
    """构造适配器不启动工作区，准备动作发生在持久受理之后的后台任务中。"""
    reader = SystemConfigReader(
        getattr(request.app.state, "source_system_config_service", None),
    )
    return SystemExecutionService(
        SystemResultWriter(system_database(request)),
        reader.read,
        SystemBusinessActions(request.app).prepare,
    )


async def _execute(request, row, job):
    service = await get_system_execution_service(request)
    await service.execute(row, job)


def _completed(tasks, execution_id, task):
    if tasks.get(execution_id) is task:
        tasks.pop(execution_id, None)
    if not task.cancelled() and task.exception() is not None:
        logger.error(
            "系统后台结果尚未提交 execution_id=%s error_type=%s",
            execution_id,
            type(task.exception()).__name__,
        )


def _launch(request, row, job):
    tasks = getattr(request.app.state, "system_cron_tasks", None)
    if tasks is None:
        tasks = {}
        request.app.state.system_cron_tasks = tasks
    execution_id = row["execution_id"]
    if execution_id not in tasks:
        task = asyncio.create_task(
            _execute(request, row, job),
            name=f"system-cron:{execution_id}",
        )
        tasks[execution_id] = task
        task.add_done_callback(
            lambda done: _completed(tasks, execution_id, done),
        )


@router.post(
    "/execute",
    status_code=202,
    summary="持久受理并后台执行已登记系统任务",
)
async def execute(request: Request, body: ExecuteRequest):
    """完整路由、执行身份及共库派发围栏一致后受理；重复请求不重新开始业务。"""
    try:
        job = await authorized_job(request, body.job_id)
        writer = SystemResultWriter(system_database(request))
        row = await writer.accept(
            body.execution_id,
            body.job_id,
            body.dispatch_token,
        )
        if row["status"] == "accepted":
            _launch(request, row, job)
        return {
            "execution_id": row["execution_id"],
            "job_id": row["job_id"],
            "status": row["status"],
            "accepted": True,
        }
    except HTTPException:
        raise
    except SystemExecutionLeaseLost as error:
        raise HTTPException(409, "system_dispatch_lease_lost") from error
    except LookupError as error:
        raise HTTPException(404, "cron_system_execution_not_found") from error
    except Exception as error:
        logger.warning(
            "系统执行受理失败 execution_id=%s error_type=%s",
            body.execution_id,
            type(error).__name__,
        )
        raise HTTPException(503, "cron_system_storage_unavailable") from error

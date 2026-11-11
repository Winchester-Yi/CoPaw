# -*- coding: utf-8 -*-
"""系统任务的按需刷新和状态接口，与用户任务管理入口隔离。"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .managed_cron import caller_identity
from ..database import get_db_connection
from ..services.cron.system.management_store import SystemManagementStore
from ..services.cron.system.queries import SystemQueries, public_job
from ..services.cron.system.schema import (
    SystemSchemaUnavailable,
    assert_system_schema_ready,
)

router = APIRouter(prefix="/scheduler/cron/system/jobs", tags=["system-cron"])


class RefreshRequest(BaseModel):
    """只提交刷新意图，不接受过时配置、文件路径或任意维护命令。"""

    model_config = ConfigDict(extra="forbid")
    task_type: Literal[
        "heartbeat",
        "dream",
        "cleanup",
        "task_session_cleanup",
        "archive_maintenance",
    ]
    request_id: str = Field(min_length=1, max_length=128)


class RetryRequest(BaseModel):
    """版本控制的管理重试，不是重跑系统业务。"""

    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=1, strict=True)


async def system_database(request: Request):
    """只校验独立系统表，不借用户模型池或任务schema初始化系统任务。"""
    if getattr(
        request.app.state,
        "system_cron_prepared",
        False,
    ) and not getattr(request.app.state, "system_cron_ready", False):
        raise HTTPException(503, "cron_system_runtime_unavailable")
    try:
        db = get_db_connection()
        if not getattr(db, "is_connected", False):
            raise RuntimeError("database unavailable")
        await assert_system_schema_ready(db)
        return db
    except SystemSchemaUnavailable as error:
        raise HTTPException(503, "cron_system_schema_unavailable") from error
    except Exception as error:
        raise HTTPException(503, "cron_system_database_unavailable") from error


def _raise(error):
    if isinstance(error, PermissionError):
        raise HTTPException(403, "cron_system_scope_forbidden") from error
    if isinstance(error, LookupError):
        raise HTTPException(404, "cron_system_job_not_found") from error
    if isinstance(error, ValueError):
        status = (
            409
            if str(error) in {"system_version_conflict", "system_sync_busy"}
            else 400
        )
        raise HTTPException(status, str(error)) from error
    raise HTTPException(503, "cron_system_storage_unavailable") from error


@router.post("/refresh", status_code=202, summary="受理系统配置刷新意图")
async def refresh(
    body: RefreshRequest,
    caller=Depends(caller_identity),
    db=Depends(system_database),
):
    """先持久受理再返回，平台同步由后台读取当前配置后完成。"""
    try:
        return public_job(
            await SystemManagementStore(db).refresh(
                caller,
                body.task_type,
                body.request_id,
            ),
        )
    except Exception as error:
        _raise(error)


@router.get("", summary="查询当前身份的系统同步状态")
async def jobs(
    scope_kind: Literal["agent", "source"] | None = Query(None),
    caller=Depends(caller_identity),
    db=Depends(system_database),
):
    """状态查询不能注册平台、导入绑定或启动工作区。"""
    try:
        return await SystemQueries(db).page(caller, scope_kind)
    except Exception as error:
        _raise(error)


@router.get("/{job_id}", summary="查询一个系统任务状态")
async def detail(
    job_id: str,
    caller=Depends(caller_identity),
    db=Depends(system_database),
):
    """按完整身份隔离来源、租户及智能体。"""
    try:
        return await SystemQueries(db).detail(caller, job_id)
    except Exception as error:
        _raise(error)


@router.post(
    "/{job_id}/retry",
    status_code=202,
    summary="重试当前系统平台同步",
)
async def retry(
    job_id: str,
    body: RetryRequest,
    caller=Depends(caller_identity),
    db=Depends(system_database),
):
    """仅续办同步，不解除未知执行、不重跑业务。"""
    try:
        return public_job(
            await SystemManagementStore(db).retry(
                caller,
                job_id,
                body.expected_version,
                body.request_id,
            ),
        )
    except Exception as error:
        _raise(error)

# -*- coding: utf-8 -*-
"""系统固定Body触发和管理员核实接口，与用户队列及回执路径隔离。"""

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .managed_callbacks import callback_caller, callback_params
from .managed_cron import caller_identity
from .system_cron import _raise, system_database
from ..services.cron.system.executions import SystemExecutionStore
from ..services.cron.system.queries import SystemQueries, public_execution

router = APIRouter(
    prefix="/scheduler/cron/system",
    tags=["system-cron-execution"],
)


class ConfirmEndedRequest(BaseModel):
    """管理员必须实际核实；不能提交替代结果或要求重跑。"""

    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)
    request_id: str = Field(min_length=1, max_length=128)
    confirmed_ended: bool = Field(strict=True)
    reason: str = Field(min_length=1, max_length=512)


@router.post("/callback", status_code=202, summary="受理新版系统任务平台触发")
async def callback(
    request: Request,
    body: dict = Body(...),
    db=Depends(system_database),
):
    """每次有效Body独立触发，无Header也可核验共库绑定；不按周期去重。"""
    try:
        params = callback_params(body)
        caller = callback_caller(request.headers, params)
        return await SystemExecutionStore(db).accept(caller, params)
    except Exception as error:
        _raise(error)


@router.get(
    "/jobs/{job_id}/executions",
    summary="管理员查询系统执行及待核实记录",
)
async def executions(
    job_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    caller=Depends(caller_identity),
    db=Depends(system_database),
):
    """公开必要事实与审计字段，不返回派发和运行租约令牌。"""
    try:
        return await SystemQueries(db).executions(
            caller,
            job_id,
            limit=limit,
            offset=offset,
        )
    except Exception as error:
        _raise(error)


@router.post(
    "/jobs/{job_id}/executions/{execution_id}/confirm-ended",
    summary="管理员核实结束并解除系统阻塞",
)
async def confirm_ended(
    job_id: str,
    execution_id: str,
    body: ConfirmEndedRequest,
    caller=Depends(caller_identity),
    db=Depends(system_database),
):
    """只记录真实核实与推进围栏，不伪造成功、不自动重跑。"""
    try:
        row = await SystemExecutionStore(db).confirm_ended(
            caller,
            job_id,
            execution_id,
            body.model_dump(),
        )
        return public_execution(row)
    except ValueError as error:
        if str(error) in {
            "system_execution_version_conflict",
            "system_execution_not_unconfirmed",
            "system_active_execution_changed",
            "system_resolution_request_conflict",
        }:
            raise HTTPException(409, str(error)) from error
        _raise(error)
    except Exception as error:
        _raise(error)

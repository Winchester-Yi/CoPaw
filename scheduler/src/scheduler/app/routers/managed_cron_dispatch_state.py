# -*- coding: utf-8 -*-
"""新版整组三态的独立管理接口，不携带散列参数或重新分发。"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import Field

from .managed_cron import caller_identity
from .managed_cron_management import (
    OperationRequest,
    management_database,
    require_managed_runtime,
)
from .managed_cron_controls import control_error
from ..services.cron.managed.batch_follow import set_task_follow_level
from ..services.cron.managed.contracts import BatchFollowRequest
from ..services.cron.managed.dispatch_state import DispatchStateService
from ..services.cron.managed.dispatch_state_store import DispatchStateStore
from ..services.cron.managed.identity import CallerIdentity
from ..services.cron.managed.platform import (
    ManagedPlatformClient,
    platform_config,
)

router = APIRouter(
    prefix="/scheduler/cron/jobs",
    tags=["managed-cron-dispatch-state"],
)


class DispatchStateRequest(OperationRequest):
    state: Literal["ordinary", "batch_enabled", "batch_paused"] = Field(
        description="整组目标模式：普通、开启批调度或暂停批调度",
    )
    expected_version: int = Field(
        ge=1,
        strict=True,
        description="调度状态 GET 返回的 version，不是任务定义或运行版本",
    )


def dispatch_service(db=Depends(management_database)):
    try:
        return DispatchStateService(
            db,
            ManagedPlatformClient(platform_config()),
        )
    except (RuntimeError, ValueError, KeyError) as exc:
        raise HTTPException(503, "cron_dispatch_service_unavailable") from exc


@router.get("/{job_id}/dispatch-state", summary="查询整组调度目标及同步进度")
async def get_dispatch_state(
    job_id: str,
    caller: CallerIdentity = Depends(caller_identity),
    db=Depends(management_database),
):
    """返回三态目标、配置版本、平台同步计数及批调度父任务的关注等级。

    Header 使用源任务完整身份；查询不启停平台、不重新分发，临时混合状态通过 effective_state=mixed 明确表达。
    """
    try:
        return await DispatchStateStore(db).get(caller, job_id)
    except Exception as exc:
        raise control_error(exc) from exc


@router.put(
    "/{job_id}/batch-dispatch/follow",
    summary="设置批调度父任务关注等级",
)
async def follow_managed_task(
    job_id: str,
    body: BatchFollowRequest,
    caller: CallerIdentity = Depends(caller_identity),
    db=Depends(management_database),
):
    """设置父任务关注等级，0 取消关注。使用完整父任务身份，限来源管理员。

    已排队批次同步应用新等级，首次跑批前也可设置。
    """
    try:
        return await set_task_follow_level(db, caller, job_id, body.level)
    except PermissionError as exc:
        raise HTTPException(403, "cron_batch_manager_required") from exc
    except Exception as exc:
        raise control_error(exc) from exc


@router.put(
    "/{job_id}/dispatch-state",
    status_code=202,
    summary="异步设置整组调度模式",
    dependencies=[Depends(require_managed_runtime)],
)
async def set_dispatch_state(
    job_id: str,
    body: DispatchStateRequest,
    caller: CallerIdentity = Depends(caller_identity),
    service: DispatchStateService = Depends(dispatch_service),
):
    """先保存源任务及全部有效关联对象的目标和进度，再异步同步平台。

    分发和散列参数不在本接口提交。未完成时不能更换另一个目标；外部失败不增加整组触发禁用，已入队和执行中的任务继续。
    """
    try:
        return JSONResponse(
            status_code=202,
            content=await service.set(caller, job_id, body.model_dump()),
        )
    except Exception as exc:
        raise control_error(exc) from exc

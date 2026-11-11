# -*- coding: utf-8 -*-
"""仅供可信内网SWE工作区启动的固定按需刷新，不向公网开放。"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from .managed_cron import caller_identity
from .system_cron import _raise, system_database
from ..services.cron.system.management_store import SystemManagementStore
from ..services.cron.system.queries import public_job

router = APIRouter(
    prefix="/scheduler/internal/cron/system/workspaces",
    tags=["system-cron-internal"],
)


class WorkspaceRefreshRequest(BaseModel):
    """不允许选择类型、传配置、伪造目标身份或任意维护命令。"""

    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=128)


@router.post(
    "/refresh",
    status_code=202,
    summary="可信工作区启动受理固定系统刷新",
)
async def refresh(
    body: WorkspaceRefreshRequest,
    caller=Depends(caller_identity),
    db=Depends(system_database),
):
    """只登记Header对应工作区及当前source四类任务，部署须隔离此内部路径。"""
    try:
        rows = await SystemManagementStore(db).refresh_workspace(
            caller,
            body.request_id,
        )
        return {"items": [public_job(row) for row in rows]}
    except Exception as error:
        _raise(error)

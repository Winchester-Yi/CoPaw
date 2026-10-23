# -*- coding: utf-8 -*-
"""Shell script backup API router.

提供基于 Shell 脚本的独立备份接口，与原有的 Python zipfile 备份接口并存。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .io_worker import run_backup_io
from .listing import list_restore_tenants
from .models import BackupTaskStatus
from .shell_service import ShellBackupService

router = APIRouter(prefix="/backup/shell", tags=["backup-shell"])


class CreateShellBackupRequest(BaseModel):
    """Shell 备份请求。"""

    tenant_ids: Optional[list[str]] = None  # 指定租户，None 表示所有租户
    instance_id: Optional[str] = None  # 实例标识，用于多实例部署
    date: Optional[str] = None  # YYYY-MM-DD，默认当前日期
    hour: Optional[int] = None  # 0-23，默认当前小时


class CreateShellBackupResponse(BaseModel):
    """Shell 备份响应。"""

    task_id: str
    status: str
    task_type: str
    message: str
    target_tenants: list[str]
    created_at: str
    instance_id: str
    backup_date: str
    backup_hour: int


class CreateShellRestoreRequest(BaseModel):
    """Shell 恢复请求。"""

    date: Optional[str] = None  # YYYY-MM-DD，默认当天
    hour: Optional[int] = None  # 0-23
    instance_id: Optional[str] = None  # 实例标识
    tenant_ids: Optional[list[str]] = None  # 指定租户


class CreateShellRestoreResponse(BaseModel):
    """Shell 恢复响应。"""

    task_id: str
    status: str
    task_type: str
    message: str
    target_tenants: list[str]
    created_at: str
    instance_id: str
    backup_date: str
    backup_hour: int


class ShellTaskListResponse(BaseModel):
    """任务列表响应。"""

    tasks: list[dict]
    total: int


class ShellTaskDetailResponse(BaseModel):
    """任务详情响应。"""

    task: dict


def get_shell_backup_service() -> ShellBackupService:
    """获取 Shell 备份服务实例。"""
    return ShellBackupService()


@router.post(
    "/upload",
    response_model=CreateShellBackupResponse,
    summary="创建 Shell 备份任务",
    description="使用 Shell 脚本压缩用户文件并上传到 OSS。支持指定租户和实例。",
)
async def create_shell_backup(
    request: CreateShellBackupRequest,
) -> CreateShellBackupResponse:
    """创建 Shell 脚本备份任务。"""
    service = get_shell_backup_service()

    # 获取目标租户
    all_tenants = ShellBackupService.list_all_tenant_ids()
    if request.tenant_ids:
        target_tenants = [t for t in request.tenant_ids if t in all_tenants]
    else:
        target_tenants = all_tenants

    task = await service.create_backup_task(
        tenant_ids=request.tenant_ids,
        instance_id=request.instance_id,
        backup_date=request.date,
        backup_hour=request.hour,
    )

    return CreateShellBackupResponse(
        task_id=task.task_id,
        status=task.status.value,
        task_type=task.task_type.value,
        message="Shell backup task created successfully",
        target_tenants=target_tenants,
        created_at=task.created_at.isoformat(),
        instance_id=task.instance_id or "",
        backup_date=task.backup_date or "",
        backup_hour=task.backup_hour if task.backup_hour is not None else 0,
    )


@router.post(
    "/download",
    response_model=CreateShellRestoreResponse,
    summary="创建 Shell 恢复任务",
    description="从 OSS 下载备份并使用 Shell 脚本解压恢复。支持按实例、日期、小时和租户筛选。",
)
async def create_shell_restore(
    request: CreateShellRestoreRequest,
) -> CreateShellRestoreResponse:
    """创建 Shell 脚本恢复任务。"""
    service = get_shell_backup_service()

    # 获取可用备份的租户列表
    target_tenants = await run_backup_io(
        list_restore_tenants,
        service.list_available_backups,
        request.instance_id,
        request.date or "",
        request.hour,
        request.tenant_ids,
    )

    task = await service.create_restore_task(
        date=request.date,
        hour=request.hour,
        instance_id=request.instance_id,
        tenant_ids=target_tenants if request.tenant_ids else None,
    )

    return CreateShellRestoreResponse(
        task_id=task.task_id,
        status=task.status.value,
        task_type=task.task_type.value,
        message=f"Shell restore task created for {request.date}",
        target_tenants=target_tenants,
        created_at=task.created_at.isoformat(),
        instance_id=task.instance_id or "",
        backup_date=task.backup_date or "",
        backup_hour=task.backup_hour if task.backup_hour is not None else 0,
    )


@router.get(
    "/tasks",
    response_model=ShellTaskListResponse,
    summary="查询 Shell 备份任务列表",
    description="查询 Shell 备份任务列表，支持按状态和类型筛选。",
)
async def list_shell_tasks(
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(
        50,
        ge=1,
        le=100,
        description="Maximum number of tasks",
    ),
) -> ShellTaskListResponse:
    """查询 Shell 备份任务列表。"""
    service = get_shell_backup_service()
    tasks = service.list_tasks(
        status=BackupTaskStatus(status) if status else None,
        limit=limit,
    )
    return ShellTaskListResponse(
        tasks=[t.model_dump(mode="json") for t in tasks],
        total=len(tasks),
    )


@router.get(
    "/tasks/{task_id}",
    response_model=ShellTaskDetailResponse,
    summary="查询 Shell 备份任务详情",
    description="获取指定任务的详细信息。",
)
async def get_shell_task(task_id: str) -> ShellTaskDetailResponse:
    """查询 Shell 备份任务详情。"""
    service = get_shell_backup_service()
    task = service.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return ShellTaskDetailResponse(task=task.model_dump(mode="json"))


@router.get(
    "/list",
    summary="列出可用 Shell 备份",
    description="列出 OSS 上可用的备份列表。",
)
async def list_shell_backups(
    instance_id: Optional[str] = Query(
        None,
        description="Filter by instance ID",
    ),
    date: Optional[str] = Query(
        None,
        description="Filter by date (YYYY-MM-DD)",
    ),
    hour: Optional[int] = Query(None, description="Filter by hour (0-23)"),
    tenant_id: Optional[str] = Query(None, description="Filter by tenant ID"),
):
    """列出 OSS 上可用的备份。"""
    service = get_shell_backup_service()
    result = await run_backup_io(
        service.list_available_backups,
        instance_id=instance_id,
        date=date,
        hour=hour,
        tenant_id=tenant_id,
    )
    return result

# -*- coding: utf-8 -*-
"""Cron query API router for frontend.

Provides endpoints for frontend to query job definitions and execution history.
"""

import logging
from datetime import datetime
from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..models.cron import (
    CronJobModel,
    CronJobQueryParams,
    CronOverviewResponse,
    ExecutionModel,
    ExecutionQueryParams,
    PaginatedResponse,
    ExecutionDetailResponse,
    MarkReadResponse,
    SubscriptionDetailItem,
    SubscriptionOverviewItem,
    UnreadCountResponse,
)
from ..services.cron import QueryService, get_query_service
from ..services.cron.export_service import ExportService, get_export_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/monitor/cron", tags=["cron"])


def _get_source_id_from_header(request: Request) -> str:
    """从请求头获取 source_id."""
    header_source_id = request.headers.get("X-Source-Id")
    if header_source_id:
        return header_source_id
    return "default"


@router.get("/filter-options")
async def get_filter_options(
    service: QueryService = Depends(get_query_service),
) -> dict:
    """获取筛选项下拉框选项列表。

    返回用户、分行、渠道、来源、任务名称等筛选项的可选值列表，
    用于前端下拉框组件。

    Args:
        service: Query service

    Returns:
        包含各筛选项列表的字典
    """
    return await service.get_filter_options()


@router.get("/overview", response_model=CronOverviewResponse)
async def get_overview(
    request: Request,
    tenant_id: str | None = Query(default=None, description="Tenant ID filter"),
    bbk_id: str | None = Query(default=None, description="Branch ID filter"),
    start_time: datetime | None = Query(default=None, description="Range start"),
    end_time: datetime | None = Query(default=None, description="Range end"),
    service: QueryService = Depends(get_query_service),
) -> CronOverviewResponse:
    """Get aggregated data for the cron overview page."""
    actual_source_id = _get_source_id_from_header(request)
    return await service.get_overview(
        tenant_id=tenant_id,
        bbk_id=bbk_id,
        source_id=actual_source_id,
        start_time=start_time,
        end_time=end_time,
    )


@router.get("/jobs", response_model=PaginatedResponse[CronJobModel])
async def list_jobs(
    request: Request,
    tenant_id: str | None = Query(default=None, description="Tenant ID filter"),
    bbk_id: str | None = Query(default=None, description="Branch ID filter"),
    creator_user_id: str | None = Query(
        default=None,
        description="创建者ID筛选",
    ),
    job_origin: str | None = Query(default=None, description="Job origin filter"),
    status: str | None = Query(default=None, description="Status filter"),
    enabled: bool | None = Query(default=None, description="Enabled filter"),
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=10, ge=1, le=100, description="Page size"),
    service: QueryService = Depends(get_query_service),
) -> PaginatedResponse[CronJobModel]:
    """List cron jobs with pagination and filters.

    Args:
        request: FastAPI request object
        tenant_id: Tenant ID filter
        bbk_id: BBK ID filter (分行号)
        creator_user_id: Creator user ID filter
        status: Status filter
        enabled: Enabled filter
        page: Page number
        page_size: Page size
        service: Query service

    Returns:
        Paginated job list
    """
    actual_source_id = _get_source_id_from_header(request)
    params = CronJobQueryParams(
        tenant_id=tenant_id,
        bbk_id=bbk_id,
        source_id=actual_source_id,
        creator_user_id=creator_user_id,
        job_origin=job_origin,
        status=status,
        enabled=enabled,
        page=page,
        page_size=page_size,
    )
    return await service.list_jobs(params)


@router.get(
    "/subscription-overview",
    response_model=PaginatedResponse[SubscriptionOverviewItem],
)
async def get_subscription_overview(
    keyword: str | None = Query(default=None, description="Subscription task keyword"),
    tenant_id: str | None = Query(default=None, description="Tenant ID filter"),
    bbk_id: str | None = Query(default=None, description="Branch ID filter"),
    source_id: str | None = Query(default=None, description="Source ID filter"),
    start_time: datetime | None = Query(default=None, description="Range start"),
    end_time: datetime | None = Query(default=None, description="Range end"),
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=10, ge=1, le=100, description="Page size"),
    service: QueryService = Depends(get_query_service),
) -> PaginatedResponse[SubscriptionOverviewItem]:
    """查询订阅任务概览聚合数据。"""
    return await service.get_subscription_overview(
        keyword=keyword,
        tenant_id=tenant_id,
        bbk_id=bbk_id,
        source_id=source_id,
        start_time=start_time,
        end_time=end_time,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/subscription-overview/{subscription_key}/jobs",
    response_model=PaginatedResponse[SubscriptionDetailItem],
)
async def get_subscription_details(
    subscription_key: str,
    tenant_id: str | None = Query(default=None, description="Tenant ID filter"),
    bbk_id: str | None = Query(default=None, description="Branch ID filter"),
    source_id: str | None = Query(default=None, description="Source ID filter"),
    start_time: datetime | None = Query(default=None, description="Range start"),
    end_time: datetime | None = Query(default=None, description="Range end"),
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=10, ge=1, le=100, description="Page size"),
    service: QueryService = Depends(get_query_service),
) -> PaginatedResponse[SubscriptionDetailItem]:
    """查询订阅任务详情弹窗数据。"""
    return await service.get_subscription_details(
        subscription_key=subscription_key,
        tenant_id=tenant_id,
        bbk_id=bbk_id,
        source_id=source_id,
        start_time=start_time,
        end_time=end_time,
        page=page,
        page_size=page_size,
    )


@router.get("/jobs/{job_id}", response_model=CronJobModel)
async def get_job(
    job_id: str,
    service: QueryService = Depends(get_query_service),
) -> CronJobModel:
    """Get a single job by ID.

    Args:
        job_id: Job ID
        service: Query service

    Returns:
        Job details

    Raises:
        HTTPException: If job not found
    """
    job = await service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/executions", response_model=PaginatedResponse[ExecutionModel])
async def list_executions(
    request: Request,
    job_id: str | None = Query(default=None, description="Job ID filter"),
    tenant_id: str | None = Query(default=None, description="Tenant ID filter"),
    status: str | None = Query(default=None, description="Execution status filter"),
    start_time: datetime | None = Query(
        default=None,
        description="开始时间范围",
    ),
    end_time: datetime | None = Query(
        default=None,
        description="结束时间范围",
    ),
    page: int = Query(default=1, ge=1, description="Page number"),
    page_size: int = Query(default=10, ge=1, le=100, description="Page size"),
    service: QueryService = Depends(get_query_service),
) -> PaginatedResponse[ExecutionModel]:
    """List execution history with pagination and filters.

    Args:
        request: FastAPI request object
        job_id: Job ID filter
        tenant_id: Tenant ID filter
        status: Status filter
        start_time: Start time filter
        end_time: End time filter
        page: Page number
        page_size: Page size
        service: Query service

    Returns:
        Paginated execution list
    """
    actual_source_id = _get_source_id_from_header(request)
    params = ExecutionQueryParams(
        job_id=job_id,
        tenant_id=tenant_id,
        source_id=actual_source_id,
        status=status,
        start_time=start_time,
        end_time=end_time,
        page=page,
        page_size=page_size,
    )
    return await service.list_executions(params)


@router.get(
    "/executions/{execution_id}",
    response_model=ExecutionDetailResponse,
)
async def get_execution(
    execution_id: int,
    service: QueryService = Depends(get_query_service),
) -> ExecutionDetailResponse:
    """Get a single execution by ID.

    Args:
        execution_id: Execution ID
        service: Query service

    Returns:
        Execution details

    Raises:
        HTTPException: If execution not found
    """
    execution = await service.get_execution(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    return ExecutionDetailResponse.model_validate(execution)


@router.get("/export")
async def export_data(
    request: Request,
    job_id: str | None = Query(default=None, description="Job ID filter"),
    tenant_id: str | None = Query(default=None, description="Tenant ID filter"),
    bbk_id: str | None = Query(default=None, description="Branch ID filter"),
    enabled: bool | None = Query(default=None, description="Enabled filter"),
    status: str | None = Query(default=None, description="Status filter"),
    start_time: datetime | None = Query(
        default=None,
        description="开始时间范围",
    ),
    end_time: datetime | None = Query(
        default=None,
        description="结束时间范围",
    ),
    export_type: str = Query(
        default="executions",
        description="导出类型: jobs/executions",
    ),
    query_service: QueryService = Depends(get_query_service),
    export_service: ExportService = Depends(get_export_service),
) -> StreamingResponse:
    """Export cron data to Excel.

    Args:
        request: FastAPI request object
        job_id: Job ID filter (for executions)
        tenant_id: Tenant ID filter
        bbk_id: BBK ID filter (分行号)
        enabled: Enabled filter (是否启用)
        status: Status filter
        start_time: Start time filter (for executions)
        end_time: End time filter (for executions)
        export_type: Export type (jobs or executions)
        query_service: Query service
        export_service: Export service

    Returns:
        Excel file download
    """
    actual_source_id = _get_source_id_from_header(request)
    try:
        if export_type == "jobs":
            jobs = await query_service.get_jobs_for_export(
                tenant_id=tenant_id,
                bbk_id=bbk_id,
                source_id=actual_source_id,
                enabled=enabled,
                status=status,
            )
            excel_bytes = export_service.export_jobs(jobs)
            filename = "定时任务.xlsx"
        else:
            executions = await query_service.get_executions_for_export(
                job_id=job_id,
                tenant_id=tenant_id,
                source_id=actual_source_id,
                status=status,
                start_time=start_time,
                end_time=end_time,
            )
            excel_bytes = export_service.export_executions(executions)
            filename = "定时任务执行情况.xlsx"

        # RFC 5987: 使用filename*参数支持中文文件名
        encoded_filename = quote(filename)
        return StreamingResponse(
            BytesIO(excel_bytes),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
            },
        )
    except Exception as e:
        logger.error("Failed to export data: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/jobs/{job_id}/mark-read", response_model=MarkReadResponse)
async def mark_job_as_read(
    job_id: str,
    service: QueryService = Depends(get_query_service),
) -> MarkReadResponse:
    """标记任务为已读。

    将指定任务的所有成功执行的未读记录标记为已读。
    用户查看任务执行结果后调用此接口。

    Args:
        job_id: 任务ID
        service: Query service

    Returns:
        标记结果，包含更新的记录数
    """
    try:
        count = await service.mark_job_as_read(job_id)
        return MarkReadResponse(marked=True, count=count)
    except Exception as e:
        logger.error("Failed to mark job as read: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/unread-count", response_model=UnreadCountResponse)
async def get_unread_count(
    tenant_id: str | None = Query(default=None, description="Tenant ID filter"),
    service: QueryService = Depends(get_query_service),
) -> UnreadCountResponse:
    """获取未读任务数量统计。

    返回各任务的未读成功执行记录数量，用于前端展示未读提醒。

    Args:
        tenant_id: 租户ID筛选（可选）
        service: Query service

    Returns:
        未读数量统计
    """
    return await service.get_unread_count(tenant_id)

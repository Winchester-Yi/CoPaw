# -*- coding: utf-8 -*-
"""外部平台按来源触发补跑，不要求用户 Header，不等待业务执行完成。"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import Field, field_validator

from ..database.connection import get_db_connection
from ..services.cron.managed.daily_retry_filters import DailyRetryFilters
from ..services.cron.managed.daily_retry_service import DailyFailureRetry
from ..services.cron.managed.diagnostics import format_management_error
from ..services.cron.managed.identity import identity_value

logger = logging.getLogger(__name__)


class DailyRetryRoute(APIRoute):
    """新增入口自己记录脱敏异常，不引入旧分支的全局日志改造。"""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def with_diagnostics(request):
            try:
                return await handler(request)
            except Exception as error:
                status = getattr(error, "status_code", 500)
                if isinstance(error, RequestValidationError):
                    status = 422
                logger.log(
                    logging.ERROR if status >= 500 else logging.WARNING,
                    "当日失败补跑接口失败 method=%s route=%s http_status=%s\n%s",
                    request.method,
                    self.path,
                    status,
                    format_management_error(error),
                )
                raise

        return with_diagnostics


router = APIRouter(
    prefix="/scheduler/cron/failure-reruns",
    tags=["cron-daily-failure-rerun"],
    route_class=DailyRetryRoute,
)


class DailyRetryRequest(DailyRetryFilters):
    """调用方可按来源和任务筛选补跑范围。"""

    source_id: str = Field(min_length=1, max_length=128)

    @field_validator("source_id")
    @classmethod
    def validate_source(cls, value):
        return identity_value(value)


def daily_service():
    try:
        db = get_db_connection()
        if not db.is_connected:
            raise RuntimeError("cron_database_unavailable")
        return DailyFailureRetry(db)
    except Exception as error:
        raise HTTPException(
            503,
            "cron_daily_retry_storage_unavailable",
        ) from error


def _runtime_ready(request):
    state = request.app.state
    if getattr(state, "managed_cron_runtime_prepared", False) and not getattr(
        state,
        "managed_cron_runtime_ready",
        False,
    ):
        raise HTTPException(503, "cron_managed_runtime_not_ready")


def _error(error):
    if isinstance(error, LookupError):
        return HTTPException(404, "cron_daily_retry_call_not_found")
    if isinstance(error, ValueError):
        return HTTPException(400, "invalid_cron_daily_retry_query")
    return HTTPException(503, "cron_daily_retry_storage_unavailable")


@router.post(
    "",
    status_code=202,
    summary="异步补跑指定来源当日失败的新定时任务",
)
async def submit(
    body: DailyRetryRequest,
    request: Request,
    service=Depends(daily_service),
):
    """先持久受理再返回；不注册平台任务，不需要用户身份或认证 Header。"""
    _runtime_ready(request)
    try:
        call = await service.calls.submit(
            body.source_id,
            task_scope=body.task_scope,
            job_ids=body.job_ids,
        )
    except Exception as error:
        raise _error(error) from error
    logger.info(
        "当日失败补跑调用已受理 call_id=%s source_id=%s",
        call["call_id"],
        call["source_id"],
    )
    return {"call": call}


@router.get("/{call_id}", summary="查询补跑调用进度和分页明细")
async def detail(
    call_id: UUID,
    source_id: str = Query(min_length=1, max_length=128),
    after_event_order: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    service=Depends(daily_service),
):
    """completed 仅表示扫描处理结束；逐项 execution 才是当前业务执行事实。"""
    try:
        return await service.detail(
            source_id,
            str(call_id),
            after=after_event_order,
            limit=limit,
        )
    except Exception as error:
        raise _error(error) from error

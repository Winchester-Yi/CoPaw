# -*- coding: utf-8 -*-
"""SWE 业务入口的新版任务进度查询，不修改旧接口。"""

import logging
from datetime import date as CalendarDate

from fastapi import APIRouter, Depends, HTTPException, Query

from .managed_cron import caller_identity, get_managed_repository, query_error
from ..services.cron.managed.contracts import (
    ManagedCronNotFound,
    ManagedCronUnavailable,
)
from ..services.cron.managed.task_progress import progress_page

router = APIRouter(prefix="/scheduler/cron", tags=["managed-cron"])
logger = logging.getLogger(__name__)


@router.get("/progress", summary="查询指定北京时间日期的任务进度摘要")
async def get_task_progress(
    date: CalendarDate,
    limit: int = Query(200, ge=1, le=200),
    cursor: str | None = Query(None, max_length=8192),
    caller=Depends(caller_identity),
    repo=Depends(get_managed_repository),
):
    """每任务一条最近状态摘要，不是逐次执行历史；空页仍须读取游标。"""
    try:
        return await progress_page(
            repo,
            caller,
            date,
            limit=limit,
            cursor=cursor,
        )
    except (ManagedCronNotFound, ManagedCronUnavailable, ValueError) as error:
        if isinstance(error, ManagedCronUnavailable):
            logger.exception("Managed task progress query unavailable")
        raise query_error(error) from error
    except Exception as error:
        logger.exception("Managed task progress query failed")
        raise HTTPException(503, "cron_database_unavailable") from error

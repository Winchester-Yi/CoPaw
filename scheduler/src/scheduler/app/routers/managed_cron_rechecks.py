# -*- coding: utf-8 -*-
"""Read-only managed Cron recheck lists."""

from fastapi import APIRouter, Depends, Query

from ..services.cron.managed.contracts import ManagedCronUnavailable
from ..services.cron.managed.identity import CallerIdentity
from ..services.cron.managed.recheck_query import page_rechecks
from .managed_cron import caller_identity, get_managed_repository, query_error

router = APIRouter(prefix="/scheduler/cron/rechecks", tags=["managed-cron"])


@router.get("", summary="Query managed Cron rechecks")
async def list_rechecks(
    view: str = "active",
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, max_length=4096),
    caller: CallerIdentity = Depends(caller_identity),
    repo=Depends(get_managed_repository),
):
    try:
        return await page_rechecks(
            repo.db,
            caller,
            view=view,
            limit=limit,
            cursor=cursor,
        )
    except (ManagedCronUnavailable, ValueError) as exc:
        raise query_error(exc) from exc

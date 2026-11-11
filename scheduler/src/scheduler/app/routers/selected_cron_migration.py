# -*- coding: utf-8 -*-
"""Selected old-page migration. Background work is explicitly confirmed/retried."""

import logging
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import Field

from .managed_cron import caller_identity
from .managed_cron_management import (
    OperationRequest,
    management_database,
    get_management_service,
    _error,
)
from ..services.cron.managed.selected_migration import (
    SelectedMigrationService,
    summary,
)
from ..services.cron.managed.selected_migration_store import (
    key_of,
    PUBLIC_COLUMNS,
)
from ..services.cron.managed.migration_policy import validated_fact
from ..services.cron.managed.diagnostics import format_management_error
from ..services.cron.managed import channel_migration

router = APIRouter(
    prefix="/scheduler/cron/migrations",
    tags=["selected-cron-migration"],
)
logger = logging.getLogger(__name__)


class SelectionRequest(OperationRequest):
    job_ids: list[str] = Field(min_length=1, max_length=100)


def migration_service(management=Depends(get_management_service)):
    return SelectedMigrationService(
        management.store.db,
        management.resources,
        management.platform,
    )


def failure(exc):
    logger.error("勾选迁移接口失败\n%s", format_management_error(exc))
    if isinstance(exc, ValueError) and str(exc).startswith("cron_migration_"):
        return HTTPException(409, str(exc))
    return _error(exc)


def channel_manager(caller=Depends(caller_identity)):
    if not caller.is_manager:
        raise HTTPException(403, "cron_migration_channel_manager_required")
    return caller


@router.get("/channel/jobs", summary="分页查询当前授权渠道的旧任务")
async def channel_jobs(
    caller=Depends(channel_manager),
    db=Depends(management_database),
    cursor: str = Query("", max_length=128),
    limit: int = Query(50, ge=1, le=100),
    tenant: str | None = Query(None, max_length=128),
    query: str | None = Query(None, max_length=128),
    status: str | None = Query(None, max_length=32),
):
    try:
        return await channel_migration.jobs(
            db,
            caller.source_id,
            cursor=cursor,
            limit=limit,
            tenant=tenant,
            query=query,
            status=status,
        )
    except Exception as exc:
        raise failure(exc) from exc


@router.post(
    "/channel/operations",
    status_code=202,
    summary="按真实工作区提交迁移预览",
)
async def channel_submit(
    body: SelectionRequest,
    tasks: BackgroundTasks,
    caller=Depends(channel_manager),
    service=Depends(migration_service),
):
    try:
        items = []
        for target, payload in await channel_migration.selections(
            service.store.db,
            caller,
            body,
        ):
            item = await service.submit(target, payload)
            tasks.add_task(service.run, target, item["operation_id"])
            items.append(item)
        return {"items": items}
    except Exception as exc:
        raise failure(exc) from exc


@router.get("/channel/operations", summary="查看当前渠道近期迁移进度")
async def channel_recent(
    caller=Depends(channel_manager),
    db=Depends(management_database),
):
    try:
        rows = await db.fetch_all(
            f"SELECT {PUBLIC_COLUMNS} FROM swe_cron_registration_journal "
            "WHERE source_id=%s AND source_id COLLATE utf8mb4_bin=%s "
            "AND action='migration_batch' ORDER BY created_at DESC LIMIT 50",
            (caller.source_id, caller.source_id),
        )
        return {
            "items": [
                {
                    **summary(row),
                    "tenant_id": row["tenant_id"],
                    "agent_id": row["agent_id"],
                    "can_confirm": row["actor_id"] == caller.actor_id,
                }
                for row in rows
            ],
        }
    except Exception as exc:
        raise failure(exc) from exc


@router.get("/channel/operations/{operation_id}")
async def channel_progress(
    operation_id: str,
    caller=Depends(channel_manager),
    db=Depends(management_database),
):
    try:
        target = await channel_migration.operation_context(
            db,
            caller,
            operation_id,
        )
        return await SelectedMigrationService(db, None, None).get(
            target,
            operation_id,
        )
    except Exception as exc:
        raise failure(exc) from exc


@router.post("/channel/operations/{operation_id}/confirm", status_code=202)
async def channel_confirm(
    operation_id: str,
    tasks: BackgroundTasks,
    caller=Depends(channel_manager),
    service=Depends(migration_service),
):
    try:
        target = await channel_migration.operation_context(
            service.store.db,
            caller,
            operation_id,
            write=True,
        )
        result = await service.confirm(target, operation_id)
        tasks.add_task(service.run, target, operation_id)
        return result
    except Exception as exc:
        raise failure(exc) from exc


@router.post("", status_code=202, summary="采集所选旧任务及子任务快照")
async def submit(
    body: SelectionRequest,
    tasks: BackgroundTasks,
    caller=Depends(caller_identity),
    service=Depends(migration_service),
):
    try:
        result = await service.submit(caller, body.model_dump())
        tasks.add_task(service.run, caller, result["operation_id"])
        return result
    except Exception as exc:
        raise failure(exc) from exc


@router.get("", summary="查询当前身份最近的迁移记录")
async def recent(
    caller=Depends(caller_identity),
    db=Depends(management_database),
):
    rows = await db.fetch_all(
        f"SELECT {PUBLIC_COLUMNS} FROM swe_cron_registration_journal WHERE tenant_id=%s AND source_id=%s AND agent_id=%s "
        "AND actor_id=%s AND action='migration_batch' ORDER BY created_at DESC LIMIT 20",
        (caller.tenant_id, caller.source_id, caller.agent_id, caller.actor_id),
    )
    return {
        "items": [
            summary(row)
            for row in rows
            if key_of(row)[:3] == caller.job_key(row["job_id"])[:3]
            and row["actor_id"] == caller.actor_id
        ],
    }


@router.get("/ownership", summary="查询可信迁移归属，含已删除任务及未完成迁移")
async def ownership(
    cursor: str = Query("", max_length=128),
    caller=Depends(caller_identity),
    db=Depends(management_database),
):
    try:
        return await _ownership_page(db, caller, cursor)
    except Exception as exc:
        raise failure(exc) from exc


async def _ownership_page(db, caller, cursor):
    rows = await db.fetch_all(
        "SELECT j.* FROM swe_cron_registration_journal j JOIN swe_cron_jobs_v2 d "
        "ON j.tenant_id=d.tenant_id AND j.source_id=d.source_id AND j.agent_id=d.agent_id AND j.job_id=d.job_id "
        "WHERE j.tenant_id=%s AND j.source_id=%s AND j.agent_id=%s AND j.action='migrate' "
        "AND d.creator_user_id=%s AND d.creator_user_id COLLATE utf8mb4_bin=%s "
        "AND j.operation_id>%s ORDER BY j.operation_id LIMIT 201",
        (
            caller.tenant_id,
            caller.source_id,
            caller.agent_id,
            caller.actor_id,
            caller.actor_id,
            cursor,
        ),
    )
    items = []
    for row in rows[:200]:
        key = key_of(row)
        if key[:3] != (caller.tenant_id, caller.source_id, caller.agent_id):
            continue
        fact = validated_fact(row, key)
        items.append(
            {
                "tenant_id": key[0],
                "source_id": key[1],
                "agent_id": key[2],
                "job_id": key[3],
                "chat_id": fact["chat_id"],
                "session_id": fact["session_id"],
                "completed": fact["status"] == "completed",
            },
        )
    return {
        "items": items,
        "next_cursor": rows[199]["operation_id"] if len(rows) > 200 else None,
    }


@router.get("/{operation_id}")
async def progress(
    operation_id: str,
    caller=Depends(caller_identity),
    db=Depends(management_database),
):
    try:
        return await SelectedMigrationService(db, None, None).get(
            caller,
            operation_id,
        )
    except Exception as exc:
        raise failure(exc) from exc


@router.post(
    "/{operation_id}/confirm",
    status_code=202,
    summary="确认范围或续办未完成组",
)
async def confirm(
    operation_id: str,
    tasks: BackgroundTasks,
    caller=Depends(caller_identity),
    service=Depends(migration_service),
):
    try:
        result = await service.confirm(caller, operation_id)
        tasks.add_task(service.run, caller, operation_id)
        return result
    except Exception as exc:
        raise failure(exc) from exc

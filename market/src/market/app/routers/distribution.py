# -*- coding: utf-8 -*-
"""管理员批量分发技能和 MCP 接口。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ...marketplace.schemas import (
    BatchDistributionQueryResponse,
    BatchDistributionQueryItem,
    BatchDistributionRequest,
    BatchDistributionResponse,
    BatchDistributionTask,
    DistributeRequest,
    MCPDistributionRequest,
)
from ...marketplace.service import load_index
from ...database.connection import DatabaseConnection
from ..async_tasks import AsyncTaskStore
from ..deps import decode_user_name, require_source_id
from .mcp_market import _run_mcp_distribution_task
from .skills_market import _distribution_summary, _run_skill_distribution_task

router = APIRouter()
_LOCAL_BATCH_LOCKS: dict[str, asyncio.Lock] = {}


@asynccontextmanager
async def _batch_creation_guard(
    db: Any,
    source_id: str,
    batch_id: str,
):
    """串行化同一来源批次的创建，并保证数据库写入原子性。"""
    lock_key = f"{source_id}:{batch_id}"
    if isinstance(db, DatabaseConnection):
        async with db.transaction_with_named_lock(lock_key):
            yield
        return

    lock = _LOCAL_BATCH_LOCKS.setdefault(lock_key, asyncio.Lock())
    async with lock:
        yield


def _require_manager(x_manager: Optional[str]) -> None:
    if x_manager != "true":
        raise HTTPException(status_code=403, detail="Manager access required")


def _unique(values: list[str]) -> list[str]:
    return list(
        dict.fromkeys(value.strip() for value in values if value.strip()),
    )


def _request_hash(
    *,
    source_id: str,
    body: BatchDistributionRequest,
    skill_item_ids: list[str],
    mcp_item_ids: list[str],
    target_tenant_ids: list[str],
) -> str:
    payload = {
        "source_id": source_id,
        "batch_id": body.batch_id.strip(),
        "skill_item_ids": skill_item_ids,
        "mcp_item_ids": mcp_item_ids,
        "target_tenant_ids": target_tenant_ids,
        "overwrite": body.overwrite,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _task_id(
    source_id: str,
    batch_id: str,
    request_hash: str,
    resource_type: str,
    item_id: str,
) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{source_id}:{batch_id}:{request_hash}:{resource_type}:{item_id}",
        ),
    )


def _market_item(svc: Any, source_id: str, item_id: str, item_type: str):
    return next(
        (
            item
            for item in load_index(svc.marketplace_root, source_id)
            if item.item_id == item_id and item.item_type == item_type
        ),
        None,
    )


def _build_tasks(
    source_id: str,
    batch_id: str,
    request_hash: str,
    skill_item_ids: list[str],
    mcp_item_ids: list[str],
) -> list[BatchDistributionTask]:
    return [
        BatchDistributionTask(
            task_id=_task_id(
                source_id,
                batch_id,
                request_hash,
                "skill",
                item_id,
            ),
            resource_type="skill",
            item_id=item_id,
        )
        for item_id in skill_item_ids
    ] + [
        BatchDistributionTask(
            task_id=_task_id(
                source_id,
                batch_id,
                request_hash,
                "mcp",
                item_id,
            ),
            resource_type="mcp",
            item_id=item_id,
        )
        for item_id in mcp_item_ids
    ]


def _status(tasks: list[dict[str, Any]]) -> str:
    statuses = {str(task.get("status") or "queued") for task in tasks}
    if not statuses or statuses == {"queued"}:
        return "queued"
    if statuses & {"queued", "running"}:
        return "running"
    if statuses <= {"succeeded"}:
        return "succeeded"
    if statuses <= {"failed"}:
        return "failed"
    return "partial_failed"


def _reused_response(
    batch_id: str,
    tasks: list[BatchDistributionTask],
    existing: list[dict[str, Any]],
) -> BatchDistributionResponse:
    status_by_id = {str(row["task_id"]): row["status"] for row in existing}
    return BatchDistributionResponse(
        batch_id=batch_id,
        status=_status(existing),
        reused=True,
        task_ids=[task.task_id for task in tasks],
        tasks=[
            task.model_copy(update={"status": status_by_id[task.task_id]})
            for task in tasks
        ],
    )


async def _existing_tasks(
    db: Any,
    source_id: str,
    batch_id: str,
) -> list[dict]:
    return await db.fetch_all(
        """
        SELECT task_id, batch_id, task_type, status, result_json
        FROM swe_async_tasks
        WHERE source_id = %s
          AND batch_id = %s
          AND service = 'market'
          AND task_type IN ('market.skill.distribute', 'market.mcp.distribute')
        ORDER BY created_at ASC, task_id ASC
        """,
        (source_id, batch_id),
    )


async def _existing_tasks_by_batch_ids(
    db: Any,
    source_id: str,
    batch_ids: list[str],
) -> list[dict]:
    placeholders = ", ".join(["%s"] * len(batch_ids))
    return await db.fetch_all(
        f"""
        SELECT task_id, batch_id, task_type, status, result_json,
               target_count, done_count, failed_count
        FROM swe_async_tasks
        WHERE source_id = %s
          AND batch_id IN ({placeholders})
          AND service = 'market'
          AND task_type IN ('market.skill.distribute', 'market.mcp.distribute')
        ORDER BY batch_id ASC, created_at ASC, task_id ASC
        """,
        (source_id, *batch_ids),
    )


async def _create_batch_tasks(
    *,
    svc: Any,
    source_id: str,
    batch_id: str,
    tasks: list[BatchDistributionTask],
    target_tenant_ids: list[str],
    overwrite: bool,
    operator_id: str,
    operator_name: str,
) -> tuple[BatchDistributionResponse, list[tuple[str, dict[str, Any]]]]:
    """在批次锁内创建任务；后台执行在锁释放后启动。"""
    existing = await _existing_tasks(svc.db, source_id, batch_id)
    existing_ids = {str(row["task_id"]) for row in existing}
    expected_ids = {task.task_id for task in tasks}
    if existing:
        if not existing_ids <= expected_ids:
            raise HTTPException(
                status_code=409,
                detail="batch_id already exists with a different request",
            )
        if existing_ids == expected_ids:
            return _reused_response(batch_id, tasks, existing), []

    store = AsyncTaskStore(svc.db)
    skill_req = DistributeRequest(
        target_type="user_id",
        target_values=target_tenant_ids,
    )
    scheduled: list[tuple[str, dict[str, Any]]] = []
    for task in tasks:
        if task.task_id in existing_ids:
            continue
        item = _market_item(svc, source_id, task.item_id, task.resource_type)
        task_result = {
            "batch_id": batch_id,
            "resource_type": task.resource_type,
            "item_id": task.item_id,
        }
        if task.resource_type == "skill":
            created = await store.start_task(
                task_id=task.task_id,
                batch_id=batch_id,
                service="market",
                task_type="market.skill.distribute",
                source_id=source_id,
                actor_user_id=operator_id,
                actor_user_name=operator_name,
                target_ids=target_tenant_ids,
                result=task_result,
                summary=_distribution_summary(
                    "技能",
                    item.name,
                    len(target_tenant_ids),
                ),
            )
            if created:
                scheduled.append(
                    (
                        "skill",
                        {
                            "task_id": task.task_id,
                            "store": store,
                            "svc": svc,
                            "source_id": source_id,
                            "item_id": item.item_id,
                            "operator_id": operator_id,
                            "operator_name": operator_name,
                            "req": skill_req,
                            "target_user_ids": target_tenant_ids,
                        },
                    ),
                )
        else:
            created = await store.start_task(
                task_id=task.task_id,
                batch_id=batch_id,
                service="market",
                task_type="market.mcp.distribute",
                source_id=source_id,
                actor_user_id=operator_id,
                actor_user_name=operator_name,
                target_ids=target_tenant_ids,
                result=task_result,
                summary=_distribution_summary(
                    "MCP",
                    item.name or item.client_key,
                    len(target_tenant_ids),
                ),
            )
            if created:
                scheduled.append(
                    (
                        "mcp",
                        {
                            "task_id": task.task_id,
                            "store": store,
                            "svc": svc,
                            "source_id": source_id,
                            "item_id": item.item_id,
                            "operator_id": operator_id,
                            "operator_name": operator_name,
                            "req": MCPDistributionRequest(
                                target_tenant_ids=target_tenant_ids,
                                overwrite=overwrite,
                            ),
                        },
                    ),
                )

    response = BatchDistributionResponse(
        batch_id=batch_id,
        task_ids=[task.task_id for task in tasks],
        tasks=tasks,
    )
    return response, scheduled


@router.post(
    "/market/distributions",
    response_model=BatchDistributionResponse,
)
async def create_batch_distribution(
    body: BatchDistributionRequest,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_user_name: Optional[str] = Header(default=None, alias="X-User-Name"),
) -> BatchDistributionResponse:
    """批量创建技能和 MCP 分发任务。"""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    svc = request.app.state.marketplace
    if not getattr(svc.db, "is_connected", False):
        raise HTTPException(
            status_code=503,
            detail="Async task database connection is not available",
        )

    batch_id = body.batch_id.strip()
    if not batch_id:
        raise HTTPException(status_code=400, detail="batch_id is required")
    skill_item_ids = sorted(_unique(body.skill_item_ids))
    mcp_item_ids = sorted(_unique(body.mcp_item_ids))
    target_tenant_ids = sorted(_unique(body.target_tenant_ids))
    if not skill_item_ids and not mcp_item_ids:
        raise HTTPException(
            status_code=400,
            detail="At least one item is required",
        )
    if not target_tenant_ids:
        raise HTTPException(
            status_code=400,
            detail="At least one target tenant is required",
        )

    for item_id in skill_item_ids:
        if _market_item(svc, source_id, item_id, "skill") is None:
            raise HTTPException(
                status_code=404,
                detail=f"Skill {item_id} not found",
            )
    for item_id in mcp_item_ids:
        if _market_item(svc, source_id, item_id, "mcp") is None:
            raise HTTPException(
                status_code=404,
                detail=f"MCP {item_id} not found",
            )

    request_hash = _request_hash(
        source_id=source_id,
        body=body,
        skill_item_ids=skill_item_ids,
        mcp_item_ids=mcp_item_ids,
        target_tenant_ids=target_tenant_ids,
    )
    tasks = _build_tasks(
        source_id,
        batch_id,
        request_hash,
        skill_item_ids,
        mcp_item_ids,
    )
    operator_name = decode_user_name(x_user_name) or ""
    async with _batch_creation_guard(svc.db, source_id, batch_id):
        response, scheduled = await _create_batch_tasks(
            svc=svc,
            source_id=source_id,
            batch_id=batch_id,
            tasks=tasks,
            target_tenant_ids=target_tenant_ids,
            overwrite=body.overwrite,
            operator_id=x_user_id or "",
            operator_name=operator_name,
        )
    for resource_type, kwargs in scheduled:
        if resource_type == "skill":
            asyncio.create_task(_run_skill_distribution_task(**kwargs))
        else:
            asyncio.create_task(_run_mcp_distribution_task(**kwargs))
    return response


@router.get(
    "/market/distributions",
    response_model=BatchDistributionQueryResponse,
)
async def get_batch_distribution(
    request: Request,
    batchids: list[str] = Query(
        ...,
        min_length=1,
        description="要查询的批次 ID，可重复传入",
    ),
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
) -> BatchDistributionQueryResponse:
    """按批次 ID 查询批量分发任务。"""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)
    db = request.app.state.marketplace.db
    if not getattr(db, "is_connected", False):
        raise HTTPException(
            status_code=503,
            detail="Async task database connection is not available",
        )
    normalized_batch_ids = list(
        dict.fromkeys(
            batch_id.strip() for batch_id in batchids if batch_id.strip()
        ),
    )
    if not normalized_batch_ids:
        raise HTTPException(status_code=400, detail="batchids is required")
    rows = await _existing_tasks_by_batch_ids(
        db,
        source_id,
        normalized_batch_ids,
    )

    def item_id(row: dict[str, Any]) -> str:
        raw = row.get("result_json")
        if isinstance(raw, dict):
            return str(raw.get("item_id") or "")
        if not raw:
            return ""
        try:
            return str(json.loads(raw).get("item_id") or "")
        except (TypeError, json.JSONDecodeError):
            return ""

    rows_by_batch: dict[str, list[dict[str, Any]]] = {
        batch_id: [] for batch_id in normalized_batch_ids
    }
    for row in rows:
        rows_by_batch.setdefault(row["batch_id"], []).append(row)

    batches = []
    for batch_id in normalized_batch_ids:
        batch_rows = rows_by_batch[batch_id]
        tasks = [
            BatchDistributionTask(
                task_id=row["task_id"],
                resource_type=(
                    "skill"
                    if row["task_type"] == "market.skill.distribute"
                    else "mcp"
                ),
                item_id=item_id(row),
                status=row["status"],
            )
            for row in batch_rows
        ]
        batches.append(
            BatchDistributionQueryItem(
                batch_id=batch_id,
                status=_status(batch_rows),
                task_ids=[task.task_id for task in tasks],
                tasks=tasks,
                total_task_count=len(batch_rows),
                done_task_count=sum(
                    row["status"] in {"succeeded", "failed", "partial_failed"}
                    for row in batch_rows
                ),
                failed_task_count=sum(
                    row["status"] in {"failed", "partial_failed"}
                    for row in batch_rows
                ),
            ),
        )
    return BatchDistributionQueryResponse(batches=batches)

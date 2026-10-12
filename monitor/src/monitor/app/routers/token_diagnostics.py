"""Token-only APIs behind the deployment's authenticated identity gateway."""

import asyncio
import base64
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request

router = APIRouter(prefix="/token-diagnostics", tags=["token-diagnostics"])


def request_scope(request: Request):
    source = request.headers.get("X-Source-Id", "").strip()
    tenant = request.headers.get("X-Tenant-Id", "").strip()
    user = request.headers.get("X-User-Id", "").strip()
    if (
        not source
        or not tenant
        or not user
        or len(source) > 64
        or len(tenant) > 128
    ):
        raise HTTPException(400, "缺少有效的来源或租户身份")
    if tenant != user:
        raise HTTPException(403, "租户身份不匹配")
    return source, tenant


def queries(request: Request):
    service = getattr(request.app.state, "token_queries", None)
    if service is None:
        raise HTTPException(503, "Token 数据服务未启用或暂不可用")
    return service


async def read_result(awaitable):
    try:
        return await asyncio.wait_for(awaitable, 5)
    except Exception:
        raise HTTPException(503, "Token 数据服务暂不可用") from None


def validate_cursor(cursor):
    if cursor is None:
        return
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor).decode())
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError
        datetime.fromisoformat(value[0])
        if not isinstance(value[1], str) or len(value[1]) > 64:
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(400, "分页游标无效") from None


@router.get("/runs")
async def list_runs(
    scope=Depends(request_scope),
    service=Depends(queries),
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None, max_length=512),
    model: str | None = Query(None, max_length=128),
):
    validate_cursor(cursor)
    return await read_result(service.list_runs(scope, limit, cursor, model))


@router.get("/runs/{task_id}")
async def run_detail(
    task_id: str, scope=Depends(request_scope), service=Depends(queries)
):
    result = await read_result(service.get_run(task_id, scope))
    if result is None:
        raise HTTPException(404, "未找到当前租户的 Token 记录")
    return result


@router.get("/runs/{task_id}/calls")
async def calls(
    task_id: str,
    scope=Depends(request_scope),
    service=Depends(queries),
    branch: str | None = Query(None, max_length=64),
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
):
    return await read_result(
        service.calls(task_id, scope, branch, limit, offset)
    )


@router.get("/calls/{call_id}/inputs")
async def inputs(
    call_id: str,
    scope=Depends(request_scope),
    service=Depends(queries),
    category: str | None = Query(None, max_length=32),
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
):
    return await read_result(
        service.inputs(call_id, scope, category, limit, offset)
    )


@router.get("/runs/{task_id}/findings")
async def findings(
    task_id: str, scope=Depends(request_scope), service=Depends(queries)
):
    return {"items": await read_result(service.findings(task_id, scope))}


@router.get("/by-trace/{trace_id}")
async def by_trace(
    trace_id: str, scope=Depends(request_scope), service=Depends(queries)
):
    return {"items": await read_result(service.by_trace(trace_id, scope))}

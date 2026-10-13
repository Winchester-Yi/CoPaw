# -*- coding: utf-8 -*-
"""Scoped read-only context diagnosis resources."""

import asyncio
from dataclasses import replace

from fastapi import APIRouter, Depends, HTTPException, Request

from .token_diagnostics import request_scope
from ..services.token_diagnostics.diagnostic_filters import (
    DiagnosticFilters,
    validate_cursor,
    validate_identity,
)

router = APIRouter(
    prefix="/token-diagnostics/v2",
    tags=["token-diagnostics-v2"],
)

UNAVAILABLE = "Token 诊断数据服务暂不可用"
BAD_FILTER = "诊断筛选或分页游标无效"
NOT_FOUND = "未找到当前租户的诊断证据"
READ_SECONDS = 5.0


def queries(request: Request):
    service = getattr(request.app.state, "token_diagnostic_queries", None)
    if service is None:
        raise HTTPException(503, UNAVAILABLE)
    return service


def filters(request: Request):
    names = (
        "start",
        "end",
        "agent_id",
        "root_agent_id",
        "model_name",
        "rule_id",
        "state",
        "branch_run_id",
        "cursor",
    )
    try:
        values = {
            name: request.query_params[name]
            for name in names
            if name in request.query_params
        }
        return DiagnosticFilters(
            **values,
            limit=int(request.query_params.get("limit", "50")),
        )
    except ValueError:
        raise HTTPException(400, BAD_FILTER) from None


async def read(method, scope, query, identity=None, **kwargs):
    try:
        name = method.__name__
        if identity is not None:
            validate_identity(identity, 128 if name == "instances" else 64)
        # Validate here as well as in the service so invalid pagination never
        # reaches a database reader, including dependency overrides.
        resource = name if identity is None else f"{name}:{identity}"
        if name == "operations" and kwargs.get("operation_id") is not None:
            resource += f":operation:{kwargs['operation_id']}"
        validate_cursor(scope, query, resource)
        args = (scope, query) if identity is None else (identity, scope, query)
        result = await asyncio.wait_for(method(*args, **kwargs), READ_SECONDS)
    except ValueError:
        raise HTTPException(400, BAD_FILTER) from None
    except Exception:
        raise HTTPException(503, UNAVAILABLE) from None
    if result is None:
        raise HTTPException(404, NOT_FOUND)
    return result


@router.get("/dashboard")
async def dashboard(
    scope=Depends(request_scope),
    service=Depends(queries),
    query=Depends(filters),
):
    return await read(service.dashboard, scope, query)


@router.get("/tasks")
async def tasks(
    scope=Depends(request_scope),
    service=Depends(queries),
    query=Depends(filters),
):
    return await read(service.tasks, scope, query)


@router.get("/tasks/{task_id}")
async def task(
    task_id: str,
    scope=Depends(request_scope),
    service=Depends(queries),
    query=Depends(filters),
):
    return await read(service.task, scope, query, task_id)


@router.get("/tasks/{task_id}/branches")
async def branches(
    task_id: str,
    scope=Depends(request_scope),
    service=Depends(queries),
    query=Depends(filters),
):
    return await read(service.branches, scope, query, task_id)


@router.get("/tasks/{task_id}/calls")
async def calls(
    task_id: str,
    scope=Depends(request_scope),
    service=Depends(queries),
    query=Depends(filters),
):
    return await read(service.calls, scope, query, task_id)


@router.get("/contexts/{context_id}")
async def context(
    context_id: str,
    scope=Depends(request_scope),
    service=Depends(queries),
    query=Depends(filters),
):
    return await read(service.context, scope, query, context_id)


@router.get("/calls/{call_id}/prefix")
async def prefix(
    call_id: str,
    scope=Depends(request_scope),
    service=Depends(queries),
    query=Depends(filters),
):
    return await read(service.prefix, scope, query, call_id)


@router.get("/tasks/{task_id}/operations")
async def operations(
    task_id: str,
    request: Request,
    scope=Depends(request_scope),
    service=Depends(queries),
    query=Depends(filters),
):
    operation_id = request.query_params.get("operation_id")
    relation_cursor = request.query_params.get("relation_cursor")
    try:
        if operation_id is not None:
            validate_identity(operation_id)
        if relation_cursor is not None:
            if operation_id is None:
                raise ValueError("operation_id_required")
            validate_cursor(
                scope,
                replace(query, cursor=None),
                f"segments:{operation_id}",
                relation_cursor,
            )
    except ValueError:
        raise HTTPException(400, BAD_FILTER) from None
    return await read(
        service.operations,
        scope,
        query,
        task_id,
        operation_id=operation_id,
        relation_cursor=relation_cursor,
    )


@router.get("/issues")
async def issues(
    scope=Depends(request_scope),
    service=Depends(queries),
    query=Depends(filters),
):
    return await read(service.issues, scope, query)


@router.get("/issues/{group_key}/instances")
async def instances(
    group_key: str,
    scope=Depends(request_scope),
    service=Depends(queries),
    query=Depends(filters),
):
    return await read(service.instances, scope, query, group_key)


@router.get("/evaluations/{evaluation_id}")
async def evaluation(
    evaluation_id: str,
    scope=Depends(request_scope),
    service=Depends(queries),
    query=Depends(filters),
):
    return await read(service.evaluation, scope, query, evaluation_id)

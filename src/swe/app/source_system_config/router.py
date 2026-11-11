# -*- coding: utf-8 -*-
"""Source 系统配置 HTTP API。"""

from typing import Any, NoReturn

import logging
from fastapi import APIRouter, HTTPException, Request

from swe.config.context import is_valid_identity_value

from .models import (
    CurrentSourceSystemConfigResponse,
    CurrentSourceSystemConfigUpdateRequest,
    EffectiveSourceSystemConfig,
    SourceSystemConfigRecord,
    SourceSystemConfigUpsert,
)
from .service import (
    SourceSystemConfigDataInvalid,
    SourceSystemConfigService,
)
from .store import SourceSystemConfigStoreUnavailable

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/source-system-config",
    tags=["source-system-config"],
)

MANAGER_ROLES = frozenset({"manager", "admin"})


class SourceSystemConfigListResponse(SourceSystemConfigRecord):
    """保留单条记录 response schema 的导出入口。"""


def _get_service(request: Request) -> SourceSystemConfigService:
    """从 app.state 读取 source 系统配置服务。"""
    service = getattr(request.app.state, "source_system_config_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Source system config service unavailable",
        )
    return service


def _require_manager(request: Request) -> str:
    """校验 manager 权限并返回审计用户。"""
    role = request.headers.get("X-User-Role", "").strip().lower()
    if role not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="Manager role required")
    return (
        getattr(request.state, "user", None)
        or request.headers.get("X-User-Id")
        or "unknown"
    )


def _validate_source_id(source_id: str) -> None:
    """校验 source_id，避免管理接口写入非法身份值。"""
    if not is_valid_identity_value(source_id):
        raise HTTPException(status_code=400, detail="Invalid source_id format")


def _get_request_source_id(request: Request) -> str:
    """从请求上下文读取当前 source_id。"""
    source_id = getattr(request.state, "source_id", None)
    if source_id is None:
        raise HTTPException(status_code=400, detail="Source context missing")
    return source_id


def _raise_storage_unavailable(exc: Exception) -> NoReturn:
    """将存储不可用错误转换为标准 HTTP 503。"""
    raise HTTPException(
        status_code=503,
        detail="Source system config storage unavailable",
    ) from exc


def _raise_invalid_storage_data(exc: Exception) -> NoReturn:
    """将脏数据错误转换为标准 HTTP 500。"""
    raise HTTPException(
        status_code=500,
        detail="Source system config data is invalid",
    ) from exc


def _get_scheduler_identity(
    request: Request,
    *,
    updated_by: str | None,
):
    """按最后修改配置的请求身份构造外部调度回调参数。"""
    from .task_scheduler import SourceSchedulerIdentity

    tenant_id = str(
        getattr(request.state, "tenant_id", None)
        or request.headers.get("X-Tenant-Id")
        or updated_by
        or "",
    )
    return SourceSchedulerIdentity(
        tenant_id=tenant_id,
        from_id=tenant_id,
        updated_by=updated_by,
        role=request.headers.get("X-User-Role"),
    )


async def _refresh_source_tasks(
    request: Request,
    *,
    source_id: str,
    updated_by: str | None,
) -> dict:
    """只提交已保存配置的刷新意图，配置由Scheduler后台重新读取。"""
    scheduler = getattr(
        request.app.state,
        "source_system_task_scheduler",
        None,
    )
    if scheduler is None:
        logger.warning("Source system task scheduler is not initialized")
        return {
            "submission_status": "failed",
            "error_code": "system_sync_not_submitted",
            "items": [],
        }

    identity = _get_scheduler_identity(
        request,
        updated_by=updated_by,
    )
    refreshers = (
        ("task_session_cleanup", scheduler.refresh_task_session_cleanup),
        ("archive_maintenance", scheduler.refresh_archive_maintenance),
    )
    items = []
    for task_name, refresh in refreshers:
        try:
            result = await refresh(
                source_id=source_id,
                config=None,
                identity=identity,
            )
            submission = getattr(result, "scheduler_sync", None)
            if not isinstance(submission, dict):
                submission = {
                    "submission_status": "failed",
                    "error_code": "system_sync_not_submitted",
                }
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "配置已保存但系统同步未受理 task_type=%s error_type=%s",
                task_name,
                type(error).__name__,
            )
            submission = {
                "submission_status": "failed",
                "error_code": "system_sync_not_submitted",
            }
        items.append({"task_type": task_name, **submission})
    accepted = all(item["submission_status"] == "accepted" for item in items)
    return {
        "submission_status": "accepted" if accepted else "failed",
        "items": items,
    }


def _attach_sync(response, sync):
    """不修改配置服务缓存对象，同步受理结果属于本次保存响应而非配置正文。"""
    if isinstance(response, dict):
        return {**response, "scheduler_sync": sync}
    return response.model_copy(update={"scheduler_sync": sync})


@router.get("/effective", response_model=EffectiveSourceSystemConfig)
async def get_effective_source_system_config(
    request: Request,
) -> EffectiveSourceSystemConfig:
    """读取当前请求 source_id 的 effective 配置。"""
    bound_config = getattr(request.state, "source_system_config", None)
    if bound_config is not None:
        return bound_config

    source_id = _get_request_source_id(request)
    try:
        return await _get_service(request).resolve_config(source_id)
    except SourceSystemConfigDataInvalid as exc:
        _raise_invalid_storage_data(exc)


@router.get("/current", response_model=CurrentSourceSystemConfigResponse)
async def get_current_source_system_config(
    request: Request,
) -> CurrentSourceSystemConfigResponse:
    """读取当前请求 source_id 的原始配置。"""
    _require_manager(request)
    source_id = _get_request_source_id(request)
    try:
        return await _get_service(request).resolve_raw_config(source_id)
    except SourceSystemConfigStoreUnavailable as exc:
        _raise_storage_unavailable(exc)
    except ValueError as exc:
        _raise_invalid_storage_data(exc)


@router.put("/current", response_model=CurrentSourceSystemConfigResponse)
async def upsert_current_source_system_config(
    payload: CurrentSourceSystemConfigUpdateRequest,
    request: Request,
) -> CurrentSourceSystemConfigResponse:
    """写入当前请求 source_id 的原始配置。"""
    updated_by = _require_manager(request)
    source_id = _get_request_source_id(request)
    service = _get_service(request)
    try:
        response = await service.upsert_current_source_config(
            source_id,
            payload.config,
            updated_by=updated_by,
        )
        sync = await _refresh_source_tasks(
            request,
            source_id=source_id,
            updated_by=updated_by,
        )
        return _attach_sync(response, sync)
    except SourceSystemConfigStoreUnavailable as exc:
        _raise_storage_unavailable(exc)
    except ValueError as exc:
        _raise_invalid_storage_data(exc)


@router.delete("/current")
async def delete_current_source_system_config(
    request: Request,
) -> dict[str, Any]:
    """删除当前请求 source_id 的原始配置。"""
    updated_by = _require_manager(request)
    source_id = _get_request_source_id(request)
    service = _get_service(request)
    try:
        deleted = await service.delete_current_source_config(source_id)
    except SourceSystemConfigStoreUnavailable as exc:
        _raise_storage_unavailable(exc)
    if not deleted:
        raise HTTPException(status_code=404, detail="Source config not found")
    sync = await _refresh_source_tasks(
        request,
        source_id=source_id,
        updated_by=updated_by,
    )
    return {"deleted": True, "scheduler_sync": sync}


@router.get("/sources")
async def list_source_system_configs(request: Request) -> dict:
    """列出全部 source 系统配置记录。"""
    _require_manager(request)
    try:
        records = await _get_service(request).store.list_configs()
    except SourceSystemConfigStoreUnavailable as exc:
        _raise_storage_unavailable(exc)
    except ValueError as exc:
        _raise_invalid_storage_data(exc)
    return {"configs": records, "total": len(records)}


@router.get(
    "/sources/{source_id}",
    response_model=SourceSystemConfigRecord,
)
async def get_source_system_config(
    source_id: str,
    request: Request,
) -> SourceSystemConfigRecord:
    """读取指定 source 的系统配置记录。"""
    _require_manager(request)
    _validate_source_id(source_id)
    try:
        record = await _get_service(request).store.get_config(source_id)
    except SourceSystemConfigStoreUnavailable as exc:
        _raise_storage_unavailable(exc)
    except ValueError as exc:
        _raise_invalid_storage_data(exc)
    if record is None:
        raise HTTPException(status_code=404, detail="Source config not found")
    return record


@router.post(
    "/sources/{source_id}",
    response_model=SourceSystemConfigRecord,
)
@router.put(
    "/sources/{source_id}",
    response_model=SourceSystemConfigRecord,
)
async def upsert_source_system_config(
    source_id: str,
    payload: SourceSystemConfigUpsert,
    request: Request,
) -> SourceSystemConfigRecord:
    """创建或更新指定 source 的系统配置。"""
    updated_by = _require_manager(request)
    _validate_source_id(source_id)
    service = _get_service(request)
    payload.updated_by = updated_by
    try:
        record = await service.store.upsert_config(source_id, payload)
    except SourceSystemConfigStoreUnavailable as exc:
        _raise_storage_unavailable(exc)
    except ValueError as exc:
        _raise_invalid_storage_data(exc)
    service.invalidate(source_id)
    sync = await _refresh_source_tasks(
        request,
        source_id=source_id,
        updated_by=updated_by,
    )
    return _attach_sync(record, sync)


@router.delete("/sources/{source_id}")
async def delete_source_system_config(
    source_id: str,
    request: Request,
) -> dict[str, Any]:
    """删除指定 source 的系统配置。"""
    updated_by = _require_manager(request)
    _validate_source_id(source_id)
    service = _get_service(request)
    try:
        deleted = await service.store.delete_config(source_id)
    except SourceSystemConfigStoreUnavailable as exc:
        _raise_storage_unavailable(exc)
    service.invalidate(source_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Source config not found")
    sync = await _refresh_source_tasks(
        request,
        source_id=source_id,
        updated_by=updated_by,
    )
    return {"deleted": True, "scheduler_sync": sync}

# -*- coding: utf-8 -*-
"""MCP 版本管理 API 路由（与 skill_versions.py 对称）."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request

from ..deps import require_source_id
from ...utils.logging_utils import log_params
from ...marketplace.fs import get_mcp_dir, load_index, save_index
from ...marketplace.mcp_version_service import MCPVersionService
from ...marketplace.version_models import (
    VersionCompareRequest,
    VersionCompareResult,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _require_manager(x_manager: Optional[str]) -> None:
    if x_manager != "true":
        raise HTTPException(status_code=403, detail="Manager access required")


def _get_service(request: Request) -> MCPVersionService:
    marketplace = request.app.state.marketplace
    return MCPVersionService(marketplace.marketplace_root)


def _validate_item_exists(
    svc: MCPVersionService,
    source_id: str,
    item_id: str,
) -> None:
    items = load_index(svc.marketplace_root, source_id)
    found = any(i.item_id == item_id and i.item_type == "mcp" for i in items)
    if not found:
        raise HTTPException(
            status_code=404,
            detail=f"MCP item {item_id} not found",
        )


@router.get("/market/mcp/{item_id}/versions")
async def list_mcp_versions(
    item_id: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
):
    """列出某 MCP 条目的所有版本（按时间倒序）."""
    source_id = require_source_id(x_source_id)

    log_params(logger, request.method, request.url.path, item_id=item_id)

    svc = _get_service(request)
    _validate_item_exists(svc, source_id, item_id)
    return svc.list_versions(source_id, item_id)


@router.post("/market/mcp/{item_id}/versions/{version_id}/switch")
async def switch_mcp_version(
    item_id: str,
    version_id: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
):
    """切换 MCP 当前版本（管理员）；R8：同步更新 MarketItem.creator."""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)

    log_params(
        logger,
        request.method,
        request.url.path,
        item_id=item_id,
        version_id=version_id,
    )

    svc = _get_service(request)
    _validate_item_exists(svc, source_id, item_id)

    mcp_dir = get_mcp_dir(svc.marketplace_root, source_id, item_id)
    result = svc.switch_version(source_id, item_id, version_id, mcp_dir)
    if not result["success"]:
        raise HTTPException(
            status_code=400,
            detail=result.get("message", "switch failed"),
        )

    # R8: 同步更新 MarketItem.version + creator_id（跟随目标快照来源）
    items = load_index(svc.marketplace_root, source_id)
    item = next((i for i in items if i.item_id == item_id), None)
    if item is not None:
        item.version = version_id
        item.updated_at = datetime.now(timezone.utc).isoformat()
        manifest = svc._load_manifest(source_id, item_id)
        target = next(
            (v for v in manifest.versions if v.version_id == version_id),
            None,
        )
        if target is not None:
            if target.source_user_id:
                item.creator_id = target.source_user_id
                item.creator_name = (
                    target.source_user_name or target.source_user_id
                )
            elif target.created_by:
                item.creator_id = target.created_by
                item.creator_name = target.created_by_name or target.created_by
        save_index(svc.marketplace_root, source_id, items)

    return result


@router.post(
    "/market/mcp/{item_id}/versions/compare",
    response_model=VersionCompareResult,
)
async def compare_mcp_versions(
    item_id: str,
    compare_request: VersionCompareRequest,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
):
    """比对两个 MCP 版本（仅 mcp.json，与 skill_versions.compare_versions 对称）."""
    source_id = require_source_id(x_source_id)

    log_params(
        logger,
        request.method,
        request.url.path,
        item_id=item_id,
        base_version_id=compare_request.base_version_id,
        target_version_id=compare_request.target_version_id,
    )

    svc = _get_service(request)
    _validate_item_exists(svc, source_id, item_id)

    try:
        return svc.compare_versions(
            source_id,
            item_id,
            compare_request.base_version_id,
            compare_request.target_version_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/market/mcp/{item_id}/versions/{version_id}")
async def delete_mcp_version(
    item_id: str,
    version_id: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
):
    """删除某个 MCP 版本快照（管理员；拒删 current/initial）."""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)

    log_params(
        logger,
        request.method,
        request.url.path,
        item_id=item_id,
        version_id=version_id,
    )

    svc = _get_service(request)
    _validate_item_exists(svc, source_id, item_id)

    result = svc.delete_version(source_id, item_id, version_id)
    if not result["success"]:
        raise HTTPException(
            status_code=400,
            detail=result.get("message", "delete failed"),
        )
    return result

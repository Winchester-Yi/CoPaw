# -*- coding: utf-8 -*-
"""技能版本管理 API 路由."""

from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel

from ..deps import require_source_id
from ...utils.logging_utils import log_params
from ...marketplace.zip_download import build_skill_zip
from ...marketplace.version_models import (
    VersionCompareRequest,
    VersionCompareResult,
    VersionDeleteResult,
    VersionSwitchResult,
    VersionsManifest,
)
from ...marketplace.version_service import SkillVersionService
from ...marketplace.fs import get_skill_dir
from ...marketplace.service import load_index, save_index

router = APIRouter()
logger = logging.getLogger(__name__)


class VersionInitRequest(BaseModel):
    """版本初始化请求."""

    item_ids: list[str] = (
        []
    )  # 可选，指定要初始化的技能 ID 列表，为空则初始化所有
    description: str = "初始化版本"
    dry_run: bool = False  # 预览模式，不实际执行初始化


class VersionInitResult(BaseModel):
    """单个技能初始化结果."""

    item_id: str
    skill_name: str = ""  # 技能名称
    success: bool
    version_id: str = ""
    message: str = ""


class BatchVersionInitResult(BaseModel):
    """批量初始化结果."""

    total: int  # 总处理数
    initialized: int  # 成功初始化数
    skipped: int  # 已有版本跳过数
    failed: int  # 失败数
    results: list[VersionInitResult] = []


def _parse_skill_md_frontmatter(
    skill_md_content: str,
    default_name: str,
    default_desc: str,
) -> tuple[str, str]:
    """解析 SKILL.md frontmatter 提取 name 和 description（委托共享工具）.

    Args:
        skill_md_content: SKILL.md 文件内容
        default_name: 默认名称（解析失败时使用）
        default_desc: 默认描述（解析失败时使用）

    Returns:
        (name, description) 元组
    """
    from ...utils.skill_md import extract_metadata

    meta = extract_metadata(skill_md_content)
    name = meta["name"] or default_name
    desc = meta["description"] or default_desc
    return name, desc


def _update_skill_index(
    marketplace: object,
    source_id: str,
    item_id: str,
    skill_dir: Path,
    version_id: str,
) -> None:
    """切换版本后更新市场索引中的技能信息（R8：同步 creator_id/name）."""
    items = load_index(marketplace.marketplace_root, source_id)
    item = next((i for i in items if i.item_id == item_id), None)

    if not item:
        return

    skill_md_path = skill_dir / "SKILL.md"
    if skill_md_path.exists():
        skill_md_content = skill_md_path.read_text(encoding="utf-8")
        new_name, new_desc = _parse_skill_md_frontmatter(
            skill_md_content,
            item.name,
            item.description,
        )
        item.name = new_name
        item.description = new_desc

    item.version = version_id
    item.updated_at = datetime.now(timezone.utc).isoformat()

    # R8: 同步更新 creator_id/creator_name 到目标快照的来源
    from ...marketplace.version_service import SkillVersionService

    svc = SkillVersionService(marketplace.marketplace_root)
    manifest = svc._load_versions_manifest(source_id, item_id)
    target = next(
        (v for v in manifest.versions if v.version_id == version_id),
        None,
    )
    if target is not None:
        # 优先使用 source_user_*；为空时回退到 created_by
        if target.source_user_id:
            item.creator_id = target.source_user_id
            item.creator_name = (
                target.source_user_name or target.source_user_id
            )
        elif target.created_by:
            item.creator_id = target.created_by
            item.creator_name = target.created_by_name or target.created_by

    save_index(marketplace.marketplace_root, source_id, items)


def _require_manager(x_manager: Optional[str]) -> None:
    """验证管理员权限."""
    if x_manager != "true":
        raise HTTPException(status_code=403, detail="Manager access required")


def _get_version_service(request: Request) -> SkillVersionService:
    """从 app.state 获取版本服务."""
    marketplace = request.app.state.marketplace
    return SkillVersionService(marketplace.marketplace_root)


def _validate_item_exists(
    svc: SkillVersionService,
    source_id: str,
    item_id: str,
) -> None:
    """验证技能条目存在."""
    items = load_index(svc.marketplace_root, source_id)
    item = next((i for i in items if i.item_id == item_id), None)
    if not item:
        raise HTTPException(
            status_code=404,
            detail=f"Skill item {item_id} not found",
        )


def _build_version_download_name(
    versions: VersionsManifest,
    version_id: str,
) -> str:
    """构建历史版本下载文件名。"""
    skill_name = versions.skill_name or "skill"
    return f"{skill_name}-{version_id}.zip"


@router.get(
    "/market/skills/{item_id}/versions",
    response_model=VersionsManifest,
    status_code=status.HTTP_200_OK,
)
async def list_versions(
    request: Request,
    item_id: str,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
):
    """获取技能版本历史列表."""
    source_id = require_source_id(x_source_id)

    log_params(logger, request.method, request.url.path, item_id=item_id)

    svc = _get_version_service(request)

    _validate_item_exists(svc, source_id, item_id)

    versions = svc.list_versions(source_id, item_id)
    return VersionsManifest(
        skill_name=versions.get("skill_name", ""),
        versions=versions.get("versions", []),
    )


@router.get(
    "/market/skills/{item_id}/versions/{version_id}",
    response_model=dict,
    status_code=status.HTTP_200_OK,
)
async def get_version_detail(
    request: Request,
    item_id: str,
    version_id: str,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
):
    """获取单个版本详情（含文件树）."""
    source_id = require_source_id(x_source_id)

    log_params(
        logger,
        request.method,
        request.url.path,
        item_id=item_id,
        version_id=version_id,
    )

    svc = _get_version_service(request)

    _validate_item_exists(svc, source_id, item_id)

    detail = svc.get_version_detail(source_id, item_id, version_id)
    if not detail:
        raise HTTPException(
            status_code=404,
            detail=f"Version {version_id} not found",
        )
    return detail


@router.get(
    "/market/skills/{item_id}/versions/{version_id}/download",
    status_code=status.HTTP_200_OK,
)
async def download_version_snapshot(
    request: Request,
    item_id: str,
    version_id: str,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
):
    """下载指定历史版本快照 ZIP。"""
    source_id = require_source_id(x_source_id)

    log_params(
        logger,
        request.method,
        request.url.path,
        item_id=item_id,
        version_id=version_id,
    )

    svc = _get_version_service(request)

    _validate_item_exists(svc, source_id, item_id)

    versions = svc._load_versions_manifest(source_id, item_id)
    version_info = next(
        (
            version
            for version in versions.versions
            if version.version_id == version_id
        ),
        None,
    )
    if version_info is None:
        raise HTTPException(
            status_code=404,
            detail=f"Version {version_id} not found",
        )

    version_dir = svc._get_version_dir(source_id, item_id, version_id)
    if not version_dir.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"Version {version_id} not found",
        )

    with tempfile.TemporaryDirectory(
        prefix="copaw_skill_version_download_",
    ) as temp_dir:
        zip_path = build_skill_zip(
            version_dir,
            _build_version_download_name(versions, version_id),
            Path(temp_dir),
        )
        zip_bytes = zip_path.read_bytes()

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_path.name}"',
        },
    )


@router.post(
    "/market/skills/{item_id}/versions/{version_id}/switch",
    response_model=VersionSwitchResult,
    status_code=status.HTTP_200_OK,
)
async def switch_version(
    request: Request,
    item_id: str,
    version_id: str,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
):
    """切换到指定版本（管理员）."""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)

    log_params(
        logger,
        request.method,
        request.url.path,
        item_id=item_id,
        version_id=version_id,
    )

    svc = _get_version_service(request)
    marketplace = request.app.state.marketplace

    _validate_item_exists(svc, source_id, item_id)

    skill_dir = get_skill_dir(
        marketplace.marketplace_root,
        source_id,
        item_id,
    )

    result = svc.switch_version(source_id, item_id, version_id, skill_dir)

    if not result.success:
        raise HTTPException(
            status_code=400,
            detail=result.message or "Version switch failed",
        )

    # 更新市场索引中的技能信息
    _update_skill_index(marketplace, source_id, item_id, skill_dir, version_id)

    return result


@router.post(
    "/market/skills/{item_id}/versions/compare",
    response_model=VersionCompareResult,
    status_code=status.HTTP_200_OK,
)
async def compare_versions(
    request: Request,
    item_id: str,
    compare_request: VersionCompareRequest,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
):
    """比对两个版本."""
    source_id = require_source_id(x_source_id)

    log_params(
        logger,
        request.method,
        request.url.path,
        item_id=item_id,
        base_version_id=compare_request.base_version_id,
        target_version_id=compare_request.target_version_id,
    )

    svc = _get_version_service(request)

    _validate_item_exists(svc, source_id, item_id)

    result = svc.compare_versions(
        source_id,
        item_id,
        compare_request.base_version_id,
        compare_request.target_version_id,
    )

    if not result:
        raise HTTPException(
            status_code=400,
            detail="Version comparison failed",
        )

    return result


@router.delete(
    "/market/skills/{item_id}/versions/{version_id}",
    response_model=VersionDeleteResult,
    status_code=status.HTTP_200_OK,
)
async def delete_version(
    request: Request,
    item_id: str,
    version_id: str,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
):
    """删除指定版本（管理员）."""
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)

    log_params(
        logger,
        request.method,
        request.url.path,
        item_id=item_id,
        version_id=version_id,
    )

    svc = _get_version_service(request)

    _validate_item_exists(svc, source_id, item_id)

    success = svc.delete_version(source_id, item_id, version_id)

    if not success:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete current version or initial version",
        )

    return VersionDeleteResult(
        success=True,
        deleted_version=version_id,
        message="Version deleted successfully",
    )


@router.post(
    "/market/skills/versions/init-batch",
    response_model=BatchVersionInitResult,
    status_code=status.HTTP_200_OK,
)
async def batch_initialize_versions(
    request: Request,
    init_request: VersionInitRequest,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_manager: Optional[str] = Header(default=None, alias="X-Manager"),
):
    """批量初始化所有无版本历史的技能（管理员）.

    遍历所有技能，对没有版本历史的技能创建初始版本快照。
    已有版本历史的技能会被跳过。
    """
    source_id = require_source_id(x_source_id)
    _require_manager(x_manager)

    log_params(
        logger,
        request.method,
        request.url.path,
        item_ids=init_request.item_ids,
        dry_run=init_request.dry_run,
    )

    svc = _get_version_service(request)
    marketplace = request.app.state.marketplace

    # 获取所有技能（过滤掉 MCP 等非技能类型）
    items = load_index(marketplace.marketplace_root, source_id)
    skill_items = [item for item in items if item.item_type == "skill"]
    if not skill_items:
        return BatchVersionInitResult(
            total=0,
            initialized=0,
            skipped=0,
            failed=0,
            results=[],
        )

    # 如果指定了 item_ids，只处理指定的技能
    if init_request.item_ids:
        target_items = [
            item
            for item in skill_items
            if item.item_id in init_request.item_ids
        ]
        # 检查是否有不存在的 item_id
        found_ids = {item.item_id for item in target_items}
        missing_ids = set(init_request.item_ids) - found_ids
        if missing_ids:
            logger.warning("Some item_ids not found: %s", missing_ids)
    else:
        target_items = skill_items

    results: list[VersionInitResult] = []
    initialized = 0
    skipped = 0
    failed = 0

    for item in target_items:
        item_id = item.item_id
        skill_name = item.name or item_id  # 技能名称，fallback 到 item_id
        skill_dir = get_skill_dir(
            marketplace.marketplace_root,
            source_id,
            item_id,
        )

        # 检查是否已有版本历史
        manifest = svc._load_versions_manifest(source_id, item_id)
        if manifest.versions:
            skipped += 1
            results.append(
                VersionInitResult(
                    item_id=item_id,
                    skill_name=skill_name,
                    success=False,
                    message="已有版本历史，跳过",
                ),
            )
            continue

        # dry_run 模式：只检查不执行
        if init_request.dry_run:
            initialized += 1  # 预览模式下统计待初始化数量
            results.append(
                VersionInitResult(
                    item_id=item_id,
                    skill_name=skill_name,
                    success=True,
                    message="待初始化（预览模式）",
                ),
            )
            continue

        # 初始化版本，复用技能原始数据的创建者和创建时间
        current_market_version = item.version if item else None
        creator_name = item.creator_name or item.creator_id or ""
        skill_created_at = item.created_at
        try:
            new_version = svc.initialize_version(
                source_id=source_id,
                item_id=item_id,
                skill_dir=skill_dir,
                creator=creator_name,
                description=init_request.description,
                current_market_version=current_market_version,
                created_at=skill_created_at,
            )
            initialized += 1
            results.append(
                VersionInitResult(
                    item_id=item_id,
                    skill_name=skill_name,
                    success=True,
                    version_id=new_version.version_id,
                    message=f"初始化版本 {new_version.version_id}",
                ),
            )
            logger.info(
                "Initialized version %s for skill %s",
                new_version.version_id,
                item_id,
            )
        except Exception as e:
            failed += 1
            logger.error("Failed to initialize skill %s: %s", item_id, e)
            results.append(
                VersionInitResult(
                    item_id=item_id,
                    skill_name=skill_name,
                    success=False,
                    message=str(e),
                ),
            )

    logger.info(
        "Batch init completed: total=%d, initialized=%d, skipped=%d, failed=%d",
        len(target_items),
        initialized,
        skipped,
        failed,
    )

    return BatchVersionInitResult(
        total=len(target_items),
        initialized=initialized,
        skipped=skipped,
        failed=failed,
        results=results,
    )

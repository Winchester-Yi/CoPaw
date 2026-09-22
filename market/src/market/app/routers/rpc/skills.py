# -*- coding: utf-8 -*-
"""技能查询 RPC 接口."""

import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request

from market.marketplace.schemas import (
    SkillInfo,
    SkillQueryRequest,
    SkillQueryResponse,
    SkillQueryResult,
)
from market.app.deps import require_source_id
from market.utils.logging_utils import log_params

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/skills/query", response_model=SkillQueryResponse)
async def query_skills(
    request: Request,
    body: SkillQueryRequest,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
):
    """查询技能基本信息.

    支持批量查询、来源类型过滤。用于控制台技能展示、数据同步等场景。
    """
    # 参数校验：X-Source-Id 必填
    source_id = require_source_id(x_source_id)

    log_params(
        logger,
        request.method,
        request.url.path,
        skill_names=body.skill_names,
        source_types=body.source_types,
        enabled_only=body.enabled_only,
    )

    # 参数校验：技能名称列表不为空
    if not body.skill_names:
        raise HTTPException(
            status_code=400,
            detail="skill_names cannot be empty",
        )

    # 参数校验：不超过 100 个
    if len(body.skill_names) > 100:
        raise HTTPException(
            status_code=400,
            detail="Maximum 100 skill_names allowed per request",
        )

    # 规范化技能名称：去重 + strip
    seen: set[str] = set()
    unique_names: list[str] = []
    for name in body.skill_names:
        normalized = name.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique_names.append(normalized)

    if not unique_names:
        raise HTTPException(
            status_code=400,
            detail="No valid skill_names provided",
        )

    # 获取服务
    svc = request.app.state.marketplace
    registry = svc.skill_registry

    # 查询数据库
    skills_map = await registry.query_skills_by_names(
        skill_names=unique_names,
        source_id=source_id,
        source_types=body.source_types,
        enabled_only=body.enabled_only,
    )

    # 构建响应（按请求顺序）
    results: list[SkillQueryResult] = []
    total_found = 0

    for name in unique_names:
        skill_data = skills_map.get(name)
        if skill_data:
            skill_info = SkillInfo(
                skill_id=skill_data.get("skill_id", ""),
                skill_name=skill_data.get("skill_name", ""),
                cn_name=skill_data.get("cn_name", ""),
                source=skill_data.get("source", ""),
                enabled=skill_data.get("enabled", False),
                version_text=skill_data.get("version_text", "1.0.0"),
            )
            results.append(
                SkillQueryResult(
                    skill_name=name,
                    found=True,
                    skill=skill_info,
                ),
            )
            total_found += 1
        else:
            results.append(
                SkillQueryResult(
                    skill_name=name,
                    found=False,
                    skill=None,
                ),
            )

    return SkillQueryResponse(
        results=results,
        total_requested=len(unique_names),
        total_found=total_found,
    )

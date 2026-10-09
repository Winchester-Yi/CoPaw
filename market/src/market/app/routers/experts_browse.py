# -*- coding: utf-8 -*-
"""Expert Community browse routes."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ...marketplace.schemas import MarketExpertDetail, MarketExpertResponse
from ...utils.logging_utils import log_params
from ..deps import require_source_id

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/market/experts", response_model=list[MarketExpertResponse])
async def list_market_experts(
    request: Request,
    category_id: Optional[int] = None,
    bbk_ids: Optional[str] = Query(default=None),
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_bbk_id: Optional[str] = Header(default=None, alias="X-Bbk-Id"),
):
    """Browse active community experts."""
    source_id = require_source_id(x_source_id)
    user_bbk_id = x_bbk_id or "100"

    log_params(
        logger,
        request.method,
        request.url.path,
        category_id=category_id,
        bbk_ids=bbk_ids,
    )

    parsed_bbk_ids = (
        [item.strip() for item in bbk_ids.split(",") if item.strip()]
        if bbk_ids
        else None
    )
    svc = request.app.state.marketplace
    return await svc.list_expert_items(
        source_id,
        user_bbk_id,
        category_id=category_id,
        bbk_ids=parsed_bbk_ids,
    )


@router.get("/market/experts/{item_id}", response_model=MarketExpertDetail)
async def get_market_expert_detail(
    item_id: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_bbk_id: Optional[str] = Header(default=None, alias="X-Bbk-Id"),
):
    """Get a community expert detail."""
    source_id = require_source_id(x_source_id)
    user_bbk_id = x_bbk_id or "100"

    log_params(logger, request.method, request.url.path, item_id=item_id)

    svc = request.app.state.marketplace
    detail = await svc.get_expert_detail(source_id, item_id, user_bbk_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Expert not found")
    return detail

# -*- coding: utf-8 -*-
"""Expert Community version routes."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request

from ...marketplace.fs import load_index
from ...marketplace.schemas import ExpertVersionListResponse
from ...marketplace.service import MarketplaceService
from ...utils.logging_utils import log_params
from ..deps import require_source_id

router = APIRouter()
logger = logging.getLogger(__name__)


def _get_service(request: Request) -> MarketplaceService:
    return request.app.state.marketplace


def _validate_item_exists(
    svc: MarketplaceService,
    source_id: str,
    item_id: str,
) -> None:
    items = load_index(svc.marketplace_root, source_id)
    found = any(
        i.item_id == item_id
        and i.item_type == "expert"
        and i.status == "active"
        for i in items
    )
    if not found:
        raise HTTPException(
            status_code=404,
            detail=f"Expert item {item_id} not found",
        )


@router.get(
    "/market/experts/{item_id}/versions",
    response_model=ExpertVersionListResponse,
)
async def list_expert_versions(
    item_id: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
):
    """List expert version history."""
    source_id = require_source_id(x_source_id)

    log_params(logger, request.method, request.url.path, item_id=item_id)

    svc = _get_service(request)
    _validate_item_exists(svc, source_id, item_id)
    return svc._get_expert_version_service().list_versions(source_id, item_id)


@router.get("/market/experts/{item_id}/versions/{version_id}")
async def get_expert_version_detail(
    item_id: str,
    version_id: str,
    request: Request,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
):
    """Get a historical expert version detail."""
    source_id = require_source_id(x_source_id)

    log_params(
        logger,
        request.method,
        request.url.path,
        item_id=item_id,
        version_id=version_id,
    )

    svc = _get_service(request)
    _validate_item_exists(svc, source_id, item_id)
    try:
        return svc._get_expert_version_service().get_version_detail(
            source_id,
            item_id,
            version_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

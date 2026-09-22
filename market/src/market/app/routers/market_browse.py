# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ...marketplace.browse import (
    ORPHANED_CATEGORY_ID,
    UNCATEGORIZED_CATEGORY_ID,
    count_by_branch,
    count_by_category,
    filter_market_items,
    is_orphaned_item,
)
from ...marketplace.fs import load_index
from ...marketplace.schemas import (
    MarketBrowseBranch,
    MarketBrowseCategory,
    MarketBrowseResponse,
)
from ..deps import require_source_id

router = APIRouter()


async def _load_categories(request: Request, source_id: str) -> list[dict]:
    db = request.app.state.marketplace.db
    if not db.is_connected:
        raise HTTPException(status_code=503, detail="Database unavailable")
    return await db.fetch_all(
        "SELECT id, name, sort_order, COALESCE(branch_visible, 1) AS branch_visible "
        "FROM swe_marketplace_categories WHERE source_id = %s ORDER BY sort_order ASC",
        (source_id,),
    )


def _visible_category_ids(
    categories: list[dict],
    is_head_office: bool,
) -> set[int] | None:
    if is_head_office:
        return None
    return {
        int(category["id"])
        for category in categories
        if bool(category.get("branch_visible", True))
    }


def _branch_ids(
    items: list,
    user_bbk_id: str,
    is_head_office: bool,
) -> list[str]:
    ids = {"100"}
    if is_head_office:
        for item in items:
            ids.update(item.bbk_ids)
    elif user_bbk_id != "100":
        ids.add(user_bbk_id)
    return sorted(ids)


def _normalize_resource_filters(
    resource_type: str,
    category_id: int | None,
    uncategorized: bool,
    orphaned: bool,
    visible_ids: set[int] | None,
    known_category_ids: set[int],
) -> tuple[
    int | None,
    bool,
    bool,
    set[int] | None,
    set[int],
]:
    if resource_type != "mcp":
        return (
            category_id,
            uncategorized,
            orphaned,
            visible_ids,
            known_category_ids,
        )
    return None, False, False, None, set()


def _build_category_facets(
    resource_type: str,
    categories: list[dict],
    branch_items: list,
    known_category_ids: set[int],
    is_head_office: bool,
) -> list[MarketBrowseCategory]:
    if resource_type != "skill":
        return []
    visible_categories = [
        category
        for category in categories
        if is_head_office or bool(category.get("branch_visible", True))
    ]
    category_counts = count_by_category(
        branch_items,
        (int(category["id"]) for category in visible_categories),
    )
    result = [
        MarketBrowseCategory(
            id=int(category["id"]),
            name=category["name"],
            count=category_counts[int(category["id"])],
        )
        for category in visible_categories
    ]
    uncategorized_count = sum(
        1 for item in branch_items if item.category_id is None
    )
    orphaned_count = sum(
        1
        for item in branch_items
        if is_orphaned_item(item, known_category_ids)
    )
    if uncategorized_count > 0:
        result.append(
            MarketBrowseCategory(
                id=UNCATEGORIZED_CATEGORY_ID,
                name="未分类",
                count=uncategorized_count,
            ),
        )
    if orphaned_count > 0:
        result.append(
            MarketBrowseCategory(
                id=ORPHANED_CATEGORY_ID,
                name="待整理分类",
                count=orphaned_count,
            ),
        )
    return result


async def _load_items(
    request: Request,
    source_id: str,
    resource_type: str,
    user_bbk_id: str,
    selected_bbk_id: str | None,
    selected_category_id: int | None,
    is_head_office: bool,
    visible_category_ids: set[int] | None,
    selected_uncategorized: bool,
    selected_orphaned: bool,
    known_category_ids: set[int],
) -> list[dict]:
    svc = request.app.state.marketplace
    if resource_type == "skill":
        result = await svc.list_skills(
            source_id,
            user_bbk_id,
            category_id=selected_category_id,
            bbk_ids=[selected_bbk_id] if selected_bbk_id else None,
            is_manager=is_head_office,
            visible_category_ids=visible_category_ids,
            selected_uncategorized=selected_uncategorized,
            selected_orphaned=selected_orphaned,
            known_category_ids=known_category_ids,
        )
    else:
        result = await svc.list_mcp_items(
            source_id,
            user_bbk_id,
            category_id=selected_category_id,
            bbk_ids=[selected_bbk_id] if selected_bbk_id else None,
            is_manager=is_head_office,
            visible_category_ids=visible_category_ids,
            selected_uncategorized=selected_uncategorized,
            selected_orphaned=selected_orphaned,
            known_category_ids=known_category_ids,
        )
    return [item.model_dump() for item in result]


@router.get("/market/browse", response_model=MarketBrowseResponse)
async def browse_market(
    request: Request,
    resource_type: Literal["skill", "mcp"] = Query(...),
    category_id: Optional[int] = None,
    bbk_id: Optional[str] = None,
    uncategorized: bool = False,
    orphaned: bool = False,
    x_source_id: Optional[str] = Header(default=None, alias="X-Source-Id"),
    x_bbk_id: Optional[str] = Header(default=None, alias="X-Bbk-Id"),
) -> MarketBrowseResponse:
    source_id = require_source_id(x_source_id)
    user_bbk_id = x_bbk_id or "100"
    is_head_office = user_bbk_id == "100"
    categories = await _load_categories(request, source_id)
    known_category_ids = {int(category["id"]) for category in categories}
    visible_ids = _visible_category_ids(categories, is_head_office)
    (
        category_id,
        uncategorized,
        orphaned,
        visible_ids,
        known_category_ids,
    ) = _normalize_resource_filters(
        resource_type,
        category_id,
        uncategorized,
        orphaned,
        visible_ids,
        known_category_ids,
    )
    raw_items = load_index(
        request.app.state.marketplace.marketplace_root,
        source_id,
    )
    base_items = filter_market_items(
        raw_items,
        resource_type,
        user_bbk_id,
        None,
        None,
        is_head_office,
        visible_ids,
        False,
        False,
        known_category_ids,
    )
    branch_items = filter_market_items(
        base_items,
        resource_type,
        user_bbk_id,
        bbk_id,
        None,
        is_head_office,
        visible_ids,
        False,
        False,
        known_category_ids,
    )
    category_items = filter_market_items(
        base_items,
        resource_type,
        user_bbk_id,
        None,
        category_id,
        is_head_office,
        visible_ids,
        uncategorized,
        orphaned,
        known_category_ids,
    )
    branch_ids = _branch_ids(base_items, user_bbk_id, is_head_office)
    branch_counts = count_by_branch(category_items, branch_ids)
    browse_categories = _build_category_facets(
        resource_type,
        categories,
        branch_items,
        known_category_ids,
        is_head_office,
    )
    result_items = await _load_items(
        request,
        source_id,
        resource_type,
        user_bbk_id,
        bbk_id,
        category_id,
        is_head_office,
        visible_ids,
        uncategorized,
        orphaned,
        known_category_ids,
    )
    return MarketBrowseResponse(
        resource_type=resource_type,
        items=result_items,
        total=len(result_items),
        category_total=len(branch_items),
        branch_total=len(category_items),
        categories=browse_categories,
        branches=[
            MarketBrowseBranch(
                bbk_id=branch_id,
                count=branch_counts[branch_id],
            )
            for branch_id in branch_ids
        ],
    )

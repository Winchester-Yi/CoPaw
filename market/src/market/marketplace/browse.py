# -*- coding: utf-8 -*-
from __future__ import annotations

from collections.abc import Iterable

from .models import MarketItem

UNCATEGORIZED_CATEGORY_ID = -1
ORPHANED_CATEGORY_ID = -2


def is_head_office_item(item: MarketItem) -> bool:
    return not item.bbk_ids or "100" in item.bbk_ids


def matches_branch_scope(
    item: MarketItem,
    user_bbk_id: str,
    selected_bbk_id: str | None,
    is_head_office: bool,
) -> bool:
    if selected_bbk_id == "100":
        return is_head_office_item(item)
    if selected_bbk_id:
        if not is_head_office and selected_bbk_id != user_bbk_id:
            return False
        return selected_bbk_id in item.bbk_ids
    if is_head_office:
        return True
    return is_head_office_item(item) or user_bbk_id in item.bbk_ids


def is_visible_to_user(
    item: MarketItem,
    visible_category_ids: set[int] | None,
) -> bool:
    return (
        visible_category_ids is None
        or item.category_id is None
        or item.category_id in visible_category_ids
    )


def is_orphaned_item(
    item: MarketItem,
    known_category_ids: set[int],
) -> bool:
    return (
        item.category_id is not None
        and item.category_id not in known_category_ids
    )


def filter_market_items(
    items: Iterable[MarketItem],
    resource_type: str,
    user_bbk_id: str,
    selected_bbk_id: str | None,
    selected_category_id: int | None,
    is_head_office: bool,
    visible_category_ids: set[int] | None,
    selected_uncategorized: bool = False,
    selected_orphaned: bool = False,
    known_category_ids: set[int] | None = None,
) -> list[MarketItem]:
    def matches_category(item: MarketItem) -> bool:
        if selected_uncategorized:
            return item.category_id is None
        if selected_orphaned:
            return known_category_ids is not None and is_orphaned_item(
                item,
                known_category_ids,
            )
        return (
            selected_category_id is None
            or item.category_id == selected_category_id
        )

    return [
        item
        for item in items
        if item.item_type == resource_type
        and item.status == "active"
        and is_visible_to_user(item, visible_category_ids)
        and matches_branch_scope(
            item,
            user_bbk_id,
            selected_bbk_id,
            is_head_office,
        )
        and matches_category(item)
    ]


def count_by_category(
    items: Iterable[MarketItem],
    category_ids: Iterable[int],
) -> dict[int, int]:
    counts = {category_id: 0 for category_id in category_ids}
    for item in items:
        if item.category_id is not None and item.category_id in counts:
            counts[item.category_id] += 1
    return counts


def count_by_branch(
    items: Iterable[MarketItem],
    branch_ids: Iterable[str],
) -> dict[str, int]:
    counts = {branch_id: 0 for branch_id in branch_ids}
    for item in items:
        branch_targets = item.bbk_ids or ["100"]
        for branch_id in branch_targets:
            if branch_id in counts:
                counts[branch_id] += 1
    return counts

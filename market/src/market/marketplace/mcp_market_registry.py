# -*- coding: utf-8 -*-
"""市场 MCP 元数据数据库操作."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from .errors import MarketplaceDatabaseUnavailableError
from .models import MarketItem


def _as_iso_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _as_bbk_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return [value] if value else []
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if str(item).strip()]


def market_mcp_from_row(row: dict[str, Any]) -> MarketItem:
    """Convert one market-MCP TDSQL row to the shared market model."""
    return MarketItem(
        item_id=str(row.get("item_id") or ""),
        item_type="mcp",
        client_key=str(row.get("client_key") or ""),
        name=str(row.get("name") or ""),
        chinese_name=str(row.get("chinese_name") or ""),
        description=str(row.get("description") or ""),
        guidance=str(row.get("guidance") or ""),
        version=str(row.get("version") or "1.0.0"),
        creator_id=str(row.get("creator_id") or ""),
        creator_name=str(row.get("creator_name") or ""),
        category_id=row.get("category_id"),
        bbk_ids=_as_bbk_ids(row.get("bbk_ids")),
        status=str(row.get("status") or "active"),
        created_at=_as_iso_string(row.get("created_at")),
        updated_at=_as_iso_string(row.get("updated_at")),
        content_path=str(row.get("content_path") or ""),
    )


class MCPMarketRegistry:
    """市场 MCP 元数据查询与写入入口."""

    def __init__(self, db):
        self.db = db

    def is_connected(self) -> bool:
        return bool(self.db.is_connected)

    async def upsert_market_mcp(
        self,
        source_id: str,
        item: MarketItem,
        content_path: str,
        updator_id: str = "",
        updator_name: str = "",
    ) -> bool:
        """Upsert market MCP metadata while keeping its config on NAS."""
        if not self.is_connected():
            return False
        await self.db.execute(
            """
            INSERT INTO swe_marketplace_mcps
                (source_id, item_id, client_key, name, chinese_name,
                 description, guidance, version, creator_id, creator_name,
                 category_id, bbk_ids, content_path, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                client_key = VALUES(client_key),
                name = VALUES(name),
                chinese_name = VALUES(chinese_name),
                description = VALUES(description),
                guidance = VALUES(guidance),
                version = VALUES(version),
                category_id = VALUES(category_id),
                bbk_ids = VALUES(bbk_ids),
                content_path = VALUES(content_path),
                status = VALUES(status),
                updator_id = %s,
                updator_name = %s,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                source_id,
                item.item_id,
                item.client_key,
                item.name,
                item.chinese_name,
                item.description,
                item.guidance,
                item.version,
                item.creator_id,
                item.creator_name,
                item.category_id,
                json.dumps(item.bbk_ids),
                content_path,
                item.status,
                updator_id,
                updator_name,
            ),
        )
        return True

    async def mark_deleted(
        self,
        source_id: str,
        item_id: str,
        updator_id: str = "",
        updator_name: str = "",
    ) -> bool:
        """Soft-delete one market MCP metadata row."""
        if not self.is_connected():
            return False
        await self.db.execute(
            """
            UPDATE swe_marketplace_mcps
            SET status = 'deleted',
                updated_at = CURRENT_TIMESTAMP
            WHERE source_id = %s AND item_id = %s
            """,
            (source_id, item_id),
        )
        return True

    async def list_market_mcps(
        self,
        source_id: str,
        user_bbk_id: str = "100",
        category_id: int | None = None,
        is_manager: bool = False,
    ) -> list[MarketItem]:
        """List active market MCP metadata from TDSQL."""
        if not self.is_connected():
            raise MarketplaceDatabaseUnavailableError("Database unavailable")
        clauses = ["source_id = %s", "status = 'active'"]
        params: list[Any] = [source_id]
        if category_id is not None:
            clauses.append("category_id = %s")
            params.append(category_id)
        if not is_manager and user_bbk_id != "100":
            clauses.append(
                "("
                "bbk_ids IS NULL OR JSON_LENGTH(bbk_ids) = 0 "
                "OR JSON_CONTAINS(bbk_ids, JSON_QUOTE(%s)) "
                "OR JSON_CONTAINS(bbk_ids, JSON_QUOTE('100'))"
                ")",
            )
            params.append(user_bbk_id)
        rows = await self.db.fetch_all(
            """
            SELECT item_id, client_key, name, chinese_name, description,
                   guidance, version, creator_id, creator_name, category_id,
                   bbk_ids, content_path, status, created_at, updated_at
            FROM swe_marketplace_mcps
            WHERE """
            + " AND ".join(clauses)
            + " ORDER BY COALESCE(updated_at, created_at) DESC, item_id",
            tuple(params),
        )
        return [market_mcp_from_row(dict(row)) for row in rows]

    async def get_market_mcp(
        self,
        source_id: str,
        item_id: str,
    ) -> MarketItem | None:
        """Get one active market MCP metadata row from TDSQL."""
        if not self.is_connected():
            raise MarketplaceDatabaseUnavailableError("Database unavailable")
        row = await self.db.fetch_one(
            """
            SELECT item_id, client_key, name, chinese_name, description,
                   guidance, version, creator_id, creator_name, category_id,
                   bbk_ids, content_path, status, created_at, updated_at
            FROM swe_marketplace_mcps
            WHERE source_id = %s
              AND item_id = %s
              AND status = 'active'
            """,
            (source_id, item_id),
        )
        if not row or not row.get("item_id"):
            return None
        return market_mcp_from_row(dict(row))

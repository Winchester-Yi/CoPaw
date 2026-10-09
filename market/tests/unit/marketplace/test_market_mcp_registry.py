# -*- coding: utf-8 -*-
"""市场 MCP 元数据 Registry 测试."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from market.marketplace.mcp_market_registry import MCPMarketRegistry
from market.marketplace.errors import MarketplaceDatabaseUnavailableError


@pytest.mark.asyncio
async def test_list_market_mcps_maps_tdsql_metadata_to_market_items():
    db = MagicMock()
    db.is_connected = True
    db.fetch_all = AsyncMock(
        return_value=[
            {
                "item_id": "mcp-item-1",
                "client_key": "weather",
                "name": "Weather",
                "chinese_name": "天气",
                "description": "weather tool",
                "guidance": "use it",
                "version": "1.0.1",
                "creator_id": "admin",
                "creator_name": "管理员",
                "category_id": None,
                "bbk_ids": ["100"],
                "content_path": "/market/mcp-item-1/mcp.json",
                "status": "active",
                "created_at": None,
                "updated_at": None,
            },
        ],
    )

    registry = MCPMarketRegistry(db)
    items = await registry.list_market_mcps(
        source_id="src-1",
        user_bbk_id="100",
    )

    assert len(items) == 1
    assert items[0].item_type == "mcp"
    assert items[0].client_key == "weather"
    assert items[0].bbk_ids == ["100"]
    assert items[0].content_path == "/market/mcp-item-1/mcp.json"
    sql = db.fetch_all.call_args.args[0]
    assert "FROM swe_marketplace_mcps" in sql
    assert db.fetch_all.call_args.args[1] == ("src-1",)


@pytest.mark.asyncio
async def test_get_market_mcp_filters_inactive_rows():
    db = MagicMock()
    db.is_connected = True
    db.fetch_one = AsyncMock(return_value=None)

    registry = MCPMarketRegistry(db)

    assert (
        await registry.get_market_mcp(
            source_id="src-1",
            item_id="mcp-item-1",
        )
        is None
    )
    sql = db.fetch_one.call_args.args[0]
    assert "status = 'active'" in sql


@pytest.mark.asyncio
async def test_market_mcp_queries_raise_when_database_is_unavailable():
    db = MagicMock()
    db.is_connected = False
    registry = MCPMarketRegistry(db)

    with pytest.raises(MarketplaceDatabaseUnavailableError):
        await registry.list_market_mcps("src-1")

    with pytest.raises(MarketplaceDatabaseUnavailableError):
        await registry.get_market_mcp("src-1", "mcp-item-1")

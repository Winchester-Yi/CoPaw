# -*- coding: utf-8 -*-
"""MCP 版本浏览 API 集成测试 (T10)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_app(tmp_path):
    from market.app.routers import api_router
    from market.database.connection import DatabaseConnection
    from market.marketplace.service import MarketplaceService

    mock_db = AsyncMock(spec=DatabaseConnection)
    mock_db.is_connected = True
    mock_db.execute = AsyncMock(return_value=1)
    mock_db.fetch_one = AsyncMock(return_value=None)
    mock_db.fetch_all = AsyncMock(return_value=[])

    svc = MarketplaceService(
        db=mock_db,
        marketplace_root=tmp_path / "market",
        swe_root=tmp_path / "swe",
    )
    svc.mcp_market_registry.upsert_market_mcp = AsyncMock(return_value=True)
    app = FastAPI()
    app.state.marketplace = svc
    app.include_router(api_router, prefix="/api")
    return app


def _hdr_manager():
    return {
        "X-Source-Id": "src1",
        "X-Manager": "true",
        "X-User-Id": "admin",
        "X-User-Name": "admin",
    }


def _hdr_user():
    return {"X-Source-Id": "src1", "X-User-Id": "user1"}


@pytest.mark.asyncio
async def test_list_mcp_versions_returns_versions(tmp_path):
    """publish 之后 GET /market/mcp/{id}/versions 返回快照列表."""
    from market.marketplace.schemas import PublishMCPRequest

    app = _make_app(tmp_path)
    svc = app.state.marketplace
    item, _ = await svc.publish_mcp(
        "src1",
        PublishMCPRequest(
            client_key="m1",
            name="demo",
            description="d",
            creator_id="alice",
            creator_name="Alice",
            config={"name": "demo", "transport": "stdio", "command": "/a"},
            version="1.0.0",
        ),
    )
    client = TestClient(app)
    resp = client.get(
        f"/api/market/mcp/{item.item_id}/versions",
        headers=_hdr_user(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "versions" in data
    assert len(data["versions"]) >= 1
    assert data["versions"][0]["version_id"] == "1.0.0"


@pytest.mark.asyncio
async def test_switch_mcp_version_updates_market_item(tmp_path):
    """T10：切版本同步 MarketItem creator/version (R8)."""
    from market.marketplace.schemas import PublishMCPRequest
    from market.marketplace.fs import load_index

    app = _make_app(tmp_path)
    svc = app.state.marketplace

    item, _ = await svc.publish_mcp(
        "src1",
        PublishMCPRequest(
            client_key="m1",
            name="demo",
            description="a",
            creator_id="alice",
            creator_name="Alice",
            config={"name": "demo", "transport": "stdio", "command": "/a"},
            version="1.0.0",
        ),
    )
    await svc.publish_mcp(
        "src1",
        PublishMCPRequest(
            client_key="m1",
            name="demo",
            description="b",
            creator_id="bob",
            creator_name="Bob",
            config={"name": "demo", "transport": "stdio", "command": "/b"},
            version="2.0.0",
            overwrite=True,
        ),
    )

    client = TestClient(app)
    resp = client.post(
        f"/api/market/mcp/{item.item_id}/versions/1.0.0/switch",
        headers=_hdr_manager(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["current_version"] == "1.0.0"

    # MarketItem 同步更新到 alice
    items = load_index(tmp_path / "market", "src1")
    item_after = next(i for i in items if i.item_id == item.item_id)
    assert item_after.version == "1.0.0"
    assert item_after.creator_id == "alice"


@pytest.mark.asyncio
async def test_delete_current_mcp_version_returns_400(tmp_path):
    from market.marketplace.schemas import PublishMCPRequest

    app = _make_app(tmp_path)
    svc = app.state.marketplace
    item, _ = await svc.publish_mcp(
        "src1",
        PublishMCPRequest(
            client_key="m1",
            name="demo",
            description="a",
            creator_id="alice",
            creator_name="Alice",
            config={"name": "demo", "transport": "stdio", "command": "/a"},
            version="1.0.0",
        ),
    )
    client = TestClient(app)
    resp = client.delete(
        f"/api/market/mcp/{item.item_id}/versions/1.0.0",
        headers=_hdr_manager(),
    )
    assert resp.status_code == 400

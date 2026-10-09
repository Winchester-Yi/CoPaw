# -*- coding: utf-8 -*-
"""市场列表和详情在 TDSQL 不可用时的错误语义测试."""

from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from market.app.routers.mcp_browse import router as mcp_router
from market.app.routers.skills_browse import router as skills_router
from market.database.connection import DatabaseConnection
from market.marketplace.service import MarketplaceService


def _make_app(tmp_path: Path) -> FastAPI:
    db = AsyncMock(spec=DatabaseConnection)
    db.is_connected = False
    app = FastAPI()
    app.state.marketplace = MarketplaceService(
        db=db,
        marketplace_root=tmp_path / "market",
        swe_root=tmp_path / "swe",
    )
    app.include_router(skills_router, prefix="/api")
    app.include_router(mcp_router, prefix="/api")
    return app


def test_market_lists_return_503_when_database_is_unavailable(tmp_path):
    client = TestClient(_make_app(tmp_path))
    headers = {"X-Source-Id": "src-1", "X-Bbk-Id": "100"}

    assert client.get("/api/market/skills", headers=headers).status_code == 503
    assert client.get("/api/market/mcp", headers=headers).status_code == 503
    assert (
        client.get("/api/market/bbk-ids", headers=headers).status_code == 503
    )


def test_market_details_return_503_when_database_is_unavailable(tmp_path):
    client = TestClient(_make_app(tmp_path))
    headers = {"X-Source-Id": "src-1", "X-Bbk-Id": "100"}

    assert (
        client.get("/api/market/skills/item-1", headers=headers).status_code
        == 503
    )
    assert (
        client.get("/api/market/mcp/item-1", headers=headers).status_code
        == 503
    )

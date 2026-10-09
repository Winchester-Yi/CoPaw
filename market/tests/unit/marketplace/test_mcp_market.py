# -*- coding: utf-8 -*-
"""市场 MCP 管理路由测试."""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock


def _make_app(tmp_path):
    from market.app.routers.mcp_market import router
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
    app = FastAPI()
    app.state.marketplace = svc
    app.include_router(router, prefix="/api")
    return app


def test_upload_mcp_returns_503_when_database_is_unavailable(tmp_path):
    app = _make_app(tmp_path)
    app.state.marketplace.db.is_connected = False
    client = TestClient(app)

    response = client.post(
        "/api/market/mcp/upload",
        data={
            "raw_json": json.dumps(
                {
                    "mcpServers": {
                        "demo": {
                            "transport": "stdio",
                            "command": "echo",
                        },
                    },
                },
            ),
        },
        headers={"X-Source-Id": "src_a", "X-Manager": "true"},
    )

    assert response.status_code == 503

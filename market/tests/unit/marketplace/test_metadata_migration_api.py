# -*- coding: utf-8 -*-
"""NAS 元数据初始化接口测试."""

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from market.app.routers import api_router
from market.database.connection import DatabaseConnection
from market.marketplace.service import MarketplaceService


def _make_app(tmp_path):
    db = AsyncMock(spec=DatabaseConnection)
    db.is_connected = True
    app = FastAPI()
    app.state.marketplace = MarketplaceService(
        db=db,
        marketplace_root=tmp_path / "market",
        swe_root=tmp_path / "swe",
    )
    app.include_router(api_router, prefix="/api")
    return app


def test_metadata_migration_requires_manager_access(tmp_path):
    client = TestClient(_make_app(tmp_path))

    response = client.post(
        "/api/market/admin/metadata/migrate",
        json={"source_ids": ["src-1"]},
    )

    assert response.status_code == 403


def test_metadata_migration_returns_503_when_database_is_unavailable(tmp_path):
    app = _make_app(tmp_path)
    app.state.marketplace.db.is_connected = False

    response = TestClient(app).post(
        "/api/market/admin/metadata/migrate",
        json={"source_ids": ["src-1"]},
        headers={"X-Manager": "true"},
    )

    assert response.status_code == 503


def test_metadata_migration_dry_run_summarizes_multiple_sources(
    tmp_path,
    monkeypatch,
):
    app = _make_app(tmp_path)
    results = {
        "src-1": {
            "source_id": "src-1",
            "dry_run": True,
            "total": 2,
            "skills": 1,
            "mcps": 1,
            "migrated": 0,
            "missing_content": [],
            "errors": [
                {"item_type": "mcp", "item_id": "mcp-1", "error": "failed"},
            ],
        },
        "src-2": {
            "source_id": "src-2",
            "dry_run": True,
            "total": 1,
            "skills": 1,
            "mcps": 0,
            "migrated": 0,
            "missing_content": [
                {"item_type": "skill", "item_id": "skill-2"},
            ],
            "errors": [],
        },
    }
    migrate = AsyncMock(
        side_effect=lambda **kwargs: results[kwargs["source_id"]],
    )
    monkeypatch.setattr(
        "market.app.routers.metadata_migration.migrate_source_metadata",
        migrate,
    )

    response = TestClient(app).post(
        "/api/market/admin/metadata/migrate",
        json={"source_ids": ["src-1", "src-2"]},
        headers={"X-Manager": "true"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "dry_run": True,
        "source_ids": ["src-1", "src-2"],
        "total": 3,
        "skills": 2,
        "mcps": 1,
        "migrated": 0,
        "missing_content": [
            {"source_id": "src-2", "item_type": "skill", "item_id": "skill-2"},
        ],
        "errors": [
            {
                "source_id": "src-1",
                "item_type": "mcp",
                "item_id": "mcp-1",
                "error": "failed",
            },
        ],
        "details": [results["src-1"], results["src-2"]],
    }
    assert migrate.await_count == 2


def test_metadata_migration_rejects_invalid_source_id(tmp_path):
    client = TestClient(_make_app(tmp_path))

    response = client.post(
        "/api/market/admin/metadata/migrate",
        json={"source_ids": ["../outside"]},
        headers={"X-Manager": "true"},
    )

    assert response.status_code == 400

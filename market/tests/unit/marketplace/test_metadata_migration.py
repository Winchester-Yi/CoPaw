# -*- coding: utf-8 -*-
"""市场元数据迁移测试."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from market.marketplace.fs import save_index, save_mcp_config
from market.marketplace.metadata_migration import migrate_source_metadata
from market.marketplace.models import MarketItem


@pytest.mark.asyncio
async def test_migrate_source_metadata_is_idempotent_at_registry_boundary(
    tmp_path,
):
    db = MagicMock()
    db.is_connected = True
    db.fetch_one = AsyncMock(return_value=None)
    db.execute = AsyncMock()
    root = tmp_path / "market"
    source_id = "src-1"
    skill = MarketItem(
        item_id="skill-1",
        item_type="skill",
        name="risk",
        creator_id="u1",
    )
    mcp = MarketItem(
        item_id="mcp-1",
        item_type="mcp",
        client_key="weather",
        name="weather",
        creator_id="u1",
    )
    save_index(root, source_id, [skill, mcp])
    (root / source_id / "skills" / "skill-1").mkdir(parents=True)
    save_mcp_config(
        root,
        source_id,
        "mcp-1",
        {"client_key": "weather", "config": {"command": "weather"}},
    )

    result = await migrate_source_metadata(
        db=db,
        marketplace_root=root,
        source_id=source_id,
    )

    assert result["total"] == 2
    assert result["migrated"] == 2
    assert result["missing_content"] == []
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_migrate_source_metadata_dry_run_reports_missing_content(
    tmp_path,
):
    db = MagicMock()
    db.is_connected = True
    db.execute = AsyncMock()
    root = tmp_path / "market"
    save_index(
        root,
        "src-1",
        [
            MarketItem(
                item_id="skill-1",
                item_type="skill",
                name="risk",
                creator_id="u1",
            ),
        ],
    )

    result = await migrate_source_metadata(
        db=db,
        marketplace_root=root,
        source_id="src-1",
        dry_run=True,
    )

    assert result["migrated"] == 0
    assert result["missing_content"][0]["item_id"] == "skill-1"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_migrate_source_metadata_does_not_count_failed_skill_upsert(
    tmp_path,
    monkeypatch,
):
    db = MagicMock()
    db.is_connected = True
    db.execute = AsyncMock()
    root = tmp_path / "market"
    save_index(
        root,
        "src-1",
        [
            MarketItem(
                item_id="skill-1",
                item_type="skill",
                name="risk",
                creator_id="u1",
            ),
        ],
    )
    (root / "src-1" / "skills" / "skill-1").mkdir(parents=True)

    from market.marketplace.metadata_migration import MarketSkillRegistry

    failed_upsert = AsyncMock(return_value=False)
    monkeypatch.setattr(
        MarketSkillRegistry,
        "upsert_market_skill",
        failed_upsert,
    )
    result = await migrate_source_metadata(
        db=db,
        marketplace_root=root,
        source_id="src-1",
    )

    assert result["migrated"] == 0
    assert result["errors"] == [
        {
            "item_type": "skill",
            "item_id": "skill-1",
            "error": "metadata upsert returned false",
        },
    ]


@pytest.mark.asyncio
async def test_migrate_source_metadata_does_not_count_failed_mcp_upsert(
    tmp_path,
    monkeypatch,
):
    db = MagicMock()
    db.is_connected = True
    db.execute = AsyncMock()
    root = tmp_path / "market"
    save_index(
        root,
        "src-1",
        [
            MarketItem(
                item_id="mcp-1",
                item_type="mcp",
                client_key="weather",
                name="weather",
                creator_id="u1",
            ),
        ],
    )
    save_mcp_config(root, "src-1", "mcp-1", {"command": "weather"})

    from market.marketplace.metadata_migration import MCPMarketRegistry

    failed_upsert = AsyncMock(return_value=False)
    monkeypatch.setattr(MCPMarketRegistry, "upsert_market_mcp", failed_upsert)
    result = await migrate_source_metadata(
        db=db,
        marketplace_root=root,
        source_id="src-1",
    )

    assert result["migrated"] == 0
    assert result["errors"] == [
        {
            "item_type": "mcp",
            "item_id": "mcp-1",
            "error": "metadata upsert returned false",
        },
    ]

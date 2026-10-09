# -*- coding: utf-8 -*-
"""市场技能数据库操作类单元测试."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from market.marketplace.market_skill_registry import MarketSkillRegistry
from market.marketplace.errors import MarketplaceDatabaseUnavailableError


@pytest.mark.asyncio
class TestMarketSkillRegistry:
    """MarketSkillRegistry 单元测试."""

    def test_is_connected(self):
        """测试数据库连接状态检查."""
        db = MagicMock()
        db.is_connected = True
        registry = MarketSkillRegistry(db)
        assert registry.is_connected() is True

    async def test_upsert_market_skill_insert(self):
        """测试插入新记录."""
        db = MagicMock()
        db.is_connected = True
        db.fetch_one = AsyncMock(return_value=None)
        db.execute = AsyncMock()

        registry = MarketSkillRegistry(db)
        result = await registry.upsert_market_skill(
            source_id="test_source",
            item_id="test_item",
            skill_id="test_skill_id",
            skill_name="test_skill",
            cn_name="测试技能",
            include_in_statistics=True,
            creator_id="user1",
            creator_name="用户1",
        )

        assert result is True
        db.execute.assert_called_once()
        params = db.execute.call_args.args[1]
        assert len(params) == 16

    async def test_upsert_market_skill_update(self):
        """测试更新现有记录."""
        db = MagicMock()
        db.is_connected = True
        db.fetch_one = AsyncMock(return_value={"id": 1})
        db.execute = AsyncMock()

        registry = MarketSkillRegistry(db)
        result = await registry.upsert_market_skill(
            source_id="test_source",
            item_id="test_item",
            skill_id="test_skill_id",
            skill_name="test_skill",
            cn_name="测试技能",
            include_in_statistics=False,
            updator_id="user2",
            updator_name="用户2",
        )

        assert result is True
        db.execute.assert_called_once()
        sql = db.execute.call_args.args[0]
        assert "is_unpublished = 0" in sql
        assert "is_deleted = 0" in sql

    async def test_update_statistics_config(self):
        """测试更新统计配置."""
        db = MagicMock()
        db.is_connected = True
        db.execute = AsyncMock()

        registry = MarketSkillRegistry(db)
        result = await registry.update_statistics_config(
            source_id="test_source",
            item_id="test_item",
            include_in_statistics=False,
            updator_id="admin",
            updator_name="管理员",
        )

        assert result is True
        db.execute.assert_called_once()

    async def test_get_statistics_eligible_skill_names(self):
        """测试获取纳入统计的技能名称."""
        db = MagicMock()
        db.is_connected = True
        db.fetch_all = AsyncMock(
            return_value=[
                {"skill_name": "skill1"},
                {"skill_name": "skill2"},
            ],
        )

        registry = MarketSkillRegistry(db)
        result = await registry.get_statistics_eligible_skill_names(
            "test_source",
        )

        assert result == {"skill1", "skill2"}

    async def test_list_statistics_eligible_unique_skills_by_source_id(self):
        """测试获取纳入统计的技能下拉选项."""
        db = MagicMock()
        db.is_connected = True
        db.fetch_all = AsyncMock(
            return_value=[
                {
                    "skill_id": "skill_a",
                    "skill_name": "skill_a_name",
                    "cn_name": "技能A",
                },
                {
                    "skill_id": "skill_b",
                    "skill_name": "skill_b_name",
                    "cn_name": "技能B",
                },
            ],
        )

        registry = MarketSkillRegistry(db)
        result = (
            await registry.list_statistics_eligible_unique_skills_by_source_id(
                "test_source",
            )
        )

        assert result == [
            {
                "skill_id": "skill_a",
                "skill_name": "skill_a_name",
                "cn_name": "技能A",
            },
            {
                "skill_id": "skill_b",
                "skill_name": "skill_b_name",
                "cn_name": "技能B",
            },
        ]
        sql = db.fetch_all.call_args.args[0]
        assert "FROM swe_marketplace_skills" in sql
        assert "include_in_statistics = 1" in sql
        assert "is_unpublished = 0" in sql
        assert "is_deleted = 0" in sql
        assert "GROUP BY skill_id" in sql
        assert db.fetch_all.call_args.args[1] == ("test_source",)

    async def test_database_not_connected(self):
        """测试数据库未连接时的处理."""
        db = MagicMock()
        db.is_connected = False

        registry = MarketSkillRegistry(db)
        result = await registry.upsert_market_skill(
            source_id="test_source",
            item_id="test_item",
            skill_id="test_skill_id",
            skill_name="test_skill",
        )

        assert result is False

    async def test_list_market_skills_maps_tdsql_metadata_to_market_items(
        self,
    ):
        """市场技能列表应从 TDSQL 元数据映射为 MarketItem."""
        db = MagicMock()
        db.is_connected = True
        db.fetch_all = AsyncMock(
            return_value=[
                {
                    "item_id": "item-1",
                    "skill_id": "skill-1",
                    "skill_name": "risk_check",
                    "cn_name": "风险检查",
                    "description": "desc",
                    "version": "1.2.0",
                    "creator_id": "u1",
                    "creator_name": "用户1",
                    "category_id": 3,
                    "bbk_ids": '["100", "110"]',
                    "status": "active",
                    "created_at": "2026-09-01 10:00:00",
                    "updated_at": "2026-09-02 10:00:00",
                    "include_in_statistics": 1,
                },
            ],
        )

        registry = MarketSkillRegistry(db)
        items = await registry.list_market_skills(
            source_id="src-1",
            user_bbk_id="110",
            category_id=3,
        )

        assert len(items) == 1
        assert items[0].item_id == "item-1"
        assert items[0].item_type == "skill"
        assert items[0].skill_id == "skill-1"
        assert items[0].bbk_ids == ["100", "110"]
        assert items[0].include_in_statistics is True
        sql = db.fetch_all.call_args.args[0]
        assert "FROM swe_marketplace_skills" in sql
        assert "JSON_CONTAINS" in sql
        assert db.fetch_all.call_args.args[1] == (
            "src-1",
            3,
            "110",
        )

    async def test_get_market_skill_returns_none_when_database_is_unavailable(
        self,
    ):
        """数据库不可用时，Registry 不应回退读取 index.json."""
        db = MagicMock()
        db.is_connected = False

        registry = MarketSkillRegistry(db)

        with pytest.raises(MarketplaceDatabaseUnavailableError):
            await registry.get_market_skill(
                source_id="src-1",
                item_id="item-1",
            )

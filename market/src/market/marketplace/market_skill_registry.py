# -*- coding: utf-8 -*-
"""市场技能数据库操作类.

隔离 swe_marketplace_skills 表相关的数据库操作。
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class MarketSkillRegistry:
    """市场技能数据库操作类."""

    def __init__(self, db):
        """初始化，接收数据库连接对象."""
        self.db = db

    def is_connected(self) -> bool:
        """检查数据库是否已连接."""
        return self.db.is_connected

    async def upsert_market_skill(
        self,
        source_id: str,
        item_id: str,
        skill_id: str,
        skill_name: str,
        cn_name: str = "",
        include_in_statistics: bool = True,
        creator_id: str = "",
        creator_name: str = "",
        updator_id: str = "",
        updator_name: str = "",
    ) -> bool:
        """插入或更新市场技能记录.

        按 source_id + item_id 判断是否存在：
        - 存在：更新现有记录
        - 不存在：插入新记录

        Args:
            source_id: 应用入口标识
            item_id: 市场条目ID
            skill_id: 技能唯一标识符
            skill_name: 技能目录名
            cn_name: 中文展示名
            include_in_statistics: 是否纳入统计
            creator_id: 创建人ID
            creator_name: 创建人名称
            updator_id: 更新人ID
            updator_name: 更新人名称

        Returns:
            是否成功插入/更新
        """
        if not self.is_connected():
            logger.warning(
                "Database not connected, skip upsert swe_marketplace_skills",
            )
            return False

        try:
            # 先查询是否存在
            existing = await self.db.fetch_one(
                """
                SELECT id FROM swe_marketplace_skills
                WHERE source_id = %s AND item_id = %s
                """,
                (source_id, item_id),
            )

            if existing:
                # 更新现有记录
                await self.db.execute(
                    """
                    UPDATE swe_marketplace_skills
                    SET skill_id = %s, skill_name = %s, cn_name = %s,
                        include_in_statistics = %s,
                        is_unpublished = 0, is_deleted = 0,
                        updator_id = %s, updator_name = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (
                        skill_id,
                        skill_name,
                        cn_name,
                        1 if include_in_statistics else 0,
                        updator_id,
                        updator_name,
                        existing.get("id"),
                    ),
                )
                logger.info(
                    "Updated swe_marketplace_skills: item_id=%s, skill_name=%s, include=%s",
                    item_id,
                    skill_name,
                    include_in_statistics,
                )
            else:
                # 插入新记录
                await self.db.execute(
                    """
                    INSERT INTO swe_marketplace_skills
                        (source_id, item_id, skill_id, skill_name, cn_name,
                         include_in_statistics, is_unpublished, is_deleted,
                         creator_id, creator_name, updator_id, updator_name)
                    VALUES (%s, %s, %s, %s, %s, %s, 0, 0, %s, %s, %s, %s)
                    """,
                    (
                        source_id,
                        item_id,
                        skill_id,
                        skill_name,
                        cn_name,
                        1 if include_in_statistics else 0,
                        creator_id,
                        creator_name,
                        updator_id,
                        updator_name,
                    ),
                )
                logger.info(
                    "Inserted swe_marketplace_skills: item_id=%s, skill_name=%s, include=%s",
                    item_id,
                    skill_name,
                    include_in_statistics,
                )
            return True
        except Exception as e:
            logger.warning("Failed to upsert swe_marketplace_skills: %s", e)
            return False

    async def update_statistics_config(
        self,
        source_id: str,
        item_id: str,
        include_in_statistics: bool,
        updator_id: str = "",
        updator_name: str = "",
    ) -> bool:
        """更新统计配置.

        Args:
            source_id: 应用入口标识
            item_id: 市场条目ID
            include_in_statistics: 是否纳入统计
            updator_id: 更新人ID
            updator_name: 更新人名称

        Returns:
            是否成功更新
        """
        if not self.is_connected():
            logger.warning(
                "Database not connected, skip update statistics config",
            )
            return False

        try:
            await self.db.execute(
                """
                UPDATE swe_marketplace_skills
                SET include_in_statistics = %s,
                    updator_id = %s,
                    updator_name = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE source_id = %s AND item_id = %s
                """,
                (
                    1 if include_in_statistics else 0,
                    updator_id,
                    updator_name,
                    source_id,
                    item_id,
                ),
            )
            logger.info(
                "Updated statistics config: item_id=%s, include=%s",
                item_id,
                include_in_statistics,
            )
            return True
        except Exception as e:
            logger.warning("Failed to update statistics config: %s", e)
            return False

    async def get_statistics_eligible_skill_names(
        self,
        source_id: str,
    ) -> set[str]:
        """获取纳入统计的技能名称集合.

        Args:
            source_id: 应用入口标识

        Returns:
            纳入统计的技能名称集合
        """
        if not self.is_connected():
            logger.warning("Database not connected, return empty set")
            return set()

        try:
            rows = await self.db.fetch_all(
                """
                SELECT skill_name FROM swe_marketplace_skills
                WHERE source_id = %s AND include_in_statistics = 1
                """,
                (source_id,),
            )
            return {row["skill_name"] for row in rows if row.get("skill_name")}
        except Exception as e:
            logger.warning("Failed to get statistics eligible skills: %s", e)
            return set()

    async def list_statistics_eligible_unique_skills_by_source_id(
        self,
        source_id: str,
    ) -> list[dict]:
        """查询纳入统计的市场技能下拉选项，按 skill_id 去重.

        Args:
            source_id: 应用入口标识

        Returns:
            技能列表，每个 skill_id 只返回一条记录，包含 skill_id、skill_name、cn_name
        """
        if not self.is_connected():
            return []

        try:
            rows = await self.db.fetch_all(
                """
                SELECT
                    skill_id,
                    MIN(skill_name) AS skill_name,
                    MIN(cn_name) AS cn_name
                FROM swe_marketplace_skills
                WHERE source_id = %s
                  AND include_in_statistics = 1
                  AND is_unpublished = 0
                  AND is_deleted = 0
                  AND skill_id IS NOT NULL
                  AND skill_id != ''
                GROUP BY skill_id
                ORDER BY skill_id
                """,
                (source_id,),
            )
            logger.info(
                "Listed statistics eligible marketplace skills: source_id=%s, count=%d",
                source_id,
                len(rows),
            )
            return rows
        except Exception as e:
            logger.warning(
                "Failed to list statistics eligible marketplace skills: %s",
                e,
            )
            return []

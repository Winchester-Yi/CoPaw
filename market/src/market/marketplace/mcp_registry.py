# -*- coding: utf-8 -*-
"""MCP 客户端注册表数据库操作.

隔离 swe_mcp_clients 表相关的数据库操作，便于统一管理和扩展。
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class MCPRegistry:
    """MCP 客户端注册表数据库操作类."""

    def __init__(self, db):
        """初始化，接收数据库连接对象."""
        self.db = db

    def is_connected(self) -> bool:
        """检查数据库是否已连接."""
        return self.db.is_connected

    async def upsert_mcp_client(
        self,
        client_key: str,
        mcp_name: str,
        tenant_id: str,
        tenant_name: str,
        bbk_id: str,
        source: str,
        source_id: str,
        transport: str | None = None,
        url: str | None = None,
        enabled: bool = True,
        cn_name: str = "",
    ) -> bool:
        """插入或更新 MCP 客户端记录.

        Args:
            client_key: MCP 客户端密钥（marketplace 唯一标识）
            mcp_name: MCP 显示名称
            tenant_id: 租户ID
            tenant_name: 租户名称（用于展示）
            bbk_id: 机构ID（用于展示和过滤）
            source: 来源标记，格式：marketplace:{source_id}
            source_id: marketplace source_id
            transport: 传输类型
            url: MCP 服务 URL
            enabled: 是否启用

        Returns:
            是否成功
        """
        if not self.is_connected():
            logger.warning(
                "Database not connected, skip upsert swe_mcp_clients",
            )
            return False

        try:
            # 查询是否存在
            existing = await self.db.fetch_one(
                """
                SELECT id FROM swe_mcp_clients
                WHERE tenant_id = %s AND source_id = %s AND client_key = %s
                """,
                (tenant_id, source_id, client_key),
            )

            if existing:
                await self.db.execute(
                    """
                    UPDATE swe_mcp_clients
                    SET mcp_name = %s, tenant_name = %s, bbk_id = %s,
                        cn_name = %s, source = %s, transport = %s, url = %s,
                        enabled = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND source_id = %s AND client_key = %s
                    """,
                    (
                        mcp_name,
                        tenant_name,
                        bbk_id,
                        cn_name,
                        source,
                        transport,
                        url,
                        enabled,
                        tenant_id,
                        source_id,
                        client_key,
                    ),
                )
                logger.info(
                    "Updated swe_mcp_clients: client_key=%s, mcp_name=%s, tenant=%s, source_id=%s",
                    client_key,
                    mcp_name,
                    tenant_id,
                    source_id,
                )
            else:
                await self.db.execute(
                    """
                    INSERT INTO swe_mcp_clients
                        (client_key, mcp_name, tenant_id, tenant_name, bbk_id,
                         cn_name, source, source_id, transport, url, enabled)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        client_key,
                        mcp_name,
                        tenant_id,
                        tenant_name,
                        bbk_id,
                        cn_name,
                        source,
                        source_id,
                        transport,
                        url,
                        enabled,
                    ),
                )
                logger.info(
                    "Inserted swe_mcp_clients: client_key=%s, mcp_name=%s, tenant=%s, source_id=%s",
                    client_key,
                    mcp_name,
                    tenant_id,
                    source_id,
                )
            return True
        except Exception as e:
            logger.warning("Failed to upsert swe_mcp_clients: %s", e)
            return False

    async def delete_mcp_client(
        self,
        tenant_id: str,
        source_id: str,
        client_key: str,
    ) -> bool:
        """删除 MCP 客户端记录.

        Args:
            tenant_id: 租户ID
            source_id: 来源ID
            client_key: MCP 客户端密钥

        Returns:
            是否成功删除
        """
        if not self.is_connected():
            return False

        try:
            await self.db.execute(
                """
                DELETE FROM swe_mcp_clients
                WHERE tenant_id = %s AND source_id = %s AND client_key = %s
                """,
                (tenant_id, source_id, client_key),
            )
            logger.info(
                "Deleted swe_mcp_clients: client_key=%s, tenant=%s, source_id=%s",
                client_key,
                tenant_id,
                source_id,
            )
            return True
        except Exception as e:
            logger.warning("Failed to delete swe_mcp_clients: %s", e)
            return False

    async def delete_mcp_by_name(
        self,
        tenant_id: str,
        source_id: str,
        mcp_name: str,
    ) -> bool:
        """删除租户下指定名称的所有 MCP 客户端记录.

        Args:
            tenant_id: 租户ID
            source_id: 来源ID
            mcp_name: MCP 显示名称

        Returns:
            是否成功删除
        """
        if not self.is_connected():
            return False

        try:
            await self.db.execute(
                """
                DELETE FROM swe_mcp_clients
                WHERE tenant_id = %s AND source_id = %s AND mcp_name = %s
                """,
                (tenant_id, source_id, mcp_name),
            )
            logger.info(
                "Deleted swe_mcp_clients by name: mcp_name=%s, tenant=%s, source_id=%s",
                mcp_name,
                tenant_id,
                source_id,
            )
            return True
        except Exception as e:
            logger.warning("Failed to delete swe_mcp_clients by name: %s", e)
            return False

    async def list_mcp_clients(
        self,
        tenant_id: str,
        source_id: str,
    ) -> list[dict[str, Any]]:
        """查询租户下所有 MCP 客户端记录.

        Args:
            tenant_id: 租户ID
            source_id: 来源ID

        Returns:
            MCP 客户端记录列表
        """
        if not self.is_connected():
            return []

        try:
            rows = await self.db.fetch_all(
                """
                SELECT client_key, mcp_name, tenant_name, bbk_id,
                       source, transport, url, enabled, created_at
                FROM swe_mcp_clients
                WHERE tenant_id = %s AND source_id = %s AND enabled = TRUE
                """,
                (tenant_id, source_id),
            )
            return [dict(row) for row in rows]
        except Exception as e:
            logger.warning("Failed to list swe_mcp_clients: %s", e)
            return []

    async def list_mcp_clients_by_source(
        self,
        source_id: str,
        mcp_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """查询指定市场来源下所有租户的 MCP 客户端记录.

        Args:
            source_id: 来源ID
            mcp_name: 可选，按 MCP 名称过滤

        Returns:
            MCP 客户端记录列表
        """
        if not self.is_connected():
            return []

        try:
            if mcp_name:
                rows = await self.db.fetch_all(
                    """
                    SELECT client_key, mcp_name, tenant_id, tenant_name, bbk_id,
                           source, transport, url, enabled, created_at
                    FROM swe_mcp_clients
                    WHERE source_id = %s AND mcp_name = %s AND enabled = TRUE
                    """,
                    (source_id, mcp_name),
                )
            else:
                rows = await self.db.fetch_all(
                    """
                    SELECT client_key, mcp_name, tenant_id, tenant_name, bbk_id,
                           source, transport, url, enabled, created_at
                    FROM swe_mcp_clients
                    WHERE source_id = %s AND enabled = TRUE
                    """,
                    (source_id,),
                )
            return [dict(row) for row in rows]
        except Exception as e:
            logger.warning("Failed to list swe_mcp_clients by source: %s", e)
            return []

    async def update_cn_name_by_marketplace_item(
        self,
        source_id: str,
        marketplace_item_id: str,
        cn_name: str,
    ) -> int:
        """批量更新 MCP 的中文名称（用于市场端修改中文名后的同步）。

        Args:
            source_id: marketplace source_id
            marketplace_item_id: marketplace item_id（不含 marketplace: 前缀）
            cn_name: 新的中文名称

        Returns:
            实际更新的记录数
        """
        if not self.is_connected():
            return 0
        try:
            source_pattern = f"marketplace:{marketplace_item_id}"
            affected = await self.db.execute(
                """
                UPDATE swe_mcp_clients
                SET cn_name = %s, updated_at = CURRENT_TIMESTAMP
                WHERE source = %s AND source_id = %s
                """,
                (cn_name, source_pattern, source_id),
            )
            logger.info(
                "Updated cn_name for %d records: source=%s, cn_name=%s",
                affected,
                source_pattern,
                cn_name,
            )
            return affected
        except Exception as e:
            logger.warning("Failed to update cn_name: %s", e)
            return 0

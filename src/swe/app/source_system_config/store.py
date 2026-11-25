# -*- coding: utf-8 -*-
"""Source 级系统配置数据库存储。"""

import json
import logging
from typing import Any

from .models import (
    SourceSystemConfig,
    SourceSystemConfigRecord,
    SourceSystemConfigUpsert,
)

logger = logging.getLogger(__name__)

_CONFIG_COLUMN = "config_text"
_STORAGE_UNAVAILABLE_PREFIX = "source system config storage unavailable"


class SourceSystemConfigStoreUnavailable(RuntimeError):
    """Source 系统配置存储不可用。"""


class SourceSystemConfigStore:
    """按 source_id 读写系统配置。"""

    def __init__(self, db: Any | None = None):
        """初始化存储。"""
        self.db = db

    @property
    def is_available(self) -> bool:
        """返回当前存储是否可用。"""
        return self.db is not None and bool(
            getattr(self.db, "is_connected", False),
        )

    def _require_db(self) -> Any:
        """校验 DB 可用性并返回连接对象。"""
        if not self.is_available:
            raise SourceSystemConfigStoreUnavailable(
                f"{_STORAGE_UNAVAILABLE_PREFIX}: db is not connected",
            )
        return self.db

    async def _call_db(
        self,
        operation: str,
        db_call: Any,
        *args: Any,
    ) -> Any:
        """执行数据库调用并统一包装底层存储异常。"""
        try:
            return await db_call(*args)
        except SourceSystemConfigStoreUnavailable:
            raise
        except Exception as exc:
            raise SourceSystemConfigStoreUnavailable(
                f"{_STORAGE_UNAVAILABLE_PREFIX}: {operation} failed: {exc}",
            ) from exc

    async def get_config(
        self,
        source_id: str,
    ) -> SourceSystemConfigRecord | None:
        """按 source_id 查询配置记录。"""
        db = self._require_db()

        query = """
            SELECT source_id, config_text, version, updated_by, updated_at
            FROM swe_source_system_config
            WHERE source_id = %s
        """
        row = await self._call_db(
            "fetch config",
            db.fetch_one,
            query,
            (source_id,),
        )
        if row is None:
            return None
        return self._row_to_record(row)

    async def list_configs(self) -> list[SourceSystemConfigRecord]:
        """列出全部 source 系统配置。"""
        db = self._require_db()

        query = """
            SELECT source_id, config_text, version, updated_by, updated_at
            FROM swe_source_system_config
            ORDER BY updated_at DESC, source_id ASC
        """
        rows = await self._call_db("list configs", db.fetch_all, query)
        return [self._row_to_record(row) for row in rows]

    async def get_config_version(self, source_id: str) -> int | None:
        """查询指定 source 配置版本，不存在时返回 None。"""
        db = self._require_db()
        row = await self._call_db(
            "fetch config version",
            db.fetch_one,
            "SELECT version FROM swe_source_system_config WHERE source_id = %s",
            (source_id,),
        )
        if row is None:
            return None
        return int(row["version"])

    async def list_cron_batch_retry_overrides(self) -> dict[str, int]:
        """只读取已保存的批调度尝试上限，不返回其他来源配置。"""
        db = self._require_db()
        path = "$.cron_batch_max_attempts"
        rows = await self._call_db(
            "list cron batch retry overrides",
            db.fetch_all,
            "SELECT source_id, JSON_UNQUOTE(JSON_EXTRACT(config_text, %s)) "
            "AS max_attempts FROM swe_source_system_config "
            "WHERE JSON_TYPE(JSON_EXTRACT(config_text, %s)) = 'INTEGER'",
            (path, path),
        )
        return {row["source_id"]: int(row["max_attempts"]) for row in rows}

    async def upsert_config(
        self,
        source_id: str,
        payload: SourceSystemConfigUpsert,
    ) -> SourceSystemConfigRecord:
        """创建或更新 source 系统配置并递增版本。"""
        db = self._require_db()
        config_text = payload.config.model_dump_json()

        # 依赖数据库在单条 UPSERT 语句内递增 version，
        # 避免并发请求基于同一旧版本计算出相同的新版本。
        query = """
            INSERT INTO swe_source_system_config
                (source_id, config_text, version, updated_by)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                config_text = VALUES(config_text),
                version = version + 1,
                updated_by = VALUES(updated_by),
                updated_at = CURRENT_TIMESTAMP
        """
        await self._call_db(
            "upsert config",
            db.execute,
            query,
            (
                source_id,
                config_text,
                1,
                payload.updated_by,
            ),
        )
        record = await self.get_config(source_id)
        if record is None:
            raise ValueError(
                f"source system config upsert did not return row: {source_id}",
            )
        return record

    async def delete_config(self, source_id: str) -> bool:
        """删除指定 source 的系统配置。"""
        db = self._require_db()

        result = await self._call_db(
            "delete config",
            db.execute,
            "DELETE FROM swe_source_system_config WHERE source_id = %s",
            (source_id,),
        )
        return bool(result)

    def _row_to_record(self, row: dict[str, Any]) -> SourceSystemConfigRecord:
        """将数据库行解析为配置记录。"""
        try:
            # 兼容旧字段名，便于灰度期间混合读写。
            raw_config = row.get(_CONFIG_COLUMN, row.get("config_json"))
            config_data = (
                json.loads(raw_config)
                if isinstance(raw_config, str)
                else raw_config
            )
            config = SourceSystemConfig.model_validate(config_data)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid source system config for {row.get('source_id')}: {exc}",
            ) from exc

        return SourceSystemConfigRecord(
            source_id=row["source_id"],
            config=config,
            version=int(row["version"]),
            updated_by=row.get("updated_by"),
            updated_at=row.get("updated_at"),
        )

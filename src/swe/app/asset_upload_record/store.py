# -*- coding: utf-8 -*-
"""资产上传记录数据库存储。"""

from typing import Any, Optional

from .models import AssetUploadRecord


class AssetUploadRecordStore:
    """负责资产上传记录的落库操作。"""

    def __init__(self, db: Optional[Any] = None):
        self.db = db
        self._use_db = db is not None and db.is_connected

    @staticmethod
    def _to_record(row: dict[str, Any]) -> AssetUploadRecord:
        """把数据库行转换为上传记录模型。"""
        return AssetUploadRecord(
            id=int(row["id"]),
            file_name=row["file_name"],
            file_size=int(row["file_size"]),
            asset_path=row["asset_path"],
            source_id=row.get("source_id"),
            template_flag=row.get("template_flag"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    async def insert_record(
        self,
        *,
        file_name: str,
        file_size: int,
        asset_path: str,
        source_id: Optional[str] = None,
        template_flag: Optional[str] = None,
    ) -> Optional[int]:
        """插入或更新一条上传记录，返回自增 ID。"""
        if not self._use_db:
            return None

        query = """
            INSERT INTO swe_asset_upload_record
                (file_name, file_size, asset_path, source_id, template_flag)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                file_size = VALUES(file_size),
                asset_path = VALUES(asset_path),
                source_id = VALUES(source_id),
                template_flag = VALUES(template_flag)
        """
        inserted_id = await self.db.insert(
            query,
            (
                file_name,
                file_size,
                asset_path,
                source_id,
                template_flag,
            ),
        )
        return inserted_id or None

    async def list_records(
        self,
        *,
        source_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> list[AssetUploadRecord]:
        """分页查询上传记录。"""
        if not self._use_db:
            return []

        where_clauses: list[str] = []
        params: list[Any] = []

        if source_id is not None:
            where_clauses.append("source_id <=> %s")
            params.append(source_id)

        where_sql = (
            f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        )

        offset = (page - 1) * page_size
        query = (
            f"SELECT * FROM swe_asset_upload_record {where_sql} "
            "ORDER BY created_at DESC LIMIT %s OFFSET %s"
        )
        params.extend([page_size, offset])

        rows = await self.db.fetch_all(query, tuple(params))
        return [self._to_record(row) for row in rows]

    async def count_records(
        self,
        *,
        source_id: Optional[str] = None,
    ) -> int:
        """统计上传记录总数。"""
        if not self._use_db:
            return 0

        where_clauses: list[str] = []
        params: list[Any] = []

        if source_id is not None:
            where_clauses.append("source_id <=> %s")
            params.append(source_id)

        where_sql = (
            f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        )

        query = (
            f"SELECT COUNT(*) AS cnt FROM swe_asset_upload_record {where_sql}"
        )
        row = await self.db.fetch_one(query, tuple(params))
        return int(row["cnt"]) if row else 0

    async def list_all_file_names(self) -> list[dict[str, Any]]:
        """查询所有上传文件名及ID。"""
        if not self._use_db:
            return []

        query = "SELECT id, file_name, template_flag FROM swe_asset_upload_record ORDER BY created_at DESC"
        rows = await self.db.fetch_all(query, ())
        return [
            {
                "id": row["id"],
                "file_name": row["file_name"],
                "template_flag": row.get("template_flag"),
            }
            for row in rows
        ]

    async def get_template_id_by_name(self, file_name: str) -> Optional[int]:
        """根据文件名查询模板ID。"""
        if not self._use_db:
            return None

        query = "SELECT id FROM swe_asset_upload_record WHERE file_name = %s LIMIT 1"
        row = await self.db.fetch_one(query, (file_name,))
        return int(row["id"]) if row else None

    async def delete_record(self, record_id: int) -> bool:
        """根据ID删除上传记录。"""
        if not self._use_db:
            return False
        query = "DELETE FROM swe_asset_upload_record WHERE id = %s"
        result = await self.db.execute(query, (record_id,))
        return result > 0

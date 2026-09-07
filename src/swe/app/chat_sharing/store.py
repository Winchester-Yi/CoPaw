# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import ChatShareRecord

_TABLE = "swe_chat_shares"


class ChatShareStore:
    """Persist share indexes and access audit metadata in MySQL."""

    def __init__(self, db: Any):
        self.db = db

    async def ensure_schema(self) -> None:
        await self.db.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_TABLE} (
                token VARCHAR(128) PRIMARY KEY,
                chat_id VARCHAR(128) NOT NULL,
                creator_id VARCHAR(256) NOT NULL,
                tenant_id VARCHAR(256) NOT NULL,
                snapshot_key VARCHAR(512) NOT NULL,
                created_at DATETIME(6) NOT NULL,
                access_count BIGINT NOT NULL DEFAULT 0,
                last_access_at DATETIME(6) NULL,
                INDEX idx_{_TABLE}_chat_id (chat_id),
                INDEX idx_{_TABLE}_creator_id (creator_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """,
        )

    @staticmethod
    def _record_from_row(row: dict[str, Any]) -> ChatShareRecord:
        return ChatShareRecord(
            token=str(row["token"]),
            chat_id=str(row["chat_id"]),
            creator_id=str(row["creator_id"]),
            snapshot_key=str(row["snapshot_key"]),
            tenant_id=str(row.get("tenant_id") or "default"),
            created_at=row["created_at"],
            access_count=int(row.get("access_count") or 0),
            last_access_at=row.get("last_access_at"),
        )

    async def create(self, record: ChatShareRecord) -> None:
        await self.db.execute(
            f"""
            INSERT INTO {_TABLE}
              (token, chat_id, creator_id, tenant_id, snapshot_key, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                record.token,
                record.chat_id,
                record.creator_id,
                record.tenant_id,
                record.snapshot_key,
                record.created_at,
            ),
        )

    async def get(self, token: str) -> ChatShareRecord | None:
        row = await self.db.fetch_one(
            f"SELECT * FROM {_TABLE} WHERE token = %s",
            (token,),
        )
        return self._record_from_row(row) if row else None

    async def record_access(self, token: str) -> None:
        now = datetime.now(timezone.utc)
        await self.db.execute(
            f"""
            UPDATE {_TABLE}
            SET access_count = access_count + 1, last_access_at = %s
            WHERE token = %s
            """,
            (now, token),
        )

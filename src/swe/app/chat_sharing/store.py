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

    async def verify_locator_schema(self) -> None:
        """New authority mode validates installed schema and never performs DDL."""
        columns = await self.db.fetch_all(
            "SELECT COLUMN_NAME,COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
            (_TABLE,),
        )
        actual = {
            row["COLUMN_NAME"]: row["COLUMN_TYPE"].lower() for row in columns
        }
        required = {
            "publication_id": "bigint unsigned",
            "byte_offset": "bigint unsigned",
            "byte_length": "bigint unsigned",
        }
        if any(actual.get(name) != kind for name, kind in required.items()):
            raise RuntimeError("Chat share locator schema is not installed")
        if await self.db.fetch_one(
            f"SELECT token FROM {_TABLE} WHERE publication_id IS NULL OR byte_offset IS NULL OR byte_length IS NULL LIMIT 1",
        ):
            raise RuntimeError(
                "Chat share locators require complete migration",
            )

    @staticmethod
    def _record_from_row(row: dict[str, Any]) -> ChatShareRecord:
        return ChatShareRecord(
            token=str(row["token"]),
            chat_id=str(row["chat_id"]),
            creator_id=str(row["creator_id"]),
            snapshot_key=str(row["snapshot_key"]),
            tenant_id=(
                str(row["tenant_id"]) if "tenant_id" in row else "default"
            ),
            created_at=row["created_at"],
            access_count=int(row.get("access_count") or 0),
            last_access_at=row.get("last_access_at"),
            publication_id=row.get("publication_id"),
            byte_offset=row.get("byte_offset"),
            byte_length=row.get("byte_length"),
        )

    async def create(self, record: ChatShareRecord) -> None:
        if record.publication_id is not None:
            await self.db.execute(
                f"INSERT INTO {_TABLE} (token,chat_id,creator_id,tenant_id,snapshot_key,created_at,access_count,last_access_at,publication_id,byte_offset,byte_length) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    record.token,
                    record.chat_id,
                    record.creator_id,
                    record.tenant_id,
                    record.snapshot_key,
                    record.created_at,
                    record.access_count,
                    record.last_access_at,
                    record.publication_id,
                    record.byte_offset,
                    record.byte_length,
                ),
            )
            return
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

    async def get(self, token: str, *, lock=False) -> ChatShareRecord | None:
        row = await self.db.fetch_one(
            f"SELECT * FROM {_TABLE} WHERE token = %s"
            + (" FOR UPDATE" if lock else ""),
            (token,),
        )
        return self._record_from_row(row) if row else None

    async def record_access(self, token: str) -> None:
        now = datetime.now(timezone.utc)
        changed = await self.db.execute(
            f"""
            UPDATE {_TABLE}
            SET access_count = access_count + 1, last_access_at = %s
            WHERE token = %s
            """,
            (now, token),
        )
        if changed != 1:
            raise RuntimeError(
                "Chat share access count update was not confirmed",
            )

# -*- coding: utf-8 -*-
"""Fail closed when the original migration SQL lock connection is lost."""

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class MigrationLockGuard:
    database: object
    lock_name: str
    server_uuid: str
    schema: str
    connection_id: int
    owner: object

    @classmethod
    async def capture(cls, database, lock_name, *, schema, server_uuid):
        row = await database.fetch_one(
            "SELECT CONNECTION_ID() AS connection_id",
        )
        guard = cls(
            database,
            lock_name,
            server_uuid,
            schema,
            row["connection_id"],
            asyncio.current_task(),
        )
        await guard.check()
        return guard

    async def check(self):
        if asyncio.current_task() is not self.owner:
            raise RuntimeError(
                "migration lock check requires original SQL task",
            )
        row = await self.database.fetch_one(
            "SELECT CONNECTION_ID() AS connection_id, "
            "IS_USED_LOCK(%s) AS lock_owner, "
            "@@server_uuid AS server_uuid,DATABASE() AS schema_name",
            (self.lock_name,),
        )
        if not row or (
            row["connection_id"] != self.connection_id
            or row["lock_owner"] != self.connection_id
            or row["server_uuid"] != self.server_uuid
            or row["schema_name"] != self.schema
        ):
            raise RuntimeError(
                "migration database identity or named lock lost",
            )

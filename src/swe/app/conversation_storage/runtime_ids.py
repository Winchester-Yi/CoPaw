# -*- coding: utf-8 -*-
"""One identity sequence for runtime writes alongside tenant migration tickets."""

from contextlib import asynccontextmanager
from uuid import UUID, uuid4


class SharedIdDatabase:
    """Preserve pinned sessions and transactions while exposing an opt-in allocator."""

    def __init__(self, database, migration_id):
        if str(UUID(migration_id)) != migration_id:
            raise ValueError("invalid runtime migration identity")
        self.database = database
        self.migration_id = migration_id

    def __getattr__(self, name):
        return getattr(self.database, name)

    @asynccontextmanager
    async def transaction(self):
        async with self.database.transaction() as session:
            yield SharedIdDatabase(session or self.database, self.migration_id)

    @asynccontextmanager
    async def acquire(self):
        async with self.database.acquire() as session:
            yield SharedIdDatabase(session or self.database, self.migration_id)

    async def allocate_runtime_identity(self):
        identity = await self.database.insert(
            "INSERT INTO swe_conversation_migration_entries "
            "(migration_id,entry_kind,source_key,state,created_at,updated_at) "
            "VALUES (%s,'runtime_id',%s,'committed',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))",
            (self.migration_id.encode(), uuid4().hex.encode()),
        )
        if type(identity) is not int or identity <= 0:
            raise ValueError("runtime identity ticket was not allocated")
        return identity


async def runtime_identity(database):
    """Allocate only for explicitly wrapped partial-publication runtime databases."""
    from .tenant_admission import TenantDatabase

    if isinstance(database, TenantDatabase):
        await database.admission.require(database.tenant_id)
        return await runtime_identity(database.database)
    if isinstance(database, SharedIdDatabase):
        return await database.allocate_runtime_identity()
    return None

# -*- coding: utf-8 -*-
"""Verify the parallel scheduler on a real task-owned MySQL lock session."""

import asyncio
import threading

from swe.app.conversation_storage.migration.parallel import (
    MigrationCoordinator,
    MigrationIO,
)


async def test_parallel_io_keeps_sql_in_original_named_lock_task(
    conversation_test_database,
):
    db = conversation_test_database
    await db.execute(
        "CREATE TABLE migration_parallel_probe (id INT PRIMARY KEY)",
    )
    owner = asyncio.current_task()
    lock_name = "parallel-migration-integration"
    barrier = threading.Barrier(2)
    results = []

    def source_io(value):
        barrier.wait(timeout=10)
        return value

    async with db.named_lock(lock_name, exact_name=True):
        original = await db.fetch_one(
            "SELECT CONNECTION_ID() AS connection_id",
        )

        async def record(value):
            assert asyncio.current_task() is owner
            async with db.transaction():
                current = await db.fetch_one(
                    "SELECT CONNECTION_ID() AS connection_id, "
                    "IS_USED_LOCK(%s) AS lock_owner",
                    (lock_name,),
                )
                assert current["connection_id"] == original["connection_id"]
                assert current["lock_owner"] == original["connection_id"]
                await db.execute(
                    "INSERT INTO migration_parallel_probe (id) VALUES (%s)",
                    (value,),
                )
            return value

        async with MigrationIO(2) as io:

            async def workspace(value, request):
                assert asyncio.current_task() is not owner
                parsed = await io.call(source_io, value)
                results.append(await request("record", parsed))

            await MigrationCoordinator({"record": record}, workers=2).run(
                range(4),
                workspace,
            )
        assert sorted(results) == list(range(4))
        rows = await db.fetch_all(
            "SELECT id FROM migration_parallel_probe ORDER BY id",
        )
        assert [row["id"] for row in rows] == list(range(4))
    released = await db.fetch_one(
        "SELECT IS_USED_LOCK(%s) AS lock_owner",
        (lock_name,),
    )
    assert released["lock_owner"] is None

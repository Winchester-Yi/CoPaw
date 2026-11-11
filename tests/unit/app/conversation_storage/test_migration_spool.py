# -*- coding: utf-8 -*-
import asyncio
import stat

import pytest

from swe.app.conversation_storage.migration.spool import (
    ByteBudget,
    InventorySpool,
    MigrationCapacityError,
)


async def test_byte_budget_blocks_and_releases_after_cancel():
    budget = ByteBudget(10)
    entered = asyncio.Event()

    async def work():
        async with budget.reserve(8):
            entered.set()
            await asyncio.Event().wait()

    first = asyncio.create_task(work())
    await entered.wait()
    entered.clear()
    second = asyncio.create_task(work())
    await asyncio.sleep(0)
    assert budget.used == 8
    assert not entered.is_set()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await entered.wait()
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert budget.used == 0
    assert budget.peak == 8
    with pytest.raises(MigrationCapacityError):
        async with budget.reserve(11):
            pytest.fail("oversized record was admitted")


def test_spool_roundtrip_private_files_and_cleanup(tmp_path):
    value = {
        "scope": ["服务", "tenant", "source", "agent"],
        "sessions": [{"state": {"history": [None, True, 123, "正文"]}}],
    }
    with InventorySpool(tmp_path, max_bytes=4096, record_limit=2048) as spool:
        record = spool.write(0, value)
        assert spool.read(record) == value
        assert stat.S_IMODE(spool.root.stat().st_mode) == 0o700
        assert stat.S_IMODE((spool.root / "0.json").stat().st_mode) == 0o600
        assert spool.used_bytes == record.byte_length
        directory = spool.root
    assert not directory.exists()


def test_capacity_failure_does_not_leave_partial_record(tmp_path):
    with InventorySpool(tmp_path, max_bytes=32, record_limit=16) as spool:
        with pytest.raises(MigrationCapacityError):
            spool.write(0, {"body": "x" * 100})
        assert list(spool.root.iterdir()) == []
        assert spool.used_bytes == 0
        record = spool.write(0, [1])
        assert spool.read(record) == [1]
        with pytest.raises(ValueError, match="unique"):
            spool.write(0, [2])


def test_corrupt_record_fails_before_import(tmp_path):
    with InventorySpool(tmp_path, max_bytes=128, record_limit=128) as spool:
        record = spool.write(0, {"value": 1})
        (spool.root / "0.json").write_text('{"value":2}')
        with pytest.raises(ValueError, match="changed"):
            spool.read(record)


def test_protected_root_and_symlink_rejected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    nested = source / "work"
    nested.mkdir()
    with pytest.raises(ValueError, match="overlap"):
        InventorySpool(
            nested,
            max_bytes=10,
            record_limit=10,
            protected=[source],
        )
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match="canonical"):
        InventorySpool(alias, max_bytes=10, record_limit=10)


def test_huge_string_is_rejected_without_large_encoding_copy(tmp_path):
    import tracemalloc

    value = "x" * (16 * 1024 * 1024)
    with InventorySpool(tmp_path, max_bytes=2048, record_limit=1024) as spool:
        tracemalloc.start()
        try:
            with pytest.raises(MigrationCapacityError):
                spool.write(0, {"body": value})
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 1024 * 1024


async def test_concurrent_writes_reserve_capacity_and_unique_indexes(tmp_path):
    from swe.app.conversation_storage.migration.parallel import MigrationIO

    with InventorySpool(tmp_path, max_bytes=64, record_limit=32) as spool:
        async with MigrationIO(4) as io:
            results = await asyncio.gather(
                *(io.call(spool.write, index, "x" * 20) for index in range(8)),
                return_exceptions=True,
            )
        assert sum(not isinstance(item, Exception) for item in results) == 2
        assert spool.used_bytes == 44
        assert len(list(spool.root.iterdir())) == 2

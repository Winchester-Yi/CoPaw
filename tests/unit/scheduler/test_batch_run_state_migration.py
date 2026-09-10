from unittest.mock import AsyncMock

import pytest

from tests.unit.scheduler.test_batch_run_state import ControlDb


def test_apply_and_dry_run_are_mutually_exclusive():
    from scheduler.migrate_batch_run_state import parse_args

    with pytest.raises(SystemExit) as exc:
        parse_args(["--apply", "--dry-run"])
    assert exc.value.code == 2


@pytest.mark.asyncio
async def test_cli_does_not_hide_database_failure(monkeypatch):
    from scheduler import migrate_batch_run_state as migration

    monkeypatch.setattr(
        migration,
        "init_db_connection",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    close = AsyncMock()
    monkeypatch.setattr(migration, "close_db_connection", close)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await migration.main(["--dry-run"])
    close.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_migration_is_read_only():
    from scheduler.migrate_batch_run_state import run_migration, parse_args

    db = ControlDb()
    try:
        db.add_job()
        assert parse_args([]).apply is False
        report = await run_migration(db)
        assert report["eligible"] == 1
        assert (
            await db.fetch_all("SELECT * FROM swe_cron_dispatch_controls")
            == []
        )
    finally:
        db.connection.close()


@pytest.mark.asyncio
async def test_schema_apply_only_tolerates_duplicate_columns():
    from scheduler.app.database.batch_run_state_schema import apply_schema

    db = AsyncMock()
    db.execute.side_effect = [0, Exception(1060, "duplicate column"), 0]
    await apply_schema(db)
    assert db.execute.await_count == 3
    db.execute.side_effect = Exception(1142, "ALTER command denied")
    with pytest.raises(Exception, match="denied"):
        await apply_schema(db)


@pytest.mark.asyncio
async def test_apply_backfills_legacy_cancelled_counts_without_resetting_completion(
    monkeypatch,
):
    from scheduler import migrate_batch_run_state as migration

    db = ControlDb()
    try:
        for column in (
            "total_count INTEGER",
            "completed_count INTEGER",
            "failed_count INTEGER",
            "completed_at TEXT",
            "updated_at TEXT",
        ):
            await db.execute(
                f"ALTER TABLE swe_cron_dispatch_batches ADD COLUMN {column}"
            )
        db.insert(
            "swe_cron_dispatch_batches",
            batch_id="batch-1",
            source_id="source-a",
            status="completed",
            completed_at="2026-09-01 10:00:00",
        )
        db.add_intent(status="cancelled")
        monkeypatch.setattr(migration, "apply_schema", AsyncMock())
        report = await migration.run_migration(db, apply=True)
        batch = await db.fetch_one("SELECT * FROM swe_cron_dispatch_batches")
        assert batch["skipped_count"] == 1
        assert batch["completed_count"] == 0
        assert batch["completed_at"] == "2026-09-01 10:00:00"
        assert report["refreshed_batches"] == 1
    finally:
        db.connection.close()

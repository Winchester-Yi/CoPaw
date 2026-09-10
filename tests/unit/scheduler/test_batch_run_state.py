"""Persisted compatibility and control state; SQLite does not prove row locks."""

import json

import pytest

from tests.unit.scheduler.test_cron_execution_reconciliation import (
    _SqliteDb,
    _Cursor,
)


class ControlCursor(_Cursor):
    async def execute(self, sql, params=()):
        await super().execute(sql, params)
        return self.rowcount

    async def executemany(self, sql, params):
        for values in params:
            await self.execute(sql, values)


class ControlDb(_SqliteDb):
    def cursor(self):
        return ControlCursor(self)

    def __init__(self):
        super().__init__()
        self.connection.executescript("""
            CREATE TABLE swe_cron_jobs (
                id TEXT PRIMARY KEY, tenant_id TEXT, source_id TEXT,
                enabled INTEGER, status TEXT, deleted_at TEXT, meta TEXT
            );
            CREATE TABLE swe_cron_dispatch_controls (
                source_id TEXT, tenant_id TEXT, parent_job_id TEXT,
                paused INTEGER, version INTEGER DEFAULT 1, resumed_at TEXT,
                updated_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (source_id,tenant_id,parent_job_id)
            );
            CREATE TABLE swe_cron_dispatch_batches (
                batch_id TEXT PRIMARY KEY, source_id TEXT, tenant_id TEXT,
                parent_job_id TEXT, skipped_count INTEGER DEFAULT 0,
                status TEXT,lock_owner TEXT DEFAULT '',locked_at TEXT
            );
            ALTER TABLE swe_cron_dispatch_intents ADD COLUMN acked_at TEXT;
            ALTER TABLE swe_cron_dispatch_events ADD COLUMN batch_id TEXT;
            ALTER TABLE swe_cron_dispatch_events ADD COLUMN source_id TEXT;
            ALTER TABLE swe_cron_dispatch_events ADD COLUMN tenant_id TEXT;
            ALTER TABLE swe_cron_dispatch_events ADD COLUMN job_id TEXT;
            ALTER TABLE swe_cron_dispatch_events ADD COLUMN details TEXT;
        """)

    def run(self, sql, params=()):
        sql = sql.replace(
            "ON DUPLICATE KEY UPDATE parent_job_id = parent_job_id",
            "ON CONFLICT DO NOTHING",
        )
        return super().run(sql, params)

    def add_job(self, job_id="parent", enabled=True, meta=None, **extra):
        row = dict(
            id=job_id,
            tenant_id="tenant-1",
            source_id="source-a",
            enabled=int(enabled),
            status="active" if enabled else "paused",
            deleted_at=None,
            meta=json.dumps(
                meta
                if meta is not None
                else {
                    "broadcast_dispatch_intents_enabled": True,
                }
            ),
        )
        row.update(extra)
        self.insert("swe_cron_jobs", **row)
        return row


@pytest.fixture
def control_db(monkeypatch):
    from scheduler.app.services.cron import batch_run_state as state

    db = ControlDb()
    monkeypatch.setattr(state, "get_db_connection", lambda: db)
    yield db
    db.connection.close()


@pytest.mark.parametrize(
    "meta,want",
    [
        ({}, False),
        ({"broadcast_source_job_id": "parent"}, False),
        ({"broadcast_dispatch_intents_enabled": False}, False),
        ({"broadcast_dispatch_intents_enabled": "false"}, False),
        ({"broadcast_dispatch_intents_enabled": True}, True),
        ({"broadcast_dispatch_intents_enabled": 1}, True),
        ({"broadcast_dispatch_intents_enabled": "true"}, True),
        (
            {
                "broadcast_dispatch_intents_enabled": True,
                "broadcast_source_job_id": "parent",
            },
            False,
        ),
        ("not-json", False),
    ],
)
def test_classification_uses_saved_mode_not_enabled_or_runtime(
    monkeypatch,
    meta,
    want,
):
    from scheduler.app.services.cron import batch_run_state as state

    monkeypatch.setenv("SWE_CRON_DISPATCH_INTENTS_ENABLED", "false")
    for enabled in (0, 1):
        assert (
            state.is_batch_parent_definition(
                dict(
                    enabled=enabled,
                    status="paused",
                    deleted_at=None,
                    meta=meta,
                )
            )
            is want
        )


@pytest.mark.asyncio
async def test_migration_reports_ambiguous_metadata_instead_of_initializing(
    control_db,
):
    from scheduler.app.services.cron import batch_run_state as state

    control_db.add_job("broken", meta="not an object")
    control_db.add_job(
        "unknown", meta={"broadcast_dispatch_intents_enabled": "yes"}
    )
    control_db.add_job("ordinary", meta={})
    report = await state.initialize_missing_controls(control_db, dry_run=True)
    assert report["invalid"] == 2
    assert report["eligible"] == 0
    assert report["ignored"] == 1


@pytest.mark.asyncio
async def test_initialization_skips_normal_and_children_and_never_overwrites(
    control_db,
):
    from scheduler.app.services.cron import batch_run_state as state

    db = control_db
    db.add_job("normal", meta={})
    db.add_job("broadcast", meta={"broadcast_source_job_id": "p"})
    db.add_job(
        "child",
        meta={
            "broadcast_dispatch_intents_enabled": True,
            "broadcast_source_job_id": "parent",
        },
    )
    db.add_job("on")
    db.add_job("off", enabled=False)
    db.add_job("deleted", deleted_at="2026-09-01")
    db.add_job("invalid", source_id="")
    await state.initialize_missing_controls(db, dry_run=True)
    assert await db.fetch_all("SELECT * FROM swe_cron_dispatch_controls") == []
    await state.initialize_missing_controls(db)
    rows = await db.fetch_all(
        "SELECT parent_job_id,paused FROM swe_cron_dispatch_controls "
        "ORDER BY parent_job_id"
    )
    assert rows == [
        {"parent_job_id": "off", "paused": 1},
        {"parent_job_id": "on", "paused": 0},
    ]
    await db.execute("UPDATE swe_cron_jobs SET enabled=1-enabled")
    await state.initialize_missing_controls(db)
    assert (
        await db.fetch_all(
            "SELECT parent_job_id,paused FROM swe_cron_dispatch_controls "
            "ORDER BY parent_job_id"
        )
        == rows
    )
    assert (
        len(await db.fetch_all("SELECT * FROM swe_cron_dispatch_events")) == 2
    )


@pytest.mark.asyncio
async def test_readiness_detects_missing_columns(control_db):
    from scheduler.app.services.cron import batch_run_state as state

    await state.assert_run_state_schema_ready(control_db)
    await control_db.execute("DROP TABLE swe_cron_dispatch_controls")
    with pytest.raises(Exception):
        await state.assert_run_state_schema_ready(control_db)


@pytest.mark.asyncio
async def test_get_is_read_only_and_checks_current_membership(control_db):
    from scheduler.app.services.cron import batch_run_state as state

    control_db.add_job()
    with pytest.raises(state.RunStateNotReady):
        await state.get_run_state("source-a", "tenant-1", "parent")
    assert (
        await control_db.fetch_all("SELECT * FROM swe_cron_dispatch_controls")
        == []
    )
    await state.initialize_missing_controls(control_db)
    assert (await state.get_run_state("source-a", "tenant-1", "parent"))[
        "paused"
    ] is False
    with pytest.raises(LookupError):
        await state.get_run_state("source-b", "tenant-1", "parent")
    await control_db.execute("UPDATE swe_cron_jobs SET meta='{}'")
    with pytest.raises(ValueError):
        await state.get_run_state("source-a", "tenant-1", "parent")


@pytest.mark.asyncio
async def test_pause_is_versioned_and_refunds_only_unstarted_claims(
    control_db,
):
    from scheduler.app.services.cron import batch_run_state as state

    db = control_db
    db.add_job()
    await state.initialize_missing_controls(db)
    db.insert(
        "swe_cron_dispatch_batches",
        batch_id="batch-1",
        source_id="source-a",
        tenant_id="tenant-1",
        parent_job_id="parent",
    )
    db.add_intent(status="claimed", claim_token="new-claim", attempt_count=2)
    db.add_intent(id=8, status="acknowledged", claim_token="inflight")
    db.add_intent(id=9, status="claimed", claim_token="")
    result = await state.set_run_state(
        "source-a",
        "tenant-1",
        "parent",
        "admin",
        True,
        1,
    )
    assert result["paused"] is True
    assert result["version"] == 2
    assert db.intent()["status"] == "pending"
    assert db.intent()["attempt_count"] == 1
    assert db.intent()["max_attempts"] == 3
    assert db.intent()["claim_token"] == ""
    assert (
        await db.fetch_one("SELECT status FROM swe_cron_dispatch_batches")
    )["status"] == "running"
    assert (
        await db.fetch_one(
            "SELECT status FROM swe_cron_dispatch_intents WHERE id=8"
        )
    )["status"] == "acknowledged"
    assert (
        await db.fetch_one(
            "SELECT status FROM swe_cron_dispatch_intents WHERE id=9"
        )
    )["status"] == "claimed"
    assert (
        await state.set_run_state(
            "source-a",
            "tenant-1",
            "parent",
            "admin",
            True,
            1,
        )
    )["version"] == 2
    with pytest.raises(state.RunStateConflict):
        await state.set_run_state(
            "source-a",
            "tenant-1",
            "parent",
            "admin",
            False,
            1,
        )
    resumed = await state.set_run_state(
        "source-a",
        "tenant-1",
        "parent",
        "admin",
        False,
        2,
    )
    assert resumed["version"] == 3
    assert resumed["resumed_at"]
    assert (
        await state.set_run_state(
            "source-a",
            "tenant-1",
            "parent",
            "admin",
            False,
            3,
        )
    )["resumed_at"] == resumed["resumed_at"]

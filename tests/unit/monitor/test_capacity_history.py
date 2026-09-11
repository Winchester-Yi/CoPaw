import sqlite3
from datetime import datetime, timedelta

import pytest


class HistoryDb:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE swe_cron_dispatch_worker_capacity "
            "(id INTEGER PRIMARY KEY, source_id TEXT, created_at TEXT)"
        )
        for name in (
            "worker_id",
            "provider_id",
            "model_id",
            "strategy_id",
            "previous_workers",
            "baseline_workers",
            "min_workers",
            "max_workers",
            "effective_workers",
            "pending_count",
            "claimed_count",
            "running_count",
            "success_count",
            "failure_count",
            "error_rate",
            "matched_rule",
            "avg_latency_ms",
            "decision_reason",
        ):
            self.conn.execute(
                "ALTER TABLE swe_cron_dispatch_worker_capacity "
                f"ADD COLUMN {name} TEXT"
            )

    async def fetch_one(self, sql, params):
        values = tuple(
            v.isoformat() if isinstance(v, datetime) else v for v in params
        )
        row = self.conn.execute(sql.replace("%s", "?"), values).fetchone()
        return dict(row) if row else None

    async def fetch_all(self, sql, params):
        values = tuple(
            v.isoformat() if isinstance(v, datetime) else v for v in params
        )
        return [
            dict(row)
            for row in self.conn.execute(sql.replace("%s", "?"), values)
        ]


@pytest.mark.asyncio
async def test_pages_return_every_record_once_with_fixed_snapshot():
    from monitor.app.services.cron.capacity_history import read_capacity_page

    db = HistoryDb()
    for i in range(1, 206):
        db.conn.execute(
            "INSERT INTO swe_cron_dispatch_worker_capacity "
            "(id,source_id,created_at) VALUES (?,?,?)",
            (
                i,
                "s",
                (
                    datetime(2026, 9, 9, 8) + timedelta(seconds=i // 3)
                ).isoformat(),
            ),
        )
    db.conn.execute(
        "INSERT INTO swe_cron_dispatch_worker_capacity "
        "(id,source_id,created_at) VALUES (999,'other','2026-09-09T08:00:00')"
    )
    page, cursor = await read_capacity_page(db, "s", None, None)
    assert len(page) == 100
    assert [r["id"] for r in page[:2]] == [205, 204]
    with pytest.raises(ValueError):
        await read_capacity_page(db, "other", None, None, cursor)
    with pytest.raises(ValueError):
        await read_capacity_page(db, "s", datetime(2026, 9, 9), None, cursor)
    db.conn.execute(
        "INSERT INTO swe_cron_dispatch_worker_capacity "
        "(id,source_id,created_at) VALUES (1000,'s','2026-09-09T08:00:00')"
    )
    all_ids = [r["id"] for r in page]
    while cursor:
        page, cursor = await read_capacity_page(db, "s", None, None, cursor)
        all_ids.extend(r["id"] for r in page)
    assert all_ids == list(range(205, 0, -1))
    db.conn.close()


@pytest.mark.asyncio
async def test_time_range_and_cursor_validation():
    from monitor.app.services.cron.capacity_history import read_capacity_page

    db = HistoryDb()
    db.conn.execute(
        "INSERT INTO swe_cron_dispatch_worker_capacity "
        "(id,source_id,created_at) VALUES (1,'s','2026-09-09T08:00:00')"
    )
    start = datetime(2026, 9, 9, 9)
    end = datetime(2026, 9, 9, 10)
    rows, cursor = await read_capacity_page(db, "s", start, end)
    assert rows == [] and cursor is None
    boundary = datetime(2026, 9, 9, 8)
    rows, _ = await read_capacity_page(db, "s", boundary, boundary)
    assert [row["id"] for row in rows] == [1]
    with pytest.raises(ValueError):
        await read_capacity_page(db, "s", end, start)
    with pytest.raises(ValueError):
        await read_capacity_page(db, "s", start, end, "not-a-valid-cursor")
    db.conn.close()

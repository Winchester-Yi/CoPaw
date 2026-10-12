"""Transaction replay, ownership and concurrent analysis revision checks."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "monitor" / "src")
)


@pytest.mark.asyncio
async def test_write_replay_does_not_rewrite_children_or_increment_revision():
    from swe.token_diagnostics.store import TokenStore

    class Cursor:
        queries = []

        async def execute(self, sql, params=()):
            self.queries.append((sql, params))

        async def fetchone(self):
            return {"revision": 2}

    cursor = Cursor()
    await TokenStore()._write_record(
        cursor,
        {
            "kind": "call",
            "data": {
                "branch_run_id": "b",
                "call_id": "c",
                "task_run_id": "t",
                "source_id": "s",
                "tenant_id": "u",
                "revision": 2,
            },
        },
    )
    assert not any(
        "DELETE" in sql or "data_revision=data_revision+1" in sql
        for sql, _ in cursor.queries
    )


@pytest.mark.asyncio
async def test_new_data_during_analysis_keeps_branch_dirty_and_unknown_rules_do_not_resolve():
    from monitor.app.services.token_diagnostics.store import TokenStore

    class Cursor:
        queries = []

        async def execute(self, sql, params=()):
            self.queries.append((sql, params))

        async def fetchone(self):
            return {
                "task_run_id": "t",
                "data_revision": 8,
                "claim_token": "lease",
                "lease_until": datetime.now(timezone.utc)
                + timedelta(seconds=30),
            }

    cursor = Cursor()

    class Store(TokenStore):
        @asynccontextmanager
        async def transaction(self):
            yield cursor

    result = {
        "findings": [],
        "rules": [
            {
                "rule_id": "duplicate_injection",
                "rule_version": 1,
                "state": "insufficient_evidence",
                "missing_fields": ["fingerprint"],
            }
        ],
    }
    assert await Store().commit_analysis(
        {
            "branch_run_id": "b",
            "source_id": "s",
            "tenant_id": "u",
            "claim_token": "lease",
        },
        7,
        result,
    )
    assert not any("state='resolved'" in sql for sql, _ in cursor.queries)
    update = next(
        params
        for sql, params in cursor.queries
        if "SET analyzed_revision" in sql
    )
    assert update[0] == 7
    assert update[2:4] == ("pending", 1)


@pytest.mark.asyncio
async def test_retention_owns_only_token_tables_and_retains_active_or_recent_child_tasks():
    from monitor.app.services.token_diagnostics.store import TokenStore

    class Cursor:
        queries = []

        async def execute(self, sql, params=()):
            self.queries.append((sql, params))

        async def fetchall(self):
            return [{"branch_run_id": "b"}]

    cursor = Cursor()

    class Store(TokenStore):
        @asynccontextmanager
        async def transaction(self):
            yield cursor

    assert await Store().cleanup(30) == 1
    query = cursor.queries[0][0]
    assert "child.ended_at IS NULL" in query
    assert "child.ended_at>=DATE_SUB" in query
    assert "r.parent_branch_run_id IS NULL" in query
    assert "LIMIT 20 FOR UPDATE" in query
    deletes = [sql for sql, _ in cursor.queries if sql.startswith("DELETE")]
    assert len(deletes) == 5
    assert all("swe_token_" in sql and "tracing" not in sql for sql in deletes)

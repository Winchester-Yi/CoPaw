# -*- coding: utf-8 -*-
"""B retention owns all typed facts and keeps active descendants protected."""

from contextlib import asynccontextmanager
from datetime import datetime

import pytest

from tests.unit.token_diagnostics.test_relational_storage import (
    RelationalCursor,
    facts,
    relational_db,
)  # noqa: F401


@pytest.mark.asyncio
async def test_cleanup_removes_all_24_tables_using_actual_adapter(
    relational_db,
):
    from swe.token_diagnostics.store import TokenStore as Writer
    from monitor.app.services.token_diagnostics.store import TokenStore
    from monitor.app.services.token_diagnostics.storage_schema import COLUMNS
    from tests.unit.token_diagnostics.test_b_storage import context

    db = relational_db
    cursor = RelationalCursor(db)
    await Writer()._write_record(
        cursor,
        {"kind": "run", "data": facts(ended_at="2000-01-01T00:00:00Z")},
    )
    await Writer()._write_record(
        cursor,
        {"kind": "context", "data": context()},
    )
    db.execute("UPDATE swe_token_runs SET ended_at=?", (datetime(2000, 1, 1),))
    db.connection.commit()

    class Cursor(RelationalCursor):
        async def execute(self, sql, params=()):
            # SQLite has no MySQL DATE_SUB; the cutoff is the only translated expression.
            if db.dialect == "sqlite" and "DATE_SUB" in sql:
                sql = sql.replace(
                    "DATE_SUB(UTC_TIMESTAMP(6),INTERVAL %s DAY)",
                    "'2026-09-01'",
                )
                params = ()
            await super().execute(sql, params)

    cursor = Cursor(db)

    class Store(TokenStore):
        @asynccontextmanager
        async def transaction(self):
            yield cursor

    assert await Store().cleanup(30) == 1
    deleted = [
        sql.split()[2] for sql, _ in cursor.queries if sql.startswith("DELETE")
    ]
    assert len(deleted) == len(COLUMNS) == 24
    assert set(deleted) == set(COLUMNS)
    assert deleted.index("swe_token_prefix_candidates") < deleted.index(
        "swe_token_prefix_comparisons",
    )
    assert deleted.index("swe_token_diagnostic_refs") < deleted.index(
        "swe_token_diagnostic_evaluations",
    )
    assert not db.execute(
        "SELECT context_id FROM swe_token_contexts",
    ).fetchall()


@pytest.mark.asyncio
async def test_active_child_keeps_ended_task_tree(relational_db):
    from swe.token_diagnostics.store import TokenStore as Writer
    from monitor.app.services.token_diagnostics.store import TokenStore

    db = relational_db
    cursor = RelationalCursor(db)
    for data in (
        facts(ended_at="2000-01-01T00:00:00Z"),
        facts(
            branch_run_id="child",
            parent_branch_run_id="branch",
            ended_at=None,
        ),
    ):
        await Writer()._write_record(cursor, {"kind": "run", "data": data})
    # Scalar run writes preserve explicit active lifecycle.
    db.execute(
        "UPDATE swe_token_runs SET ended_at=NULL WHERE branch_run_id='child'",
    )
    db.connection.commit()

    class Cursor(RelationalCursor):
        async def execute(self, sql, params=()):
            if db.dialect == "sqlite" and "DATE_SUB" in sql:
                sql = sql.replace(
                    "DATE_SUB(UTC_TIMESTAMP(6),INTERVAL %s DAY)",
                    "'2026-09-01'",
                )
                params = ()
            await super().execute(sql, params)

    cursor = Cursor(db)

    class Store(TokenStore):
        @asynccontextmanager
        async def transaction(self):
            yield cursor

    assert await Store().cleanup(30) == 0
    assert (
        len(db.execute("SELECT branch_run_id FROM swe_token_runs").fetchall())
        == 2
    )


@pytest.mark.asyncio
async def test_b_disabled_old_only_schema_still_cleans_a(relational_db):
    import aiomysql
    from swe.token_diagnostics.store import TokenStore as Writer
    from monitor.app.services.token_diagnostics.store import TokenStore
    from tests.unit.token_diagnostics.test_b_storage import B_TABLES

    db = relational_db
    for table in B_TABLES:
        db.execute(f"DROP TABLE {table}")
    await Writer()._write_record(
        RelationalCursor(db),
        {"kind": "run", "data": facts(ended_at="2000-01-01T00:00:00Z")},
    )
    db.connection.commit()

    class Cursor(RelationalCursor):
        async def execute(self, sql, params=()):
            if db.dialect == "sqlite" and "DATE_SUB" in sql:
                sql = sql.replace(
                    "DATE_SUB(UTC_TIMESTAMP(6),INTERVAL %s DAY)",
                    "'2026-09-01'",
                )
                params = ()
            try:
                await super().execute(sql, params)
            except Exception as exc:
                if db.dialect == "sqlite" and "no such table" in str(exc):
                    raise aiomysql.ProgrammingError(
                        1146,
                        "test_missing_table",
                    ) from None
                raise

    cursor = Cursor(db)

    class Store(TokenStore):
        @asynccontextmanager
        async def transaction(self):
            yield cursor

    assert await Store(context_enabled=False).cleanup(30) == 1
    assert not db.execute(
        "SELECT branch_run_id FROM swe_token_runs",
    ).fetchall()

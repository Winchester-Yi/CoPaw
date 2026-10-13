# -*- coding: utf-8 -*-
"""B evaluation history and current heads share the actual lease transaction."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from tests.unit.token_diagnostics.test_relational_storage import (
    RelationalCursor,
    facts,
    relational_db,
)  # noqa: F401


def store_for(db):
    from monitor.app.services.token_diagnostics.store import TokenStore

    class Store(TokenStore):
        @asynccontextmanager
        async def transaction(self):
            cursor = RelationalCursor(db)
            db.execute("BEGIN")
            try:
                yield cursor
                db.connection.commit()
            except BaseException:
                db.connection.rollback()
                raise

    return Store()


async def leased_run(db):
    from swe.token_diagnostics.store import TokenStore

    await TokenStore()._write_record(
        RelationalCursor(db),
        {"kind": "run", "data": facts()},
    )
    db.execute(
        "UPDATE swe_token_runs SET data_revision=7,claim_token=?,lease_until=?",
        (
            "lease",
            datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(seconds=60),
        ),
    )
    db.connection.commit()
    return {
        "branch_run_id": "branch",
        "source_id": "source",
        "tenant_id": "tenant",
        "claim_token": "lease",
    }


def analysis_result(snapshot="s", revision=7):
    from monitor.app.services.token_diagnostics.diagnostic_rules import (
        evaluate_context_rules,
    )

    run = facts(data_revision=revision)
    return {
        "rules": [],
        "findings": [],
        "prefix_comparisons": [],
        "diagnostic_evaluations": evaluate_context_rules(
            {
                "run": run,
                "input_revision": revision,
                "snapshot_id": snapshot,
                "calls": [],
                "contexts": [],
                "events": [],
                "steps": [],
            },
        ),
    }


@pytest.mark.asyncio
async def test_valid_lease_commits_b_history_and_idempotent_head(
    relational_db,
):
    db = relational_db
    claim = await leased_run(db)
    store = store_for(db)
    assert await store.commit_analysis(claim, 7, analysis_result())
    rows = db.execute(
        "SELECT evaluation_id,input_revision FROM swe_token_diagnostic_evaluations",
    ).fetchall()
    assert len(rows) == 8
    assert (
        len(
            db.execute(
                "SELECT evaluation_id FROM swe_token_diagnostic_heads",
            ).fetchall(),
        )
        == 8
    )
    # A replay of an already released claim does not append or change history.
    assert await store.commit_analysis(claim, 7, analysis_result()) is False
    assert (
        len(
            db.execute(
                "SELECT evaluation_id FROM swe_token_diagnostic_evaluations",
            ).fetchall(),
        )
        == 8
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["expired", "replaced", "foreign"])
async def test_invalid_lease_does_not_publish_any_b_results(
    relational_db,
    mode,
):
    db = relational_db
    claim = await leased_run(db)
    if mode == "expired":
        db.execute(
            "UPDATE swe_token_runs SET lease_until=?",
            (datetime(2000, 1, 1),),
        )
    elif mode == "replaced":
        db.execute("UPDATE swe_token_runs SET claim_token='new'")
    else:
        claim["tenant_id"] = "other"
    db.connection.commit()
    assert (
        await store_for(db).commit_analysis(claim, 7, analysis_result())
        is False
    )
    assert not db.execute(
        "SELECT evaluation_id FROM swe_token_diagnostic_evaluations",
    ).fetchall()
    assert not db.execute(
        "SELECT evaluation_id FROM swe_token_diagnostic_heads",
    ).fetchall()


@pytest.mark.asyncio
async def test_new_input_keeps_old_evaluation_historical_and_branch_pending(
    relational_db,
):
    db = relational_db
    claim = await leased_run(db)
    db.execute("UPDATE swe_token_runs SET data_revision=8")
    db.connection.commit()
    assert await store_for(db).commit_analysis(claim, 7, analysis_result())
    assert (
        len(
            db.execute(
                "SELECT evaluation_id FROM swe_token_diagnostic_evaluations",
            ).fetchall(),
        )
        == 8
    )
    assert not db.execute(
        "SELECT evaluation_id FROM swe_token_diagnostic_heads",
    ).fetchall()
    row = db.execute(
        "SELECT analysis_state,dirty FROM swe_token_runs",
    ).fetchone()
    assert row["analysis_state"] == "pending" and row["dirty"] == 1


@pytest.mark.asyncio
async def test_replaced_snapshot_cannot_roll_current_head_back(relational_db):
    db = relational_db
    store = store_for(db)
    claim = await leased_run(db)
    assert await store.commit_analysis(claim, 7, analysis_result("s1"))
    first = db.execute(
        "SELECT evaluation_id FROM swe_token_diagnostic_heads ORDER BY rule_id",
    ).fetchall()
    claim = await leased_run(db)
    assert await store.commit_analysis(claim, 7, analysis_result("s2"))
    second = db.execute(
        "SELECT evaluation_id FROM swe_token_diagnostic_heads ORDER BY rule_id",
    ).fetchall()
    assert [row["evaluation_id"] for row in first] != [
        row["evaluation_id"] for row in second
    ]
    claim = await leased_run(db)
    assert await store.commit_analysis(claim, 7, analysis_result("s1"))
    actual = db.execute(
        "SELECT evaluation_id FROM swe_token_diagnostic_heads ORDER BY rule_id",
    ).fetchall()
    assert [row["evaluation_id"] for row in actual] == [
        row["evaluation_id"] for row in second
    ]
    assert (
        len(
            db.execute(
                "SELECT evaluation_id FROM swe_token_diagnostic_evaluations",
            ).fetchall(),
        )
        == 16
    )


@pytest.mark.asyncio
async def test_b_detail_failure_rolls_back_legacy_and_new_heads(
    relational_db,
    monkeypatch,
):
    from monitor.app.services.token_diagnostics import diagnostic_storage

    db = relational_db
    claim = await leased_run(db)

    async def fail(cursor, data):
        raise RuntimeError("injected_detail_failure")

    monkeypatch.setattr(diagnostic_storage, "write_evaluation_details", fail)
    with pytest.raises(RuntimeError, match="injected_detail_failure"):
        await store_for(db).commit_analysis(claim, 7, analysis_result())
    assert not db.execute(
        "SELECT evaluation_id FROM swe_token_diagnostic_evaluations",
    ).fetchall()
    assert not db.execute(
        "SELECT evaluation_id FROM swe_token_diagnostic_heads",
    ).fetchall()
    assert (
        db.execute("SELECT claim_token FROM swe_token_runs").fetchone()[
            "claim_token"
        ]
        == "lease"
    )


@pytest.mark.asyncio
async def test_lease_expiring_during_detail_write_rolls_back(
    relational_db,
    monkeypatch,
):
    from monitor.app.services.token_diagnostics import store as module

    db = relational_db
    claim = await leased_run(db)
    checks = iter([True, False])
    monkeypatch.setattr(module, "valid_claim", lambda row, token: next(checks))
    with pytest.raises(ValueError, match="analysis_lease_expired"):
        await store_for(db).commit_analysis(claim, 7, analysis_result())
    assert not db.execute(
        "SELECT evaluation_id FROM swe_token_diagnostic_heads",
    ).fetchall()
    assert not db.execute(
        "SELECT evaluation_id FROM swe_token_diagnostic_evaluations",
    ).fetchall()


@pytest.mark.asyncio
async def test_b_enabled_analyzer_commits_b_and_legacy_in_same_result():
    from monitor.app.services.token_diagnostics.analysis import Analyzer

    class Store:
        context_enabled = True
        saved = None

        async def claim_next(self):
            return {"branch_run_id": "branch", "claim_token": "lease"}

        async def analysis_data(self, claim, **kwargs):
            return {
                "run": facts(data_revision=7),
                "calls": [],
                "items": [],
                "events": [],
                "contexts": [],
                "prefix_comparisons": [],
                "history_contexts": [],
                "truncated": False,
                "snapshot_id": "s",
            }

        async def commit_analysis(self, claim, revision, result):
            self.saved = result
            return True

        async def fail_analysis(self, claim, code):
            self.failed = code

        async def renew(self, claim):
            return True

    store = Store()
    assert await Analyzer(store).analyze_once()
    assert len(store.saved["rules"]) == 5
    assert len(store.saved["diagnostic_evaluations"]) == 8


@pytest.mark.asyncio
async def test_b_analysis_timeout_does_not_commit(monkeypatch):
    from monitor.app.services.token_diagnostics.analysis import Analyzer
    from monitor.app.services.token_diagnostics import diagnostic_analysis

    async def slow(*args, **kwargs):
        raise TimeoutError("analysis_timeout")

    monkeypatch.setattr(diagnostic_analysis, "analyze_snapshot", slow)

    class Store:
        context_enabled = True
        committed = False
        failed = None

        async def claim_next(self):
            return {"branch_run_id": "branch", "claim_token": "lease"}

        async def analysis_data(self, claim, **kwargs):
            return {
                "run": facts(data_revision=7),
                "calls": [],
                "items": [],
                "events": [],
                "contexts": [],
                "truncated": False,
                "snapshot_id": "s",
            }

        async def commit_analysis(self, *args):
            self.committed = True

        async def fail_analysis(self, claim, code):
            self.failed = code

        async def renew(self, claim):
            return True

    store = Store()
    assert await Analyzer(store).analyze_once() is False
    assert store.committed is False
    assert store.failed == "analysis_timeout"


@pytest.mark.asyncio
async def test_b_snapshot_uses_actual_contexts_and_fixed_historical_visibility(
    relational_db,
):
    from tests.unit.token_diagnostics.test_prefix_analysis import save_context
    from monitor.app.services.token_diagnostics.diagnostic_analysis import (
        analyze_snapshot,
    )
    from time import monotonic

    db = relational_db
    claim = await leased_run(db)
    cursor = RelationalCursor(db)
    start = datetime(2026, 10, 12, 12)
    await save_context(
        cursor,
        "current",
        start,
        request_started_at=start - timedelta(seconds=1),
    )
    await save_context(
        cursor,
        "h1",
        start - timedelta(minutes=1),
        branch_run_id="history",
    )
    db.connection.commit()
    store = store_for(db)
    store.context_enabled = True
    snapshot = await store.analysis_data(claim, deadline=monotonic() + 5)
    assert len(snapshot["contexts"]) == 1
    comparison = snapshot["prefix_comparisons"][0]
    assert comparison["candidate_count"] == 1
    assert comparison["candidates"][0]["historical_context_id"] == "h1"
    assert snapshot["history_contexts"][0]["context_id"] == "h1"
    # Late arrival is visible only to a later snapshot, never to a saved result.
    await save_context(
        cursor,
        "h2",
        start - timedelta(seconds=30),
        branch_run_id="history2",
    )
    db.connection.commit()
    snapshot["run"]["data_revision"] = 7
    result = await analyze_snapshot(snapshot, deadline=monotonic() + 5)
    assert await store.commit_analysis(claim, 7, result)
    saved = db.execute(
        "SELECT candidate_count,snapshot_id FROM swe_token_prefix_comparisons",
    ).fetchone()
    assert saved["candidate_count"] == 1
    assert saved["snapshot_id"] == snapshot["snapshot_id"]
    second = await store.analysis_data(claim, deadline=monotonic() + 5)
    assert second["snapshot_id"] != snapshot["snapshot_id"]
    assert second["prefix_comparisons"][0]["candidate_count"] == 2


@pytest.mark.asyncio
async def test_b_transaction_sets_repeatable_read_before_begin():
    from monitor.app.services.token_diagnostics.store import TokenStore

    actions = []

    class Cursor:
        async def execute(self, sql, params=()):
            actions.append(sql)

    class Connection:
        @asynccontextmanager
        async def cursor(self, _kind):
            yield Cursor()

        async def begin(self):
            actions.append("begin")

        async def commit(self):
            actions.append("commit")

        async def rollback(self):
            actions.append("rollback")

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    async with TokenStore(Pool(), context_enabled=True).transaction():
        actions.append("work")
    assert actions == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
        "begin",
        "work",
        "commit",
    ]


@pytest.mark.asyncio
async def test_failure_status_write_is_bounded(monkeypatch):
    import asyncio
    from monitor.app.services.token_diagnostics import analysis as module

    monkeypatch.setattr(module, "FAILURE_WRITE_SECONDS", 0.01)

    class Store:
        context_enabled = True
        committed = False

        async def claim_next(self):
            return {"branch_run_id": "b", "claim_token": "lease"}

        async def analysis_data(self, *args, **kwargs):
            raise TimeoutError("analysis_timeout")

        async def fail_analysis(self, *args):
            await asyncio.sleep(60)

        async def renew(self, claim):
            return True

        async def commit_analysis(self, *args):
            self.committed = True

    store = Store()
    assert (
        await asyncio.wait_for(module.Analyzer(store).analyze_once(), 0.5)
        is False
    )
    assert store.committed is False


def blocked_driver_store():
    """Exercise real aiomysql cancellation and rollback over a blocked reader."""
    import asyncio
    from types import SimpleNamespace

    import aiomysql
    from monitor.app.services.token_diagnostics.store import TokenStore

    class Reader:
        def __init__(self):
            self.started = asyncio.Event()

        async def readexactly(self, _size):
            self.started.set()
            await asyncio.Future()

    class Transport:
        closed = False

        def close(self):
            self.closed = True

    reader = Reader()
    transport = Transport()

    class Connection(aiomysql.Connection):
        rollback_attempts = 0

        async def begin(self):
            pass

        @asynccontextmanager
        async def cursor(self, _kind):
            yield self

        async def execute(self, query, params=()):
            if query.startswith("SET TRANSACTION"):
                return
            await self._read_packet()

        async def rollback(self):
            self.rollback_attempts += 1
            await super().rollback()

    connection = Connection()
    connection._reader = reader
    connection._writer = SimpleNamespace(transport=transport)

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield connection

    class Store(TokenStore):
        failed = None
        committed = False

        async def claim_next(self):
            return {"branch_run_id": "b", "claim_token": "lease"}

        async def analysis_data(self, *args, **kwargs):
            async with self.transaction() as cursor:
                await cursor.execute(
                    "SELECT branch_run_id FROM swe_token_runs",
                )

        async def fail_analysis(self, _claim, code):
            self.failed = code

        async def renew(self, _claim):
            return True

        async def commit_analysis(self, *args):
            self.committed = True

    return Store(Pool(), context_enabled=True), connection, reader, transport


@pytest.mark.asyncio
async def test_analyzer_close_during_driver_read_preserves_cancellation():
    import asyncio
    from monitor.app.services.token_diagnostics.analysis import Analyzer

    store, connection, reader, transport = blocked_driver_store()
    analyzer = Analyzer(store)
    analyzer.start()
    task = analyzer.task
    await asyncio.wait_for(reader.started.wait(), 0.5)
    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(analyzer.close(), 0.5)
    assert asyncio.get_running_loop().time() - started < 0.2
    assert task.done() and task.cancelled()
    assert analyzer.task is None
    assert transport.closed and connection.closed
    assert store.failed is None
    assert store.committed is False


@pytest.mark.asyncio
async def test_driver_read_timeout_remains_analysis_timeout(monkeypatch):
    import asyncio
    from monitor.app.services.token_diagnostics.analysis import Analyzer

    original_timeout = asyncio.timeout
    monkeypatch.setattr(
        asyncio,
        "timeout",
        lambda _delay: original_timeout(0.01),
    )
    store, connection, _reader, transport = blocked_driver_store()
    assert await asyncio.wait_for(Analyzer(store).analyze_once(), 0.5) is False
    assert store.failed == "analysis_timeout"
    assert store.committed is False
    assert transport.closed and connection.closed


@pytest.mark.asyncio
async def test_transaction_preserves_error_when_driver_rollback_fails():
    store, connection, _reader, transport = blocked_driver_store()
    failure = RuntimeError("snapshot failed")
    with pytest.raises(RuntimeError) as raised:
        async with store.transaction():
            connection.close()
            raise failure
    assert raised.value is failure
    assert connection.rollback_attempts == 1
    assert transport.closed and connection.closed

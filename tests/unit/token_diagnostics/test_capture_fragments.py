# -*- coding: utf-8 -*-
"""Resumed and concurrent execution fragments retain independent denominators."""

from types import SimpleNamespace

import pytest

from tests.unit.token_diagnostics.test_relational_storage import (
    RelationalCursor,
    relational_db,
)  # noqa: F401


@pytest.mark.asyncio
async def test_resume_survives_queue_coalescing_and_persistence(relational_db):
    from swe.token_diagnostics.collector import Collector
    from swe.token_diagnostics.config import DiagnosticConfig
    from swe.token_diagnostics.context import accounting_run, child_snapshot
    from swe.token_diagnostics.model_observer import (
        AttemptObserver,
        LogicalObserver,
    )
    from swe.token_diagnostics.store import TokenStore

    cursor = RelationalCursor(relational_db)

    class Store(TokenStore):
        async def write_batch(self, records):
            for record in records:
                await self._write_record(cursor, record)

        async def close(self):
            pass

    capture = Collector(DiagnosticConfig(enabled=True), Store())

    class Model:
        async def __call__(self, messages, **kwargs):
            return SimpleNamespace(
                usage={"input_tokens": 1, "output_tokens": 0},
            )

    model = LogicalObserver(AttemptObserver(Model(), "p", {}))
    metadata = {"source_id": "s", "tenant_id": "t", "execution_id": "turn"}
    children = []
    for count in (2, 1):
        with accounting_run(metadata, capture) as branch:
            for _ in range(count):
                await model([])
            children.append(child_snapshot()["branch_run_id"])
    await capture.writer.flush_once()
    await cursor.execute(
        "SELECT expected_attempt_count,execution_state FROM swe_token_runs WHERE branch_run_id=%s",
        (branch.fields["branch_run_id"],),
    )
    run = await cursor.fetchone()
    assert run["expected_attempt_count"] == 3
    await cursor.execute(
        "SELECT child_branch_run_id FROM swe_token_expected_children WHERE branch_run_id=%s",
        (branch.fields["branch_run_id"],),
    )
    assert {
        row["child_branch_run_id"] for row in await cursor.fetchall()
    } == set(children)
    assert run["execution_state"] == "completed"
    await capture.close()


@pytest.mark.asyncio
async def test_fragment_start_keeps_resumed_execution_open(relational_db):
    from swe.token_diagnostics.store import TokenStore
    from tests.unit.token_diagnostics.test_relational_storage import facts

    cursor = RelationalCursor(relational_db)
    store = TokenStore()

    async def fragment(identity, revision, count, state, start):
        await store._write_record(
            cursor,
            {
                "kind": "event",
                "data": facts(
                    event_id=identity,
                    capture_fragment_id=identity,
                    event_type="accounting_fragment",
                    revision=revision,
                    expected_attempt_count=count,
                    execution_state=state,
                    started_at=start,
                    ended_at=None if count is None else start,
                    expected_child_branches=[identity + "-child"],
                ),
            },
        )

    await fragment("first", 2, 2, "completed", "2026-10-12T12:00:00Z")
    await fragment("second", 1, None, "running", "2026-10-12T12:01:00Z")
    await cursor.execute(
        "SELECT expected_attempt_count,execution_state,ended_at FROM swe_token_runs",
    )
    assert await cursor.fetchone() == {
        "expected_attempt_count": None,
        "execution_state": "running",
        "ended_at": None,
    }
    # A delayed root snapshot must not close a different active fragment.
    await store._write_record(
        cursor,
        {
            "kind": "run",
            "data": facts(
                revision=9,
                capture_fragment_id="first",
                expected_attempt_count=2,
                execution_state="completed",
            ),
        },
    )
    await cursor.execute("SELECT execution_state FROM swe_token_runs")
    assert (await cursor.fetchone())["execution_state"] == "running"
    await fragment("second", 2, 1, "completed", "2026-10-12T12:01:00Z")
    await fragment("second", 1, None, "running", "2026-10-12T12:01:00Z")
    await cursor.execute(
        "SELECT expected_attempt_count,execution_state FROM swe_token_runs",
    )
    assert await cursor.fetchone() == {
        "expected_attempt_count": 3,
        "execution_state": "completed",
    }

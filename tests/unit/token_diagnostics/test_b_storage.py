# -*- coding: utf-8 -*-
"""Initial B schema and atomic context writes with independent readers."""

import re
from pathlib import Path

import pytest

from tests.unit.token_diagnostics.test_relational_storage import (
    RelationalCursor,
    facts,
    relational_db,
)  # noqa: F401

B_TABLES = {
    "swe_token_contexts",
    "swe_token_context_occurrences",
    "swe_token_prefix_blocks",
    "swe_token_context_operations",
    "swe_token_operation_segments",
    "swe_token_prefix_comparisons",
    "swe_token_prefix_candidates",
    "swe_token_diagnostic_evaluations",
    "swe_token_diagnostic_findings",
    "swe_token_diagnostic_values",
    "swe_token_diagnostic_gaps",
    "swe_token_diagnostic_refs",
    "swe_token_diagnostic_heads",
}


def test_initial_schema_contains_b_tables():
    ddl = Path("scripts/sql/token_diagnostics_tables.sql").read_text()
    names = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", ddl))
    assert B_TABLES <= names
    assert len(names) == 24


def context(**updates):
    data = facts(
        context_id="ctx",
        context_revision=1,
        context_role="prepared",
        call_id="call",
        request_started_at="2026-10-12T12:00:00Z",
        request_ended_at="2026-10-12T12:01:00Z",
        representation_version="client-ordered-text-v1",
        key_version="v1",
        text_bytes=5,
        encoded_bytes=8,
        capture_state="complete",
        expected_item_count=1,
        occurrences=[
            {
                "occurrence_index": 0,
                "category": "user",
                "text_start": 0,
                "text_end": 5,
            },
        ],
        blocks=[
            {
                "block_index": 0,
                "chained_digest": "a" * 64,
                "encoded_start": 0,
                "encoded_end": 8,
                "text_start": 0,
                "text_end": 5,
            },
        ],
    )
    return {**data, **updates}


@pytest.mark.asyncio
async def test_context_roundtrip_and_old_revision_does_not_replace(
    relational_db,
):
    from swe.token_diagnostics.store import TokenStore
    from monitor.app.services.token_diagnostics.diagnostic_storage import (
        read_context,
    )

    cursor = RelationalCursor(relational_db)
    store = TokenStore()
    await store._write_record(cursor, {"kind": "context", "data": context()})
    await store._write_record(
        cursor,
        {
            "kind": "context",
            "data": context(
                context_revision=0,
                revision=0,
            ),
        },
    )
    row = await read_context(cursor, "ctx", ("source", "tenant"))
    assert row["context_revision"] == 1
    assert len(row["occurrences"]) == len(row["blocks"]) == 1
    assert row["text_bytes"] == 5
    assert await read_context(cursor, "ctx", ("source", "other")) is None


@pytest.mark.asyncio
async def test_failed_child_insert_rolls_back_context_header(relational_db):
    from swe.token_diagnostics.store import TokenStore

    class FailingCursor(RelationalCursor):
        async def execute(self, sql, params=()):
            if sql.startswith("INSERT INTO swe_token_prefix_blocks"):
                raise RuntimeError("injected_block_failure")
            await super().execute(sql, params)

    cursor = FailingCursor(relational_db)
    db = relational_db
    from contextlib import asynccontextmanager

    class Connection:
        rollback_count = 0

        async def begin(self):
            db.execute("BEGIN")

        async def commit(self):
            db.connection.commit()

        async def rollback(self):
            self.rollback_count += 1
            db.connection.rollback()

        @asynccontextmanager
        async def cursor(self, _cursor_type):
            yield cursor

    connection = Connection()

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield connection

    with pytest.raises(RuntimeError, match="injected_block_failure"):
        await TokenStore(Pool()).write_batch(
            [{"kind": "context", "data": context()}],
        )
    assert connection.rollback_count == 1
    assert (
        db.execute("SELECT COUNT(*) AS n FROM swe_token_contexts").fetchone()[
            "n"
        ]
        == 0
    )


@pytest.mark.asyncio
async def test_operation_without_after_remains_unpaired(relational_db):
    from swe.token_diagnostics.store import TokenStore
    from monitor.app.services.token_diagnostics.diagnostic_storage import (
        read_operation,
    )

    cursor = RelationalCursor(relational_db)
    await TokenStore()._write_record(
        cursor,
        {
            "kind": "context_operation",
            "data": facts(
                operation_id="op",
                operation_type="compact",
                operation_state="error",
                before_context_id="ctx",
                after_context_id=None,
                missing_after=True,
                revision=2,
            ),
        },
    )
    op = await read_operation(cursor, "op", ("source", "tenant"))
    assert op["after_context_id"] is None
    assert op["missing_after"]
    assert await read_operation(cursor, "op", ("other", "tenant")) is None


@pytest.mark.asyncio
async def test_evaluation_history_is_idempotent_and_stale_cannot_publish(
    relational_db,
):
    from monitor.app.services.token_diagnostics.diagnostic_storage import (
        append_evaluations,
    )
    from swe.token_diagnostics.store import TokenStore

    cursor = RelationalCursor(relational_db)
    await TokenStore()._write_record(cursor, {"kind": "run", "data": facts()})
    evaluation = {
        "object_type": "branch",
        "object_id": "branch",
        "rule_id": "context_growth",
        "rule_version": 1,
        "threshold_version": 1,
        "input_revision": 1,
        "snapshot_id": "snapshot-1",
        "state": "insufficient_evidence",
        "values": [],
        "gaps": ["missing_context"],
        "refs": [],
        "findings": [],
    }
    run = facts(data_revision=1)
    await append_evaluations(cursor, run, [evaluation])
    await append_evaluations(cursor, run, [evaluation])
    assert (
        relational_db.execute(
            "SELECT COUNT(*) AS n FROM swe_token_diagnostic_evaluations",
        ).fetchone()["n"]
        == 1
    )
    assert (
        relational_db.execute(
            "SELECT COUNT(*) AS n FROM swe_token_diagnostic_gaps",
        ).fetchone()["n"]
        == 1
    )
    later = {
        **evaluation,
        "snapshot_id": "snapshot-2",
        "state": "not_hit",
        "gaps": [],
    }
    await append_evaluations(cursor, {**run, "data_revision": 2}, [later])
    assert (
        relational_db.execute(
            "SELECT COUNT(*) AS n FROM swe_token_diagnostic_evaluations",
        ).fetchone()["n"]
        == 2
    )
    head = relational_db.execute(
        "SELECT snapshot_id,input_revision FROM swe_token_diagnostic_heads",
    ).fetchone()
    assert dict(head) == {"snapshot_id": "snapshot-1", "input_revision": 1}
    latest = {
        **evaluation,
        "snapshot_id": "snapshot-3",
        "state": "not_hit",
        "gaps": [],
    }
    await append_evaluations(cursor, run, [latest])
    await append_evaluations(cursor, run, [evaluation])
    head = relational_db.execute(
        "SELECT snapshot_id FROM swe_token_diagnostic_heads",
    ).fetchone()
    assert head["snapshot_id"] == "snapshot-3"


@pytest.mark.asyncio
async def test_step_heads_and_replacement_history_are_branch_scoped(
    relational_db,
):
    from monitor.app.services.token_diagnostics.diagnostic_storage import (
        append_evaluations,
    )

    cursor = RelationalCursor(relational_db)
    evaluation = {
        "object_type": "step",
        "object_id": "shared-step",
        "rule_id": "suspected_loop",
        "rule_version": 1,
        "threshold_version": 1,
        "input_revision": 1,
        "snapshot_id": "shared-snapshot",
        "state": "not_hit",
        "values": [],
        "gaps": [],
        "refs": [],
        "findings": [],
    }
    runs = [
        facts(
            task_run_id=f"task-{i}",
            branch_run_id=f"branch-{i}",
            data_revision=1,
        )
        for i in (1, 2)
    ]
    for run in runs:
        await append_evaluations(cursor, run, [evaluation])
    rows = relational_db.execute(
        "SELECT evaluation_id,branch_run_id,replaces_evaluation_id FROM swe_token_diagnostic_evaluations ORDER BY branch_run_id",
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["evaluation_id"] != rows[1]["evaluation_id"]
    assert all(row["replaces_evaluation_id"] is None for row in rows)
    for run in runs:
        await append_evaluations(
            cursor,
            run,
            [{**evaluation, "snapshot_id": "next-snapshot"}],
        )
    heads = relational_db.execute(
        "SELECT branch_run_id,evaluation_id FROM swe_token_diagnostic_heads ORDER BY branch_run_id",
    ).fetchall()
    assert len(heads) == 2
    for old, head in zip(rows, heads):
        saved = relational_db.execute(
            "SELECT branch_run_id,replaces_evaluation_id FROM swe_token_diagnostic_evaluations WHERE evaluation_id=?",
            (head["evaluation_id"],),
        ).fetchone()
        assert saved["branch_run_id"] == old["branch_run_id"]
        assert saved["replaces_evaluation_id"] == old["evaluation_id"]


@pytest.mark.asyncio
async def test_same_context_id_cannot_be_reassigned_to_other_scope(
    relational_db,
):
    from swe.token_diagnostics.store import TokenStore

    cursor = RelationalCursor(relational_db)
    store = TokenStore()
    await store._write_record(cursor, {"kind": "context", "data": context()})
    with pytest.raises(ValueError, match="scope_mismatch"):
        await store._write_record(
            cursor,
            {
                "kind": "context",
                "data": context(
                    tenant_id="other",
                    context_revision=2,
                ),
            },
        )


@pytest.mark.asyncio
async def test_unknown_operation_time_stays_unknown(relational_db):
    from swe.token_diagnostics.store import TokenStore
    from monitor.app.services.token_diagnostics.diagnostic_storage import (
        read_operation,
    )

    cursor = RelationalCursor(relational_db)
    await TokenStore()._write_record(
        cursor,
        {
            "kind": "context_operation",
            "data": facts(
                operation_id="op",
                started_at=None,
                operation_state="error",
            ),
        },
    )
    row = await read_operation(cursor, "op", ("source", "tenant"))
    assert row["started_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_child",
    [
        {"occurrence_index": 0, "tenant_id": "other"},
        {"occurrence_index": 1},
    ],
)
async def test_complete_context_rejects_invalid_child_contract(
    relational_db,
    bad_child,
):
    from swe.token_diagnostics.store import TokenStore

    with pytest.raises(ValueError, match="context_child_contract"):
        await TokenStore()._write_record(
            RelationalCursor(relational_db),
            {
                "kind": "context",
                "data": context(occurrences=[bad_child]),
            },
        )


@pytest.mark.asyncio
async def test_prefix_snapshot_retains_two_independent_best_refs(
    relational_db,
):
    from monitor.app.services.token_diagnostics.diagnostic_storage import (
        write_prefix_comparison,
    )

    cursor = RelationalCursor(relational_db)
    comparison = facts(
        current_context_id="ctx",
        current_context_revision=1,
        snapshot_id="snap",
        best_lower_context_id="history-a",
        max_upper_context_id="history-b",
        candidate_count=2,
        candidates=[
            {
                "historical_context_id": "history-a",
                "lower_text_bytes": 20,
                "upper_text_bytes": 25,
            },
            {
                "historical_context_id": "history-b",
                "lower_text_bytes": 15,
                "upper_text_bytes": 30,
            },
        ],
    )
    await write_prefix_comparison(cursor, comparison)
    await write_prefix_comparison(cursor, comparison)
    head = relational_db.execute(
        "SELECT best_lower_context_id,max_upper_context_id FROM swe_token_prefix_comparisons",
    ).fetchone()
    assert dict(head) == {
        "best_lower_context_id": "history-a",
        "max_upper_context_id": "history-b",
    }
    assert (
        relational_db.execute(
            "SELECT COUNT(*) AS n FROM swe_token_prefix_candidates",
        ).fetchone()["n"]
        == 2
    )


@pytest.mark.asyncio
async def test_operation_revision_replaces_segments_atomically(relational_db):
    from swe.token_diagnostics.store import TokenStore
    from monitor.app.services.token_diagnostics.diagnostic_storage import (
        read_operation,
    )

    cursor = RelationalCursor(relational_db)
    store = TokenStore()
    initial = facts(
        operation_id="op",
        revision=1,
        operation_state="running",
        segments=[
            {
                "relation_index": 0,
                "relation_type": "removed",
                "source_object_id": "old",
            },
        ],
    )
    await store._write_record(
        cursor,
        {"kind": "context_operation", "data": initial},
    )
    final = {
        **initial,
        "revision": 2,
        "operation_state": "ok",
        "after_context_id": "after",
        "segments": [
            {
                "relation_index": 0,
                "relation_type": "created",
                "source_object_id": "new",
            },
        ],
    }
    await store._write_record(
        cursor,
        {"kind": "context_operation", "data": final},
    )
    await store._write_record(
        cursor,
        {"kind": "context_operation", "data": initial},
    )
    row = await read_operation(cursor, "op", ("source", "tenant"))
    assert row["operation_state"] == "ok"
    assert row["segments"][0]["source_object_id"] == "new"

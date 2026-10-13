# -*- coding: utf-8 -*-
"""Local B browser fixture: actual readers over disposable in-memory relations.

Run with venv/bin/python scripts/testing/token_diagnostics_v2_fixture.py.
No business database, Provider, credentials or persisted user data is used.
"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "monitor/src")]

from fastapi import FastAPI
import uvicorn

from monitor.app.routers.token_diagnostics_v2 import router
from monitor.app.services.token_diagnostics.diagnostic_storage import (
    append_evaluations,
    write_prefix_comparison,
)
from tests.unit.token_diagnostics.test_b_queries import seed_task, service_for
from tests.unit.token_diagnostics.test_b_storage import context
from tests.unit.token_diagnostics.test_relational_storage import (
    RelationalCursor,
    facts,
    relational_db,
)
from swe.token_diagnostics.store import TokenStore


async def prepare(db):
    calls = [
        (f"call-{i}", "controlled-model", i * 1000, f"2026-10-12T1{i}:00:00Z")
        for i in (1, 2, 3)
    ]
    await seed_task(db, calls=calls)
    db.execute(
        "UPDATE swe_token_model_calls SET provider_id=? WHERE task_run_id=?",
        ("controlled-provider", "task"),
    )
    cursor, writer = RelationalCursor(db), TokenStore()
    for i in (1, 2, 3):
        await writer._write_record(
            cursor,
            {
                "kind": "context",
                "data": context(
                    context_id=f"ctx-{i}",
                    call_id=f"call-{i}",
                    estimated_tokens=i * 100,
                    model_name="controlled-model",
                    agent_id="executor",
                    provider_id="controlled-provider",
                    request_started_at=f"2026-10-12T1{i}:00:00Z",
                    request_ended_at=f"2026-10-12T1{i}:00:30Z",
                    estimator_version="text-density-v1",
                    occurrences=[
                        {
                            "occurrence_index": 0,
                            "category": "user",
                            "attribution": "role",
                            "source_ref": "controlled-local-reference",
                            "estimated_tokens": i * 100,
                            "text_start": 0,
                            "text_end": 5,
                        },
                    ],
                ),
            },
        )
        db.execute(
            "UPDATE swe_token_model_calls SET context_id=?,logical_seq=?,output_tokens=?,usage_status=?,ended_at=? WHERE call_id=?",
            (
                f"ctx-{i}",
                i,
                None if i == 3 else 0,
                "partial" if i == 3 else "reported",
                f"2026-10-12T1{i}:00:30Z",
                f"call-{i}",
            ),
        )
        if i > 1:
            await write_prefix_comparison(
                cursor,
                {
                    **facts(),
                    "current_context_id": f"ctx-{i}",
                    "current_context_revision": 1,
                    "call_id": f"call-{i}",
                    "snapshot_id": "controlled-snapshot",
                    "input_revision": 1,
                    "representation_version": "client-ordered-text-v1",
                    "key_version": "v1",
                    "window_hours": 24,
                    "candidate_limit": 100,
                    "candidate_count": 1,
                    "candidate_limited": False,
                    "selection_version": "v1",
                    "best_lower_context_id": f"ctx-{i-1}",
                    "max_upper_context_id": f"ctx-{i-1}",
                    "lower_text_bytes": 5,
                    "upper_text_bytes": 5,
                    "total_text_bytes": 5,
                    "analysis_state": "ready",
                    "eligible": i == 2,
                    "created_at": "2026-10-12T14:00:00Z",
                    "candidates": [
                        {
                            "historical_context_id": f"ctx-{i-1}",
                            "historical_context_revision": 1,
                            "historical_call_id": f"call-{i-1}",
                            "historical_ended_at": f"2026-10-12T1{i-1}:00:30Z",
                            "lower_text_bytes": 5,
                            "upper_text_bytes": 5,
                            "first_difference_index": None,
                        },
                    ],
                },
            )
    for role, amount in (("before", 200), ("after", 100)):
        await writer._write_record(
            cursor,
            {
                "kind": "context",
                "data": context(
                    context_id=f"operation-{role}",
                    call_id=None,
                    operation_id="controlled-operation",
                    context_role=role,
                    request_started_at=None,
                    request_ended_at=None,
                    estimated_tokens=amount,
                    estimator_version="text-density-v1",
                ),
            },
        )
    await writer._write_record(
        cursor,
        {
            "kind": "context_operation",
            "data": facts(
                operation_id="controlled-operation",
                operation_type="compaction",
                operation_state="completed",
                before_context_id="operation-before",
                after_context_id="operation-after",
                summary_call_id="call-3",
                missing_before=False,
                missing_after=False,
                capture_state="complete",
                ended_at="2026-10-12T14:00:00Z",
                segments=[
                    {
                        "relation_index": 0,
                        "relation_type": "retained",
                        "before_context_id": "operation-before",
                        "before_occurrence_index": 0,
                        "after_context_id": "operation-after",
                        "after_occurrence_index": 0,
                    },
                ],
            ),
        },
    )
    revision = db.execute(
        "SELECT data_revision FROM swe_token_runs WHERE branch_run_id='branch'",
    ).fetchone()["data_revision"]
    await append_evaluations(
        cursor,
        facts(data_revision=revision),
        [
            {
                "object_type": "call",
                "object_id": f"call-{i}",
                "rule_id": "context_growth",
                "rule_version": 1,
                "threshold_version": 1,
                "input_revision": revision,
                "snapshot_id": "controlled-snapshot",
                "state": "candidate" if i == 3 else "insufficient_evidence",
                "values": [],
                "gaps": ["missing_comparison_evidence"] if i < 3 else [],
                "refs": [
                    {"reference_type": "context", "object_id": f"ctx-{i}"},
                ],
                "findings": (
                    [
                        {
                            "instance_key": f"controlled-instance-{i}",
                            "signature": None,
                            "signature_version": 1,
                            "reliable_grouping": False,
                            "recommendation_code": "review_summary_and_budget",
                            "call_id": f"call-{i}",
                            "context_id": f"ctx-{i}",
                        },
                    ]
                    if i == 3
                    else []
                ),
            }
            for i in (1, 2, 3)
        ],
    )
    db.execute(
        "UPDATE swe_token_runs SET analysis_state='ready',analyzed_revision=data_revision",
    )
    for table in (
        "swe_token_diagnostic_evaluations",
        "swe_token_diagnostic_findings",
    ):
        db.execute(f"UPDATE {table} SET created_at='2026-10-12T14:00:00Z'")
    db.execute(
        "UPDATE swe_token_prefix_comparisons SET input_revision=? WHERE call_id='call-2'",
        (revision,),
    )


def main():
    fixture = relational_db.__wrapped__(SimpleNamespace(param="sqlite"))
    db = next(fixture)
    try:
        asyncio.run(prepare(db))
        app = FastAPI()
        app.state.token_diagnostic_queries = service_for(db)
        app.include_router(router, prefix="/api/monitor")
        uvicorn.run(app, host="127.0.0.1", port=9191)
    finally:
        fixture.close()


if __name__ == "__main__":
    main()

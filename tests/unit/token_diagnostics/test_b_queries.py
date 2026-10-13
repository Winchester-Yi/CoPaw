# -*- coding: utf-8 -*-
"""B projections execute against the actual 24-table relational contract."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from dataclasses import replace
import base64
import json

import pytest

from tests.unit.token_diagnostics.test_relational_storage import (
    RelationalCursor,
    facts,
    relational_db,
)  # noqa: F401

NOW = datetime(2026, 10, 13, 0, tzinfo=timezone.utc)
SCOPE = ("source", "tenant")


def service_for(db):
    from monitor.app.services.token_diagnostics.store import TokenStore
    from monitor.app.services.token_diagnostics.diagnostic_queries import (
        DiagnosticQueryService,
    )

    class Store(TokenStore):
        @asynccontextmanager
        async def transaction(self):
            yield RelationalCursor(db)

    return DiagnosticQueryService(Store())


def filters(**values):
    from monitor.app.services.token_diagnostics.diagnostic_filters import (
        DiagnosticFilters,
    )

    return DiagnosticFilters(start=NOW - timedelta(days=1), end=NOW, **values)


async def seed_task(
    db,
    task="task",
    branch="branch",
    tenant="tenant",
    calls=None,
    **updates,
):
    from swe.token_diagnostics.store import TokenStore

    writer = TokenStore()
    cursor = RelationalCursor(db)
    calls = (
        calls
        if calls is not None
        else [
            ("yesterday", "A", 10, "2026-10-11T12:00:00Z"),
            ("today-a", "A", 20, "2026-10-12T12:00:00Z"),
            ("today-b", "B", 30, "2026-10-12T13:00:00Z"),
        ]
    )
    for identity, model, total, start in calls:
        await writer._write_record(
            cursor,
            {
                "kind": "call",
                "data": facts(
                    task_run_id=task,
                    branch_run_id=branch,
                    tenant_id=tenant,
                    call_id=identity,
                    logical_call_id=identity,
                    model_name=model,
                    started_at=start,
                    ended_at=start,
                    usage_status="reported",
                    capture_state="complete",
                    input_tokens=total,
                    output_tokens=0,
                    agent_id="executor",
                    logical_seq=1,
                    expected_item_count=0,
                ),
            },
        )
    run = facts(
        task_run_id=task,
        branch_run_id=branch,
        tenant_id=tenant,
        started_at="2026-10-12T11:00:00Z",
        ended_at="2026-10-12T14:00:00Z",
        expected_attempt_count=len(calls),
        expected_child_branches=[],
        agent_id="root-agent",
    )
    await writer._write_record(
        cursor,
        {"kind": "run", "data": {**run, **updates}},
    )


@pytest.mark.asyncio
async def test_window_usage_is_distinct_from_lifecycle_and_selected_model(
    relational_db,
):
    await seed_task(relational_db, started_at="2026-10-11T11:00:00Z")
    service = service_for(relational_db)
    dashboard = await service.dashboard(SCOPE, filters())
    assert dashboard["window_attempt_usage"]["known_total_tokens"] == 50
    assert (await service.dashboard(SCOPE, filters(model_name="A")))[
        "window_attempt_usage"
    ]["known_total_tokens"] == 20
    task = await service.task("task", SCOPE, filters(model_name="B"))
    assert task["lifecycle_usage"]["known_total_tokens"] == 60
    assert task["selected_contribution"]["known_total_tokens"] == 30
    assert task["window_attempt_usage"]["known_total_tokens"] == 30
    assert task["completeness"]["usage_complete"] is True
    assert task["scope"] == {"source_id": "source", "tenant_id": "tenant"}
    assert task["window"]["end"] == "2026-10-13T00:00:00Z"
    assert dashboard["lifecycle_distribution"]["sample_count"] == 0
    assert dashboard["lifecycle_distribution"]["mean_total_tokens"] is None


@pytest.mark.asyncio
async def test_executor_root_and_tenant_scope_are_distinct(relational_db):
    await seed_task(relational_db)
    await seed_task(
        relational_db,
        task="foreign",
        branch="foreign-b",
        tenant="other",
        calls=[("foreign-c", "A", 900, "2026-10-12T13:00:00Z")],
    )
    service = service_for(relational_db)
    assert (await service.dashboard(SCOPE, filters(agent_id="executor")))[
        "window_attempt_usage"
    ]["known_total_tokens"] == 50
    assert (await service.dashboard(SCOPE, filters(agent_id="root-agent")))[
        "window_attempt_usage"
    ]["observed_call_count"] == 0
    assert (
        await service.dashboard(SCOPE, filters(root_agent_id="root-agent"))
    )["window_attempt_usage"]["known_total_tokens"] == 50
    assert await service.task("foreign", SCOPE, filters()) is None
    assert await service.context("foreign", SCOPE, filters()) is None


@pytest.mark.asyncio
async def test_missing_child_partial_usage_and_branch_freshness(relational_db):
    await seed_task(relational_db, expected_child_branches=["missing"])
    relational_db.execute(
        "UPDATE swe_token_model_calls SET output_tokens=NULL,usage_status='partial' WHERE call_id='today-b'",
    )
    service = service_for(relational_db)
    task = await service.task("task", SCOPE, filters())
    assert task["completeness"]["capture_state"] == "incomplete"
    assert task["completeness"]["usage_complete"] is False
    assert task["lifecycle_usage"]["actual_total_tokens"] is None
    assert task["lifecycle_usage"]["known_total_tokens"] == 60
    assert (await service.dashboard(SCOPE, filters()))[
        "lifecycle_distribution"
    ]["sample_count"] == 0
    assert "data_revision" not in task["completeness"]
    assert task["branches"][0]["freshness"] in {"pending", "stale"}


@pytest.mark.asyncio
async def test_global_aggregate_does_not_depend_on_page_limit_and_half_open_window(
    relational_db,
):
    for index in range(4):
        await seed_task(
            relational_db,
            task=f"t{index}",
            branch=f"b{index}",
            calls=[(f"c{index}", "A", index, "2026-10-12T12:00:00Z")],
        )
    await seed_task(
        relational_db,
        task="end",
        branch="end-b",
        started_at="2026-10-13T00:00:00Z",
        calls=[("end-c", "A", 100, "2026-10-13T00:00:00Z")],
    )
    service = service_for(relational_db)
    result = await service.dashboard(SCOPE, filters(limit=1))
    assert result["window_attempt_usage"]["known_total_tokens"] == 6
    assert result["lifecycle_distribution"]["sample_count"] == 4
    assert len((await service.tasks(SCOPE, filters(limit=1)))["items"]) == 1


@pytest.mark.asyncio
async def test_cursors_bind_scope_filters_and_tie_breaking(relational_db):
    await seed_task(relational_db)
    service = service_for(relational_db)
    first = await service.calls("task", SCOPE, filters(limit=1))
    second = await service.calls(
        "task",
        SCOPE,
        replace(filters(limit=1), cursor=first["next_cursor"]),
    )
    assert first["items"][0]["call_id"] != second["items"][0]["call_id"]
    for scope, selected in [
        (SCOPE, filters(limit=1, model_name="A")),
        (("source", "other"), filters(limit=1)),
    ]:
        with pytest.raises(ValueError, match="cursor"):
            await service.calls(
                "task",
                scope,
                replace(selected, cursor=first["next_cursor"]),
            )
    bad = base64.urlsafe_b64encode(
        json.dumps(["time", "id"]).encode(),
    ).decode()
    with pytest.raises(ValueError, match="cursor"):
        await service.calls("task", SCOPE, filters(cursor=bad))


def test_filter_validation_and_default_window():
    from monitor.app.services.token_diagnostics.diagnostic_filters import (
        DiagnosticFilters,
        validate_identity,
    )
    from monitor.app.services.token_diagnostics.diagnostic_filters import (
        DiagnosticScope,
    )

    assert DiagnosticScope("source", "t" * 100).tenant_id == "t" * 100
    default = DiagnosticFilters()
    assert default.end - default.start == timedelta(hours=24)
    for kwargs in [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"start": NOW, "end": NOW},
        {"start": NOW - timedelta(days=31), "end": NOW},
        {"start": "2026-10-12T00:00:00", "end": NOW},
    ]:
        with pytest.raises(ValueError):
            DiagnosticFilters(**kwargs)
    with pytest.raises(ValueError):
        validate_identity("x" * 65)


async def seed_evaluation(
    db,
    identity,
    branch="branch",
    revision=None,
    reliable=True,
    signature="sig",
    state="candidate",
    created="2026-10-12 12:00:00",
):
    from monitor.app.services.token_diagnostics.store import insert_record

    cursor = RelationalCursor(db)
    if revision is None:
        revision = db.execute(
            "SELECT data_revision FROM swe_token_runs WHERE branch_run_id=?",
            (branch,),
        ).fetchone()["data_revision"]
    common = {
        "source_id": "source",
        "tenant_id": "tenant",
        "task_run_id": "task",
        "branch_run_id": branch,
        "evaluation_id": identity,
        "object_type": "context",
        "object_id": identity,
        "rule_id": "context_growth",
        "rule_version": 1,
        "threshold_version": "v1",
        "input_revision": revision,
        "snapshot_id": "snapshot",
        "state": state,
        "created_at": datetime.fromisoformat(created),
    }
    await insert_record(cursor, "swe_token_diagnostic_evaluations", common)
    await insert_record(
        cursor,
        "swe_token_diagnostic_heads",
        {**common, "updated_at": created},
    )
    await insert_record(
        cursor,
        "swe_token_diagnostic_findings",
        {
            **common,
            "finding_id": "f-" + identity,
            "instance_key": identity,
            "signature": signature,
            "signature_version": "v1",
            "reliable_grouping": reliable,
            "recommendation_code": "dedupe",
            "agent_id": "executor",
            "model_name": "A",
        },
    )
    return common


@pytest.mark.asyncio
async def test_issues_group_reliable_signatures_and_exclude_stale_heads(
    relational_db,
):
    await seed_task(relational_db)
    await seed_evaluation(relational_db, "e1")
    await seed_evaluation(relational_db, "e2", created="2026-10-12 13:00:00")
    await seed_evaluation(relational_db, "unknown1", reliable=False)
    await seed_evaluation(relational_db, "unknown2", reliable=False)
    await seed_evaluation(relational_db, "stale", revision=0)
    service = service_for(relational_db)
    result = await service.issues(SCOPE, filters())
    assert len(result["items"]) == 3
    reliable = next(row for row in result["items"] if row["reliable_grouping"])
    assert reliable["hit_count"] == 2 and reliable["distinct_task_count"] == 1
    assert reliable["first_seen"].startswith("2026-10-12")
    instances = await service.instances(
        reliable["group_id"],
        SCOPE,
        filters(limit=1),
    )
    assert len(instances["items"]) == 1 and instances["next_cursor"]
    history = await service.evaluation("stale", SCOPE, filters())
    assert history["is_current"] is False
    assert (
        await service.evaluation("e1", ("source", "other"), filters()) is None
    )


@pytest.mark.asyncio
async def test_context_pages_and_operation_comparison_preserve_nulls(
    relational_db,
):
    from swe.token_diagnostics.store import TokenStore
    from tests.unit.token_diagnostics.test_b_storage import context

    await seed_task(relational_db)
    cursor = RelationalCursor(relational_db)
    writer = TokenStore()
    for identity, total in [("before", 100), ("after", 0)]:
        await writer._write_record(
            cursor,
            {
                "kind": "context",
                "data": context(
                    context_id=identity,
                    estimated_tokens=total,
                    estimator_version="same",
                    model_name="A",
                    agent_id="executor",
                    occurrences=[
                        {
                            "occurrence_index": index,
                            "category": "user",
                            "text_start": index,
                            "text_end": index + 1,
                        }
                        for index in range(3)
                    ],
                    expected_item_count=3,
                ),
            },
        )
    await writer._write_record(
        cursor,
        {
            "kind": "context_operation",
            "data": facts(
                operation_id="op",
                operation_type="compaction",
                operation_state="completed",
                before_context_id="before",
                after_context_id="after",
                missing_before=False,
                missing_after=False,
                capture_state="complete",
                segments=[
                    {
                        "relation_index": index,
                        "relation_type": "retained",
                        "before_context_id": "before",
                        "before_occurrence_index": index,
                        "after_context_id": "after",
                        "after_occurrence_index": index,
                    }
                    for index in range(3)
                ],
            ),
        },
    )
    service = service_for(relational_db)
    saved = await service.context("before", SCOPE, filters(limit=1))
    assert (
        len(saved["occurrences"]["items"]) == 1
        and saved["occurrences"]["next_cursor"]
    )
    next_page = await service.context(
        "before",
        SCOPE,
        filters(limit=1, cursor=saved["occurrences"]["next_cursor"]),
    )
    assert next_page["occurrences"]["items"][0]["occurrence_index"] == 1
    operations = await service.operations("task", SCOPE, filters(limit=1))
    op = operations["items"][0]
    assert op["comparison"]["saved_estimated_tokens"] == 100
    assert op["comparison"]["after_estimated_tokens"] == 0
    assert op["segments"]["next_cursor"]
    relational_db.execute(
        "UPDATE swe_token_contexts SET estimator_version='different' WHERE context_id='after'",
    )
    op = (await service.operations("task", SCOPE, filters(limit=1)))["items"][
        0
    ]
    assert op["comparison"]["saved_estimated_tokens"] is None
    assert op["comparison"]["reason_code"] == "incompatible_estimator"


@pytest.mark.asyncio
async def test_prefix_returns_saved_candidates_without_sensitive_domains(
    relational_db,
):
    from monitor.app.services.token_diagnostics.diagnostic_storage import (
        write_prefix_comparison,
    )

    await seed_task(relational_db)
    cursor = RelationalCursor(relational_db)
    data = {
        "task_run_id": "task",
        "branch_run_id": "branch",
        "source_id": "source",
        "tenant_id": "tenant",
        "current_context_id": "ctx",
        "current_context_revision": 1,
        "call_id": "today-a",
        "snapshot_id": "snapshot",
        "input_revision": 1,
        "representation_version": "v1",
        "key_version": "key-version",
        "compatibility_digest": "secret-domain-digest",
        "window_hours": 24,
        "candidate_limit": 100,
        "candidate_count": 1,
        "candidate_limited": False,
        "selection_version": "v1",
        "lower_text_bytes": 0,
        "upper_text_bytes": 10,
        "total_text_bytes": 10,
        "analysis_state": "ready",
        "eligible": True,
        "created_at": "2026-10-12 12:00:00",
        "candidates": [
            {
                "historical_context_id": "history",
                "historical_context_revision": 2,
                "historical_call_id": "h-call",
                "historical_ended_at": "2026-10-11 12:00:00",
                "lower_text_bytes": 0,
                "upper_text_bytes": 10,
            },
        ],
    }
    await write_prefix_comparison(cursor, data)
    saved = await service_for(relational_db).prefix(
        "today-a",
        SCOPE,
        filters(),
    )
    assert saved["comparison"]["lower_text_bytes"] == 0
    assert (
        saved["candidates"]["items"][0]["historical_context_id"] == "history"
    )
    assert "compatibility_digest" not in json.dumps(saved)
    assert "secret-domain-digest" not in json.dumps(saved)
    assert (
        await service_for(relational_db).prefix(
            "today-a",
            ("source", "other"),
            filters(),
        )
        is None
    )


@pytest.mark.asyncio
async def test_invalid_cursor_types_are_rejected_before_any_database_read():
    from monitor.app.services.token_diagnostics.diagnostic_filters import (
        encode_cursor,
    )
    from monitor.app.services.token_diagnostics.diagnostic_queries import (
        DiagnosticQueryService,
    )

    class NoReadStore:
        def transaction(self):
            raise AssertionError("cursor validation must precede reads")

    service = DiagnosticQueryService(NoReadStore())
    valid = encode_cursor(SCOPE, filters(), "context:ctx", NOW, "0")
    value = json.loads(base64.urlsafe_b64decode(valid))
    for key, bad in [
        ("sort_id", "-1"),
        ("sort_id", "0 OR 1=1"),
        ("sort_time", "not-time"),
        ("scope_hash", []),
        ("filter_hash", "foreign"),
    ]:
        token = base64.urlsafe_b64encode(
            json.dumps({**value, key: bad}).encode(),
        ).decode()
        with pytest.raises(ValueError, match="cursor"):
            await service.context("ctx", SCOPE, filters(cursor=token))


@pytest.mark.asyncio
async def test_all_reported_unknown_field_is_not_complete_and_zero_task_is_complete(
    relational_db,
):
    await seed_task(relational_db)
    relational_db.execute(
        "UPDATE swe_token_model_calls SET input_tokens=NULL WHERE call_id='today-b'",
    )
    service = service_for(relational_db)
    task = await service.task("task", SCOPE, filters())
    assert task["completeness"]["usage_complete"] is False
    assert task["lifecycle_usage"]["known_total_tokens"] == 30
    assert task["lifecycle_usage"]["actual_total_tokens"] is None
    await seed_task(relational_db, task="zero", branch="zero-b", calls=[])
    zero = await service.task("zero", SCOPE, filters())
    assert zero["lifecycle_usage"]["actual_total_tokens"] == 0
    assert zero["completeness"]["usage_complete"] is True


@pytest.mark.asyncio
async def test_old_evaluation_after_head_replacement_is_historical(
    relational_db,
):
    await seed_task(relational_db)
    await seed_evaluation(relational_db, "old")
    await seed_evaluation(relational_db, "new")
    relational_db.execute(
        "DELETE FROM swe_token_diagnostic_heads WHERE object_id='old'",
    )
    service = service_for(relational_db)
    assert (await service.evaluation("old", SCOPE, filters()))[
        "is_current"
    ] is False
    assert (await service.evaluation("new", SCOPE, filters()))[
        "is_current"
    ] is True
    assert (
        sum(
            row["hit_count"]
            for row in (await service.issues(SCOPE, filters()))["items"]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_context_filters_root_scope_and_pending_prefix(relational_db):
    from swe.token_diagnostics.store import TokenStore
    from tests.unit.token_diagnostics.test_b_storage import context

    await seed_task(relational_db)
    await TokenStore()._write_record(
        RelationalCursor(relational_db),
        {
            "kind": "context",
            "data": context(agent_id="executor", model_name="A"),
        },
    )
    service = service_for(relational_db)
    assert (
        await service.context("ctx", SCOPE, filters(agent_id="foreign-agent"))
        is None
    )
    assert (
        await service.context(
            "ctx",
            SCOPE,
            filters(root_agent_id="foreign-root"),
        )
        is None
    )
    assert (await service.prefix("today-b", SCOPE, filters()))[
        "reason_code"
    ] == "analysis_pending"


def test_rule_and_state_filters_are_allowlisted():
    from monitor.app.services.token_diagnostics.diagnostic_filters import (
        DiagnosticFilters,
    )

    for kwargs in [{"rule_id": "invented"}, {"state": "invented"}]:
        with pytest.raises(ValueError):
            DiagnosticFilters(**kwargs)


@pytest.mark.asyncio
async def test_dashboard_agent_rule_and_prefix_metrics_are_global(
    relational_db,
):
    from swe.token_diagnostics.store import TokenStore
    from tests.unit.token_diagnostics.test_b_storage import context
    from monitor.app.services.token_diagnostics.diagnostic_storage import (
        write_prefix_comparison,
    )

    await seed_task(relational_db)
    await seed_evaluation(relational_db, "e1")
    await seed_evaluation(relational_db, "stale", revision=0)
    cursor = RelationalCursor(relational_db)
    await TokenStore()._write_record(
        cursor,
        {
            "kind": "context",
            "data": context(
                context_id="ctx",
                call_id="today-a",
                estimated_tokens=20,
                text_bytes=100,
                model_name="A",
                agent_id="executor",
            ),
        },
    )
    revision = relational_db.execute(
        "SELECT data_revision FROM swe_token_runs WHERE branch_run_id='branch'",
    ).fetchone()["data_revision"]
    # Context arrival made previous heads stale; keep one current head explicitly.
    relational_db.execute(
        "UPDATE swe_token_diagnostic_heads SET input_revision=? WHERE object_id='e1'",
        (revision,),
    )
    relational_db.execute(
        "UPDATE swe_token_diagnostic_evaluations SET input_revision=? WHERE evaluation_id='e1'",
        (revision,),
    )
    await write_prefix_comparison(
        cursor,
        {
            "task_run_id": "task",
            "branch_run_id": "branch",
            "source_id": "source",
            "tenant_id": "tenant",
            "current_context_id": "ctx",
            "current_context_revision": 1,
            "call_id": "today-a",
            "snapshot_id": "metrics",
            "input_revision": revision,
            "representation_version": "client-ordered-text-v1",
            "candidate_count": 1,
            "candidate_limit": 100,
            "candidate_limited": False,
            "lower_text_bytes": 50,
            "upper_text_bytes": 60,
            "total_text_bytes": 100,
            "analysis_state": "ready",
            "eligible": True,
            "created_at": datetime(2026, 10, 12, 13),
            "candidates": [
                {
                    "historical_context_id": "old",
                    "historical_context_revision": 1,
                    "lower_text_bytes": 50,
                    "upper_text_bytes": 60,
                },
            ],
        },
    )
    result = await service_for(relational_db).dashboard(
        SCOPE,
        filters(limit=1),
    )
    assert (
        result["agent_attribution"]["executing_agents"]["items"][0]["agent_id"]
        == "executor"
    )
    assert (
        result["agent_attribution"]["executing_agents"]["items"][0]["usage"][
            "known_total_tokens"
        ]
        == 50
    )
    assert (
        result["agent_attribution"]["root_agents"]["items"][0]["agent_id"]
        == "root-agent"
    )
    assert len(result["rule_coverage"]) == 8
    growth = next(
        r for r in result["rule_coverage"] if r["rule_id"] == "context_growth"
    )
    assert growth["candidate"] == 1
    assert result["prefix"]["ratio"] == 0.5
    assert result["prefix"]["request_count"] == 1
    assert sum(result["prefix"]["excluded"].values()) == 1
    prefix = await service_for(relational_db).prefix(
        "today-a",
        SCOPE,
        filters(),
    )
    assert prefix["is_current"] is True
    relational_db.execute(
        "UPDATE swe_token_runs SET data_revision=data_revision+1",
    )
    prefix = await service_for(relational_db).prefix(
        "today-a",
        SCOPE,
        filters(),
    )
    assert prefix["is_current"] is False
    assert prefix["reason_code"] == "stale_analysis"


@pytest.mark.asyncio
async def test_calls_paginate_in_logical_attempt_order_and_error_is_terminal(
    relational_db,
):
    await seed_task(relational_db)
    relational_db.execute(
        "UPDATE swe_token_model_calls SET logical_seq=2,attempt_no=1,execution_state='error' WHERE call_id='today-a'",
    )
    relational_db.execute(
        "UPDATE swe_token_model_calls SET logical_seq=1,attempt_no=2 WHERE call_id='today-b'",
    )
    service = service_for(relational_db)
    first = await service.calls("task", SCOPE, filters(limit=1))
    assert first["items"][0]["call_id"] == "today-b"
    second = await service.calls(
        "task",
        SCOPE,
        filters(limit=1, cursor=first["next_cursor"]),
    )
    assert second["items"][0]["call_id"] == "today-a"
    assert (await service.task("task", SCOPE, filters()))["completeness"][
        "usage_complete"
    ] is True


@pytest.mark.asyncio
async def test_prefix_expired_reference_and_version_metadata_are_explicit(
    relational_db,
):
    from monitor.app.services.token_diagnostics.diagnostic_storage import (
        write_prefix_comparison,
    )

    await seed_task(relational_db)
    await write_prefix_comparison(
        RelationalCursor(relational_db),
        {
            "task_run_id": "task",
            "branch_run_id": "branch",
            "source_id": "source",
            "tenant_id": "tenant",
            "current_context_id": "ctx",
            "current_context_revision": 1,
            "call_id": "today-a",
            "snapshot_id": "snapshot",
            "input_revision": 1,
            "representation_version": "v1",
            "key_version": "version-only",
            "candidate_count": 1,
            "candidate_limit": 100,
            "candidate_limited": False,
            "lower_text_bytes": 0,
            "upper_text_bytes": 10,
            "total_text_bytes": 10,
            "analysis_state": "ready",
            "eligible": True,
            "created_at": datetime(2026, 10, 12, 13),
            "candidates": [
                {
                    "historical_context_id": "expired",
                    "historical_context_revision": 1,
                    "lower_text_bytes": 0,
                    "upper_text_bytes": 10,
                },
            ],
        },
    )
    saved = await service_for(relational_db).prefix(
        "today-a",
        SCOPE,
        filters(),
    )
    assert saved["comparison"]["key_version"] == "version-only"
    assert saved["candidates"]["items"][0]["evidence_expired"] is True
    assert saved["evidence_expired"] is True


@pytest.mark.asyncio
async def test_operation_exposes_pair_headers_summary_usage_and_relation_pages(
    relational_db,
):
    from swe.token_diagnostics.store import TokenStore
    from tests.unit.token_diagnostics.test_b_storage import context

    await seed_task(relational_db)
    cursor = RelationalCursor(relational_db)
    writer = TokenStore()
    for identity in ("before", "after"):
        await writer._write_record(
            cursor,
            {
                "kind": "context",
                "data": context(
                    context_id=identity,
                    estimated_tokens=10,
                    estimator_version="same",
                    model_name="A",
                    agent_id="executor",
                ),
            },
        )
    await writer._write_record(
        cursor,
        {
            "kind": "context_operation",
            "data": facts(
                operation_id="op",
                operation_type="compaction",
                operation_state="completed",
                before_context_id="before",
                after_context_id="after",
                summary_call_id="today-a",
                missing_before=False,
                missing_after=False,
                capture_state="complete",
                segments=[
                    {
                        "relation_index": index,
                        "relation_type": "retained",
                        "before_context_id": "before",
                        "before_occurrence_index": 0,
                        "after_context_id": "after",
                        "after_occurrence_index": 0,
                    }
                    for index in range(3)
                ],
            ),
        },
    )
    service = service_for(relational_db)
    saved = (await service.operations("task", SCOPE, filters(limit=1)))[
        "items"
    ][0]
    assert saved["before_context"]["capture_state"] == "complete"
    assert saved["after_context"]["estimated_tokens"] == 10
    assert saved["summary_call"]["input_tokens"] == 20
    page = await service.operations(
        "task",
        SCOPE,
        filters(limit=1),
        operation_id="op",
        relation_cursor=saved["segments"]["next_cursor"],
    )
    assert page["items"][0]["segments"]["items"][0]["relation_index"] == 1


@pytest.mark.asyncio
async def test_rule_filter_selects_tasks_using_current_heads(relational_db):
    await seed_task(relational_db)
    service = service_for(relational_db)
    assert (await service.tasks(SCOPE, filters(rule_id="context_growth")))[
        "items"
    ] == []
    await seed_evaluation(relational_db, "current")
    assert (
        len(
            (await service.tasks(SCOPE, filters(rule_id="context_growth")))[
                "items"
            ],
        )
        == 1
    )
    relational_db.execute(
        "UPDATE swe_token_runs SET data_revision=data_revision+1",
    )
    assert (await service.tasks(SCOPE, filters(rule_id="context_growth")))[
        "items"
    ] == []


@pytest.mark.asyncio
async def test_task_population_uses_root_window_model_membership_uses_lifecycle(
    relational_db,
):
    await seed_task(relational_db, started_at="2026-10-11T11:00:00Z")
    service = service_for(relational_db)
    assert (await service.tasks(SCOPE, filters()))["items"] == []
    assert (await service.dashboard(SCOPE, filters()))["window_attempt_usage"][
        "known_total_tokens"
    ] == 50
    for index in range(10):
        await seed_task(
            relational_db,
            task=f"t{index}",
            branch=f"b{index}",
            calls=[
                (f"c{index}", "previous-model", index, "2026-10-11T12:00:00Z"),
            ],
        )
    selected = filters(model_name="previous-model", limit=1)
    page = await service.tasks(SCOPE, selected)
    assert len(page["items"]) == 1
    assert (
        page["items"][0]["selected_contribution"]["observed_call_count"] == 1
    )
    assert page["items"][0]["window_attempt_usage"]["observed_call_count"] == 0
    dashboard = await service.dashboard(SCOPE, selected)
    assert dashboard["lifecycle_distribution"]["task_count"] == 10
    assert dashboard["lifecycle_distribution"]["sample_count"] == 10
    assert dashboard["lifecycle_distribution"]["mean_total_tokens"] == 4.5


@pytest.mark.asyncio
async def test_completeness_filter_is_actual_lifecycle_state(relational_db):
    await seed_task(relational_db)
    await seed_task(
        relational_db,
        task="missing",
        branch="missing-b",
        calls=[],
        expected_child_branches=["unseen"],
    )
    service = service_for(relational_db)
    assert [
        r["task_run_id"]
        for r in (await service.tasks(SCOPE, filters(state="complete")))[
            "items"
        ]
    ] == ["task"]
    assert [
        r["task_run_id"]
        for r in (await service.tasks(SCOPE, filters(state="incomplete")))[
            "items"
        ]
    ] == ["missing"]


@pytest.mark.asyncio
async def test_branch_business_steps_and_expired_evaluation_reference(
    relational_db,
):
    from swe.token_diagnostics.store import TokenStore
    from monitor.app.services.token_diagnostics.store import insert_record

    await seed_task(relational_db)
    cursor = RelationalCursor(relational_db)
    for identity, source in [
        ("business-event", "business"),
        ("random-event", "request"),
    ]:
        await TokenStore()._write_record(
            cursor,
            {
                "kind": "event",
                "data": facts(
                    event_id=identity,
                    event_seq=2,
                    event_type="tool_completed",
                    step_id=identity,
                    identity_source=source,
                    started_at="2026-10-12T13:00:00Z",
                    ended_at="2026-10-12T13:01:00Z",
                ),
            },
        )
    await seed_evaluation(relational_db, "refs")
    await insert_record(
        cursor,
        "swe_token_diagnostic_refs",
        {
            "evaluation_id": "refs",
            "source_id": "source",
            "tenant_id": "tenant",
            "task_run_id": "task",
            "branch_run_id": "branch",
            "reference_index": 0,
            "reference_type": "context",
            "object_id": "gone",
            "context_id": "gone",
        },
    )
    service = service_for(relational_db)
    branches = await service.branches("task", SCOPE, filters())
    steps = branches["items"][0]["steps"]
    assert [step["step_id"] for step in steps["items"]] == ["business-event"]
    assert steps["order_known"] is False
    evaluation = await service.evaluation("refs", SCOPE, filters())
    assert evaluation["refs"][0]["evidence_expired"] is True
    assert evaluation["recommendation"]["prerequisites"]
    assert evaluation["recommendation"]["verification"]


def test_utc_overflow_and_default_underflow_are_value_errors():
    from monitor.app.services.token_diagnostics.diagnostic_filters import (
        DiagnosticFilters,
    )

    for end in (
        "9999-12-31T23:59:59-23:59",
        "0001-01-01T00:00:00+23:59",
        "0001-01-01T00:00:00Z",
    ):
        with pytest.raises(ValueError, match="window"):
            DiagnosticFilters(end=end)


@pytest.mark.asyncio
async def test_context_composition_is_global_and_preserves_unknown_estimates(
    relational_db,
):
    from swe.token_diagnostics.store import TokenStore
    from tests.unit.token_diagnostics.test_b_storage import context

    await seed_task(relational_db)
    await TokenStore()._write_record(
        RelationalCursor(relational_db),
        {
            "kind": "context",
            "data": context(
                expected_item_count=3,
                occurrences=[
                    {
                        "occurrence_index": 0,
                        "category": "user",
                        "estimated_tokens": 10,
                    },
                    {
                        "occurrence_index": 1,
                        "category": "user",
                        "estimated_tokens": 20,
                    },
                    {
                        "occurrence_index": 2,
                        "category": "tool_result",
                        "estimated_tokens": None,
                    },
                ],
            ),
        },
    )
    saved = await service_for(relational_db).context(
        "ctx",
        SCOPE,
        filters(limit=1),
    )
    assert len(saved["occurrences"]["items"]) == 1
    assert saved["composition"]["occurrence_count"] == 3
    assert saved["composition"]["complete"] is False
    assert saved["composition"]["estimated_tokens"] is None
    assert saved["composition"]["known_estimated_tokens"] == 30
    assert (
        next(
            row
            for row in saved["composition"]["categories"]
            if row["category"] == "user"
        )["estimated_tokens"]
        == 30
    )


@pytest.mark.asyncio
async def test_distribution_quantiles_use_all_eligible_tasks_not_page(
    relational_db,
):
    for index in range(10):
        await seed_task(
            relational_db,
            task=f"t{index}",
            branch=f"b{index}",
            calls=[(f"c{index}", "A", index * 10, "2026-10-12T12:00:00Z")],
        )
    quantiles = (
        await service_for(relational_db).dashboard(SCOPE, filters(limit=1))
    )["lifecycle_distribution"]["quantiles"]
    assert quantiles["available"] is True
    assert quantiles["sample_count"] == 10
    assert quantiles["p50"] == 40 and quantiles["p90"] == 80


@pytest.mark.asyncio
async def test_completed_analysis_without_context_is_missing_evidence_not_pending(
    relational_db,
):
    await seed_task(relational_db)
    relational_db.execute(
        "UPDATE swe_token_runs SET analysis_state='ready',analyzed_revision=data_revision",
    )
    prefix = (await service_for(relational_db).dashboard(SCOPE, filters()))[
        "prefix"
    ]
    assert prefix["excluded"]["missing_evidence"] == 2
    assert prefix["excluded"]["pending"] == 0


@pytest.mark.asyncio
async def test_estimates_with_unknown_composition_attribution_do_not_prove_denominator(
    relational_db,
):
    from swe.token_diagnostics.store import TokenStore
    from tests.unit.token_diagnostics.test_b_storage import context

    await seed_task(relational_db)
    await TokenStore()._write_record(
        RelationalCursor(relational_db),
        {
            "kind": "context",
            "data": context(
                expected_item_count=1,
                occurrences=[
                    {
                        "occurrence_index": 0,
                        "category": None,
                        "attribution": "unknown",
                        "estimated_tokens": 10,
                    },
                ],
            ),
        },
    )
    composition = (
        await service_for(relational_db).context("ctx", SCOPE, filters())
    )["composition"]
    assert composition["known_estimated_tokens"] == 10
    assert composition["complete"] is False
    assert composition["estimated_tokens"] is None
    assert composition["unknown_attribution_count"] == 1
    assert composition["reason_code"] == "untrusted_composition"


@pytest.mark.asyncio
async def test_three_sample_nearest_rank_quantiles_and_expired_operation_context(
    relational_db,
):
    from swe.token_diagnostics.store import TokenStore

    for index in range(3):
        await seed_task(
            relational_db,
            task=f"t{index}",
            branch=f"b{index}",
            calls=[
                (f"c{index}", "A", (index + 1) * 10, "2026-10-12T12:00:00Z"),
            ],
        )
    service = service_for(relational_db)
    quantiles = (await service.dashboard(SCOPE, filters(limit=1)))[
        "lifecycle_distribution"
    ]["quantiles"]
    assert (
        quantiles["p50"] == 20
        and quantiles["p90"] == 30
        and quantiles["p95"] == 30
    )
    await TokenStore()._write_record(
        RelationalCursor(relational_db),
        {
            "kind": "context_operation",
            "data": facts(
                task_run_id="t0",
                branch_run_id="b0",
                operation_id="expired-op",
                operation_type="compaction",
                operation_state="completed",
                before_context_id="gone",
                missing_before=False,
                missing_after=True,
                capture_state="complete",
                segments=[],
            ),
        },
    )
    operation = (await service.operations("t0", SCOPE, filters()))["items"][0]
    assert operation["evidence_expired"]["before_context"] is True
    assert operation["evidence_expired"]["after_context"] is False


@pytest.mark.asyncio
async def test_operations_cursor_binds_optional_operation_id_before_reads():
    from monitor.app.services.token_diagnostics.diagnostic_filters import (
        encode_cursor,
    )
    from monitor.app.services.token_diagnostics.diagnostic_queries import (
        DiagnosticQueryService,
    )

    class NoReadStore:
        def transaction(self):
            raise AssertionError("cursor validation must precede reads")

    token = encode_cursor(SCOPE, filters(), "operations:task", NOW, "op")
    with pytest.raises(ValueError, match="cursor"):
        await DiagnosticQueryService(NoReadStore()).operations(
            "task",
            SCOPE,
            filters(cursor=token),
            operation_id="op",
        )


@pytest.mark.asyncio
async def test_prefix_detail_distinguishes_completed_missing_evidence_and_failure(
    relational_db,
):
    await seed_task(relational_db)
    service = service_for(relational_db)
    relational_db.execute(
        "UPDATE swe_token_runs SET analysis_state='ready',analyzed_revision=data_revision",
    )
    assert (await service.prefix("today-a", SCOPE, filters()))[
        "reason_code"
    ] == "missing_evidence"
    relational_db.execute("UPDATE swe_token_runs SET analysis_state='failed'")
    assert (await service.prefix("today-a", SCOPE, filters()))[
        "reason_code"
    ] == "analysis_failed"


async def seed_locator(
    db,
    evaluation_id,
    object_type,
    object_id,
    rule_id,
    *,
    branch="branch",
    revision=None,
    state="insufficient_evidence",
    tenant="tenant",
):
    from monitor.app.services.token_diagnostics.store import insert_record

    revision = (
        revision
        if revision is not None
        else db.execute(
            "SELECT data_revision FROM swe_token_runs WHERE branch_run_id=? AND tenant_id=?",
            (branch, tenant),
        ).fetchone()["data_revision"]
    )
    data = {
        "evaluation_id": evaluation_id,
        "task_run_id": "task",
        "branch_run_id": branch,
        "source_id": "source",
        "tenant_id": tenant,
        "object_type": object_type,
        "object_id": object_id,
        "rule_id": rule_id,
        "rule_version": 1,
        "threshold_version": 1,
        "input_revision": revision,
        "snapshot_id": "frozen",
        "state": state,
        "created_at": datetime(2026, 10, 12, 13),
        "updated_at": datetime(2026, 10, 12, 13),
    }
    cursor = RelationalCursor(db)
    await insert_record(cursor, "swe_token_diagnostic_evaluations", data)
    await insert_record(cursor, "swe_token_diagnostic_heads", data)


@pytest.mark.asyncio
async def test_call_locators_keep_exact_object_nonhits_and_stale_head_scope(
    relational_db,
):
    await seed_task(
        relational_db,
        calls=[
            ("today-a", "A", 10, "2026-10-12T12:00:00Z"),
            ("today-b", "B", 20, "2026-10-12T13:00:00Z"),
            ("today-c", "C", 30, "2026-10-12T14:00:00Z"),
        ],
    )
    await seed_task(
        relational_db,
        branch="foreign-branch",
        tenant="foreign",
        calls=[("foreign-call", "A", 999, "2026-10-12T12:00:00Z")],
    )
    for call in ("today-a", "today-b", "today-c"):
        for rule in ("text_prefix_stability", "static_context_volume"):
            await seed_locator(
                relational_db,
                f"{call}-{rule}",
                "call",
                call,
                rule,
                state=(
                    "not_hit" if call == "today-a" else "insufficient_evidence"
                ),
            )
    await seed_locator(
        relational_db,
        "branch-locator",
        "branch",
        "branch",
        "context_growth",
    )
    await seed_locator(
        relational_db,
        "foreign-locator",
        "call",
        "today-a",
        "context_growth",
        tenant="foreign",
        branch="foreign-branch",
    )
    rows = (await service_for(relational_db).calls("task", SCOPE, filters()))[
        "items"
    ]
    assert len(rows) == 3
    for row in rows:
        locators = row["evaluations"]
        assert locators["limited"] is False
        assert len(locators["items"]) == 2
        assert {loc["object_id"] for loc in locators["items"]} == {
            row["call_id"],
        }
        assert all(loc["freshness"] == "current" for loc in locators["items"])
        assert {loc["evaluation_id"] for loc in locators["items"]} == {
            f"{row['call_id']}-text_prefix_stability",
            f"{row['call_id']}-static_context_volume",
        }
    relational_db.execute(
        "UPDATE swe_token_runs SET data_revision=data_revision+1 WHERE tenant_id='tenant'",
    )
    stale = (await service_for(relational_db).calls("task", SCOPE, filters()))[
        "items"
    ][0]["evaluations"]["items"]
    assert all(loc["freshness"] == "stale" for loc in stale)
    assert all(
        loc["input_revision"] != loc["current_data_revision"] for loc in stale
    )


@pytest.mark.asyncio
async def test_branch_and_observed_business_step_locators_are_not_call_locators(
    relational_db,
):
    from swe.token_diagnostics.store import TokenStore

    await seed_task(relational_db)
    await TokenStore()._write_record(
        RelationalCursor(relational_db),
        {
            "kind": "event",
            "data": facts(
                event_id="business",
                event_type="tool_completed",
                step_id="step-business",
                identity_source="business",
                started_at="2026-10-12T12:00:00Z",
                ended_at="2026-10-12T13:00:00Z",
            ),
        },
    )
    await seed_locator(
        relational_db,
        "growth",
        "branch",
        "branch",
        "context_growth",
        state="not_hit",
    )
    await seed_locator(
        relational_db,
        "carry",
        "branch",
        "branch",
        "segment_carryover",
    )
    await seed_locator(
        relational_db,
        "step-eval",
        "step",
        "step-business",
        "tool_context_volume",
    )
    await seed_locator(
        relational_db,
        "unknown-step",
        "step",
        "not-observed",
        "suspected_loop",
    )
    service = service_for(relational_db)
    branch = (await service.branches("task", SCOPE, filters()))["items"][0]
    assert {
        loc["evaluation_id"] for loc in branch["evaluations"]["items"]
    } == {"growth", "carry"}
    step = branch["steps"]["items"][0]
    assert {loc["evaluation_id"] for loc in step["evaluations"]["items"]} == {
        "step-eval",
    }
    assert (await service.task("task", SCOPE, filters()))["branches"][0][
        "evaluations"
    ]["items"]
    assert all(
        row["evaluations"]["items"] == []
        for row in (await service.calls("task", SCOPE, filters()))["items"]
    )


@pytest.mark.asyncio
async def test_root_execution_state_filter_does_not_include_running_tasks(
    relational_db,
):
    await seed_task(relational_db)
    await seed_task(
        relational_db,
        task="running-task",
        branch="running-branch",
        calls=[],
        execution_state="running",
        ended_at=None,
    )
    service = service_for(relational_db)
    completed = await service.tasks(SCOPE, filters(state="completed"))
    assert [row["task_run_id"] for row in completed["items"]] == ["task"]
    running = await service.tasks(SCOPE, filters(state="running"))
    assert [row["task_run_id"] for row in running["items"]] == ["running-task"]
    assert (await service.tasks(SCOPE, filters(state="failed")))["items"] == []


@pytest.mark.asyncio
async def test_step_reference_cannot_be_revived_by_another_task_same_business_id(
    relational_db,
):
    from swe.token_diagnostics.store import TokenStore
    from monitor.app.services.token_diagnostics.store import insert_record

    await seed_task(relational_db)
    await seed_task(
        relational_db,
        task="other-task",
        branch="other-branch",
        calls=[],
    )
    cursor = RelationalCursor(relational_db)
    await TokenStore()._write_record(
        cursor,
        {
            "kind": "event",
            "data": facts(
                task_run_id="other-task",
                branch_run_id="other-branch",
                event_id="other-event",
                step_id="shared-business-step",
                identity_source="business",
                event_type="tool_completed",
            ),
        },
    )
    await seed_locator(
        relational_db,
        "ref-eval",
        "branch",
        "branch",
        "tool_context_volume",
    )
    await insert_record(
        cursor,
        "swe_token_diagnostic_refs",
        {
            "evaluation_id": "ref-eval",
            "task_run_id": "task",
            "branch_run_id": "branch",
            "source_id": "source",
            "tenant_id": "tenant",
            "reference_index": 0,
            "reference_type": "step",
            "object_id": "shared-business-step",
        },
    )
    reference = (
        await service_for(relational_db).evaluation(
            "ref-eval",
            SCOPE,
            filters(),
        )
    )["refs"][0]
    assert reference["evidence_expired"] is True


async def seed_paged_comparison(db, snapshot):
    from monitor.app.services.token_diagnostics.diagnostic_storage import (
        write_prefix_comparison,
    )

    return await write_prefix_comparison(
        RelationalCursor(db),
        {
            "task_run_id": "task",
            "branch_run_id": "branch",
            "source_id": "source",
            "tenant_id": "tenant",
            "current_context_id": "ctx",
            "current_context_revision": 1,
            "call_id": "today-a",
            "snapshot_id": snapshot,
            "input_revision": 1,
            "candidate_count": 2,
            "candidate_limit": 100,
            "candidate_limited": False,
            "lower_text_bytes": 0,
            "upper_text_bytes": 10,
            "total_text_bytes": 10,
            "analysis_state": "ready",
            "eligible": True,
            "created_at": datetime(
                2026,
                10,
                12,
                13 if snapshot == "old" else 14,
            ),
            "candidates": [
                {
                    "historical_context_id": "history-0",
                    "historical_context_revision": 1,
                    "lower_text_bytes": 0,
                    "upper_text_bytes": 10,
                },
                {
                    "historical_context_id": "history-1",
                    "historical_context_revision": 1,
                    "lower_text_bytes": 0,
                    "upper_text_bytes": 10,
                },
            ],
        },
    )


@pytest.mark.asyncio
async def test_prefix_candidate_cursor_cannot_mix_reanalysis_snapshots(
    relational_db,
):
    await seed_task(relational_db)
    await seed_paged_comparison(relational_db, "old")
    service = service_for(relational_db)
    first = await service.prefix("today-a", SCOPE, filters(limit=1))
    second = await service.prefix(
        "today-a",
        SCOPE,
        filters(limit=1, cursor=first["candidates"]["next_cursor"]),
    )
    assert second["candidates"]["items"][0]["candidate_index"] == 1
    await seed_paged_comparison(relational_db, "new")
    with pytest.raises(ValueError, match="cursor"):
        await service.prefix(
            "today-a",
            SCOPE,
            filters(limit=1, cursor=first["candidates"]["next_cursor"]),
        )


@pytest.mark.asyncio
async def test_insufficient_evidence_analysis_without_comparison_is_not_pending(
    relational_db,
):
    await seed_task(relational_db)
    relational_db.execute(
        "UPDATE swe_token_runs SET analysis_state='insufficient_evidence',analyzed_revision=data_revision",
    )
    service = service_for(relational_db)
    assert (await service.prefix("today-a", SCOPE, filters()))[
        "reason_code"
    ] == "missing_evidence"
    prefix = (await service.dashboard(SCOPE, filters()))["prefix"]
    assert prefix["excluded"]["missing_evidence"] == 2
    assert prefix["excluded"]["pending"] == 0

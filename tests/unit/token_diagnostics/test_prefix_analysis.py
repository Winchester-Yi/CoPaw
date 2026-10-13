# -*- coding: utf-8 -*-
"""Prefix bounds and scoped historical selection use saved client evidence."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from tests.unit.token_diagnostics.test_relational_storage import (
    RelationalCursor,
    relational_db,
)  # noqa: F401


def captured(value, identity="current"):
    from swe.token_diagnostics.context_capture import capture_context
    from swe.token_diagnostics.config import DiagnosticConfig

    config = replace(
        DiagnosticConfig(),
        fingerprint_key="prefix-test",
        enabled=True,
        context_enabled=True,
    )
    return capture_context(
        [{"role": "user", "content": value}],
        [],
        ("source", "tenant"),
        config,
        context_id=identity,
        context_role="prepared",
    )


@pytest.mark.parametrize(
    "left,right,prefix",
    [
        ("a" * 1000 + "X", "a" * 1000 + "Y", 1000),
        ("X" + "a" * 1000, "Y" + "a" * 1000, 0),
        ("a" * 700, "a" * 900, 700),
        ("汉🙂 " * 100 + "X", "汉🙂 " * 100 + "Y", 800),
        ("same", "same", 4),
    ],
)
def test_true_common_text_prefix_is_inside_bounds(left, right, prefix):
    from monitor.app.services.token_diagnostics.prefix_analysis import (
        compare_prefix,
    )

    bounds = compare_prefix(captured(left), captured(right, "history"))
    assert bounds["lower_text_bytes"] <= prefix <= bounds["upper_text_bytes"]
    if left == right:
        assert (
            bounds["lower_text_bytes"] == bounds["upper_text_bytes"] == prefix
        )


def test_mismatched_equal_block_endpoints_keep_safe_lower_bound():
    from monitor.app.services.token_diagnostics.prefix_analysis import (
        compare_prefix,
    )

    left = captured("a" * 245 + "X" * 700)
    right = captured("a" * 245 + "Y" * 700, "history")
    left["blocks"][0]["text_end"] = 256
    left["blocks"][1]["text_start"] = 256
    bounds = compare_prefix(left, right)
    assert bounds["lower_text_bytes"] <= 245


def test_first_block_difference_is_not_fabricated_exact_zero():
    from monitor.app.services.token_diagnostics.prefix_analysis import (
        compare_prefix,
    )

    bounds = compare_prefix(captured("X" * 500), captured("Y" * 500))
    assert bounds["lower_text_bytes"] == 0
    assert bounds["upper_text_bytes"] > 0
    assert bounds["first_difference_index"] == 0


def test_impossible_text_delta_is_unknown_not_an_unsafe_upper_bound():
    from monitor.app.services.token_diagnostics.prefix_analysis import (
        compare_prefix,
    )

    left = captured("a" * 400 + "X" * 700)
    right = captured("a" * 400 + "Y" * 700)
    left["blocks"][1]["text_end"] = 236
    left["blocks"][2]["text_start"] = 236
    assert compare_prefix(left, right)["upper_text_bytes"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("capture_state", "partial"),
        ("key_version", "other"),
        ("representation_version", "other"),
        ("encoded_bytes", -1),
    ],
)
def test_incompatible_or_incomplete_evidence_has_unknown_bounds(field, value):
    from monitor.app.services.token_diagnostics.prefix_analysis import (
        compare_prefix,
    )

    current = captured("abc")
    current[field] = value
    assert compare_prefix(current, captured("abc"))["lower_text_bytes"] is None


def test_aggregate_is_weighted_and_exclusions_are_mutually_exclusive():
    from monitor.app.services.token_diagnostics.prefix_analysis import (
        aggregate_prefix,
    )

    def row(size, lower, **updates):
        return {
            "text_bytes": size,
            "lower_text_bytes": lower,
            "state": "ready",
            "candidate_count": 1,
            "candidate_limited": False,
            "capture_state": "complete",
            **updates,
        }

    result = aggregate_prefix(
        [
            row(100000, 10000),
            row(1000, 900),
            row(100, 0),
            row(20, 0, candidate_count=0),
            row(20, 0, candidate_limited=True, capture_state="partial"),
            row(20, None, state="failed"),
            row(0, 0),
            row(20, None, capture_state="partial", capture_reason="media"),
            row(20, None, state="pending"),
        ],
    )
    assert result["matched_text_bytes"] == 10900
    assert result["eligible_text_bytes"] == 101100
    assert result["ratio"] == 10900 / 101100
    assert result["request_count"] == 3
    assert sum(result["excluded"].values()) == 6
    assert result["excluded"]["candidate_limit"] == 1
    assert result["excluded"]["media"] == 1
    assert aggregate_prefix([])["ratio"] is None


async def save_context(cursor, identity, ended, **updates):
    from swe.token_diagnostics.store import TokenStore
    from tests.unit.token_diagnostics.test_b_storage import context

    data = context(**captured("text " * 100, identity))
    data.update(
        {
            "agent_id": "agent",
            "provider_id": "provider",
            "model_name": "model",
            "assembly_stage": "messages_only",
            "call_id": identity,
            "request_started_at": ended - timedelta(seconds=1),
            "request_ended_at": ended,
            "execution_state": "completed",
            **updates,
        },
    )
    for key in ("request_started_at", "request_ended_at"):
        if isinstance(data[key], datetime):
            data[key] = data[key].isoformat() + "Z"
    await TokenStore()._write_record(cursor, {"kind": "context", "data": data})
    return data


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [99, 100, 101])
async def test_history_limit_order_and_scalar_compatibility(
    relational_db,
    count,
):
    from monitor.app.services.token_diagnostics.prefix_analysis import (
        select_history,
    )

    cursor = RelationalCursor(relational_db)
    start = datetime(2026, 10, 12, 12)
    current = await save_context(
        cursor,
        "current",
        start + timedelta(seconds=1),
    )
    current["request_started_at"] = start
    for index in range(count):
        await save_context(
            cursor,
            f"h{index:03}",
            start - timedelta(minutes=1),
        )
    for identity, updates in (
        ("late", {"request_ended_at": start}),
        ("old", {"request_ended_at": start - timedelta(hours=24, seconds=1)}),
        ("foreign", {"tenant_id": "other", "branch_run_id": "foreign"}),
        ("other_agent", {"agent_id": "other"}),
        ("other_model", {"model_name": "other"}),
        ("other_key", {"key_version": "other"}),
        ("operation", {"context_role": "before"}),
        ("partial", {"capture_state": "partial"}),
    ):
        await save_context(
            cursor,
            identity,
            start - timedelta(minutes=1),
            **updates,
        )
    selected = await select_history(cursor, current)
    assert selected["candidate_limited"] is (count > 100)
    assert len(selected["contexts"]) == min(count, 100)
    assert selected["contexts"][0]["call_id"] == f"h{count - 1:03}"
    assert all(row["call_id"].startswith("h") for row in selected["contexts"])
    assert any(
        "LIMIT %s" in sql and params[-1] == 101
        for sql, params in cursor.queries
    )


@pytest.mark.asyncio
async def test_comparison_snapshot_keeps_distinct_best_references(
    relational_db,
):
    from monitor.app.services.token_diagnostics.prefix_analysis import (
        build_comparison,
    )

    current = {
        **captured("a" * 900),
        "context_revision": 2,
        "source_id": "source",
        "tenant_id": "tenant",
        "task_run_id": "task",
        "branch_run_id": "branch",
        "call_id": "call",
        "request_ended_at": "2026-10-12T12:00:00Z",
        "execution_state": "completed",
    }
    history = [
        {
            **captured("a" * 600 + "X" * 300, "long_lower"),
            "context_revision": 1,
            "call_id": "h1",
            "request_ended_at": "2026-10-12T11:59:00Z",
        },
        {
            **captured("a" * 700 + "Y" * 200, "long_upper"),
            "context_revision": 3,
            "call_id": "h2",
            "request_ended_at": "2026-10-12T11:58:00Z",
        },
    ]
    for row in history:
        row.update(
            {
                "source_id": "source",
                "tenant_id": "tenant",
                "representation_version": current["representation_version"],
                "key_version": current["key_version"],
            },
        )
    comparison = build_comparison(
        current,
        history,
        snapshot_id="snap",
        input_revision=4,
    )
    assert comparison["candidate_count"] == 2
    assert comparison["snapshot_id"] == "snap"
    assert comparison["input_revision"] == 4
    assert comparison["candidate_count"] == 2
    assert comparison["lower_text_bytes"] is not None
    assert comparison["upper_text_bytes"] is not None
    assert (
        comparison["lower_text_bytes"] <= comparison["upper_text_bytes"] <= 900
    )
    assert comparison["candidates"][1]["historical_context_revision"] == 3


def test_first_difference_is_occurrence_not_first_differing_block():
    from swe.token_diagnostics.context_capture import capture_context
    from swe.token_diagnostics.config import DiagnosticConfig
    from monitor.app.services.token_diagnostics.prefix_analysis import (
        compare_prefix,
    )

    cfg = DiagnosticConfig(
        enabled=True,
        context_enabled=True,
        fingerprint_key="test",
    )

    def context(second):
        return capture_context(
            [
                {"role": "user", "content": "same"},
                {"role": "assistant", "content": second},
            ],
            [],
            ("source", "tenant"),
            cfg,
            context_id="ctx",
            context_role="prepared",
        )

    assert (
        compare_prefix(context("first"), context("other"))[
            "first_difference_index"
        ]
        == 1
    )


def test_unfinished_request_and_empty_deadline_are_not_ready():
    from monitor.app.services.token_diagnostics.prefix_analysis import (
        build_comparison,
    )

    current = {
        **captured("text"),
        "context_revision": 1,
        "request_ended_at": None,
        "execution_state": "running",
    }
    value = build_comparison(current, [], snapshot_id="s", input_revision=1)
    assert value["analysis_state"] == "insufficient_evidence"
    assert value["reason_code"] == "missing_progress"
    assert value["eligible"] is False
    with pytest.raises(TimeoutError):
        build_comparison(
            current,
            [],
            snapshot_id="s",
            input_revision=1,
            deadline=0,
        )


@pytest.mark.parametrize("state", [None, "pending", "unknown"])
def test_unknown_request_state_cannot_be_eligible(state):
    from monitor.app.services.token_diagnostics.prefix_analysis import (
        build_comparison,
    )

    current = {
        **captured("text"),
        "context_revision": 1,
        "request_ended_at": "2026-10-12T12:00:00Z",
        "execution_state": state,
    }
    value = build_comparison(current, [], snapshot_id="s", input_revision=1)
    assert value["reason_code"] == "missing_progress"
    assert value["eligible"] is False


def test_saved_no_history_is_separate_exclusion():
    from monitor.app.services.token_diagnostics.prefix_analysis import (
        build_comparison,
        aggregate_prefix,
    )

    current = {
        **captured("text"),
        "context_revision": 1,
        "request_ended_at": "2026-10-12T12:00:00Z",
        "execution_state": "completed",
    }
    value = build_comparison(current, [], snapshot_id="s", input_revision=1)
    value["capture_state"] = "complete"
    excluded = aggregate_prefix([value])["excluded"]
    assert excluded["no_history"] == 1
    assert excluded["missing_evidence"] == 0


def test_last_candidate_exceeding_deadline_does_not_publish(monkeypatch):
    from monitor.app.services.token_diagnostics import (
        prefix_analysis as module,
    )

    clock = [0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    original = module.compare_prefix

    def delayed(current, historical):
        result = original(current, historical)
        clock[0] = 2
        return result

    monkeypatch.setattr(module, "compare_prefix", delayed)
    current = {
        **captured("text"),
        "context_revision": 1,
        "request_ended_at": "2026-10-12T12:00:00Z",
        "execution_state": "completed",
    }
    with pytest.raises(TimeoutError):
        module.build_comparison(
            current,
            [current],
            snapshot_id="s",
            input_revision=1,
            deadline=1,
        )

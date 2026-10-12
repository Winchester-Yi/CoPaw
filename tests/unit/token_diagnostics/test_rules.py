# -*- coding: utf-8 -*-
"""Deterministic rules separate observed behavior from assumed waste."""

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "monitor" / "src")
)


def analyze(calls=None, items=None, events=None):
    try:
        from monitor.app.services.token_diagnostics.rules import analyze_branch
    except ModuleNotFoundError:
        pytest.fail("Token diagnostic rules are missing")
    return analyze_branch(calls or [], items or [], events or [])


def call(index, **values):
    return {
        "call_id": f"c{index}",
        "logical_call_id": f"l{index}",
        "logical_seq": index,
        "attempt_no": 1,
        "capture_state": "complete",
        "purpose": "main",
        "model_name": "m",
        "estimator_version": "v1",
        "compaction_epoch": 0,
        **values,
    }


def item(index, **values):
    return {
        "call_id": f"c{index}",
        "category": "tool_result",
        "fingerprint": "abc",
        "producer_operation_id": "tool1",
        "key_version": "v1",
        "estimated_tokens": 8000,
        **values,
    }


def test_repeated_result_counts_logical_calls_and_represented_size():
    result = analyze([call(i) for i in range(3)], [item(i) for i in range(3)])
    finding = next(
        x for x in result["findings"] if x["rule_id"] == "repeated_tool_result"
    )
    assert finding["affected_tokens"] == 16000
    retry_calls = [call(i, logical_call_id="same") for i in range(3)]
    assert not analyze(retry_calls, [item(i) for i in range(3)])["findings"]


def test_same_call_duplicate_injection_is_not_cross_call_fixed_prefix():
    rows = [
        item(
            0,
            category="inject",
            producer_operation_id=None,
            estimated_tokens=1200,
            source_ref="a",
        ),
        item(
            0,
            category="skill",
            producer_operation_id=None,
            estimated_tokens=1200,
            source_ref="b",
        ),
    ]
    result = analyze([call(0)], rows)
    assert result["findings"][0]["rule_id"] == "duplicate_injection"
    assert result["findings"][0]["affected_tokens"] == 1200
    rows[1]["call_id"] = "c1"
    assert not analyze([call(0), call(1)], rows)["findings"]


def test_tool_result_reuse_does_not_count_as_multiple_executions():
    event = {
        "event_type": "tool_completed",
        "event_seq": 1,
        "producer_operation_id": "p1",
        "tool_name": "lookup",
        "arguments_fingerprint": "args",
        "result_fingerprint": "same",
    }
    assert not analyze(events=[event, event, event])["findings"]


def test_repeated_tool_operations_report_suspicion_and_exclude_polling():
    events = [
        {
            "event_type": "tool_completed",
            "event_seq": i,
            "producer_operation_id": f"p{i}",
            "tool_name": "lookup",
            "arguments_fingerprint": "args",
            "result_fingerprint": "same",
        }
        for i in range(3)
    ]
    finding = analyze(events=events)["findings"][0]
    assert finding["rule_id"] == "repeated_tool_call"
    assert finding["evidence_quality"] == "needs_review"
    for event in events:
        event["polling"] = True
    assert not analyze(events=events)["findings"]


def test_history_growth_resets_after_compaction_and_preserves_zero_baseline():
    calls = [call(i) for i in range(4)]
    items = [
        item(i, category="user", estimated_tokens=value)
        for i, value in enumerate([4000, 7000, 10000, 13000])
    ]
    assert any(
        x["rule_id"] == "history_growth"
        for x in analyze(calls, items)["findings"]
    )
    calls[-1]["compaction_epoch"] = 1
    assert not analyze(calls, items)["findings"]
    calls[-1]["compaction_epoch"] = 0
    items[0]["estimated_tokens"] = 0
    assert not analyze(calls, items)["findings"]


def test_missing_usage_and_limits_do_not_invent_output_risk():
    assert not analyze([call(0, output_tokens=None, max_output_tokens=100)])[
        "findings"
    ]
    assert not analyze([call(0, output_tokens=20000)])["findings"]
    finding = analyze(
        [
            call(
                0,
                output_tokens=100,
                max_output_tokens=100,
                finish_reason="length",
                usage_status="reported",
            )
        ]
    )["findings"][0]
    assert finding["rule_id"] == "output_budget"
    assert "截断" in finding["title"]


def test_incomplete_fingerprint_reports_insufficient_evidence():
    result = analyze([call(0)], [item(0, fingerprint=None)])
    assert any(x["state"] == "insufficient_evidence" for x in result["rules"])
    assert not result["findings"]


def test_explicit_length_truncation_is_observed_even_without_a_known_cap():
    findings = analyze(
        [
            call(
                0,
                output_tokens=None,
                max_output_tokens=None,
                finish_reason="length",
            )
        ]
    )["findings"]
    assert len(findings) == 1
    assert findings[0]["evidence_quality"] == "observed"
    assert "截断" in findings[0]["title"]


def test_unknown_result_does_not_prove_repeated_tool_execution_has_no_progress():
    events = [
        {
            "event_type": "tool_completed",
            "event_seq": i,
            "producer_operation_id": f"p{i}",
            "tool_name": "lookup",
            "arguments_fingerprint": "args",
            "result_fingerprint": None,
        }
        for i in range(3)
    ]
    result = analyze(events=events)
    assert not result["findings"]
    assert (
        next(
            x for x in result["rules"] if x["rule_id"] == "repeated_tool_call"
        )["state"]
        == "insufficient_evidence"
    )

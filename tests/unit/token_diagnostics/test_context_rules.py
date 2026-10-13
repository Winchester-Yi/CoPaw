# -*- coding: utf-8 -*-
"""B candidates require reliable identities and complete saved evidence."""

from copy import deepcopy
from typing import get_args

import pytest

from monitor.app.services.token_diagnostics import diagnostic_contract as dto
from monitor.app.services.token_diagnostics.diagnostic_rules import (
    classify_low_prefix,
    evaluate_context_rules,
)
from monitor.app.services.token_diagnostics.recommendations import (
    RECOMMENDATIONS,
)


def occurrence(tokens, source="config", version="v1", category="system"):
    return {
        "occurrence_index": 0,
        "source_object_id": source,
        "source_version": version,
        "category": category,
        "attribution": "explicit",
        "content_digest": f"{source}:{version}",
        "structure_digest": "structure",
        "estimated_tokens": tokens,
        "token_start": 0,
        "token_end": tokens,
    }


def snapshot(sizes=(8000, 12000, 16000)):
    run = {
        "branch_run_id": "b",
        "task_run_id": "t",
        "source_id": "s",
        "tenant_id": "tenant",
        "agent_id": "a",
        "provider_id": "p",
        "model_name": "m",
        "data_revision": 7,
        "gap_count": 0,
    }
    calls, contexts = [], []
    for index, size in enumerate(sizes):
        call = {
            **run,
            "call_id": f"c{index}",
            "logical_call_id": f"l{index}",
            "logical_seq": index,
            "attempt_no": 0,
            "context_id": f"x{index}",
            "execution_state": "completed",
            "capture_state": "complete",
            "estimated_input_tokens": size,
            "estimator_version": "est1",
        }
        calls.append(call)
        contexts.append(
            {
                **run,
                "context_id": call["context_id"],
                "call_id": call["call_id"],
                "context_role": "prepared",
                "capture_state": "complete",
                "execution_state": "completed",
                "estimator_version": "est1",
                "representation_version": "rep1",
                "key_version": "key1",
                "assembly_stage": "prepared",
                "estimated_tokens": size,
                "context_revision": 1,
                "request_started_at": f"2026-10-12T12:0{index}:00Z",
                "request_ended_at": f"2026-10-12T12:0{index}:30Z",
                "text_bytes": size * 4,
                "occurrences": [occurrence(size)],
            },
        )
    return {
        "run": run,
        "input_revision": 7,
        "snapshot_id": "saved-snapshot",
        "calls": calls,
        "contexts": contexts,
        "events": [],
        "steps": [],
        "prefix_comparisons": [],
    }


def evaluation(data, rule, object_id=None):
    rows = [
        row for row in evaluate_context_rules(data) if row["rule_id"] == rule
    ]
    if object_id:
        rows = [row for row in rows if row["object_id"] == object_id]
    assert rows
    return rows[-1]


def value(row, key):
    return next(x for x in row["values"] if x["value_key"] == key)


def test_eight_ids_typed_scalars_saved_revision_and_non_executable_advice():
    rows = evaluate_context_rules(snapshot())
    assert {row["rule_id"] for row in rows} == set(dto.RULE_IDS)
    for row in rows:
        assert set(row) == dto.Evaluation.__required_keys__
        assert row["input_revision"] == 7
        assert row["snapshot_id"] == "saved-snapshot"
        assert row["state"] in dto.EVALUATION_STATES
        assert row["object_type"] in {"branch", "call", "step"}
        assert set(row["gaps"]) <= set(get_args(dto.GapReason))
        for scalar in row["values"]:
            assert set(scalar) == dto.EvaluationValue.__required_keys__
            assert scalar["value_key"] in get_args(dto.ValueKey)
            assert (
                sum(
                    scalar[key] is not None
                    for key in (
                        "integer_value",
                        "decimal_value",
                        "enum_value",
                    )
                )
                <= 1
            )
        for finding in row["findings"]:
            assert set(finding) == dto.DiagnosticFinding.__required_keys__
            assert finding["recommendation_code"] == row["rule_id"]
    assert set(RECOMMENDATIONS) == set(dto.RULE_IDS)
    for recommendation in RECOMMENDATIONS.values():
        assert set(recommendation) == {
            "action_code",
            "location",
            "prerequisites",
            "exceptions",
            "risks",
            "verification",
        }
        assert all(recommendation.values())
        assert not any(callable(x) for x in recommendation.values())


@pytest.mark.parametrize(
    "sizes, expected",
    [
        ((8000, 12000, 16000), "candidate"),
        ((8000, 8000, 16000), "not_hit"),
        ((8000, 16000), "not_applicable"),
        ((5000, 10000, 12000), "not_hit"),
    ],
)
def test_growth_strict_three_round_thresholds(sizes, expected):
    assert evaluation(snapshot(sizes), "context_growth")["state"] == expected


def test_retry_does_not_add_round_and_changed_retry_is_insufficient():
    data = snapshot((8000, 16000))
    retry = deepcopy(data["calls"][0])
    retry.update(call_id="retry", attempt_no=1, context_id="retry-context")
    context = deepcopy(data["contexts"][0])
    context.update(call_id="retry", context_id="retry-context")
    data["calls"].append(retry)
    data["contexts"].append(context)
    assert evaluation(data, "context_growth")["state"] == "not_applicable"
    context["occurrences"][0]["content_digest"] = "different"
    assert (
        evaluation(data, "context_growth")["state"] == "insufficient_evidence"
    )


def test_input_budget_is_not_total_pressure_and_trusted_window_is():
    data = snapshot((8000,))
    data["calls"][0]["max_input_tokens"] = 9000
    assert evaluation(data, "context_growth")["state"] == "not_applicable"
    data["calls"][0].update(
        total_context_limit=10000,
        reserved_output_tokens=1000,
        context_limit_trusted=True,
    )
    row = evaluation(data, "context_growth")
    assert row["state"] == "candidate"
    assert value(row, "context_pressure_ratio")["decimal_value"] == 0.9


def test_growth_cannot_mix_estimators_partial_capture_or_branches():
    for field, replacement in (
        ("estimator_version", "est2"),
        ("capture_state", "partial"),
    ):
        data = snapshot()
        data["contexts"][1][field] = replacement
        assert (
            evaluation(data, "context_growth")["state"]
            == "insufficient_evidence"
        )
    data = snapshot((8000, 16000))
    other = snapshot((12000,))
    other["calls"][0].update(branch_run_id="other", call_id="foreign")
    other["contexts"][0].update(branch_run_id="other", call_id="foreign")
    data["calls"] += other["calls"]
    data["contexts"] += other["contexts"]
    assert evaluation(data, "context_growth")["state"] == "not_applicable"


def test_carryover_needs_same_source_version_and_final_ratio():
    data = snapshot((20000, 20000, 20000))
    for index, context in enumerate(data["contexts"]):
        context["occurrences"] = [
            occurrence(5000),
            occurrence(15000, f"other{index}"),
        ]
        context["occurrences"][1]["occurrence_index"] = 1
    row = evaluation(data, "segment_carryover")
    assert row["state"] == "candidate"
    assert value(row, "repeated_tokens")["integer_value"] == 5000
    data["contexts"][-1]["occurrences"][0]["source_version"] = "v2"
    data["contexts"][-1]["occurrences"][1]["source_version"] = "v2"
    assert evaluation(data, "segment_carryover")["state"] == "not_hit"
    for context in data["contexts"]:
        for item in context["occurrences"]:
            item["source_object_id"] = None
    row = evaluation(data, "segment_carryover")
    assert row["state"] == "insufficient_evidence"
    assert "missing_identity" in row["gaps"]
    assert row["findings"] == []


def test_tool_volume_does_not_treat_tool_end_as_business_step_end():
    data = snapshot((10000, 10000, 10000))
    for context in data["contexts"]:
        context["occurrences"] = [
            occurrence(10000, "result", category="tool_result"),
        ]
        context["occurrences"][0].update(
            business_step_id="step",
            producer_operation_id="op",
        )
    data["events"] = [{"event_type": "tool_completed", "step_id": "step"}]
    row = evaluation(data, "tool_context_volume")
    assert row["state"] == "insufficient_evidence"
    assert value(row, "tool_result_ratio")["decimal_value"] == 1
    data["steps"] = [
        {
            "step_id": "step",
            "branch_run_id": "b",
            "identity_source": "business",
            "execution_state": "completed",
            "ended_logical_seq": -1,
        },
    ]
    assert evaluation(data, "tool_context_volume")["state"] == "candidate"


def tool_event(index, **changes):
    return {
        "event_id": f"e{index}",
        "producer_operation_id": f"op{index}",
        "event_type": "tool_completed",
        "event_seq": index,
        "branch_run_id": "b",
        "tool_name": "read-file",
        "tool_kind": "read",
        "arguments_fingerprint": "args",
        "result_fingerprint": "result",
        "data_version": "v1",
        "key_version": "key1",
        **changes,
    }


def test_safe_repeat_missing_safety_is_gap_and_versions_do_not_merge():
    data = snapshot()
    data["events"] = [tool_event(0), tool_event(1)]
    row = evaluation(data, "safe_tool_repeat")
    assert row["state"] == "candidate"
    assert value(row, "extra_call_count")["integer_value"] == 1
    for item in data["events"]:
        item["tool_kind"] = "unknown"
    assert (
        evaluation(data, "safe_tool_repeat")["state"]
        == "insufficient_evidence"
    )
    data["events"] = [tool_event(0), tool_event(1, data_version="v2")]
    assert evaluation(data, "safe_tool_repeat")["state"] == "not_hit"


def test_loop_only_real_complete_deterministic_steps_progress_unknown_gap():
    data = snapshot()
    for call in data["calls"]:
        call["step_id"] = "random-" + call["call_id"]
    assert (
        evaluation(data, "suspected_loop")["state"] == "insufficient_evidence"
    )
    data["steps"] = [
        {
            "step_id": f"step{i}",
            "branch_run_id": "b",
            "step_seq": i,
            "identity_source": "business",
            "execution_state": "completed",
            "action_signature": "action",
            "result_signature": "result",
            "signature_version": 1,
        }
        for i in range(3)
    ]
    row = evaluation(data, "suspected_loop")
    assert row["state"] == "candidate"
    assert "missing_progress" in row["gaps"]
    data["steps"][1]["result_signature"] = "changed"
    assert evaluation(data, "suspected_loop")["state"] == "not_hit"
    data["steps"][1]["result_signature"] = None
    assert (
        evaluation(data, "suspected_loop")["state"] == "insufficient_evidence"
    )


@pytest.mark.parametrize(
    "lower, upper, limited, expected",
    [
        (0.15, 0.25, False, "insufficient_evidence"),
        (0.15, 0.19, False, "candidate"),
        (0.15, 0.19, True, "insufficient_evidence"),
        (0.20, 0.25, False, "not_hit"),
        (0.19, 0.20, False, "insufficient_evidence"),
        (None, 0.19, False, "insufficient_evidence"),
    ],
)
def test_exact_prefix_threshold_bounds(lower, upper, limited, expected):
    assert classify_low_prefix(lower, upper, limited) == expected


def prefix_snapshot():
    data = snapshot((10000,))
    current = data["contexts"][0]
    current["request_started_at"] = "2026-10-12T12:00:00Z"
    current["request_ended_at"] = "2026-10-12T12:05:00Z"
    current["context_revision"] = 1
    current["occurrences"] = [
        occurrence(2000),
        occurrence(8000, "dynamic", category="user"),
    ]
    historical = [deepcopy(current), deepcopy(current)]
    for index, context in enumerate(historical):
        context.update(
            context_id=f"h{index}",
            call_id=f"hc{index}",
            context_revision=1,
            request_started_at=f"2026-10-12T11:0{index}:00Z",
            request_ended_at=f"2026-10-12T11:0{index}:30Z",
        )
    data["history_contexts"] = historical
    data["prefix_comparisons"] = [
        {
            "current_context_id": current["context_id"],
            "current_context_revision": current["context_revision"],
            "analysis_state": "ready",
            "candidate_count": 2,
            "candidate_limited": False,
            "lower_text_bytes": 6000,
            "upper_text_bytes": 7600,
            "total_text_bytes": 40000,
            "candidates": [
                {
                    "historical_context_id": x["context_id"],
                    "historical_context_revision": x["context_revision"],
                    "lower_text_bytes": 6000,
                    "upper_text_bytes": 7600,
                }
                for x in historical
            ],
        },
    ]
    return data


def test_prefix_requires_independent_static_identity_and_complete_history():
    data = prefix_snapshot()
    assert evaluation(data, "text_prefix_stability")["state"] == "candidate"
    data["history_contexts"][0]["occurrences"][0]["source_version"] = "changed"
    assert evaluation(data, "text_prefix_stability")["state"] == "not_hit"
    data = prefix_snapshot()
    data["history_contexts"][0]["capture_state"] = "partial"
    assert (
        evaluation(data, "text_prefix_stability")["state"]
        == "insufficient_evidence"
    )
    data = prefix_snapshot()
    data["prefix_comparisons"][0]["candidate_limited"] = True
    assert (
        evaluation(data, "text_prefix_stability")["state"]
        == "insufficient_evidence"
    )
    data = prefix_snapshot()
    data["prefix_comparisons"][0]["candidates"][1]["upper_text_bytes"] = 10000
    assert (
        evaluation(data, "text_prefix_stability")["state"]
        == "insufficient_evidence"
    )


def test_static_threshold_trusted_categories_and_missing_denominator():
    data = snapshot((16000,))
    data["contexts"][0]["occurrences"] = [
        occurrence(4000),
        occurrence(12000, "user", category="user"),
    ]
    assert evaluation(data, "static_context_volume")["state"] == "candidate"
    data["contexts"][0]["occurrences"][0]["attribution"] = "role"
    row = evaluation(data, "static_context_volume")
    assert row["state"] == "insufficient_evidence"
    assert "missing_static_identity" in row["gaps"]
    data = snapshot((16000,))
    data["contexts"][0]["occurrences"][0]["estimated_tokens"] = None
    data["contexts"][0]["occurrences"][0]["token_end"] = None
    assert (
        evaluation(data, "static_context_volume")["state"]
        == "insufficient_evidence"
    )


def test_cohort_fixed_missing_v1_even_if_caller_supplies_group():
    data = snapshot()
    data["cohort"] = [{"tokens": 999999} for _ in range(100)]
    row = evaluation(data, "cohort_usage_outlier")
    assert row["state"] == "insufficient_evidence"
    assert row["gaps"] == ["missing_cohort"]
    assert row["findings"] == []


def test_reliable_signature_scope_versions_and_unknown_instances_are_separate():
    data = snapshot()
    first = evaluation(data, "context_growth")["findings"][0]
    second = evaluation(deepcopy(data), "context_growth")["findings"][0]
    assert first == second
    data["run"]["tenant_id"] = "other"
    for row in data["calls"] + data["contexts"]:
        row["tenant_id"] = "other"
    assert (
        evaluation(data, "context_growth")["findings"][0]["signature"]
        != first["signature"]
    )
    data = snapshot()
    data["run"]["agent_id"] = None
    for row in data["calls"] + data["contexts"]:
        row["agent_id"] = None
    first = evaluation(data, "context_growth")["findings"][0]
    assert first["signature"] is None
    assert first["reliable_grouping"] is False
    data["snapshot_id"] = "another"
    assert (
        evaluation(data, "context_growth")["findings"][0]["finding_id"]
        != first["finding_id"]
    )


def test_growth_can_use_three_consecutive_rounds_within_a_longer_branch():
    data = snapshot((9000, 8000, 12000, 16000))
    row = evaluation(data, "context_growth")
    assert row["state"] == "candidate"
    assert value(row, "first_tokens")["integer_value"] == 8000


def test_missing_tool_step_end_order_is_insufficient_not_a_negative_result():
    data = snapshot((10000, 10000, 10000))
    for context in data["contexts"]:
        context["occurrences"] = [
            occurrence(10000, "result", category="tool_result"),
        ]
        context["occurrences"][0]["business_step_id"] = "step"
    data["steps"] = [
        {
            "step_id": "step",
            "identity_source": "business",
            "execution_state": "completed",
        },
    ]
    row = evaluation(data, "tool_context_volume")
    assert row["state"] == "insufficient_evidence"
    assert "missing_progress" in row["gaps"]


def test_prefix_rejects_revision_mismatch_and_running_history():
    data = prefix_snapshot()
    data["history_contexts"][0]["context_revision"] = 2
    data["prefix_comparisons"][0]["candidates"][0][
        "historical_context_revision"
    ] = 1
    assert (
        evaluation(data, "text_prefix_stability")["state"]
        == "insufficient_evidence"
    )
    data = prefix_snapshot()
    data["history_contexts"][0]["execution_state"] = "running"
    assert (
        evaluation(data, "text_prefix_stability")["state"]
        == "insufficient_evidence"
    )


def test_prefix_static_repeat_needs_two_qualifying_histories_not_every_history():
    data = prefix_snapshot()
    unrelated = deepcopy(data["history_contexts"][0])
    unrelated["context_id"] = "unrelated"
    unrelated["call_id"] = "unrelated-call"
    unrelated["occurrences"][0]["source_version"] = "another-static-version"
    data["history_contexts"].append(unrelated)
    comparison = data["prefix_comparisons"][0]
    comparison["candidate_count"] = 3
    comparison["candidates"].append(
        {
            "historical_context_id": "unrelated",
            "historical_context_revision": 1,
            "lower_text_bytes": 4000,
            "upper_text_bytes": 5000,
        },
    )
    assert evaluation(data, "text_prefix_stability")["state"] == "candidate"


def test_partial_snapshot_cannot_report_rules_as_not_hit():
    data = snapshot((1000,))
    data["truncated"] = True
    rows = evaluate_context_rules(data)
    assert all(row["state"] == "insufficient_evidence" for row in rows)


def test_call_static_rule_has_local_evidence_not_unrelated_call_capture_gap():
    data = snapshot((10000, 10000, 10000))
    data["contexts"][0]["capture_state"] = "partial"
    assert (
        evaluation(data, "static_context_volume", "c2")["state"] == "candidate"
    )


def test_growth_any_qualifying_continuous_sequence_is_retained():
    data = snapshot((100000, 104000, 109000, 4000, 8000, 12000))
    assert evaluation(data, "context_growth")["state"] == "candidate"


def test_prefix_duplicate_history_does_not_supply_two_requests():
    data = prefix_snapshot()
    comparison = data["prefix_comparisons"][0]
    comparison["candidates"][1] = deepcopy(comparison["candidates"][0])
    assert (
        evaluation(data, "text_prefix_stability")["state"]
        == "insufficient_evidence"
    )


def test_prefix_future_or_expired_history_is_not_eligible():
    data = prefix_snapshot()
    current = data["contexts"][0]
    current["request_started_at"] = "2026-10-12T12:00:00Z"
    for previous in data["history_contexts"]:
        previous["request_ended_at"] = "2026-10-12T11:00:00Z"
    assert evaluation(data, "text_prefix_stability")["state"] == "candidate"
    data["history_contexts"][0]["request_ended_at"] = "2026-10-12T12:00:01Z"
    assert (
        evaluation(data, "text_prefix_stability")["state"]
        == "insufficient_evidence"
    )
    data["history_contexts"][0]["request_ended_at"] = "2026-10-10T12:00:00Z"
    assert (
        evaluation(data, "text_prefix_stability")["state"]
        == "insufficient_evidence"
    )


@pytest.mark.parametrize("rule", ["context_growth", "segment_carryover"])
def test_nonconsecutive_saved_logical_sequence_is_missing_evidence(rule):
    data = snapshot()
    for call, seq in zip(data["calls"], (0, 2, 3)):
        call["logical_seq"] = seq
    assert evaluation(data, rule)["state"] == "insufficient_evidence"


def test_growth_mixed_saved_assembly_stage_cannot_form_sequence():
    data = snapshot()
    data["contexts"][1]["assembly_stage"] = "before_compaction"
    assert (
        evaluation(data, "context_growth")["state"] == "insufficient_evidence"
    )


def test_nonconsecutive_real_step_sequence_cannot_form_loop():
    data = snapshot()
    data["steps"] = [
        {
            "step_id": f"s{seq}",
            "branch_run_id": "b",
            "step_seq": seq,
            "identity_source": "business",
            "execution_state": "completed",
            "action_signature": "a",
            "result_signature": "r",
            "signature_version": 1,
        }
        for seq in (0, 2, 3)
    ]
    assert (
        evaluation(data, "suspected_loop")["state"] == "insufficient_evidence"
    )


@pytest.mark.parametrize(
    "damage",
    [
        "current_running",
        "same_historical_call",
        "missing_times",
        "missing_revisions",
    ],
)
def test_prefix_saved_request_completeness_identity_and_revision_required(
    damage,
):
    data = prefix_snapshot()
    if damage == "current_running":
        data["contexts"][0]["execution_state"] = "running"
    elif damage == "same_historical_call":
        data["history_contexts"][1]["call_id"] = data["history_contexts"][0][
            "call_id"
        ]
    elif damage == "missing_times":
        data["contexts"][0].pop("request_started_at", None)
        for previous in data["history_contexts"]:
            previous.pop("request_ended_at", None)
    else:
        for previous in data["history_contexts"]:
            previous.pop("context_revision", None)
        for candidate in data["prefix_comparisons"][0]["candidates"]:
            candidate.pop("historical_context_revision", None)
    assert (
        evaluation(data, "text_prefix_stability")["state"]
        == "insufficient_evidence"
    )


@pytest.mark.parametrize(
    "rule",
    ["text_prefix_stability", "static_context_volume"],
)
def test_call_rules_keep_each_saved_attempt(rule):
    data = prefix_snapshot()
    retry = deepcopy(data["calls"][0])
    retry.update(call_id="retry", attempt_no=1, context_id="retry-context")
    context = deepcopy(data["contexts"][0])
    context.update(call_id="retry", context_id="retry-context")
    data["calls"].append(retry)
    data["contexts"].append(context)
    identities = {
        row["object_id"]
        for row in evaluate_context_rules(data)
        if row["rule_id"] == rule
    }
    assert identities == {"c0", "retry"}


def test_prefix_current_revision_is_fixed_to_comparison():
    data = prefix_snapshot()
    data["contexts"][0]["context_revision"] = 2
    data["prefix_comparisons"][0]["current_context_revision"] = 1
    assert (
        evaluation(data, "text_prefix_stability")["state"]
        == "insufficient_evidence"
    )


@pytest.mark.parametrize(
    "damage",
    [
        "run_gap",
        "call_gap",
        "foreign_context",
        "phase",
        "compaction",
        "zero_first",
    ],
)
def test_growth_known_holes_and_incompatible_saved_inputs_are_insufficient(
    damage,
):
    data = snapshot()
    if damage == "run_gap":
        data["run"]["gap_count"] = 1
    elif damage == "call_gap":
        data["calls"][1]["gap_count"] = 1
    elif damage == "foreign_context":
        data["contexts"][1]["tenant_id"] = "foreign"
    elif damage == "phase":
        for index, call in enumerate(data["calls"]):
            call["phase_id"] = "p1" if index == 0 else "p2"
    elif damage == "compaction":
        data["calls"][1]["compaction_epoch"] = 1
    else:
        data["contexts"][0]["estimated_tokens"] = 0
    assert (
        evaluation(data, "context_growth")["state"] == "insufficient_evidence"
    )


def test_persisted_run_without_provider_model_can_group_from_compatible_calls():
    data = snapshot()
    data["run"].pop("provider_id")
    data["run"].pop("model_name")
    finding = evaluation(data, "context_growth")["findings"][0]
    assert finding["reliable_grouping"] is True
    assert finding["signature"] is not None


@pytest.mark.parametrize(
    "field, replacement",
    [
        ("source_object_id", "another-config"),
        ("source_version", "v2"),
        ("category", "skill"),
        ("structure_digest", "another-structure"),
    ],
)
def test_growth_reliable_groups_distinguish_controlled_source_evidence(
    field,
    replacement,
):
    original = snapshot()
    changed = deepcopy(original)
    for context in changed["contexts"]:
        context["occurrences"][0][field] = replacement
    first = evaluation(original, "context_growth")["findings"][0]
    second = evaluation(changed, "context_growth")["findings"][0]
    assert first["reliable_grouping"] is True
    assert second["reliable_grouping"] is True
    assert first["signature"] != second["signature"]


@pytest.mark.parametrize(
    "field, replacement",
    [
        ("source_object_id", None),
        ("source_version", None),
        ("structure_digest", None),
        ("attribution", "role"),
        ("category", "user"),
        ("category", "tool_result"),
    ],
)
def test_growth_unreliable_source_keeps_independent_candidate(
    field,
    replacement,
):
    data = snapshot()
    # A known earlier-round gap cannot be hidden by a reliable final Context.
    data["contexts"][0]["occurrences"][0][field] = replacement
    row = evaluation(data, "context_growth")
    assert row["state"] == "candidate"
    assert row["findings"][0]["reliable_grouping"] is False
    assert row["findings"][0]["signature"] is None


@pytest.mark.parametrize("execution", [None, "pending", "unknown"])
def test_prefix_current_execution_must_have_known_terminal_state(execution):
    data = prefix_snapshot()
    data["contexts"][0]["execution_state"] = execution
    assert (
        evaluation(data, "text_prefix_stability")["state"]
        == "insufficient_evidence"
    )


def test_prefix_does_not_accept_history_call_equal_to_current_call():
    data = prefix_snapshot()
    data["history_contexts"][0]["call_id"] = data["calls"][0]["call_id"]
    assert (
        evaluation(data, "text_prefix_stability")["state"]
        == "insufficient_evidence"
    )


@pytest.mark.parametrize(
    "field, replacement",
    [
        ("estimator_version", "est2"),
        ("representation_version", "rep2"),
        ("key_version", "key2"),
        ("assembly_stage", "another-stage"),
    ],
)
def test_static_signatures_include_context_compatibility(field, replacement):
    data = snapshot((10000,))
    first = evaluation(data, "static_context_volume")["findings"][0][
        "signature"
    ]
    data["contexts"][0][field] = replacement
    second = evaluation(data, "static_context_volume")["findings"][0][
        "signature"
    ]
    assert first != second


@pytest.mark.parametrize(
    "damage",
    ["before_context", "foreign_context", "missing_context"],
)
def test_call_rules_keep_insufficient_evaluation_for_invalid_context(damage):
    data = snapshot()
    if damage == "before_context":
        data["contexts"][1]["context_role"] = "before"
    elif damage == "foreign_context":
        data["contexts"][1]["tenant_id"] = "foreign"
    else:
        data["contexts"].pop(1)
    for rule in ("static_context_volume", "text_prefix_stability"):
        assert evaluation(data, rule, "c1")["state"] == "insufficient_evidence"


@pytest.mark.parametrize(
    "rule",
    ["segment_carryover", "static_context_volume", "text_prefix_stability"],
)
def test_context_backed_signatures_include_compatible_versions(rule):
    data = prefix_snapshot() if rule == "text_prefix_stability" else snapshot()
    first = evaluation(data, rule)["findings"][0]["signature"]
    for context in data["contexts"] + data.get("history_contexts", []):
        context["estimator_version"] = "est2"
    second = evaluation(data, rule)["findings"][0]["signature"]
    assert first != second


def test_deadline_interrupts_work_in_prefix_candidate_loop(monkeypatch):
    from monitor.app.services.token_diagnostics import (
        diagnostic_rules,
        prefix_analysis,
    )

    data = prefix_snapshot()
    original = diagnostic_rules._static
    ticks = [0]

    def costly_static(context):
        result = original(context)
        if context["context_id"].startswith("h"):
            ticks[0] = 10
        return result

    monkeypatch.setattr(diagnostic_rules, "_static", costly_static)
    monkeypatch.setattr(prefix_analysis, "monotonic", lambda: ticks[0])
    with pytest.raises(TimeoutError, match="analysis_timeout"):
        evaluate_context_rules(data, deadline=5)
    assert "_deadline" not in data

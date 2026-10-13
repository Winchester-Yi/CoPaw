# -*- coding: utf-8 -*-
"""Independent Monitor contracts for saved B evidence and evaluations."""

from typing import Literal, TypedDict, cast

B_SCHEMA_VERSION = "1.1"
SUPPORTED_SCHEMA_VERSIONS = frozenset({"1.0", B_SCHEMA_VERSION})
CANDIDATE_LIMIT = 100
HISTORY_WINDOW_HOURS = 24
RULE_VERSION = 1
THRESHOLD_VERSION = 1
RULE_IDS = (
    "context_growth",
    "segment_carryover",
    "tool_context_volume",
    "safe_tool_repeat",
    "suspected_loop",
    "text_prefix_stability",
    "static_context_volume",
    "cohort_usage_outlier",
)
RuleId = Literal[
    "context_growth",
    "segment_carryover",
    "tool_context_volume",
    "safe_tool_repeat",
    "suspected_loop",
    "text_prefix_stability",
    "static_context_volume",
    "cohort_usage_outlier",
]
EvaluationState = Literal[
    "candidate",
    "not_hit",
    "insufficient_evidence",
    "not_applicable",
]
EVALUATION_STATES = frozenset(
    {"candidate", "not_hit", "insufficient_evidence", "not_applicable"},
)
ValueKey = Literal[
    "logical_call_count",
    "first_tokens",
    "last_tokens",
    "growth_ratio",
    "net_growth_tokens",
    "context_pressure_ratio",
    "occurrence_count",
    "repeated_tokens",
    "attributed_input_tokens",
    "tool_result_tokens",
    "tool_result_ratio",
    "static_tokens",
    "static_ratio",
    "extra_call_count",
    "step_count",
    "history_count",
    "prefix_ratio",
    "cohort_count",
]
ScalarEnum = Literal["actual", "estimated", "unknown", "true", "false"]
GapReason = Literal[
    "missing_context",
    "partial_context",
    "missing_key",
    "missing_identity",
    "missing_usage",
    "missing_denominator",
    "missing_step",
    "missing_progress",
    "missing_tool_properties",
    "missing_cohort",
    "missing_static_identity",
    "incompatible_version",
    "no_history",
    "candidate_limit",
    "capture_limit",
    "evidence_expired",
    "analysis_failed",
    "analysis_timeout",
]
ReferenceType = Literal[
    "task",
    "branch",
    "step",
    "call",
    "context",
    "occurrence",
    "operation",
]


class PrefixBounds(TypedDict):
    lower_text_bytes: int | None
    upper_text_bytes: int | None
    first_difference_index: int | None


class EvaluationValue(TypedDict):
    """Typed scalar columns; only the selected type column has a value."""

    value_key: ValueKey
    unit: Literal["tokens", "estimated_tokens", "bytes", "count", "ratio"]
    value_type: Literal["integer", "decimal", "enum"]
    integer_value: int | None
    decimal_value: float | None
    enum_value: ScalarEnum | None
    lower_bound: int | float | None
    upper_bound: int | float | None
    threshold: int | float | None


class EvaluationReference(TypedDict):
    reference_type: ReferenceType
    object_id: str
    context_id: str | None
    occurrence_index: int | None


class DiagnosticFinding(TypedDict):
    finding_id: str
    instance_key: str
    signature: str | None
    signature_version: int
    reliable_grouping: bool
    recommendation_code: RuleId


class Evaluation(TypedDict):
    evaluation_id: str
    object_type: Literal["branch", "call", "step"]
    object_id: str
    rule_id: RuleId
    rule_version: int
    threshold_version: int
    input_revision: int
    snapshot_id: str
    state: EvaluationState
    values: list[EvaluationValue]
    gaps: list[GapReason]
    refs: list[EvaluationReference]
    findings: list[DiagnosticFinding]


def validate_schema_version(version) -> str:
    if (
        not isinstance(version, str)
        or version not in SUPPORTED_SCHEMA_VERSIONS
    ):
        raise ValueError("Unsupported token diagnostic schema version")
    return version


def validate_evaluation_state(state) -> EvaluationState:
    if not isinstance(state, str) or state not in EVALUATION_STATES:
        raise ValueError("Unsupported token diagnostic evaluation state")
    return cast(EvaluationState, state)

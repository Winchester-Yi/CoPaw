# -*- coding: utf-8 -*-
"""Typed Token storage contract. Independent SWE/Monitor copies have a parity test."""

COLUMNS = {
    "swe_token_runs": tuple(
        "task_run_id branch_run_id parent_branch_run_id source_id tenant_id trace_id "
        "user_id agent_id chat_id session_id role purpose schema_version "
        "telemetry_source execution_origin execution_id identity_source agent_version "
        "phase_id max_input_tokens max_output_tokens compaction_epoch "
        "gap_tracking_limited started_at ended_at revision execution_state "
        "expected_attempt_count gap_count data_revision analyzed_revision "
        "analysis_state dirty analysis_error claim_token lease_until".split(),
    ),
    "swe_token_model_calls": tuple(
        "call_id task_run_id branch_run_id parent_branch_run_id source_id tenant_id "
        "trace_id user_id agent_id chat_id session_id role purpose schema_version "
        "telemetry_source execution_origin execution_id identity_source agent_version "
        "phase_id max_input_tokens max_output_tokens compaction_epoch "
        "gap_tracking_limited capture_fragment_id logical_call_id logical_seq attempt_no revision "
        "execution_state usage_status capture_state input_tokens output_tokens "
        "model_name provider_id started_at ended_at step_id context_id parent_step_id "
        "assembled_at assembly_stage prompt_format_confidence context_builder_version "
        "prompt_modality usage_origin usage_accuracy estimated_input_tokens "
        "expected_item_count fingerprint_available estimator_version "
        "external_usage_unavailable error_code finish_reason cache_read_tokens "
        "cache_write_tokens reasoning_tokens gap_count".split(),
    ),
    "swe_token_input_items": tuple(
        "call_id occurrence_index branch_run_id task_run_id source_id tenant_id "
        "category source_ref producer_operation_id fingerprint key_version "
        "estimated_tokens retained_bytes attribution_quality".split(),
    ),
    "swe_token_execution_events": tuple(
        "event_id task_run_id branch_run_id parent_branch_run_id source_id tenant_id "
        "trace_id user_id agent_id chat_id session_id role purpose schema_version "
        "telemetry_source execution_origin execution_id identity_source agent_version "
        "phase_id max_input_tokens max_output_tokens compaction_epoch "
        "gap_tracking_limited capture_fragment_id expected_attempt_count event_seq event_type revision created_at started_at "
        "ended_at step_id parent_step_id producer_operation_id tool_name tool_kind "
        "idempotency_class is_cacheable data_version key_version polling "
        "arguments_fingerprint result_fingerprint error_code result_status "
        "progress_version gap_count execution_state".split(),
    ),
    "swe_token_findings": tuple(
        "finding_id task_run_id branch_run_id source_id tenant_id rule_id "
        "rule_version finding_key observed_revision state updated_at title action "
        "affected_tokens evidence_quality producer_operation_id occurrences "
        "minimum_occurrences threshold_tokens tool_name initial_tokens current_tokens "
        "growth_ratio phase_known output_tokens output_limit finish_reason "
        "threshold_ratio".split(),
    ),
    "swe_token_expected_children": tuple(
        "branch_run_id source_id tenant_id occurrence_index child_branch_run_id".split(),
    ),
    "swe_token_rule_evaluations": tuple(
        "branch_run_id source_id tenant_id rule_id rule_version state "
        "observed_revision occurrence_index".split(),
    ),
    "swe_token_rule_missing_fields": tuple(
        "branch_run_id source_id tenant_id rule_id rule_version occurrence_index "
        "field_name".split(),
    ),
    "swe_token_finding_calls": tuple(
        "finding_id branch_run_id source_id tenant_id occurrence_index call_id".split(),
    ),
    "swe_token_finding_operations": tuple(
        "finding_id branch_run_id source_id tenant_id occurrence_index "
        "producer_operation_id".split(),
    ),
    "swe_token_finding_sources": tuple(
        "finding_id branch_run_id source_id tenant_id occurrence_index source_ref".split(),
    ),
    "swe_token_contexts": tuple(
        "context_id task_run_id branch_run_id source_id tenant_id "
        "capture_fragment_id agent_id provider_id model_name call_id "
        "operation_id context_role assembly_stage representation_version "
        "estimator_version key_version compatibility_digest capture_state "
        "capture_reason text_bytes encoded_bytes estimated_tokens "
        "expected_item_count context_revision revision request_started_at "
        "request_ended_at execution_state".split(),
    ),
    "swe_token_context_occurrences": tuple(
        "context_id occurrence_index task_run_id branch_run_id source_id "
        "tenant_id source_object_id source_version source_ref "
        "producer_operation_id business_step_id category attribution "
        "lifecycle_state data_version content_digest structure_digest "
        "text_start text_end encoded_start encoded_end token_start "
        "token_end estimated_tokens".split(),
    ),
    "swe_token_prefix_blocks": tuple(
        "context_id block_index task_run_id branch_run_id source_id "
        "tenant_id chained_digest encoded_start encoded_end text_start "
        "text_end representation_version key_version".split(),
    ),
    "swe_token_context_operations": tuple(
        "operation_id task_run_id branch_run_id source_id tenant_id "
        "capture_fragment_id operation_type operation_state "
        "trigger_reason started_at ended_at before_context_id "
        "after_context_id summary_call_id missing_before missing_after "
        "capture_state revision".split(),
    ),
    "swe_token_operation_segments": tuple(
        "operation_id relation_index task_run_id branch_run_id source_id "
        "tenant_id relation_type before_context_id "
        "before_occurrence_index after_context_id after_occurrence_index "
        "source_object_id source_version".split(),
    ),
    "swe_token_prefix_comparisons": tuple(
        "comparison_id task_run_id branch_run_id source_id tenant_id "
        "current_context_id current_context_revision call_id snapshot_id "
        "input_revision representation_version key_version "
        "compatibility_digest window_hours candidate_limit "
        "candidate_count candidate_limited selection_version "
        "best_lower_context_id max_upper_context_id lower_text_bytes "
        "upper_text_bytes total_text_bytes first_difference_index "
        "analysis_state reason_code eligible created_at".split(),
    ),
    "swe_token_prefix_candidates": tuple(
        "comparison_id candidate_index task_run_id branch_run_id "
        "source_id tenant_id historical_context_id "
        "historical_context_revision historical_call_id "
        "historical_ended_at lower_text_bytes upper_text_bytes "
        "first_difference_index".split(),
    ),
    "swe_token_diagnostic_evaluations": tuple(
        "evaluation_id task_run_id branch_run_id source_id tenant_id "
        "object_type object_id rule_id rule_version threshold_version "
        "input_revision snapshot_id state created_at "
        "replaces_evaluation_id".split(),
    ),
    "swe_token_diagnostic_findings": tuple(
        "finding_id task_run_id branch_run_id source_id tenant_id "
        "evaluation_id instance_key signature signature_version "
        "reliable_grouping recommendation_code rule_id rule_version "
        "threshold_version agent_id provider_id model_name call_id "
        "context_id step_id operation_id created_at".split(),
    ),
    "swe_token_diagnostic_values": tuple(
        "evaluation_id value_key task_run_id branch_run_id source_id "
        "tenant_id unit value_type integer_value decimal_value enum_value "
        "lower_bound upper_bound threshold".split(),
    ),
    "swe_token_diagnostic_gaps": tuple(
        "evaluation_id reason_code task_run_id branch_run_id source_id "
        "tenant_id".split(),
    ),
    "swe_token_diagnostic_refs": tuple(
        "evaluation_id reference_index task_run_id branch_run_id "
        "source_id tenant_id reference_type object_id context_id "
        "occurrence_index".split(),
    ),
    "swe_token_diagnostic_heads": tuple(
        "task_run_id branch_run_id source_id tenant_id object_type "
        "object_id rule_id evaluation_id input_revision snapshot_id "
        "rule_version threshold_version updated_at".split(),
    ),
}

DEFAULTS = {
    "schema_version": "1.0",
    "telemetry_source": "runtime",
    "execution_origin": "unknown",
    "identity_source": "unknown",
    "assembly_stage": "messages_only",
    "prompt_format_confidence": "message_approx",
    "context_builder_version": "prepared-messages-v1",
    "prompt_modality": "unknown",
    "usage_origin": "unknown",
    "usage_accuracy": "unavailable",
    "tool_kind": "unknown",
    "idempotency_class": "unknown",
    "revision": 1,
    "logical_seq": 0,
    "attempt_no": 1,
    "event_seq": 0,
    "compaction_epoch": 0,
    "gap_count": 0,
    "gap_tracking_limited": False,
    "execution_state": "running",
    "usage_status": "unavailable",
    "capture_state": "unavailable",
}

EVIDENCE_FIELDS = {
    "repeated_tool_result": tuple(
        "call_ids producer_operation_id occurrences minimum_occurrences "
        "threshold_tokens".split(),
    ),
    "duplicate_injection": tuple(
        "call_ids source_refs occurrences threshold_tokens".split(),
    ),
    "repeated_tool_call": tuple(
        "operation_ids tool_name occurrences minimum_occurrences ".split(),
    ),
    "history_growth": tuple(
        "call_ids initial_tokens current_tokens growth_ratio phase_known "
        "threshold_tokens".split(),
    ),
    "output_budget": tuple(
        "call_ids output_tokens output_limit finish_reason threshold_ratio".split(),
    ),
}

USAGE_DETAIL_FIELDS = (
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
BOOLEAN_FIELDS = (
    "missing_before",
    "missing_after",
    "candidate_limited",
    "eligible",
    "reliable_grouping",
    "gap_tracking_limited",
    "fingerprint_available",
    "external_usage_unavailable",
    "is_cacheable",
    "polling",
    "phase_known",
)

RUN_WRITE_COLUMNS = tuple(
    column
    for column in COLUMNS["swe_token_runs"]
    if column
    not in {
        "gap_count",
        "data_revision",
        "analyzed_revision",
        "analysis_state",
        "dirty",
        "analysis_error",
        "claim_token",
        "lease_until",
        "gap_tracking_limited",
    }
)

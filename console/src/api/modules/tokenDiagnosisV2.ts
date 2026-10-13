import { request } from "../request";

export const DIAGNOSIS_RULE_IDS = [
  "context_growth",
  "segment_carryover",
  "tool_context_volume",
  "safe_tool_repeat",
  "suspected_loop",
  "text_prefix_stability",
  "static_context_volume",
  "cohort_usage_outlier",
] as const;
export type DiagnosisRuleId = (typeof DIAGNOSIS_RULE_IDS)[number];
export type EvaluationState =
  | "candidate"
  | "not_hit"
  | "insufficient_evidence"
  | "not_applicable";
export interface DiagnosisFilters {
  start?: string;
  end?: string;
  agent_id?: string;
  root_agent_id?: string;
  model_name?: string;
  rule_id?: DiagnosisRuleId;
  state?: string;
  branch_run_id?: string;
  limit?: number;
  cursor?: string;
}
export interface DiagnosisWindow {
  start: string;
  end: string;
  semantics: string;
}
export interface DiagnosisMetadata {
  scope: { source_id: string; tenant_id: string };
  window: DiagnosisWindow;
  as_of: string;
}
export interface EvidencePage<T> {
  items: T[];
  next_cursor: string | null;
}
export interface DiagnosisPage<T> extends DiagnosisMetadata, EvidencePage<T> {}
export interface UsageDTO {
  known_input_tokens: number;
  known_output_tokens: number;
  known_total_tokens: number;
  actual_total_tokens: number | null;
  observed_call_count: number;
  reported_call_count: number;
  partial_call_count: number;
  unknown_call_count: number;
  usage_complete: boolean;
}
export interface CompletenessDTO {
  branch_count: number;
  expected_attempt_count: number | null;
  observed_call_count: number;
  missing_child_count: number;
  gap_count: number;
  gap_tracking_limited: boolean;
  capture_state: string;
  usage_complete: boolean;
}
export interface EvaluationLocatorDTO {
  evaluation_id: string;
  task_run_id: string;
  branch_run_id: string;
  object_type: "branch" | "call" | "step";
  object_id: string;
  rule_id: DiagnosisRuleId;
  state: EvaluationState;
  input_revision: number;
  current_data_revision: number;
  snapshot_id: string;
  rule_version: number;
  threshold_version: number;
  freshness: "current" | "stale";
  freshness_reason: string | null;
  analysis_state: string;
}
export interface EvaluationLocatorsDTO {
  items: EvaluationLocatorDTO[];
  limited: boolean;
}
export interface RunDTO {
  task_run_id: string;
  branch_run_id: string;
  parent_branch_run_id: string | null;
  source_id: string;
  tenant_id: string;
  trace_id: string | null;
  agent_id: string | null;
  role: string | null;
  purpose: string | null;
  phase_id: string | null;
  started_at: string | null;
  ended_at: string | null;
  execution_state: string;
  analysis_state: string;
  data_revision: number;
  analyzed_revision: number;
  max_input_tokens: number | null;
  max_output_tokens: number | null;
  compaction_epoch: number;
  expected_attempt_count: number | null;
  gap_count: number;
  gap_tracking_limited: boolean;
}
export interface BusinessStepDTO {
  step_id: string;
  started_at: string | null;
  ended_at: string | null;
  event_count: number;
  evaluations: EvaluationLocatorsDTO;
}
export interface BranchDTO extends RunDTO {
  freshness: string;
  evaluations: EvaluationLocatorsDTO;
  steps?: {
    items: BusinessStepDTO[];
    limited: boolean;
    order_known: boolean;
    reason_code: string | null;
  };
}
export interface TaskListItemDTO extends RunDTO {
  completeness: CompletenessDTO;
  lifecycle_usage: UsageDTO;
  selected_contribution: UsageDTO;
  window_attempt_usage: UsageDTO;
}
export interface TaskDTO extends DiagnosisMetadata {
  task_run_id: string;
  root: RunDTO;
  branches: BranchDTO[];
  branches_limited: boolean;
  lifecycle_usage: UsageDTO;
  selected_contribution: UsageDTO;
  window_attempt_usage: UsageDTO;
  completeness: CompletenessDTO;
}
export interface CallRecordDTO {
  call_id: string;
  task_run_id: string;
  branch_run_id: string;
  logical_call_id: string | null;
  logical_seq: number | null;
  attempt_no: number | null;
  agent_id: string | null;
  provider_id: string | null;
  model_name: string | null;
  purpose: string | null;
  started_at: string | null;
  ended_at: string | null;
  execution_state: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  estimated_input_tokens: number | null;
  max_input_tokens: number | null;
  max_output_tokens: number | null;
  usage_status: string | null;
  capture_state: string | null;
  context_id: string | null;
  step_id: string | null;
  parent_step_id: string | null;
  identity_source: string | null;
  compaction_epoch: number | null;
  assembly_stage: string | null;
  prompt_format_confidence: string | null;
  context_builder_version: string | null;
  estimator_version: string | null;
  usage_origin: string | null;
  usage_accuracy: string | null;
  finish_reason: string | null;
  usage_details: {
    cache_read_tokens?: number;
    cache_write_tokens?: number;
    reasoning_tokens?: number;
  };
}
export interface CallDTO extends CallRecordDTO {
  evaluations: EvaluationLocatorsDTO;
}
export interface ContextRecordDTO {
  context_id: string;
  task_run_id: string;
  branch_run_id: string;
  call_id: string | null;
  operation_id: string | null;
  context_role: string | null;
  agent_id: string | null;
  provider_id: string | null;
  model_name: string | null;
  assembly_stage: string | null;
  representation_version: string | null;
  estimator_version: string | null;
  key_version: string | null;
  capture_state: string | null;
  capture_reason: string | null;
  text_bytes: number | null;
  encoded_bytes: number | null;
  estimated_tokens: number | null;
  expected_item_count: number | null;
  context_revision: number | null;
  request_started_at: string | null;
  request_ended_at: string | null;
  execution_state: string | null;
}
export interface OccurrenceDTO {
  context_id: string;
  occurrence_index: number;
  source_object_id: string | null;
  source_version: string | null;
  source_ref: string | null;
  producer_operation_id: string | null;
  business_step_id: string | null;
  category: string | null;
  attribution: string | null;
  lifecycle_state: string | null;
  data_version: string | null;
  text_start: number | null;
  text_end: number | null;
  encoded_start: number | null;
  encoded_end: number | null;
  token_start: number | null;
  token_end: number | null;
  estimated_tokens: number | null;
}
export interface CompositionCategoryDTO {
  category: string | null;
  occurrence_count: number;
  known_estimated_tokens: number;
  estimated_tokens: number | null;
  unknown_estimate_count: number;
  unknown_category_count: number;
  unknown_attribution_count: number;
}
export interface CompositionDTO {
  unit: "estimated_tokens";
  categories: CompositionCategoryDTO[];
  occurrence_count: number;
  known_estimated_tokens: number;
  estimated_tokens: number | null;
  complete: boolean;
  unknown_category_count: number;
  unknown_attribution_count: number;
  reason_code: string | null;
  population: string;
}
export interface ContextDTO extends DiagnosisMetadata {
  context: ContextRecordDTO;
  occurrences: EvidencePage<OccurrenceDTO>;
  composition: CompositionDTO;
}
export interface PrefixComparisonDTO {
  comparison_id: string;
  current_context_id: string;
  current_context_revision: number;
  call_id: string;
  snapshot_id: string;
  input_revision: number;
  representation_version: string | null;
  key_version: string | null;
  window_hours: number;
  candidate_limit: number;
  candidate_count: number | null;
  candidate_limited: boolean;
  selection_version: string | null;
  best_lower_context_id: string | null;
  max_upper_context_id: string | null;
  lower_text_bytes: number | null;
  upper_text_bytes: number | null;
  total_text_bytes: number | null;
  first_difference_index: number | null;
  analysis_state: string;
  reason_code: string | null;
  eligible: boolean;
  created_at: string;
}
export interface PrefixCandidateDTO {
  candidate_index: number;
  historical_context_id: string;
  historical_context_revision: number;
  historical_call_id: string | null;
  historical_ended_at: string | null;
  lower_text_bytes: number | null;
  upper_text_bytes: number | null;
  first_difference_index: number | null;
  evidence_expired: boolean;
}
export interface PrefixDTO extends DiagnosisMetadata {
  call_id: string;
  comparison: PrefixComparisonDTO | null;
  candidates: EvidencePage<PrefixCandidateDTO>;
  is_current: boolean;
  evidence_expired?: boolean;
  reason_code: string | null;
}
export interface OperationSegmentDTO {
  operation_id: string;
  relation_index: number;
  relation_type: string;
  before_context_id: string | null;
  before_occurrence_index: number | null;
  after_context_id: string | null;
  after_occurrence_index: number | null;
  source_object_id: string | null;
  source_version: string | null;
}
export interface OperationDTO {
  operation_id: string;
  task_run_id: string;
  branch_run_id: string;
  operation_type: string | null;
  operation_state: string | null;
  trigger_reason: string | null;
  started_at: string | null;
  ended_at: string | null;
  before_context_id: string | null;
  after_context_id: string | null;
  summary_call_id: string | null;
  missing_before: boolean | null;
  missing_after: boolean | null;
  capture_state: string | null;
  before_context: ContextRecordDTO | null;
  after_context: ContextRecordDTO | null;
  summary_call: CallRecordDTO | null;
  comparison: {
    unit: "estimated_tokens";
    before_estimated_tokens: number | null;
    after_estimated_tokens: number | null;
    saved_estimated_tokens: number | null;
    reason_code: string | null;
  };
  evidence_expired: {
    before_context: boolean;
    after_context: boolean;
    summary_call: boolean;
  };
  segments: EvidencePage<OperationSegmentDTO>;
}
export interface IssueGroupDTO {
  group_id: string;
  reliable_grouping: boolean;
  rule_id: DiagnosisRuleId;
  recommendation_code: string;
  hit_count: number;
  distinct_task_count: number;
  first_seen: string;
  latest_seen: string;
}
export interface FindingDTO {
  finding_id: string;
  task_run_id: string;
  branch_run_id: string;
  evaluation_id: string;
  instance_key: string;
  signature: string | null;
  signature_version: number;
  reliable_grouping: boolean;
  recommendation_code: string;
  rule_id: DiagnosisRuleId;
  rule_version: number;
  threshold_version: number;
  agent_id: string | null;
  provider_id: string | null;
  model_name: string | null;
  call_id: string | null;
  context_id: string | null;
  step_id: string | null;
  operation_id: string | null;
  created_at: string;
  evaluation_state?: EvaluationState;
}
export interface EvaluationRecordDTO {
  evaluation_id: string;
  task_run_id: string;
  branch_run_id: string;
  object_type: "branch" | "call" | "step";
  object_id: string;
  rule_id: DiagnosisRuleId;
  state: EvaluationState;
  input_revision: number;
  snapshot_id: string;
  rule_version: number;
  threshold_version: number;
  created_at: string;
  replaces_evaluation_id: string | null;
}
export interface EvaluationValueDTO {
  value_key: string;
  unit: "tokens" | "estimated_tokens" | "bytes" | "count" | "ratio";
  value_type: "integer" | "decimal" | "enum";
  integer_value: number | null;
  decimal_value: number | null;
  enum_value: string | null;
  lower_bound: number | null;
  upper_bound: number | null;
  threshold: number | null;
}
export interface EvaluationRefDTO {
  reference_index: number;
  reference_type: string;
  object_id: string;
  context_id: string | null;
  occurrence_index: number | null;
  evidence_expired: boolean;
}
export interface RecommendationDTO {
  action_code: string;
  location: string;
  prerequisites: string[];
  exceptions: string[];
  risks: string[];
  verification: string[];
}
export interface EvaluationDTO extends DiagnosisMetadata {
  evaluation: EvaluationRecordDTO;
  is_current: boolean;
  values: EvaluationValueDTO[];
  gaps: { reason_code: string }[];
  refs: EvaluationRefDTO[];
  findings: FindingDTO[];
  recommendation: RecommendationDTO | null;
  values_limited?: boolean;
  gaps_limited?: boolean;
  refs_limited?: boolean;
  findings_limited?: boolean;
}
export interface RuleCoverageDTO {
  rule_id: DiagnosisRuleId;
  branch_count: number;
  evaluation_count: number;
  candidate: number;
  not_hit: number;
  insufficient_evidence: number;
  not_applicable: number;
  pending: number;
  failed: number;
  population: string;
}
export interface DashboardDTO extends DiagnosisMetadata {
  window_attempt_usage: UsageDTO;
  lifecycle_distribution: {
    task_count: number;
    sample_count: number;
    mean_total_tokens: number | null;
    min_total_tokens: number | null;
    max_total_tokens: number | null;
    population: string;
    quantiles: {
      sample_count: number;
      p50: number | null;
      p90: number | null;
      p95: number | null;
      method: string;
      available: boolean;
      reason_code: string | null;
    };
  };
  projection_filters: Record<string, string[]>;
  agent_attribution: {
    executing_agents: AgentAttributionDTO;
    root_agents: AgentAttributionDTO;
  };
  rule_coverage: RuleCoverageDTO[];
  prefix: {
    request_count: number;
    matched_text_bytes: number;
    eligible_text_bytes: number;
    excluded: Record<string, number>;
    population: string;
    limited: boolean;
    ratio: number | null;
  };
  limits: { available: boolean; reason_code: string | null };
}
export interface AgentAttributionDTO {
  items: { agent_id: string | null; usage: UsageDTO }[];
  limited: boolean;
  population: string;
}
export interface OperationQueryOptions {
  operation_id?: string;
  relation_cursor?: string;
}
const root = "/monitor/token-diagnostics/v2";
function read<T>(
  path: string,
  signal: AbortSignal,
  filters: DiagnosisFilters = {},
  extra: OperationQueryOptions = {},
) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries({ ...filters, ...extra }))
    if (value !== undefined && value !== "") params.set(key, String(value));
  return request<T>(`${root}/${path}${params.size ? `?${params}` : ""}`, {
    signal,
  });
}
export const tokenDiagnosisV2Api = {
  dashboard: (signal: AbortSignal, filters?: DiagnosisFilters) =>
    read<DashboardDTO>("dashboard", signal, filters),
  tasks: (signal: AbortSignal, filters?: DiagnosisFilters) =>
    read<DiagnosisPage<TaskListItemDTO>>("tasks", signal, filters),
  task: (id: string, signal: AbortSignal, filters?: DiagnosisFilters) =>
    read<TaskDTO>(`tasks/${encodeURIComponent(id)}`, signal, filters),
  branches: (id: string, signal: AbortSignal, filters?: DiagnosisFilters) =>
    read<DiagnosisPage<BranchDTO>>(
      `tasks/${encodeURIComponent(id)}/branches`,
      signal,
      filters,
    ),
  calls: (id: string, signal: AbortSignal, filters?: DiagnosisFilters) =>
    read<DiagnosisPage<CallDTO>>(
      `tasks/${encodeURIComponent(id)}/calls`,
      signal,
      filters,
    ),
  context: (id: string, signal: AbortSignal, filters?: DiagnosisFilters) =>
    read<ContextDTO>(`contexts/${encodeURIComponent(id)}`, signal, filters),
  prefix: (id: string, signal: AbortSignal, filters?: DiagnosisFilters) =>
    read<PrefixDTO>(`calls/${encodeURIComponent(id)}/prefix`, signal, filters),
  operations: (
    id: string,
    signal: AbortSignal,
    filters?: DiagnosisFilters,
    options?: OperationQueryOptions,
  ) =>
    read<DiagnosisPage<OperationDTO>>(
      `tasks/${encodeURIComponent(id)}/operations`,
      signal,
      filters,
      options,
    ),
  issues: (signal: AbortSignal, filters?: DiagnosisFilters) =>
    read<DiagnosisPage<IssueGroupDTO>>("issues", signal, filters),
  instances: (id: string, signal: AbortSignal, filters?: DiagnosisFilters) =>
    read<DiagnosisPage<FindingDTO> & { group_id: string }>(
      `issues/${encodeURIComponent(id)}/instances`,
      signal,
      filters,
    ),
  evaluation: (id: string, signal: AbortSignal, filters?: DiagnosisFilters) =>
    read<EvaluationDTO>(
      `evaluations/${encodeURIComponent(id)}`,
      signal,
      filters,
    ),
};

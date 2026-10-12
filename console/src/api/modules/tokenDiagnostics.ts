import { request } from "../request";

export interface TokenSummary {
  known_input_tokens: number;
  known_output_tokens: number;
  known_total_tokens: number;
  observed_call_count: number;
  reported_call_count: number;
  partial_call_count: number;
  unknown_call_count: number;
  usage_complete: boolean;
  composition_complete?: boolean;
  capture_state: string;
  data_revision: number;
  analyzed_revision: number;
}
export interface TokenRun extends Partial<TokenSummary> {
  task_run_id: string;
  branch_run_id: string;
  parent_branch_run_id: string | null;
  trace_id?: string | null;
  agent_id?: string;
  role: string;
  purpose: string;
  started_at: string;
  execution_state: string;
  analysis_state: string;
  gap_count?: number;
  analysis_summary?: string | RuleState[];
}
export interface RuleState {
  rule_id: string;
  state: string;
  missing_fields: string[];
}
export interface TokenCall {
  call_id: string;
  logical_call_id: string;
  logical_seq: number;
  attempt_no: number;
  branch_run_id: string;
  purpose: string;
  provider_id: string;
  model_name: string;
  started_at: string;
  input_tokens: number | null;
  output_tokens: number | null;
  estimated_input_tokens: number | null;
  usage_status: string;
  capture_state: string;
  execution_state: string;
  max_output_tokens?: number | null;
  max_input_tokens?: number | null;
  compaction_epoch: number;
  finish_reason?: string | null;
  usage_details?: Record<string, number>;
}
export interface TokenItem {
  occurrence_index: number;
  category: string;
  source_ref: string;
  estimated_tokens: number | null;
  attribution_quality: string;
  fingerprint: string | null;
  producer_operation_id?: string | null;
}
export interface TokenCategory {
  category: string;
  estimated_tokens: number | null;
  item_count?: number;
}
export interface TokenFinding {
  finding_id: string;
  rule_id: string;
  rule_version: number;
  title: string;
  action: string;
  affected_tokens: number | null;
  evidence_quality: string;
  evidence: Record<string, unknown> & { call_ids?: string[] };
}
export interface TokenDetail {
  task_run_id: string;
  run: TokenRun;
  branches: TokenRun[];
  calls: TokenCall[];
  categories: TokenCategory[];
  findings: TokenFinding[];
  summary: TokenSummary;
  analysis_state: string;
  data_limited: boolean;
}
const root = "/monitor/token-diagnostics";
export const tokenDiagnosticsApi = {
  list: (signal: AbortSignal, cursor?: string, model?: string) => {
    const params = new URLSearchParams();
    if (cursor) params.set("cursor", cursor);
    if (model) params.set("model", model);
    return request<{ items: TokenRun[]; next_cursor: string | null }>(
      `${root}/runs?${params}`,
      { signal },
    );
  },
  detail: (task: string, signal: AbortSignal) =>
    request<TokenDetail>(`${root}/runs/${encodeURIComponent(task)}`, {
      signal,
    }),
  inputs: (call: string, signal: AbortSignal, offset = 0) =>
    request<{ items: TokenItem[]; has_more: boolean }>(
      `${root}/calls/${encodeURIComponent(call)}/inputs?offset=${offset}`,
      { signal },
    ),
  byTrace: (trace: string, signal: AbortSignal) =>
    request<{ items: { task_run_id: string }[] }>(
      `${root}/by-trace/${encodeURIComponent(trace)}`,
      { signal },
    ),
};

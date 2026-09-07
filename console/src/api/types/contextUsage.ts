export type ContextUsageStatus =
  | "normal"
  | "governance"
  | "active"
  | "emergency"
  | "overflow";

export interface ContextUsageUnavailableSnapshot {
  available: false;
}

export interface ContextUsageAvailableSnapshot {
  available: true;
  schema_version: number;
  used_tokens: number;
  max_tokens: number;
  remaining_tokens: number;
  usage_ratio: number;
  system_context_tokens: number;
  tool_definition_tokens: number;
  conversation_tokens: number;
  governance_threshold_ratio: number;
  active_threshold_ratio: number;
  emergency_threshold_ratio: number;
  status: ContextUsageStatus;
  estimated: boolean;
  stale: boolean;
  as_of: string;
}

export type ContextUsageSnapshot =
  | ContextUsageUnavailableSnapshot
  | ContextUsageAvailableSnapshot;

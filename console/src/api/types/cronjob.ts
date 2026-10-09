import type { ModelSlotConfig } from "./provider";

export interface CronJobSchedule {
  type: "cron";
  cron: string;
  timezone?: string;
}

export interface CronJobTarget {
  user_id: string;
  session_id: string;
}

export interface CronJobDispatch {
  type: "channel";
  channel?: string;
  target: CronJobTarget;
  mode?: "stream" | "final";
  meta?: Record<string, unknown>;
}

export interface CronJobRuntime {
  max_concurrency?: number;
  timeout_seconds?: number;
  misfire_grace_seconds?: number;
}

export interface CronJobRequest {
  input: unknown;
  session_id?: string | null;
  user_id?: string | null;
  [key: string]: unknown;
}

export interface CronJobState {
  next_run_at?: string | null;
  next_run_times?: string[] | null;
  last_run_at?: string | null;
  last_status?:
    | "success"
    | "error"
    | "running"
    | "skipped"
    | "cancelled"
    | null;
  last_error?: string | null;
}

export interface CronTaskView {
  visible_in_my_tasks: boolean;
  chat_id?: string | null;
  session_id?: string | null;
  has_scheduled_result: boolean;
  latest_scheduled_preview: string;
  unread_execution_count: number;
  last_scheduled_run_at?: string | null;
  is_running: boolean;
  is_paused?: boolean;
  pause_reason?: "manual" | "auto_unread_threshold" | null;
  auto_paused_at?: string | null;
}

export interface CronJobSpecInput {
  id: string;
  name: string;
  enabled?: boolean;
  schedule: CronJobSchedule;
  task_type?: "text" | "agent" | "workflow";
  text?: string;
  skill_ids?: string;
  workflow_binding_id?: string | null;
  model_slot?: ModelSlotConfig | null;
  request?: CronJobRequest;
  dispatch: CronJobDispatch;
  runtime?: CronJobRuntime;
  meta?: Record<string, unknown>;
}

export interface CronJobSpecOutput extends CronJobSpecInput {
  state?: CronJobState;
  task?: CronTaskView | null;
}

export interface CronJobView {
  spec: CronJobSpecOutput;
  state?: CronJobState;
  task?: CronTaskView | null;
}

export interface CronBroadcastTenantResult {
  tenant_id: string;
  success: boolean;
  job_id: string;
  cron: string;
  timezone: string;
  offset_minutes: number;
  notification_timezone: string;
  error: string;
  warning: string;
}

export interface CronBroadcastTarget {
  tenant_id: string;
  tenant_name?: string | null;
  bbk_id?: string | null;
}

export interface CronBroadcastOptions {
  enable_offset?: boolean;
  enable_batch_dispatch?: boolean;
  offset_window_hours?: number;
}

export interface CronBatchDispatchOptions {
  offset_window_hours?: number;
}

export interface CronBroadcastResponse {
  results: CronBroadcastTenantResult[];
}

export type CronBroadcastTaskStatus = "running" | "completed" | "failed";

export interface CronBroadcastTaskResponse {
  task_id: string;
  status: CronBroadcastTaskStatus;
  tenant_count: number;
  completed_count: number;
  failed_count: number;
  results: CronBroadcastTenantResult[];
  failure_summary?: string | null;
  updated_at?: string | null;
  reused: boolean;
}

export interface CronBroadcastCurrentTaskResponse {
  task?: CronBroadcastTaskResponse | null;
}

export interface CronBroadcastChildItem {
  tenant_id: string;
  tenant_name?: string | null;
  bbk_id?: string | null;
  job_id: string;
  job_name: string;
  enabled: boolean;
  cron: string;
  timezone: string;
  offset_minutes: number;
  last_status?: string | null;
  last_run_at?: string | null;
  last_error?: string | null;
}

export type CronBroadcastChildrenLookupStatus =
  | "idle"
  | "running"
  | "completed"
  | "failed";

export interface CronBroadcastChildrenResponse {
  items: CronBroadcastChildItem[];
  status: CronBroadcastChildrenLookupStatus;
  tenant_count: number;
  failed_tenants: number;
  failure_summary?: string | null;
  updated_at?: string | null;
}

export interface CronBroadcastChildrenRefreshResponse
  extends CronBroadcastChildrenResponse {
  reused: boolean;
}

export interface CronBroadcastChildRef {
  tenant_id: string;
  job_id: string;
}

export interface CronBroadcastChildOperationResult {
  tenant_id: string;
  job_id: string;
  success: boolean;
  status: "deleted" | "started" | "skipped" | "failed" | string;
  message: string;
}

export interface CronBroadcastChildrenBatchResponse {
  results: CronBroadcastChildOperationResult[];
}

export type CronJobSpecInputLegacy = Record<string, unknown>;
export type CronJobSpecOutputLegacy = Record<string, unknown>;
export type CronJobViewLegacy = Record<string, unknown>;

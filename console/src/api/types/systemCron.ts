/** 系统任务只使用独立系统表，不是普通/批任务的用户任务定义。 */
export type SystemTaskType =
  | "heartbeat"
  | "dream"
  | "task_session_cleanup"
  | "archive_maintenance";
export type SystemScopeKind = "agent" | "source";

export interface SystemJobConfig {
  enabled: boolean;
  cron: string;
  timezone: string;
  timeout_seconds: number;
}

export interface SystemCronJob {
  job_id: string;
  scope_kind: SystemScopeKind;
  task_type: SystemTaskType;
  tenant_id: string;
  source_id: string;
  agent_id: string;
  management_owner: "scheduler";
  enabled: boolean;
  desired_config: Partial<SystemJobConfig> | null;
  effective_config: Partial<SystemJobConfig> | null;
  binding_known: boolean;
  has_effective_binding: boolean;
  sync_status: "pending" | "running" | "failed" | "synced";
  sync_stage: string;
  error_code: string | null;
  version: number;
  generation: number;
  synced_generation: number;
  active_execution_id: string | null;
  last_status: string | null;
  needs_verification: boolean;
}

/** 仅本次保存的受理事实，不冒充平台已同步，更不属于配置正文。 */
export interface SystemSyncSubmission {
  submission_status: "accepted" | "failed";
  request_id?: string;
  error_code?: string;
  items?: Array<{
    task_type: SystemTaskType;
    submission_status: "accepted" | "failed";
    request_id?: string;
    error_code?: string;
  }>;
}

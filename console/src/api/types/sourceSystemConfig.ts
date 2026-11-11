import type { SystemSyncSubmission } from "./systemCron";

export interface SourceSystemConfig extends Record<string, unknown> {
  system_prompt_injections?: string[];
  approval_notifications?: {
    zhaohu_tool_guard_enabled?: boolean;
    [key: string]: unknown;
  };
  archive_maintenance?: {
    enabled?: boolean;
    cron?: string;
    old_orphan_days?: number;
    max_workspaces_per_run?: number;
    max_files_per_workspace?: number;
    max_files_per_run?: number;
    timeout_seconds?: number;
    [key: string]: unknown;
  };
  cron_notifications?: {
    skip_weekend_zhaohu_enabled?: boolean;
    [key: string]: unknown;
  };
}

export interface EffectiveSourceSystemConfig {
  source_id: string;
  config: SourceSystemConfig;
  version: number;
  is_default: boolean;
  stale: boolean;
  last_error?: string | null;
  updated_by?: string | null;
  updated_at?: string | null;
}

export interface CurrentSourceSystemConfigResponse {
  source_id: string;
  config: SourceSystemConfig;
  version: number;
  is_default: boolean;
  updated_by?: string | null;
  updated_at?: string | null;
  scheduler_sync?: SystemSyncSubmission;
}

export interface CurrentSourceSystemConfigUpdateRequest {
  config: SourceSystemConfig;
}

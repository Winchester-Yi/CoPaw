import { request } from "../request";
import { buildAuthHeaders } from "../authHeaders";
import { getApiUrl } from "../config";

// Types for Monitor Cron Overview

export interface FilterOption {
  value: string;
  label: string;
}

export interface FilterOptionsResponse {
  users: FilterOption[];
  bbk_ids: FilterOption[];
  channels: FilterOption[];
  source_ids: FilterOption[];
  job_names: FilterOption[];
  job_ids: FilterOption[];
}

export interface CronJobItem {
  id: string;
  name: string;
  tenant_id: string;
  tenant_name: string;
  bbk_id: string;
  source_id: string;
  enabled: boolean;
  task_type: string;
  cron_expr: string;
  timezone: string;
  channel: string;
  target_user_id: string;
  target_session_id: string;
  timeout_seconds: number;
  max_concurrency: number;
  misfire_grace_seconds: number;
  text_content: string;
  request_input: string;
  creator_user_id: string;
  task_chat_id: string;
  task_session_id: string;
  job_origin: string;
  subscription_key: string;
  meta: string;
  status: string;
  pause_reason: string;
  execution_count: number;
  today_status: string | null; // 今日最新执行状态: success/error/cancelled/timeout/skipped
  created_at: string | null;
  updated_at: string | null;
  deleted_at: string | null;
}

export interface ExecutionItem {
  id: number;
  job_id: string;
  job_name: string;
  tenant_id: string;
  tenant_name: string;
  scheduled_time: string | null;
  actual_time: string;
  end_time: string | null;
  duration_ms: number;
  status: string;
  error_message: string;
  instance_id: string;
  executor_leader: string;
  is_manual: boolean;
  trace_id: string;
  session_id: string;
  input_snapshot: string;
  output_preview: string;
  meta: string;
  is_read: boolean;
  read_at: string | null;
  created_at: string | null;
}

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface MarkReadResponse {
  marked: boolean;
  count: number;
}

export interface UnreadCountItem {
  job_id: string;
  job_name: string;
  unread_count: number;
}

export interface UnreadCountResponse {
  items: UnreadCountItem[];
  total_unread: number;
}

export interface CronOverviewMetricItem {
  key: string;
  value: number;
  compare?: string;
  trend?: "up" | "down";
}

export interface CronOverviewDistributionItem {
  name: string;
  value: number;
  percent: number;
}

export interface CronOverviewBranchExecutionItem {
  name: string;
  success: number;
  failed: number;
  skipped: number;
}

export interface CronOverviewBranchReadItem {
  name: string;
  read: number;
  unread: number;
}

export interface CronOverviewResponse {
  start_time: string | null;
  end_time: string | null;
  metrics: CronOverviewMetricItem[];
  task_status: CronOverviewDistributionItem[];
  execution_result: CronOverviewDistributionItem[];
  read_status: CronOverviewDistributionItem[];
  failure_reasons: CronOverviewDistributionItem[];
  branch_tasks: CronOverviewDistributionItem[];
  branch_execution: CronOverviewBranchExecutionItem[];
  branch_read: CronOverviewBranchReadItem[];
}

export interface SubscriptionOverviewItem {
  subscription_key: string;
  task_name: string;
  subscriber_count: number;
  total_task_count: number;
  running_task_count: number;
  pending_task_count: number;
  executed_task_count: number;
  failed_task_count: number;
  avg_duration_ms: number;
  success_rate: number;
}

export interface SubscriptionDetailItem {
  job_id: string;
  subscriber_id: string;
  subscriber_name: string;
  bbk_id: string;
  enabled: boolean;
  execution_status: string;
  execution_time: string | null;
}

// API functions
export const monitorApi = {
  // Get filter options for dropdown selects
  getFilterOptions: async (): Promise<FilterOptionsResponse> => {
    return request(`/monitor/cron/filter-options`);
  },

  // Get page-shaped aggregate data for the cron overview
  getCronOverview: async (filters?: {
    tenant_id?: string;
    bbk_id?: string;
    start_time?: string;
    end_time?: string;
  }): Promise<CronOverviewResponse> => {
    const params = new URLSearchParams();
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== "") {
          params.append(key, value);
        }
      });
    }
    const query = params.toString();
    return request(`/monitor/cron/overview${query ? `?${query}` : ""}`);
  },

  // Get cron jobs list
  getJobs: async (
    page = 1,
    pageSize = 20,
    filters?: {
      tenant_id?: string;
      bbk_id?: string;
      creator_user_id?: string;
      job_origin?: string;
      status?: string;
      enabled?: boolean;
    },
  ): Promise<PaginatedResponse<CronJobItem>> => {
    const params = new URLSearchParams();
    params.append("page", page.toString());
    params.append("page_size", pageSize.toString());
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== "" && value !== "all") {
          params.append(key, value.toString());
        }
      });
    }
    return request(`/monitor/cron/jobs?${params.toString()}`);
  },

  // Get single job
  getJob: async (jobId: string): Promise<CronJobItem> => {
    return request(`/monitor/cron/jobs/${jobId}`);
  },

  // Get executions list
  getExecutions: async (
    page = 1,
    pageSize = 20,
    filters?: {
      job_id?: string;
      tenant_id?: string;
      status?: string;
      start_time?: string;
      end_time?: string;
    },
  ): Promise<PaginatedResponse<ExecutionItem>> => {
    const params = new URLSearchParams();
    params.append("page", page.toString());
    params.append("page_size", pageSize.toString());
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== "") {
          params.append(key, value);
        }
      });
    }
    return request(`/monitor/cron/executions?${params.toString()}`);
  },

  // Get single execution
  getExecution: async (executionId: number): Promise<ExecutionItem> => {
    return request(`/monitor/cron/executions/${executionId}`);
  },

  // Get subscription-level overview rows
  getSubscriptionOverview: async (
    page = 1,
    pageSize = 20,
    filters?: {
      keyword?: string;
      tenant_id?: string;
      bbk_id?: string;
      source_id?: string;
      start_time?: string;
      end_time?: string;
    },
  ): Promise<PaginatedResponse<SubscriptionOverviewItem>> => {
    const params = new URLSearchParams();
    params.append("page", page.toString());
    params.append("page_size", pageSize.toString());
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== "") {
          params.append(key, value);
        }
      });
    }
    return request(`/monitor/cron/subscription-overview?${params.toString()}`);
  },

  // Get subscription detail rows for a drawer/table
  getSubscriptionDetails: async (
    subscriptionKey: string,
    page = 1,
    pageSize = 20,
    filters?: {
      tenant_id?: string;
      bbk_id?: string;
      source_id?: string;
      start_time?: string;
      end_time?: string;
    },
  ): Promise<PaginatedResponse<SubscriptionDetailItem>> => {
    const params = new URLSearchParams();
    params.append("page", page.toString());
    params.append("page_size", pageSize.toString());
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== "") {
          params.append(key, value);
        }
      });
    }
    return request(
      `/monitor/cron/subscription-overview/${encodeURIComponent(
        subscriptionKey,
      )}/jobs?${params.toString()}`,
    );
  },

  // Export jobs to Excel
  exportJobs: async (
    filters?: {
      tenant_id?: string;
      bbk_id?: string;
      enabled?: boolean;
    },
  ): Promise<Blob> => {
    const params = new URLSearchParams();
    params.append("export_type", "jobs");
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value !== undefined && value !== null) params.append(key, value.toString());
      });
    }
    const url = getApiUrl(`/monitor/cron/export?${params.toString()}`);
    const headers = new Headers(buildAuthHeaders());
    const response = await fetch(url, { headers });
    if (!response.ok) {
      let errorMessage = `Export failed: ${response.status} ${response.statusText}`;
      try {
        const errorData = await response.json();
        if (errorData.detail) {
          errorMessage = errorData.detail;
        }
      } catch {
        // Ignore JSON parse error
      }
      throw new Error(errorMessage);
    }
    return response.blob();
  },

  // Export executions to Excel
  exportExecutions: async (
    filters?: {
      job_id?: string;
      tenant_id?: string;
      status?: string;
      start_time?: string;
      end_time?: string;
    },
  ): Promise<Blob> => {
    const params = new URLSearchParams();
    params.append("export_type", "executions");
    if (filters) {
      Object.entries(filters).forEach(([key, value]) => {
        if (value) params.append(key, value);
      });
    }
    const url = getApiUrl(`/monitor/cron/export?${params.toString()}`);
    const headers = new Headers(buildAuthHeaders());
    const response = await fetch(url, { headers });
    if (!response.ok) {
      let errorMessage = `Export failed: ${response.status} ${response.statusText}`;
      try {
        const errorData = await response.json();
        if (errorData.detail) {
          errorMessage = errorData.detail;
        }
      } catch {
        // Ignore JSON parse error
      }
      throw new Error(errorMessage);
    }
    return response.blob();
  },

  // Mark job as read
  markJobAsRead: async (jobId: string): Promise<MarkReadResponse> => {
    return request(`/monitor/cron/jobs/${jobId}/mark-read`, { method: "POST" });
  },

  // Get unread count
  getUnreadCount: async (tenantId?: string): Promise<UnreadCountResponse> => {
    const params = new URLSearchParams();
    if (tenantId) {
      params.append("tenant_id", tenantId);
    }
    return request(`/monitor/cron/unread-count?${params.toString()}`);
  },
};

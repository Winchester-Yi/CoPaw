import { request } from "../request";
import type {
  CronBroadcastChildRef,
  CronBroadcastChildrenBatchResponse,
  CronBroadcastChildrenRefreshResponse,
  CronBroadcastChildrenResponse,
  CronBroadcastCurrentTaskResponse,
  CronBroadcastOptions,
  CronBroadcastTaskResponse,
  CronBroadcastTarget,
  CronBatchDispatchOptions,
  CronJobSpecInput,
  CronJobSpecOutput,
  CronJobView,
} from "../types";

export const cronJobApi = {
  listCronJobs: () => request<CronJobSpecOutput[]>("/cron/jobs"),

  createCronJob: (spec: CronJobSpecInput) =>
    request<CronJobSpecOutput>("/cron/jobs", {
      method: "POST",
      body: JSON.stringify(spec),
    }),

  getCronJob: (jobId: string) =>
    request<CronJobView>(`/cron/jobs/${encodeURIComponent(jobId)}`),

  replaceCronJob: (jobId: string, spec: CronJobSpecInput) =>
    request<CronJobSpecOutput>(`/cron/jobs/${encodeURIComponent(jobId)}`, {
      method: "PUT",
      body: JSON.stringify(spec),
    }),

  deleteCronJob: (jobId: string) =>
    request<void>(`/cron/jobs/${encodeURIComponent(jobId)}`, {
      method: "DELETE",
    }),

  pauseCronJob: (jobId: string) =>
    request<void>(`/cron/jobs/${encodeURIComponent(jobId)}/pause`, {
      method: "POST",
    }),

  resumeCronJob: (jobId: string) =>
    request<void>(`/cron/jobs/${encodeURIComponent(jobId)}/resume`, {
      method: "POST",
    }),

  runCronJob: (jobId: string) =>
    request<void>(`/cron/jobs/${encodeURIComponent(jobId)}/run`, {
      method: "POST",
    }),

  triggerCronJob: (jobId: string) =>
    request<void>(`/cron/jobs/${encodeURIComponent(jobId)}/run`, {
      method: "POST",
    }),

  markTaskRead: (jobId: string, marked_read?: boolean) =>
    request<{ marked_read: boolean }>(
      `/cron/jobs/${encodeURIComponent(jobId)}/task/mark-read`,
      {
        method: "POST",
        body: JSON.stringify({ marked_read }),
      },
    ),

  getCronJobState: (jobId: string) =>
    request<unknown>(`/cron/jobs/${encodeURIComponent(jobId)}/state`),

  listCronBroadcastTenants: () =>
    request<{ tenant_ids: string[] }>("/cron/broadcast/tenants"),

  broadcastCronJob: (
    jobId: string,
    targets: CronBroadcastTarget[],
    options: CronBroadcastOptions = {},
  ) =>
    request<CronBroadcastTaskResponse>(
      `/cron/jobs/${encodeURIComponent(jobId)}/broadcast`,
      {
        method: "POST",
        body: JSON.stringify({
          target_tenant_ids: targets.map((target) => target.tenant_id),
          targets,
          enable_offset: options.enable_offset ?? true,
          enable_batch_dispatch: options.enable_batch_dispatch,
          offset_window_hours: options.offset_window_hours ?? 4,
        }),
      },
    ),

  enableCronBatchDispatch: (
    jobId: string,
    options: CronBatchDispatchOptions = {},
  ) =>
    request<CronJobSpecOutput>(
      `/cron/jobs/${encodeURIComponent(jobId)}/batch-dispatch/enable`,
      {
        method: "POST",
        body: JSON.stringify({
          offset_window_hours: options.offset_window_hours ?? 4,
        }),
      },
    ),

  disableCronBatchDispatch: (jobId: string) =>
    request<CronJobSpecOutput>(
      `/cron/jobs/${encodeURIComponent(jobId)}/batch-dispatch/disable`,
      {
        method: "POST",
      },
    ),

  getCronBroadcastTask: (jobId: string, taskId: string) =>
    request<CronBroadcastTaskResponse>(
      `/cron/jobs/${encodeURIComponent(
        jobId,
      )}/broadcast/tasks/${encodeURIComponent(taskId)}`,
    ),

  getCurrentCronBroadcastTask: (jobId: string) =>
    request<CronBroadcastCurrentTaskResponse>(
      `/cron/jobs/${encodeURIComponent(jobId)}/broadcast/tasks/current`,
    ),

  listCronBroadcastChildren: (jobId: string) =>
    request<CronBroadcastChildrenResponse>(
      `/cron/jobs/${encodeURIComponent(jobId)}/broadcast/children`,
    ),

  refreshCronBroadcastChildren: (jobId: string) =>
    request<CronBroadcastChildrenRefreshResponse>(
      `/cron/jobs/${encodeURIComponent(jobId)}/broadcast/children/refresh`,
      {
        method: "POST",
      },
    ),

  deleteCronBroadcastChildren: (
    jobId: string,
    items: CronBroadcastChildRef[],
  ) =>
    request<CronBroadcastChildrenBatchResponse>(
      `/cron/jobs/${encodeURIComponent(jobId)}/broadcast/children/delete`,
      {
        method: "POST",
        body: JSON.stringify({ items }),
      },
    ),

  runCronBroadcastChildren: (
    jobId: string,
    items: CronBroadcastChildRef[],
  ) =>
    request<CronBroadcastChildrenBatchResponse>(
      `/cron/jobs/${encodeURIComponent(jobId)}/broadcast/children/run`,
      {
        method: "POST",
        body: JSON.stringify({ items }),
      },
    ),
};

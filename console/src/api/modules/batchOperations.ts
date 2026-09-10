import { request } from "../request";

export interface BatchPriority {
  user_ids: string[];
  branch_ids: string[];
}
export interface BatchRunState {
  source_id: string;
  tenant_id: string;
  parent_job_id: string;
  paused: boolean;
  version: number;
  resumed_at: string | null;
  updated_at: string;
  claimed_count: number;
  inflight_count: number;
}
export interface FailureCandidate {
  id: number;
  attempt_count: number;
  tenant_id: string;
  error_message: string;
  failure_type: string;
}
export interface FailurePreview {
  counts: { failure_type: string; label: string; count: number }[];
  items: FailureCandidate[];
  has_more: boolean;
}
const path = (id: string) => `/cron/dispatch/batches/${encodeURIComponent(id)}`;
export const batchOperations = {
  getRunState: (jobId: string) =>
    request<BatchRunState>(
      `/cron/jobs/${encodeURIComponent(jobId)}/batch-dispatch/run-state`,
    ),
  setRunState: (jobId: string, paused: boolean, expectedVersion: number) =>
    request<BatchRunState>(
      `/cron/jobs/${encodeURIComponent(jobId)}/batch-dispatch/run-state`,
      {
        method: "PUT",
        body: JSON.stringify({ paused, expected_version: expectedVersion }),
      },
    ),
  savePriority: (jobId: string, priority: BatchPriority) =>
    request(`/cron/jobs/${encodeURIComponent(jobId)}/batch-dispatch/priority`, {
      method: "PUT",
      body: JSON.stringify(priority),
    }),
  failures: (batchId: string, types: string[]) =>
    request<FailurePreview>(
      `${path(batchId)}/failures?${new URLSearchParams({
        failure_types: types.join(","),
      })}`,
    ),
  retry: (
    batchId: string,
    candidates: FailureCandidate[],
    resolved: boolean,
    stopped: boolean,
  ) =>
    request<{ queued: number; skipped: number; dispatch_paused?: boolean }>(
      `${path(batchId)}/retry`,
      {
        method: "POST",
        body: JSON.stringify({
          candidates: candidates.map(({ id, attempt_count }) => ({
            id,
            attempt_count,
          })),
          confirm_resolved: resolved,
          confirm_stopped: stopped,
        }),
      },
    ),
};

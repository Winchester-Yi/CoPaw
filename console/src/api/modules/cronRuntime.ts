import { request } from "../request";
import { cronContextHeaders } from "../cronTaskContext";
import type { ManagedCronContext } from "../types/schedulerCron";
import type { CronJobState, CronTaskView } from "../types/cronjob";

export interface RuntimeStatus {
  id: string;
  status: "ok" | "unavailable" | "state_missing" | "binding_missing";
  enabled?: boolean;
  state?: CronJobState & { last_error_truncated?: boolean };
  task?: CronTaskView & { latest_scheduled_preview_truncated?: boolean };
  definition_version?: number;
  management_version?: number;
  state_version?: number;
  management_status?: string;
  dispatch_state?: string;
}

const nullableText = (value: unknown) =>
  value === null || typeof value === "string";
function validateItem(item: RuntimeStatus, scheduler: boolean) {
  const statuses = scheduler
    ? ["ok", "unavailable", "state_missing"]
    : ["ok", "unavailable", "binding_missing"];
  if (!statuses.includes(item.status))
    throw new Error("invalid_task_runtime_status");
  if (item.status !== "ok") return;
  const state = item.state,
    task = item.task;
  if (
    typeof item.enabled !== "boolean" ||
    !state ||
    !task ||
    !Array.isArray(state.next_run_times) ||
    !state.next_run_times.every((value) => typeof value === "string") ||
    ![
      state.next_run_at,
      state.last_run_at,
      state.last_error,
      task.chat_id,
      task.session_id,
      task.auto_paused_at,
      task.last_scheduled_run_at,
      task.pause_reason,
    ].every(nullableText) ||
    ![null, "running", "success", "error", "skipped", "cancelled"].includes(
      state.last_status ?? null,
    ) ||
    ![
      "visible_in_my_tasks",
      "has_scheduled_result",
      "is_running",
      "is_paused",
    ].every((key) => typeof task[key as keyof CronTaskView] === "boolean") ||
    !Number.isSafeInteger(task.unread_execution_count) ||
    task.unread_execution_count < 0 ||
    typeof task.latest_scheduled_preview !== "string" ||
    Array.from(task.latest_scheduled_preview).length > 256 ||
    typeof state.last_error_truncated !== "boolean" ||
    typeof task.latest_scheduled_preview_truncated !== "boolean"
  )
    throw new Error("invalid_task_runtime_item");
  if (
    scheduler &&
    (!nullableText(item.management_status) ||
      !nullableText(item.dispatch_state) ||
      ![
        item.definition_version,
        item.management_version,
        item.state_version,
      ].every((value) => Number.isSafeInteger(value) && value! >= 0))
  )
    throw new Error("invalid_task_runtime_version");
}

/** A complete source round is returned only after every bounded batch succeeds. */
export async function loadRuntimeStatuses(
  owner: "legacy" | "scheduler",
  context: ManagedCronContext,
  ids: string[],
  signal?: AbortSignal,
): Promise<RuntimeStatus[]> {
  const result: RuntimeStatus[] = [];
  const unique = [...new Set(ids)];
  const root = owner === "legacy" ? "/cron/jobs" : "/scheduler/cron/jobs";
  for (let offset = 0; offset < unique.length; offset += 200) {
    if (signal?.aborted)
      throw new DOMException("Task read cancelled", "AbortError");
    const batch = unique.slice(offset, offset + 200);
    const page = await request<{ items: RuntimeStatus[] }>(
      `${root}/-/runtime-statuses`,
      {
        method: "POST",
        headers: cronContextHeaders(context),
        body: JSON.stringify({ job_ids: batch }),
        signal,
      },
    );
    if (signal?.aborted)
      throw new DOMException("Task read cancelled", "AbortError");
    if (
      !page ||
      !Array.isArray(page.items) ||
      page.items.length !== batch.length
    )
      throw new Error("incomplete_task_runtime_batch");
    const pending = new Set(batch);
    for (const item of page.items) {
      if (!item || !pending.delete(item.id))
        throw new Error("invalid_task_runtime_identity");
      validateItem(item, owner === "scheduler");
    }
    result.push(...page.items);
  }
  return result;
}

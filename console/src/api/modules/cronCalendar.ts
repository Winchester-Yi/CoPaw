import { request } from "../request";
import type {
  ManagedCronContext,
  ManagedCronSchedule,
} from "../types/schedulerCron";

export interface CronCalendarPreview {
  source: "api" | "fallback";
  preview_source?: "api" | "fallback";
  next_run_times: string[];
}

export const cronCalendarApi = {
  preview: (
    context: ManagedCronContext,
    schedule: ManagedCronSchedule,
    signal?: AbortSignal,
  ) =>
    request<CronCalendarPreview>("/scheduler/cron/calendar/resolve", {
      method: "POST",
      signal,
      headers: {
        "X-User-Id": context.actor_id,
        "X-Tenant-Id": context.tenant_id,
        "X-Source-Id": context.source_id,
        "X-Agent-Id": context.agent_id,
      },
      body: JSON.stringify({
        cron: schedule.cron,
        timezone: schedule.timezone,
        run_on_non_workday: schedule.run_on_non_workday ?? false,
        count: 3,
      }),
    }),
};

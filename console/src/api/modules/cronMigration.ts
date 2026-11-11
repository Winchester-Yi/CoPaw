import { request } from "../request";
import { cronContextHeaders } from "../cronTaskContext";
import type { ManagedCronContext } from "../types/schedulerCron";

export interface MigrationOwnership {
  tenant_id: string;
  source_id: string;
  agent_id: string;
  job_id: string;
  chat_id: string;
  session_id: string;
  completed: boolean;
}
export interface MigrationProgress {
  operation_id: string;
  status: string;
  root_count: number;
  member_count: number;
  can_retry?: boolean;
  groups: {
    job_id: string;
    status: string;
    member_count?: number;
    error_code?: string | null;
    warnings?: string[];
    migrated_count?: number;
    skipped_count?: number;
  }[];
}
export interface ChannelMigrationJob {
  job_id: string;
  name: string;
  tenant_id: string;
  source_id: string;
  agent_id: string | null;
  parent_job_id: string | null;
  migration_status: string;
}
export interface ChannelMigrationOperation extends MigrationProgress {
  tenant_id: string;
  agent_id: string;
  can_confirm: boolean;
}
const ROOT = "/scheduler/cron/migrations";
function call<T>(
  context: ManagedCronContext,
  suffix = "",
  options: RequestInit = {},
) {
  return request<T>(ROOT + suffix, {
    ...options,
    headers: cronContextHeaders(context),
  });
}
export const cronMigrationApi = {
  channelJobs: (context: ManagedCronContext, params: URLSearchParams, signal?: AbortSignal) =>
    call<{ items: ChannelMigrationJob[]; next_cursor: string | null }>(
      context, `/channel/jobs?${params}`, { signal },
    ),
  channelRecent: (context: ManagedCronContext, signal?: AbortSignal) =>
    call<{ items: ChannelMigrationOperation[] }>(context, "/channel/operations", { signal }),
  channelSubmit: (context: ManagedCronContext, jobIds: string[], requestId: string) =>
    call<{ items: MigrationProgress[] }>(context, "/channel/operations", {
      method: "POST",
      body: JSON.stringify({ job_ids: jobIds, request_id: requestId }),
    }),
  channelConfirm: (context: ManagedCronContext, operationId: string) =>
    call<MigrationProgress>(context, `/channel/operations/${encodeURIComponent(operationId)}/confirm`, {
      method: "POST",
    }),
  submit: (context: ManagedCronContext, jobIds: string[], requestId: string) =>
    call<MigrationProgress>(context, "", {
      method: "POST",
      body: JSON.stringify({ job_ids: jobIds, request_id: requestId }),
    }),
  get: (context: ManagedCronContext, id: string) =>
    call<MigrationProgress>(context, `/${encodeURIComponent(id)}`),
  recent: (context: ManagedCronContext) =>
    call<{ items: MigrationProgress[] }>(context),
  confirm: (context: ManagedCronContext, id: string) =>
    call<MigrationProgress>(context, `/${encodeURIComponent(id)}/confirm`, {
      method: "POST",
    }),
  async ownership(
    context: ManagedCronContext,
    signal?: AbortSignal,
  ): Promise<MigrationOwnership[]> {
    const items: MigrationOwnership[] = [];
    const seen = new Set<string>();
    let cursor: string | null = null;
    do {
      const page: { items: MigrationOwnership[]; next_cursor?: string | null } =
        await call(
          context,
          `/ownership${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`,
          { signal },
        );
      if (!Array.isArray(page.items))
        throw new Error("invalid_cron_migration_ownership");
      for (const item of page.items) {
        if (
          item.tenant_id !== context.tenant_id ||
          item.source_id !== context.source_id ||
          item.agent_id !== context.agent_id ||
          !item.job_id ||
          !item.chat_id ||
          !item.session_id ||
          typeof item.completed !== "boolean"
        )
          throw new Error("invalid_cron_migration_ownership");
      }
      items.push(...page.items);
      cursor = page.next_cursor ?? null;
      if (cursor && seen.has(cursor))
        throw new Error("invalid_cron_migration_cursor");
      if (cursor) seen.add(cursor);
    } while (cursor);
    return items;
  },
};

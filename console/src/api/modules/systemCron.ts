import { request } from "../request";
import type { ManagedCronContext } from "../types/schedulerCron";
import type {
  SystemCronJob,
  SystemScopeKind,
  SystemTaskType,
} from "../types/systemCron";

const ROOT = "/scheduler/cron/system/jobs";

function call<T>(
  context: ManagedCronContext,
  path: string,
  options: RequestInit = {},
) {
  // source记录的tenant/Agent是规范化空维度，Header仍使用当前真实操作者工作区。
  // 管理员角色与凭证由统一请求层实时构建，不能从任务记录伪造。
  return request<T>(path, {
    ...options,
    headers: {
      "X-User-Id": context.actor_id,
      "X-Tenant-Id": context.tenant_id,
      "X-Source-Id": context.source_id,
      "X-Agent-Id": context.agent_id,
      "X-Scope-Id": `${context.tenant_id}-${context.source_id}`,
    },
  });
}

/** 原设置页只管理配置同步；人工核实只保留Postman接口，不提供业务重跑操作。 */
export const systemCronApi = {
  jobs: (context: ManagedCronContext, scope: SystemScopeKind) =>
    call<{ items: SystemCronJob[] }>(context, `${ROOT}?scope_kind=${scope}`),
  refresh: (
    context: ManagedCronContext,
    taskType: SystemTaskType,
    requestId: string,
  ) =>
    call<SystemCronJob>(context, `${ROOT}/refresh`, {
      method: "POST",
      body: JSON.stringify({ task_type: taskType, request_id: requestId }),
    }),
  retry: (
    context: ManagedCronContext,
    job: Pick<SystemCronJob, "job_id" | "version">,
    requestId: string,
  ) =>
    call<SystemCronJob>(
      context,
      `${ROOT}/${encodeURIComponent(job.job_id)}/retry`,
      {
        method: "POST",
        body: JSON.stringify({
          request_id: requestId,
          expected_version: job.version,
        }),
      },
    ),
};

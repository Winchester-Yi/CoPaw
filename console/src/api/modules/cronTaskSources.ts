import { request } from "../request";
import { cronAccessKey, cronContextHeaders } from "../cronTaskContext";
import type { CronJobSpecOutput, CronJobView } from "../types/cronjob";
import type {
  ManagedCronContext,
  ManagedJob,
  ManagedJobDetail,
} from "../types/schedulerCron";
import type {
  CronTaskOrigin,
  SourcedCronTask,
  SourcedManagedTask,
  TaskPresence,
  TaskSourceSnapshot,
} from "../types/cronTaskSource";
import { cronJobApi } from "./cronjob";
import { schedulerCronApi } from "./schedulerCron";
import { cronMigrationApi } from "./cronMigration";
import type { MigrationOwnership } from "./cronMigration";

export function taskOrigin(task: CronJobSpecOutput): CronTaskOrigin | null {
  return (task as Partial<SourcedCronTask>).clientTaskOrigin ?? null;
}

export function isSchedulerTask(
  task: CronJobSpecOutput,
): task is SourcedManagedTask {
  return taskOrigin(task)?.owner === "scheduler";
}

export function tagTask(
  task: CronJobSpecOutput,
  owner: CronTaskOrigin["owner"],
  context: ManagedCronContext,
  accessKey = cronAccessKey(context),
): SourcedCronTask {
  return {
    ...task,
    clientTaskOrigin: { owner, context: { ...context }, accessKey },
  };
}

export function taskIdentityKey(task: CronJobSpecOutput): string {
  const origin = taskOrigin(task);
  if (!origin) return task.id;
  return JSON.stringify([
    origin.owner,
    origin.context.tenant_id,
    origin.context.source_id,
    origin.context.agent_id,
    task.id,
  ]);
}

export function sameTask(
  left: CronJobSpecOutput,
  right: CronJobSpecOutput,
): boolean {
  return taskIdentityKey(left) === taskIdentityKey(right);
}

function managedTask(
  job: ManagedJob,
  context: ManagedCronContext,
  accessKey: string,
): SourcedManagedTask {
  if (
    job.management_owner !== "scheduler" ||
    !Number.isInteger(job.definition_version) ||
    job.definition_version < 1 ||
    job.tenant_id !== context.tenant_id ||
    job.source_id !== context.source_id ||
    job.agent_id !== context.agent_id
  )
    throw new Error("invalid_managed_task_identity");
  return tagTask(job, "scheduler", context, accessKey) as SourcedManagedTask;
}

export async function loadTaskSource(
  owner: CronTaskOrigin["owner"],
  context: ManagedCronContext,
  signal?: AbortSignal,
  accessKey = cronAccessKey(context),
): Promise<SourcedCronTask[]> {
  const [jobs, ownership] = await Promise.all([
    owner === "scheduler"
      ? schedulerCronApi
          .listAll(context, { signal })
          .then((items) =>
            items.map((item) => managedTask(item, context, accessKey)),
          )
      : cronJobApi.listCronJobsForContext(context, signal).then((items) => {
          if (!Array.isArray(items)) throw new Error("invalid_legacy_task_list");
          return items.map((item) => tagTask(item, "legacy", context, accessKey));
        }),
    cronMigrationApi.ownership(context, signal),
  ]);
  return filterMigrationOwnedTasks(jobs, owner, ownership);
}

function filterMigrationOwnedTasks(
  jobs: SourcedCronTask[],
  owner: CronTaskOrigin["owner"],
  ownership: MigrationOwnership[],
) {
  const facts = new Map(ownership.map((fact) => [ownershipKey(fact), fact]));
  return jobs.filter((job) => {
    const fact = facts.get(taskMigrationKey(job));
    if (!fact) return true;
    if (!bindingMatches(job, fact))
      throw new Error("task_migration_binding_conflict");
    return owner === "scheduler" ? fact.completed : !fact.completed;
  });
}

/** 两个来源各自成功或失败；新列表必须完整遍历，失败不能解释为任务已删除。 */
export async function listTaskSources(
  context: ManagedCronContext,
  signal?: AbortSignal,
): Promise<TaskSourceSnapshot> {
  const accessKey = cronAccessKey(context);
  const [oldResult, newResult, ownershipResult] = await Promise.allSettled([
    cronJobApi.listCronJobs().then((jobs) => {
      if (!Array.isArray(jobs)) throw new Error("invalid_legacy_task_list");
      return jobs.map((job) => tagTask(job, "legacy", context, accessKey));
    }),
    schedulerCronApi
      .listAll(context, { signal })
      .then((jobs) => jobs.map((job) => managedTask(job, context, accessKey))),
    cronMigrationApi.ownership(context, signal),
  ]);
  const errors: string[] = [];
  if (oldResult.status === "rejected")
    errors.push("旧任务列表暂不可用，保留已知任务");
  if (newResult.status === "rejected")
    errors.push("新版任务列表暂不可用，保留已知任务");
  if (ownershipResult.status === "rejected")
    errors.push("迁移归属暂不可用，任务仅可查看，请刷新后操作");
  return {
    legacy: oldResult.status === "fulfilled" ? oldResult.value : null,
    scheduler: newResult.status === "fulfilled" ? newResult.value : null,
    errors,
    ownership:
      ownershipResult.status === "fulfilled" ? ownershipResult.value : null,
  };
}

export function mergeTaskSources(
  previous: CronJobSpecOutput[],
  snapshot: TaskSourceSnapshot,
  knownOwnership: MigrationOwnership[] = [],
): CronJobSpecOutput[] {
  const oldJobs =
    snapshot.legacy ??
    previous.filter((job) => taskOrigin(job)?.owner !== "scheduler");
  const newJobs = snapshot.scheduler ?? previous.filter(isSchedulerTask);
  const facts = new Map(
    retainMigrationOwnership(knownOwnership, snapshot.ownership).map((fact) => [
      ownershipKey(fact),
      fact,
    ]),
  );
  const conflict = new Set<string>();
  for (const job of [...oldJobs, ...newJobs]) {
    const key = taskMigrationKey(job);
    const fact = facts.get(key);
    if (fact && !bindingMatches(job, fact)) {
      conflict.add(key);
      if (!snapshot.errors.includes("任务迁移绑定冲突，请联系管理员核对"))
        snapshot.errors.push("任务迁移绑定冲突，请联系管理员核对");
    }
  }
  const selected = [
    ...oldJobs.filter((job) => !facts.get(taskMigrationKey(job))?.completed),
    ...newJobs.filter((job) => {
      const fact = facts.get(taskMigrationKey(job));
      return !fact || fact.completed;
    }),
  ].filter((job) => !conflict.has(taskMigrationKey(job)));
  return selected.map((job) => ({
    ...job,
    clientTaskOrigin: {
      ...taskOrigin(job)!,
      unavailable:
        snapshot.ownership === null ||
        (isSchedulerTask(job)
          ? snapshot.scheduler === null
          : snapshot.legacy === null),
    },
  }));
}

function ownershipKey(fact: MigrationOwnership) {
  return JSON.stringify([
    fact.tenant_id,
    fact.source_id,
    fact.agent_id,
    fact.job_id,
  ]);
}
export function retainMigrationOwnership(
  previous: MigrationOwnership[],
  incoming?: MigrationOwnership[] | null,
) {
  if (!incoming) return previous;
  const facts = new Map(
    previous
      .filter((fact) => fact.completed)
      .map((fact) => [ownershipKey(fact), fact]),
  );
  for (const fact of incoming) {
    const key = ownershipKey(fact);
    if (!facts.get(key)?.completed) facts.set(key, fact);
  }
  return [...facts.values()];
}
function taskMigrationKey(job: CronJobSpecOutput) {
  const context = taskOrigin(job)?.context;
  return JSON.stringify([
    context?.tenant_id,
    context?.source_id,
    context?.agent_id,
    job.id,
  ]);
}
function bindingMatches(job: CronJobSpecOutput, fact: MigrationOwnership) {
  return (
    (job.task?.chat_id ?? job.meta?.task_chat_id) === fact.chat_id &&
    (job.task?.session_id ?? job.meta?.task_session_id) === fact.session_id
  );
}

export function taskPresence(snapshot: TaskSourceSnapshot): TaskPresence {
  if (snapshot.ownership === null) return { legacy: null, scheduler: null };
  const ids = (jobs: CronJobSpecOutput[] | null) =>
    jobs === null ? null : new Set(jobs.map((job) => job.id));
  const selected = mergeTaskSources([], snapshot);
  const migrated = new Set(
    (snapshot.ownership ?? []).filter((f) => f.completed).map((f) => f.job_id),
  );
  return {
    legacy:
      snapshot.legacy === null ||
      (snapshot.scheduler === null && migrated.size > 0)
        ? null
        : ids(
            selected.filter(
              (job) => !isSchedulerTask(job) || migrated.has(job.id),
            ),
          ),
    scheduler:
      snapshot.scheduler === null
        ? null
        : ids(selected.filter(isSchedulerTask)),
  };
}

async function optionalDetail<T>(
  context: ManagedCronContext,
  path: string,
): Promise<T | null> {
  try {
    return await request<T>(path, { headers: cronContextHeaders(context) });
  } catch (error) {
    if (
      error &&
      typeof error === "object" &&
      "status" in error &&
      error.status === 404
    )
      return null;
    throw error;
  }
}

/** 裸 ID 只通过两条只读详情确认归属，不试写接口，不把 legacy 新表快照当新任务。 */
export async function resolveTaskOrigin(
  context: ManagedCronContext,
  id: string,
): Promise<CronTaskOrigin> {
  const accessKey = cronAccessKey(context);
  const encoded = encodeURIComponent(id);
  const facts = await cronMigrationApi.ownership(context).catch(() => {
    throw new Error("无法确认报告任务迁移归属，未标记已读");
  });
  const fact = facts.find((item) => item.job_id === id);
  if (fact) {
    const owner = fact.completed ? "scheduler" : "legacy";
    const detail = await optionalDetail<CronJobView>(
      context,
      `/${owner === "scheduler" ? "scheduler/cron" : "cron"}/jobs/${encoded}`,
    );
    if (
      !detail ||
      !bindingMatches(
        { ...detail.spec, task: detail.task } as CronJobSpecOutput,
        fact,
      )
    )
      throw new Error("报告任务已删除或绑定冲突，未标记已读");
    return { owner, context: { ...context }, accessKey };
  }
  const [oldResult, newResult] = await Promise.allSettled([
    optionalDetail<CronJobView>(context, `/cron/jobs/${encoded}`),
    optionalDetail<ManagedJobDetail>(
      context,
      `/scheduler/cron/jobs/${encoded}`,
    ),
  ]);
  if (oldResult.status === "rejected" || newResult.status === "rejected")
    throw new Error("无法确认报告任务归属，未标记已读");
  const oldJob = oldResult.value;
  const newJob = newResult.value;
  if (oldJob && newJob)
    throw new Error("报告任务归属不唯一，请从对应任务会话打开");
  if (newJob) {
    if (
      newJob.management_owner !== "scheduler" ||
      newJob.spec.id !== id ||
      newJob.spec.tenant_id !== context.tenant_id ||
      newJob.spec.source_id !== context.source_id ||
      newJob.agent_id !== context.agent_id
    )
      throw new Error("无法确认报告任务完整身份，未标记已读");
    return { owner: "scheduler", context: { ...context }, accessKey };
  }
  if (oldJob?.spec.id === id)
    return { owner: "legacy", context: { ...context }, accessKey };
  throw new Error("报告任务不存在，未标记已读");
}

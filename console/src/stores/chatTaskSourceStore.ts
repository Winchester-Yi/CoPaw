import {
  loadRuntimeStatuses,
  type RuntimeStatus,
} from "@/api/modules/cronRuntime";
import { loadTaskSource, taskOrigin } from "@/api/modules/cronTaskSources";
import { cronAccessKey, currentCronContext } from "@/api/cronTaskContext";
import type { CronJobSpecOutput } from "@/api/types";
import type { SourcedCronTask, TaskPresence } from "@/api/types/cronTaskSource";
import type { ManagedCronContext } from "@/api/types/schedulerCron";
import { useAgentStore } from "./agentStore";
import { useIframeStore } from "./iframeStore";

export type TaskOwner = "legacy" | "scheduler";
export type TaskRefreshReason = "manual" | "poll";
const OWNERS: TaskOwner[] = ["legacy", "scheduler"];
const UNKNOWN: TaskPresence = { legacy: null, scheduler: null };
const sourceState = () => ({
  items: [] as SourcedCronTask[],
  optimistic: null as CronJobSpecOutput[] | null,
  membership: null as ReadonlySet<string> | null,
  error: null as string | null,
  loading: false,
});
type SourceState = ReturnType<typeof sourceState>;
type Flight = {
  kind: "full" | "runtime";
  controller: AbortController;
  promise: Promise<void>;
};

/** Only the Chat lifecycle reads sources. Session pagination consumes a view. */
export class ChatTaskSourceStore {
  private state = {
    key: "",
    epoch: 0,
    legacy: sourceState(),
    scheduler: sourceState(),
  };
  private context: ManagedCronContext | null = null;
  private listeners = new Set<() => void>();
  private openChats = new Map<symbol, string>();
  private flights: Partial<Record<TaskOwner, Flight>> = {};
  private revisions = { legacy: 0, scheduler: 0 };
  private mutations = { legacy: 0, scheduler: 0 };
  private consumers = 0;
  private suspended = false;
  private visible = true;
  private stopLifecycle: (() => void) | null = null;

  private unsupported = new Set<TaskOwner>();
  constructor(
    private readonly loader = loadTaskSource,
    private readonly runtimeLoader = loadRuntimeStatuses,
  ) {}

  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  private publish(owner: TaskOwner, patch: Partial<SourceState>) {
    this.state = { ...this.state, [owner]: { ...this.state[owner], ...patch } };
    this.listeners.forEach((listener) => listener());
  }

  activate(context: ManagedCronContext, key = cronAccessKey(context)) {
    if (this.suspended || this.state.key === key) return;
    this.reset();
    this.context = { ...context };
    this.state = { ...this.state, key };
    this.listeners.forEach((listener) => listener());
  }

  suspend = () => {
    this.reset();
    this.suspended = true;
  };

  resume(context: ManagedCronContext) {
    this.suspended = false;
    this.activate(context);
  }

  reset = () => {
    this.suspended = false;
    this.stopLifecycle?.();
    this.stopLifecycle = null;
    OWNERS.forEach((owner) => this.flights[owner]?.controller.abort());
    this.flights = {};
    this.openChats.clear();
    this.unsupported.clear();
    this.revisions = { legacy: 0, scheduler: 0 };
    this.mutations = { legacy: 0, scheduler: 0 };
    this.consumers = 0;
    this.context = null;
    this.state = {
      key: "",
      epoch: this.state.epoch + 1,
      legacy: sourceState(),
      scheduler: sourceState(),
    };
    this.listeners.forEach((listener) => listener());
  };

  retain(context: ManagedCronContext, key = cronAccessKey(context)) {
    this.activate(context, key);
    const epoch = this.state.epoch;
    this.consumers += 1;
    if (this.consumers === 1) {
      this.visible = document.visibilityState !== "hidden";
      this.startLifecycle();
      this.unsupported.clear();
      OWNERS.forEach((owner) => this.invalidate(owner));
      void this.refresh();
    }
    return () => {
      if (!this.matches(key, epoch)) return;
      this.consumers -= 1;
      if (!this.consumers) {
        this.visible = false;
        this.stopLifecycle?.();
        this.stopLifecycle = null;
        OWNERS.forEach((owner) => this.invalidate(owner));
      }
    };
  }

  private startLifecycle() {
    const visibility = () =>
      this.setVisible(document.visibilityState !== "hidden");
    const created = () => {
      this.invalidate("legacy");
      void this.refresh("legacy");
    };
    const timer = window.setInterval(() => {
      void this.refresh(undefined, "poll");
    }, 30_000);
    document.addEventListener("visibilitychange", visibility);
    document.addEventListener("taskCreated", created);
    this.stopLifecycle = () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", visibility);
      document.removeEventListener("taskCreated", created);
    };
  }

  private matches(key: string, epoch: number) {
    return key === this.state.key && epoch === this.state.epoch;
  }

  retainOpenChat(chatId: string | undefined, key: string, epoch: number) {
    if (!chatId || !this.matches(key, epoch)) return () => undefined;
    const token = Symbol();
    this.openChats.set(token, chatId);
    return () => {
      this.openChats.delete(token);
    };
  }

  private invalidate(owner: TaskOwner) {
    this.revisions[owner] += 1;
    this.flights[owner]?.controller.abort();
    delete this.flights[owner];
    this.publish(owner, { membership: null, loading: false });
  }

  setVisible(visible: boolean) {
    if (visible === this.visible) return;
    this.visible = visible;
    if (!visible) OWNERS.forEach((owner) => this.invalidate(owner));
    else if (this.consumers) {
      this.unsupported.clear();
      void this.refresh();
    }
  }

  presence(key = cronAccessKey()): TaskPresence {
    return this.state.key === key
      ? {
          legacy: this.state.legacy.membership,
          scheduler: this.state.scheduler.membership,
        }
      : UNKNOWN;
  }

  jobs(): CronJobSpecOutput[] {
    return OWNERS.flatMap(
      (owner) => this.state[owner].optimistic ?? this.state[owner].items,
    );
  }

  setJobs(
    update: (jobs: CronJobSpecOutput[]) => CronJobSpecOutput[],
    key: string,
    epoch: number,
  ) {
    if (!this.matches(key, epoch)) return;
    const previous = this.jobs();
    const next = update(previous);
    OWNERS.forEach((owner) => {
      const before = previous.filter((job) => taskOrigin(job)?.owner === owner);
      const after = next.filter((job) => taskOrigin(job)?.owner === owner);
      if (
        before.length !== after.length ||
        before.some((job, i) => job !== after[i])
      )
        this.publish(owner, { optimistic: after });
    });
  }

  refresh(
    owner?: TaskOwner,
    reason: TaskRefreshReason = "manual",
  ): Promise<void> {
    return Promise.all(
      (owner ? [owner] : OWNERS).map((source) => this.read(source, reason)),
    ).then(() => undefined);
  }

  private read(owner: TaskOwner, reason: TaskRefreshReason): Promise<void> {
    if (!this.context || !this.visible || this.mutations[owner])
      return Promise.resolve();
    if (
      reason === "poll" &&
      this.state[owner].membership !== null &&
      !this.unsupported.has(owner)
    )
      return this.readRuntime(owner);
    if (reason === "manual") {
      this.unsupported.delete(owner);
      if (this.flights[owner]?.kind === "runtime") this.invalidate(owner);
      this.publish(owner, { membership: null });
    }
    const existing = this.flights[owner];
    if (existing) return existing.promise;
    const context = { ...this.context };
    const { key, epoch } = this.state;
    const revision = this.revisions[owner];
    const current = () =>
      this.matches(key, epoch) && this.revisions[owner] === revision;
    const controller = new AbortController();
    this.publish(owner, { loading: true, error: null });
    const flight: Flight = {
      kind: "full",
      controller,
      promise: Promise.resolve(),
    };
    this.flights[owner] = flight;
    flight.promise = Promise.resolve().then(async () => {
      try {
        if (controller.signal.aborted) return;
        const items = await this.loader(owner, context, controller.signal, key);
        if (current() && !controller.signal.aborted)
          this.publish(owner, {
            items,
            optimistic: null,
            membership: new Set(items.map((job) => job.id)),
            error: null,
          });
      } catch {
        if (current() && !controller.signal.aborted)
          this.publish(owner, {
            optimistic: null,
            membership: null,
            error:
              owner === "legacy"
                ? "旧任务列表暂不可用，保留已知任务"
                : "新版任务列表暂不可用，保留已知任务",
          });
      } finally {
        if (this.flights[owner] === flight) {
          delete this.flights[owner];
          this.publish(owner, { loading: false });
        }
      }
    });
    return flight.promise;
  }

  private readRuntime(owner: TaskOwner): Promise<void> {
    const existing = this.flights[owner];
    if (existing) return existing.promise;
    const { key, epoch } = this.state;
    const revision = this.revisions[owner];
    const items = this.state[owner].items;
    const openChats = new Set(this.openChats.values());
    const requested = items.filter(
      (job) =>
        job.task?.visible_in_my_tasks ||
        (job.task?.chat_id && openChats.has(job.task.chat_id)),
    );
    const context = { ...this.context! };
    const current = () =>
      this.matches(key, epoch) && this.revisions[owner] === revision;
    const controller = new AbortController();
    const flight: Flight = {
      kind: "runtime",
      controller,
      promise: Promise.resolve(),
    };
    this.flights[owner] = flight;
    flight.promise = Promise.resolve().then(async () => {
      let confirm = false;
      try {
        if (!current() || controller.signal.aborted) return;
        const updates = await this.runtimeLoader(
          owner,
          context,
          requested.map((job) => job.id),
          controller.signal,
        );
        if (!current() || controller.signal.aborted) return;
        const byId = new Map(updates.map((item) => [item.id, item]));
        let stateMissing = false;
        const next = items.map((job) => {
          const update = byId.get(job.id);
          if (!update) return job;
          if (update.status === "state_missing") {
            stateMissing = true;
            return job;
          }
          const previous = job as SourcedCronTask & {
            definition_version?: number;
            state_version?: number;
            management_version?: number;
          };
          if (
            update.status !== "ok" ||
            (owner === "scheduler" &&
              update.definition_version !== previous.definition_version)
          ) {
            confirm = true;
            return job;
          }
          if (
            owner === "scheduler" &&
            ((update.state_version ?? 0) < (previous.state_version ?? 0) ||
              (update.management_version ?? 0) <
                (previous.management_version ?? 0))
          )
            return job;
          const patch = runtimePatch(update);
          return JSON.stringify(patch) ===
            JSON.stringify(runtimePatch(previous))
            ? job
            : { ...job, ...patch };
        });
        if (!confirm)
          this.publish(owner, {
            items: next.every((job, index) => job === items[index])
              ? items
              : next,
            error: stateMissing ? "任务状态记录缺失，保留已知状态" : null,
          });
      } catch (error) {
        if (!current() || controller.signal.aborted) return;
        const status = (error as { status?: number })?.status;
        if (status === 404 || status === 405) {
          this.unsupported.add(owner);
          confirm = true;
        } else this.publish(owner, { error: "任务状态暂不可用，保留已知状态" });
      } finally {
        if (this.flights[owner] === flight) delete this.flights[owner];
      }
      if (confirm && current() && !controller.signal.aborted) {
        this.invalidate(owner);
        await this.read(owner, "poll");
      }
    });
    return flight.promise;
  }

  beginMutation(
    owner: TaskOwner,
    key = cronAccessKey(),
    epoch = this.state.epoch,
  ): () => Promise<void> {
    if (!this.matches(key, epoch)) return async () => undefined;
    this.mutations[owner] += 1;
    this.invalidate(owner);
    let finished = false;
    return async () => {
      if (finished || !this.matches(key, epoch)) return;
      finished = true;
      this.mutations[owner] -= 1;
      this.invalidate(owner);
      if (!this.mutations[owner]) await this.refresh(owner);
    };
  }
}

function runtimePatch(item: Partial<RuntimeStatus>) {
  const state = item.state,
    task = item.task;
  return {
    enabled: item.enabled,
    state: state && {
      last_status: state.last_status,
      last_run_at: state.last_run_at,
      last_error: state.last_error,
      last_error_truncated: state.last_error_truncated,
      next_run_at: state.next_run_at,
      next_run_times: state.next_run_times,
    },
    task: task && {
      visible_in_my_tasks: task.visible_in_my_tasks,
      chat_id: task.chat_id,
      session_id: task.session_id,
      is_running: task.is_running,
      is_paused: task.is_paused,
      pause_reason: task.pause_reason,
      auto_paused_at: task.auto_paused_at,
      has_scheduled_result: task.has_scheduled_result,
      latest_scheduled_preview: task.latest_scheduled_preview,
      latest_scheduled_preview_truncated:
        task.latest_scheduled_preview_truncated,
      last_scheduled_run_at: task.last_scheduled_run_at,
      unread_execution_count: task.unread_execution_count,
    },
    ...(item.definition_version === undefined
      ? {}
      : {
          definition_version: item.definition_version,
          management_version: item.management_version,
          state_version: item.state_version,
          management_status: item.management_status,
          dispatch_state: item.dispatch_state,
        }),
  };
}

export const chatTaskSourceStore = new ChatTaskSourceStore();
const synchronizeIdentity = () =>
  chatTaskSourceStore.activate(currentCronContext());
useAgentStore.subscribe(synchronizeIdentity);
useIframeStore.subscribe(synchronizeIdentity);
synchronizeIdentity();
if (typeof window !== "undefined") {
  window.addEventListener("swe:auth-cleared", chatTaskSourceStore.suspend);
  window.addEventListener("swe:auth-ready", () =>
    chatTaskSourceStore.resume(currentCronContext()),
  );
}

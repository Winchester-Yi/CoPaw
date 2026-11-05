import { useCallback, useEffect, useMemo, useSyncExternalStore } from "react";
import type { Dispatch, SetStateAction } from "react";
import type { CronJobSpecOutput } from "@/api/types";
import {
  chatTaskSourceStore,
  type TaskOwner,
  type TaskRefreshReason,
} from "@/stores/chatTaskSourceStore";
import { currentCronContext, cronAccessKey } from "@/api/cronTaskContext";
import { useAgentStore } from "@/stores/agentStore";
import { useIframeStore } from "@/stores/iframeStore";

/** Subscribers share one identity-scoped read lifecycle and complete-source facts. */
export function useChatTaskSources(chatId?: string) {
  const identity = useIframeStore((state) =>
    JSON.stringify([
      state.userId,
      state.source,
      state.manager,
      state.isSuperManager,
    ]),
  );
  const agent = useAgentStore((state) => state.selectedAgent);
  const context = useMemo(currentCronContext, [identity, agent]);
  const scopeKey = cronAccessKey(context);
  const snapshot = useSyncExternalStore(
    chatTaskSourceStore.subscribe,
    chatTaskSourceStore.getSnapshot,
  );
  const current = snapshot.key === scopeKey;
  const epoch = snapshot.epoch;
  useEffect(() => {
    chatTaskSourceStore.activate(context, scopeKey);
  }, [context, scopeKey]);
  useEffect(
    () => (current ? chatTaskSourceStore.retain(context, scopeKey) : undefined),
    [context, scopeKey, current],
  );
  useEffect(
    () =>
      current
        ? chatTaskSourceStore.retainOpenChat(chatId, scopeKey, epoch)
        : undefined,
    [chatId, scopeKey, epoch, current],
  );
  const refreshJobs = useCallback(
    async (options?: { owner?: TaskOwner; reason?: TaskRefreshReason }) => {
      if (
        chatTaskSourceStore.getSnapshot().key !== scopeKey ||
        chatTaskSourceStore.getSnapshot().epoch !== epoch
      )
        return;
      await chatTaskSourceStore.refresh(options?.owner, options?.reason);
    },
    [scopeKey, epoch],
  );
  const setJobs: Dispatch<SetStateAction<CronJobSpecOutput[]>> = useCallback(
    (update) => {
      chatTaskSourceStore.setJobs(
        (jobs) => (typeof update === "function" ? update(jobs) : update),
        scopeKey,
        epoch,
      );
    },
    [scopeKey, epoch],
  );
  const beginTaskMutation = useCallback(
    (owner: TaskOwner) =>
      chatTaskSourceStore.beginMutation(owner, scopeKey, epoch),
    [scopeKey, epoch],
  );
  const jobs = useMemo(
    () =>
      current
        ? [snapshot.legacy, snapshot.scheduler].flatMap(
            (state) => state.optimistic ?? state.items,
          )
        : [],
    [current, snapshot.legacy, snapshot.scheduler],
  );
  const presence = useMemo(
    () => ({
      legacy: current ? snapshot.legacy.membership : null,
      scheduler: current ? snapshot.scheduler.membership : null,
    }),
    [current, snapshot.legacy.membership, snapshot.scheduler.membership],
  );
  const error = current
    ? [snapshot.legacy.error, snapshot.scheduler.error]
        .filter(Boolean)
        .join("；") || null
    : null;
  const loaded =
    current &&
    [snapshot.legacy, snapshot.scheduler].every(
      (state) =>
        !state.loading && (state.membership !== null || state.error !== null),
    );
  return {
    jobs,
    error,
    loaded,
    presence,
    epoch,
    active: current,
    setJobs,
    refreshJobs,
    beginTaskMutation,
    context,
    scopeKey,
  };
}

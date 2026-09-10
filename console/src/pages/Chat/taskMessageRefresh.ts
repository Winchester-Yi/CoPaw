import type { CronJobSpecOutput } from "../../api/types";

interface ShouldRefreshCurrentTaskMessagesOptions {
  previousTask: CronJobSpecOutput | null;
  currentTask: CronJobSpecOutput | null;
}

interface RefreshTaskSessionOptions {
  maxAttempts?: number;
  retryDelayMs?: number;
  retryAfterExhaustionMs?: number;
  sessionId?: string;
  wait?: (delayMs: number) => Promise<void>;
  shouldContinue?: () => boolean;
}

const TASK_SESSION_REFRESH_ATTEMPTS = 3;
const TASK_SESSION_REFRESH_RETRY_DELAY_MS = 500;
export const TASK_SESSION_REFRESH_RECOVERY_DELAY_MS = 5000;

function waitForTaskSessionRefresh(delayMs: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, delayMs);
  });
}

export async function refreshTaskSessionWithRetry(
  refreshSession: (sessionId?: string) => Promise<boolean>,
  options: RefreshTaskSessionOptions = {},
): Promise<boolean> {
  const maxAttempts = options.maxAttempts ?? TASK_SESSION_REFRESH_ATTEMPTS;
  const wait = options.wait ?? waitForTaskSessionRefresh;
  const shouldContinue = options.shouldContinue ?? (() => true);

  while (shouldContinue()) {
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      if (!shouldContinue()) {
        return false;
      }
      try {
        if (await refreshSession(options.sessionId)) {
          return true;
        }
      } catch {
        // The next scheduled result refresh is allowed to recover transient APIs.
      }
      if (attempt + 1 < maxAttempts && shouldContinue()) {
        await wait(options.retryDelayMs ?? TASK_SESSION_REFRESH_RETRY_DELAY_MS);
      }
    }
    if (options.retryAfterExhaustionMs === undefined) {
      return false;
    }
    if (shouldContinue()) {
      await wait(options.retryAfterExhaustionMs);
    }
  }

  return false;
}

export function shouldRefreshCurrentTaskMessages({
  previousTask,
  currentTask,
}: ShouldRefreshCurrentTaskMessagesOptions): boolean {
  if (!currentTask?.task?.has_scheduled_result) {
    return false;
  }

  if (!previousTask || previousTask.id !== currentTask.id) {
    return true;
  }

  return (
    previousTask.task?.last_scheduled_run_at !==
      currentTask.task?.last_scheduled_run_at ||
    previousTask.task?.has_scheduled_result !==
      currentTask.task?.has_scheduled_result
  );
}

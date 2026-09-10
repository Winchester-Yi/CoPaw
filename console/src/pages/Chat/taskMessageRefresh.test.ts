import { describe, expect, it, vi } from "vitest";
import type { CronJobSpecOutput } from "../../api/types";
import {
  refreshTaskSessionWithRetry,
  shouldRefreshCurrentTaskMessages,
} from "./taskMessageRefresh";

describe("refreshTaskSessionWithRetry", () => {
  it("retries a transient session refresh failure", async () => {
    const refreshSession = vi
      .fn<() => Promise<boolean>>()
      .mockRejectedValueOnce(new Error("server error"))
      .mockResolvedValueOnce(true);
    const wait = vi.fn<() => Promise<void>>().mockResolvedValue(undefined);

    await expect(
      refreshTaskSessionWithRetry(refreshSession, {
        maxAttempts: 3,
        retryDelayMs: 0,
        wait,
      }),
    ).resolves.toBe(true);

    expect(refreshSession).toHaveBeenCalledTimes(2);
    expect(wait).toHaveBeenCalledOnce();
  });

  it("starts another retry cycle after the initial attempts are exhausted", async () => {
    const refreshSession = vi
      .fn<() => Promise<boolean>>()
      .mockRejectedValueOnce(new Error("temporary outage"))
      .mockRejectedValueOnce(new Error("temporary outage"))
      .mockRejectedValueOnce(new Error("temporary outage"))
      .mockResolvedValueOnce(true);
    const wait = vi.fn<() => Promise<void>>().mockResolvedValue(undefined);

    await expect(
      refreshTaskSessionWithRetry(refreshSession, {
        maxAttempts: 3,
        retryDelayMs: 0,
        retryAfterExhaustionMs: 0,
        wait,
      }),
    ).resolves.toBe(true);

    expect(refreshSession).toHaveBeenCalledTimes(4);
    expect(wait).toHaveBeenCalledTimes(3);
  });

  it("refreshes the requested task session", async () => {
    const refreshSession = vi.fn(async (_sessionId?: string) => true);

    await refreshTaskSessionWithRetry(refreshSession, {
      sessionId: "task-session-b",
    });

    expect(refreshSession).toHaveBeenCalledWith("task-session-b");
  });

  it("stops after the initial attempts unless recovery is enabled", async () => {
    const refreshSession = vi
      .fn<() => Promise<boolean>>()
      .mockResolvedValue(false);

    await expect(
      refreshTaskSessionWithRetry(refreshSession, {
        maxAttempts: 2,
        retryDelayMs: 0,
        wait: vi.fn<() => Promise<void>>().mockResolvedValue(undefined),
      }),
    ).resolves.toBe(false);

    expect(refreshSession).toHaveBeenCalledTimes(2);
  });
});

describe("shouldRefreshCurrentTaskMessages", () => {
  it("refreshes when switching to a task with an existing result", () => {
    const previousTask = {
      id: "task-a",
      task: { has_scheduled_result: true },
    } as CronJobSpecOutput;
    const currentTask = {
      id: "task-b",
      task: { has_scheduled_result: true },
    } as CronJobSpecOutput;

    expect(
      shouldRefreshCurrentTaskMessages({ previousTask, currentTask }),
    ).toBe(true);
  });

  it("does not refresh only because the unread count was cleared", () => {
    const previousTask = {
      id: "task-a",
      task: {
        has_scheduled_result: true,
        last_scheduled_run_at: "2026-09-08T09:00:00Z",
        unread_execution_count: 1,
      },
    } as CronJobSpecOutput;
    const currentTask = {
      id: "task-a",
      task: {
        has_scheduled_result: true,
        last_scheduled_run_at: "2026-09-08T09:00:00Z",
        unread_execution_count: 0,
      },
    } as CronJobSpecOutput;

    expect(
      shouldRefreshCurrentTaskMessages({ previousTask, currentTask }),
    ).toBe(false);
  });
});

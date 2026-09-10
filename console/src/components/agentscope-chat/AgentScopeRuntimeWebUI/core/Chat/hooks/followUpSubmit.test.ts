import { describe, expect, it, vi } from "vitest";
import {
  FollowUpSubmitCoordinator,
  FOLLOW_UP_STOP_BACKOFF_MS,
} from "./followUpSubmit";

describe("FollowUpSubmitCoordinator", () => {
  it("stops the active run first and only auto-submits after generation ends", async () => {
    let generating = true;
    const stop = vi.fn(async () => {
      generating = false;
    });
    const submit = vi.fn(async () => {});
    const restoreInput = vi.fn();
    const notifyFailure = vi.fn();
    const sleepMs = vi.fn(async () => {});

    const coordinator = new FollowUpSubmitCoordinator(
      {
        stop,
        submit,
        isGenerating: async () => generating,
        restoreInput,
        notifyFailure,
        sleepMs,
      },
      FOLLOW_UP_STOP_BACKOFF_MS,
    );

    const task = coordinator.enqueue({
      query: "latest question",
      fileList: [],
    });

    expect(submit).not.toHaveBeenCalled();

    await task;

    expect(stop).toHaveBeenCalledTimes(1);
    expect(submit).toHaveBeenCalledWith({
      query: "latest question",
      fileList: [],
    });
    expect(restoreInput).not.toHaveBeenCalled();
    expect(notifyFailure).not.toHaveBeenCalled();
  });

  it("preserves the HTML annotation payload while stopping an active run", async () => {
    let generating = true;
    const submit = vi.fn(async () => {});
    const coordinator = new FollowUpSubmitCoordinator({
      stop: vi.fn(async () => {
        generating = false;
      }),
      submit,
      isGenerating: async () => generating,
      restoreInput: vi.fn(),
      notifyFailure: vi.fn(),
      sleepMs: vi.fn(async () => {}),
    });
    const annotationSubmission = {
      query: "处理页面批注",
      fileList: [],
      biz_params: {
        document_annotations: {
          schema_version: 1,
          source: { attachment_url: "/source.html" },
        },
      },
    };

    await coordinator.enqueue(annotationSubmission);

    expect(submit).toHaveBeenCalledWith(annotationSubmission);
  });

  it("keeps only the latest pending follow-up message while stop is in progress", async () => {
    let generating = true;
    let releaseStop: (() => void) | null = null;
    const stop = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          releaseStop = () => {
            generating = false;
            resolve();
          };
        }),
    );
    const submit = vi.fn(async () => {});

    const coordinator = new FollowUpSubmitCoordinator({
      stop,
      submit,
      isGenerating: async () => generating,
      restoreInput: vi.fn(),
      notifyFailure: vi.fn(),
      sleepMs: vi.fn(async () => {}),
    });

    const first = coordinator.enqueue({ query: "first" });
    coordinator.enqueue({ query: "second" });

    expect(submit).not.toHaveBeenCalled();

    releaseStop?.();
    await first;

    expect(stop).toHaveBeenCalledTimes(1);
    expect(submit).toHaveBeenCalledTimes(1);
    expect(submit).toHaveBeenCalledWith({ query: "second" });
  });

  it("restores the latest pending input and reports failure when stop retry budget is exhausted", async () => {
    const stop = vi.fn(async () => {});
    const submit = vi.fn(async () => {});
    const restoreInput = vi.fn();
    const notifyFailure = vi.fn();
    const sleepMs = vi.fn(async () => {});
    const fileList = [
      { uid: "1", name: "demo.txt", response: { url: "/demo" } },
    ];
    const biz_params = {
      user_prompt_params: {
        source: "follow-up",
      },
    };

    const coordinator = new FollowUpSubmitCoordinator(
      {
        stop,
        submit,
        isGenerating: async () => true,
        restoreInput,
        notifyFailure,
        sleepMs,
      },
      [10, 20],
    );

    await coordinator.enqueue({ query: "recover me", fileList, biz_params });

    expect(stop).toHaveBeenCalledTimes(3);
    expect(sleepMs).toHaveBeenCalledTimes(2);
    expect(submit).not.toHaveBeenCalled();
    expect(restoreInput).toHaveBeenCalledWith({
      query: "recover me",
      fileList,
      biz_params,
    });
    expect(notifyFailure).toHaveBeenCalledTimes(1);
  });

  it("runs another follow-up round when a newer submission arrives during auto-submit", async () => {
    let generating = true;
    let firstSubmitResolved = false;
    let releaseFirstSubmit: (() => void) | null = null;
    const stop = vi.fn(async () => {
      generating = false;
    });
    const submit = vi.fn((data: { query?: string }) => {
      if (data.query === "first") {
        generating = true;
        return new Promise<void>((resolve) => {
          releaseFirstSubmit = () => {
            firstSubmitResolved = true;
            generating = false;
            resolve();
          };
        });
      }

      return Promise.resolve();
    });

    const coordinator = new FollowUpSubmitCoordinator({
      stop,
      submit,
      isGenerating: async () => generating,
      restoreInput: vi.fn(),
      notifyFailure: vi.fn(),
      sleepMs: vi.fn(async () => {}),
    });

    const task = coordinator.enqueue({ query: "first" });
    await vi.waitFor(() => {
      expect(submit).toHaveBeenCalledWith({ query: "first" });
    });

    coordinator.enqueue({ query: "second" });
    expect(firstSubmitResolved).toBe(false);

    releaseFirstSubmit?.();
    await task;

    expect(submit).toHaveBeenNthCalledWith(1, { query: "first" });
    expect(submit).toHaveBeenNthCalledWith(2, { query: "second" });
    expect(stop).toHaveBeenCalledTimes(2);
  });
});

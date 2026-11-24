import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useRunRefresh, hasActiveExecution } from "./useRunRefresh";

const request = vi.hoisted(() => vi.fn());
vi.mock("../../api/request", () => ({ request }));
const span = (id: string) => ({
  span_id: id,
  attempt_id: "a",
  status: "succeeded",
});
const attempt = { attempt_id: "a", attempt_index: 1 };
const page = (
  spans: ReturnType<typeof span>[],
  cursor: string | null,
  attempts = [attempt],
) => ({
  run: { status: "running" },
  attempts,
  spans,
  spans_next_cursor: cursor,
  generated_at: "server-time",
});
const options = () => ({
  scopeKey: "scope",
  runId: "run",
  attemptId: "a",
  runStatus: "running",
  spans: [span("old1"), span("old2")],
  cursor: "old-cursor",
  boundaryId: "old2",
  attempts: [attempt],
  onRefresh: vi.fn(),
});
beforeEach(() => {
  vi.useFakeTimers();
  request.mockReset();
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

it("replays pages to the loaded boundary, deduplicates and announces a new attempt", async () => {
  const opts = options();
  const newer = { attempt_id: "b", attempt_index: 2 };
  request
    .mockResolvedValueOnce(
      page([span("late"), span("old1")], "new-cursor", [attempt, newer]),
    )
    .mockResolvedValueOnce(page([span("old2")], "after", [attempt, newer]));
  const { result } = renderHook(() => useRunRefresh(opts));
  await act(async () => {
    await result.current.refresh();
  });
  expect(request).toHaveBeenCalledTimes(2);
  expect(request.mock.calls[1][0]).toContain("span_cursor=new-cursor");
  const refreshed = opts.onRefresh.mock.calls[0][0];
  expect(refreshed).toEqual(
    expect.objectContaining({ generatedAt: "server-time", incomplete: false }),
  );
  expect(refreshed.mergePage(opts)).toEqual({
    spans: [span("old1"), span("old2"), span("late")],
    cursor: "after",
    boundaryId: "old2",
  });
  expect(result.current.newAttempt).toEqual(newer);
});

it("polls only while visible and active, while manual refresh remains available", async () => {
  request.mockResolvedValue(page([span("old1"), span("old2")], null));
  const opts = options();
  const { result, rerender, unmount } = renderHook(
    ({ status }) => useRunRefresh({ ...opts, runStatus: status }),
    { initialProps: { status: "running" } },
  );
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(request).toHaveBeenCalledTimes(1);
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(request).toHaveBeenCalledTimes(1);
  rerender({ status: "succeeded" });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
    await result.current.refresh();
  });
  expect(request).toHaveBeenCalledTimes(2);
  unmount();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(request).toHaveBeenCalledTimes(2);
});

it("recognizes a running background subagent after the run ends", () => {
  expect(
    hasActiveExecution("succeeded", [
      { ...span("bg"), status: "running", subagent: { mode: "background" } },
    ]),
  ).toBe(true);
  expect(hasActiveExecution("succeeded", [span("done")])).toBe(false);
});

it("merges a concurrent load and keeps the explicit boundary across refreshes", async () => {
  let resolvePage!: (value: ReturnType<typeof page>) => void;
  request.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        resolvePage = resolve;
      }),
  );
  const opts = options();
  const { result, rerender } = renderHook((props) => useRunRefresh(props), {
    initialProps: opts,
  });
  let pending!: Promise<void>;
  act(() => {
    pending = result.current.refresh();
  });
  const loaded = {
    ...opts,
    spans: [...opts.spans, span("old3")],
    cursor: "later",
    boundaryId: "old3",
  };
  rerender(loaded);
  await act(async () => {
    resolvePage(page([span("late"), span("old1"), span("old2")], "after"));
    await pending;
  });
  const first = opts.onRefresh.mock.calls[0][0];
  const state = first.mergePage(loaded);
  expect(state).toEqual({
    spans: [span("old1"), span("old2"), span("old3"), span("late")],
    cursor: "later",
    boundaryId: "old3",
  });
  rerender({ ...loaded, ...state });
  request
    .mockResolvedValueOnce(
      page([span("late"), span("old1"), span("old2")], "next"),
    )
    .mockResolvedValueOnce(
      page([{ ...span("old3"), status: "failed" }], "later"),
    );
  await act(async () => {
    await result.current.refresh();
  });
  expect(request).toHaveBeenCalledTimes(3);
  const second = opts.onRefresh.mock.calls[1][0].mergePage(state);
  expect(
    second.spans.find((s: ReturnType<typeof span>) => s.span_id === "old3")
      ?.status,
  ).toBe("failed");
  expect(second.boundaryId).toBe("old3");
});

it("observes a background task in another attempt and stops after it terminates", async () => {
  const opts = {
    ...options(),
    runStatus: "succeeded",
    spans: [
      { ...span("bg"), status: "running", subagent: { mode: "background" } },
    ],
  };
  request.mockResolvedValueOnce(page([], null)).mockResolvedValueOnce({
    items: [{ ...opts.spans[0], status: "succeeded" }],
  });
  const { rerender } = renderHook((props) => useRunRefresh(props), {
    initialProps: opts,
  });
  rerender({ ...opts, attemptId: "b", spans: [] });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(request).toHaveBeenCalledTimes(2);
  expect(request.mock.calls[1][0]).toContain("/spans/bg/ancestors");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(request).toHaveBeenCalledTimes(2);
});

it("discards a response from an earlier authorization scope", async () => {
  let resolvePage!: (value: ReturnType<typeof page>) => void;
  request.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        resolvePage = resolve;
      }),
  );
  const opts = options();
  const { result, rerender } = renderHook((props) => useRunRefresh(props), {
    initialProps: opts,
  });
  let pending!: Promise<void>;
  act(() => {
    pending = result.current.refresh();
  });
  rerender({ ...opts, scopeKey: "other" });
  await act(async () => {
    resolvePage(page([span("old2")], null));
    await pending;
  });
  expect(opts.onRefresh).not.toHaveBeenCalled();
  expect(result.current.refreshing).toBe(false);
});

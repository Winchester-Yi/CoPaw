import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { useIframeStore } from "../../../stores/iframeStore";
import { useDiagnosisResource } from "./useDiagnosisResource";
afterEach(() => useIframeStore.setState({ userId: null, source: null }));
describe("diagnosis request ownership", () => {
  it("cancels old selections and ignores late results", async () => {
    const resolvers: Record<string, (data: string) => void> = {};
    const signals: Record<string, AbortSignal> = {};
    const { result, rerender } = renderHook(
      ({ key }) =>
        useDiagnosisResource(key, (signal) => {
          signals[key] = signal;
          return new Promise<string>((resolve) => {
            resolvers[key] = resolve;
          });
        }),
      { initialProps: { key: "old" } },
    );
    rerender({ key: "new" });
    expect(signals.old.aborted).toBe(true);
    await act(async () => resolvers.new("new data"));
    expect(result.current.data).toBe("new data");
    await act(async () => resolvers.old("old data"));
    expect(result.current.data).toBe("new data");
  });
  it("hides previous data and reloads automatically when actual source or tenant changes", async () => {
    let count = 0;
    const resolvers: Array<(data: string) => void> = [];
    const { result } = renderHook(() =>
      useDiagnosisResource("same", () => {
        count++;
        return new Promise<string>((resolve) => resolvers.push(resolve));
      }),
    );
    await act(async () => resolvers[0]("tenant A"));
    expect(result.current.data).toBe("tenant A");
    act(() => useIframeStore.setState({ userId: "tenant-B" }));
    expect(result.current.data).toBeUndefined();
    expect(result.current.state).toBe("loading");
    await act(async () => resolvers[1]("tenant B"));
    act(() => useIframeStore.setState({ source: "source-C" }));
    expect(result.current.data).toBeUndefined();
    expect(count).toBe(3);
  });
  it.each([
    [503, "unavailable"],
    [404, "not_found"],
    [403, "error"],
  ])("keeps HTTP %s distinct", async (status, state) => {
    const { result } = renderHook(() =>
      useDiagnosisResource("resource", () =>
        Promise.reject(Object.assign(new Error("failed"), { status })),
      ),
    );
    await waitFor(() => expect(result.current.state).toBe(state));
    expect(result.current.data).toBeUndefined();
    expect(result.current.status).toBe(status);
  });
  it("disabled resources do not fetch and reload clears old data", async () => {
    let count = 0;
    const { result, rerender } = renderHook(
      ({ enabled }) => useDiagnosisResource("r", async () => ++count, enabled),
      { initialProps: { enabled: false } },
    );
    expect(result.current.state).toBe("idle");
    expect(count).toBe(0);
    rerender({ enabled: true });
    await waitFor(() => expect(result.current.data).toBe(1));
    act(() => result.current.reload());
    await waitFor(() => expect(result.current.data).toBe(2));
  });
  it("contains synchronous loader failures and keeps a successful empty list ready", async () => {
    const { result, rerender } = renderHook(
      ({ fail }) =>
        useDiagnosisResource(fail ? "fail" : "empty", () => {
          if (fail) throw new Error("synchronous failure");
          return Promise.resolve({ items: [] });
        }),
      { initialProps: { fail: true } },
    );
    await waitFor(() => expect(result.current.state).toBe("error"));
    rerender({ fail: false });
    await waitFor(() => expect(result.current.state).toBe("ready"));
    expect(result.current.data?.items).toEqual([]);
  });
  it("disabling a resource aborts it and late success remains hidden", async () => {
    let resolveRequest: (data: string) => void = () => undefined;
    let requestSignal: AbortSignal | undefined;
    const { result, rerender } = renderHook(
      ({ enabled }) =>
        useDiagnosisResource(
          "r",
          (signal) => {
            requestSignal = signal;
            return new Promise<string>((resolve) => {
              resolveRequest = resolve;
            });
          },
          enabled,
        ),
      { initialProps: { enabled: true } },
    );
    rerender({ enabled: false });
    expect(requestSignal?.aborted).toBe(true);
    await act(async () => resolveRequest("late"));
    expect(result.current.state).toBe("idle");
    expect(result.current.data).toBeUndefined();
  });
});

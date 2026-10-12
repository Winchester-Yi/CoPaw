import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useResource } from "./useResource";

describe("Token request ownership", () => {
  it("a late response cannot replace a newly selected tenant or task", async () => {
    const deferred: Record<string, (value: string) => void> = {};
    const load = (key: string) =>
      new Promise<string>((resolve) => {
        deferred[key] = resolve;
      });
    const { result, rerender } = renderHook(
      ({ id }) =>
        useResource(id, (signal) => {
          void signal;
          return load(id);
        }),
      { initialProps: { id: "old" } },
    );
    rerender({ id: "new" });
    await act(async () => deferred.new("new data"));
    await waitFor(() => expect(result.current.data).toBe("new data"));
    await act(async () => deferred.old("old data"));
    expect(result.current.data).toBe("new data");
  });
});

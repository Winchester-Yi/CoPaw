import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useContextUsageController } from "./useContextUsageController";
import type { ContextUsageSnapshot } from "@/api/types/contextUsage";

const mocks = vi.hoisted(() => ({
  getContextUsage: vi.fn(),
}));

vi.mock("@/api/modules/chat", () => ({
  chatApi: {
    getContextUsage: mocks.getContextUsage,
  },
}));

const freshSnapshot: ContextUsageSnapshot = {
  available: true,
  schema_version: 1,
  used_tokens: 20,
  max_tokens: 100,
  remaining_tokens: 80,
  usage_ratio: 0.2,
  system_context_tokens: 5,
  tool_definition_tokens: 5,
  conversation_tokens: 10,
  governance_threshold_ratio: 0.65,
  active_threshold_ratio: 0.8,
  emergency_threshold_ratio: 0.9,
  status: "normal",
  estimated: true,
  stale: false,
  as_of: "2026-09-02T08:00:00Z",
};

describe("useContextUsageController", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getContextUsage.mockResolvedValue(freshSnapshot);
  });

  afterEach(cleanup);

  it("issues exactly one request for each stable event and none for rerenders", async () => {
    const { rerender } = renderHook(
      ({ loading }) => useContextUsageController("chat-1", loading),
      { initialProps: { loading: false } },
    );
    await waitFor(() => expect(mocks.getContextUsage).toHaveBeenCalledTimes(1));

    rerender({ loading: false });
    await act(async () => Promise.resolve());
    expect(mocks.getContextUsage).toHaveBeenCalledTimes(1);

    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: { chat_id: "chat-1" },
      }),
    );
    await waitFor(() => expect(mocks.getContextUsage).toHaveBeenCalledTimes(2));

    window.dispatchEvent(new CustomEvent("model-switched"));
    await waitFor(() => expect(mocks.getContextUsage).toHaveBeenCalledTimes(3));
  });

  it("retries only a stale post-completion response until it becomes fresh", async () => {
    mocks.getContextUsage
      .mockResolvedValueOnce(freshSnapshot)
      .mockResolvedValueOnce({ ...freshSnapshot, stale: true })
      .mockResolvedValueOnce({ ...freshSnapshot, usage_ratio: 0.3 });
    const { result, rerender } = renderHook(
      ({ loading }) => useContextUsageController("chat-1", loading),
      { initialProps: { loading: true } },
    );
    await waitFor(() => expect(mocks.getContextUsage).toHaveBeenCalledTimes(1));

    rerender({ loading: true });
    rerender({ loading: false });

    await waitFor(() => expect(mocks.getContextUsage).toHaveBeenCalledTimes(3));
    expect(result.current.snapshot).toMatchObject({
      stale: false,
      usage_ratio: 0.3,
    });
    await new Promise((resolve) => window.setTimeout(resolve, 250));
    expect(mocks.getContextUsage).toHaveBeenCalledTimes(3);
  });

  it("stops completion retries after a chat switch", async () => {
    mocks.getContextUsage.mockResolvedValue({ ...freshSnapshot, stale: true });
    const { rerender } = renderHook(
      ({ chatId, loading }) => useContextUsageController(chatId, loading),
      { initialProps: { chatId: "chat-1", loading: true } },
    );
    await waitFor(() => expect(mocks.getContextUsage).toHaveBeenCalledTimes(1));
    rerender({ chatId: "chat-1", loading: false });
    await waitFor(() => expect(mocks.getContextUsage).toHaveBeenCalledTimes(2));

    rerender({ chatId: "chat-2", loading: false });
    await waitFor(() =>
      expect(mocks.getContextUsage).toHaveBeenLastCalledWith("chat-2"),
    );
    const callsAfterSwitch = mocks.getContextUsage.mock.calls.length;
    await new Promise((resolve) => window.setTimeout(resolve, 250));
    expect(mocks.getContextUsage).toHaveBeenCalledTimes(callsAfterSwitch);
  });

  it("stops retrying when the post-completion retry budget is exhausted", async () => {
    mocks.getContextUsage
      .mockResolvedValueOnce(freshSnapshot)
      .mockResolvedValue({ ...freshSnapshot, stale: true });
    const { rerender } = renderHook(
      ({ loading }) => useContextUsageController("chat-1", loading),
      { initialProps: { loading: true } },
    );
    await waitFor(() => expect(mocks.getContextUsage).toHaveBeenCalledTimes(1));

    rerender({ loading: false });

    await waitFor(
      () => expect(mocks.getContextUsage).toHaveBeenCalledTimes(5),
      { timeout: 1_000 },
    );
    await new Promise((resolve) => window.setTimeout(resolve, 250));
    expect(mocks.getContextUsage).toHaveBeenCalledTimes(5);
  });
});

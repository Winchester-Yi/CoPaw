import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
}));

vi.mock("../request", () => ({
  request: mocks.request,
}));

describe("chat context usage api", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("fetches the persisted context snapshot by encoded backend chat id", async () => {
    mocks.request.mockResolvedValueOnce({ available: false });
    const { chatApi } = await import("./chat");

    await expect(chatApi.getContextUsage("chat/id 1")).resolves.toEqual({
      available: false,
    });

    expect(mocks.request).toHaveBeenCalledWith(
      "/chats/chat%2Fid%201/context-usage",
    );
  });
});

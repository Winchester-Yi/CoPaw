import { describe, expect, it, vi } from "vitest";

const requestMock = vi.fn();

vi.mock("../request", () => ({
  request: requestMock,
}));

describe("marketMcpApi", () => {
  it("passes source id when loading MCP detail", async () => {
    window.__env__ = {
      systemCode: "",
      systemSect: "",
    };
    requestMock.mockResolvedValueOnce(null);
    const { marketMcpApi } = await import("./marketMcp");

    await marketMcpApi.getMarketMCPDetail("item-1", "source-a");

    expect(requestMock).toHaveBeenCalledWith(
      "/market/mcp/item-1",
      expect.objectContaining({
        headers: expect.any(Headers),
      }),
    );
    const options = requestMock.mock.calls[0][1] as RequestInit;
    expect((options.headers as Headers).get("X-Source-Id")).toBe("source-a");
  });
});

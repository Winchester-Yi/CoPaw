import { beforeEach, describe, expect, it, vi } from "vitest";
import { fetchSuggestions } from "./suggestions";

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
}));

vi.mock("../request", () => ({
  request: mocks.request,
}));

describe("suggestions api", () => {
  beforeEach(() => {
    mocks.request.mockReset();
  });

  it("polls backend suggestions by session id", async () => {
    mocks.request.mockResolvedValueOnce({
      suggestions: [
        {
          id: "1",
          suggestions: ["问题1", "问题2"],
        },
        {
          id: "2",
          suggestions: ["问题3"],
        },
        {
          id: "3",
          suggestions: null,
        },
      ],
    });

    await expect(fetchSuggestions({ sessionId: "session-1" })).resolves.toEqual([
      "问题1",
      "问题2",
      "问题3",
    ]);

    expect(mocks.request).toHaveBeenCalledWith(
      "/console/suggestions?session_id=session-1",
    );
  });

  it("includes turn id when polling a specific response", async () => {
    mocks.request.mockResolvedValueOnce({ suggestions: [] });

    await fetchSuggestions({
      sessionId: "session-1",
      turnId: "response-1",
    });

    expect(mocks.request).toHaveBeenCalledWith(
      "/console/suggestions?session_id=session-1&turn_id=response-1",
    );
  });

  it("returns empty array when backend entries are empty", async () => {
    mocks.request.mockResolvedValueOnce({
      suggestions: [
        {
          id: "1",
          suggestions: [],
        },
      ],
    });

    await expect(fetchSuggestions({ sessionId: "session-1" })).resolves.toEqual(
      [],
    );
  });

  it("encodes session id in backend query", async () => {
    mocks.request.mockResolvedValueOnce({ suggestions: [] });

    await fetchSuggestions({ sessionId: "chat 1+/?" });

    expect(mocks.request).toHaveBeenCalledWith(
      "/console/suggestions?session_id=chat+1%2B%2F%3F",
    );
  });

  it("returns empty array when backend request fails", async () => {
    mocks.request.mockRejectedValueOnce(new Error("boom"));

    await expect(fetchSuggestions({ sessionId: "session-1" })).resolves.toEqual(
      [],
    );
  });

  it("returns empty array when session id is empty", async () => {
    await expect(fetchSuggestions({ sessionId: "   " })).resolves.toEqual([]);
    expect(mocks.request).not.toHaveBeenCalled();
  });
});

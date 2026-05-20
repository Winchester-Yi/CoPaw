import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import useSuggestionsPolling from "./useSuggestionsPolling";
import type { CurrentQARef } from "./currentQARef";

const mocks = vi.hoisted(() => ({
  currentSessionId: "session-1" as string | undefined,
  sessionsContext: {},
  fetchSuggestions: vi.fn(),
  getLogicalSessionId: vi.fn(),
  updateMessage: vi.fn(),
}));

vi.mock("@/api/modules/suggestions", () => ({
  fetchSuggestions: mocks.fetchSuggestions,
}));

vi.mock("use-context-selector", () => ({
  createContext: vi.fn(() => ({})),
  useContextSelector: (
    context: unknown,
    selector: (value: unknown) => unknown,
  ) => {
    if (context === mocks.sessionsContext) {
      return selector({ currentSessionId: mocks.currentSessionId });
    }
    return selector({});
  },
}));

vi.mock("../../Context/ChatAnywhereSessionsContext", () => ({
  ChatAnywhereSessionsContext: mocks.sessionsContext,
}));

vi.mock("../../Context/ChatAnywhereOptionsContext", () => ({
  useChatAnywhereOptions: (selector: (value: unknown) => unknown) =>
    selector({
      session: {
        api: {
          getLogicalSessionId: mocks.getLogicalSessionId,
        },
      },
    }),
}));

let hookApi: ReturnType<typeof useSuggestionsPolling>;
let root: Root | undefined;
let container: HTMLDivElement | undefined;

function createCurrentQARef(): CurrentQARef {
  return {
    current: {
      request: {
        cards: [
          {
            data: {
              input: [
                {
                  content: [{ type: "text", text: "用户问题" }],
                },
              ],
            },
          },
        ],
      },
      response: {
        id: "response-1",
        role: "assistant",
        cards: [
          {
            data: {
              id: "response-1",
              output: [
                {
                  id: "message-1",
                  role: "assistant",
                  type: "message",
                  content: [{ type: "text", text: "助手回答" }],
                },
              ],
            },
          },
        ],
      },
    },
  } as CurrentQARef;
}

function Harness(props: { currentQARef: CurrentQARef }) {
  hookApi = useSuggestionsPolling({
    currentQARef: props.currentQARef,
    updateMessage: mocks.updateMessage,
  });
  return null;
}

function renderHarness(currentQARef: CurrentQARef) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(<Harness currentQARef={currentQARef} />);
  });
}

function rerenderHarness(currentQARef: CurrentQARef) {
  act(() => {
    root?.render(<Harness currentQARef={currentQARef} />);
  });
}

describe("useSuggestionsPolling", () => {
  beforeEach(() => {
    mocks.currentSessionId = "session-1";
    mocks.fetchSuggestions.mockReset();
    mocks.getLogicalSessionId.mockReset();
    mocks.updateMessage.mockReset();
    mocks.getLogicalSessionId.mockImplementation((sessionId: string) => sessionId);
    vi.useRealTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    act(() => {
      root?.unmount();
    });
    container?.remove();
    root = undefined;
    container = undefined;
  });

  it("polls backend suggestions by session id and updates response", async () => {
    const currentQARef = createCurrentQARef();
    mocks.fetchSuggestions.mockResolvedValue(["前端问题"]);

    renderHarness(currentQARef);

    await act(async () => {
      await hookApi.pollSuggestions();
    });

    expect(mocks.fetchSuggestions).toHaveBeenCalledWith({
      sessionId: "session-1",
      turnId: "response-1",
    });
    expect(currentQARef.current.response?.cards?.[0]?.data.suggestions).toEqual(
      ["前端问题"],
    );
    expect(mocks.updateMessage).toHaveBeenCalledWith(
      currentQARef.current.response,
    );
  });

  it("uses logical session id from session api", async () => {
    const currentQARef = createCurrentQARef();
    mocks.currentSessionId = "real-session-1";
    mocks.getLogicalSessionId.mockReturnValue("session-1");
    mocks.fetchSuggestions.mockResolvedValue(["前端问题"]);

    renderHarness(currentQARef);

    await act(async () => {
      await hookApi.pollSuggestions();
    });

    expect(mocks.fetchSuggestions).toHaveBeenCalledWith({
      sessionId: "session-1",
      turnId: "response-1",
    });
  });

  it("waits for session id before polling backend", async () => {
    vi.useFakeTimers();
    const currentQARef = createCurrentQARef();
    mocks.currentSessionId = undefined;
    mocks.fetchSuggestions.mockResolvedValue(["后端问题"]);

    renderHarness(currentQARef);

    const pollPromise = hookApi.pollSuggestions();

    await act(async () => {
      await Promise.resolve();
    });
    expect(mocks.fetchSuggestions).not.toHaveBeenCalled();

    mocks.currentSessionId = "session-1";
    rerenderHarness(currentQARef);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(800);
      await pollPromise;
    });

    expect(mocks.fetchSuggestions).toHaveBeenCalledWith({
      sessionId: "session-1",
      turnId: "response-1",
    });
    expect(currentQARef.current.response?.cards?.[0]?.data.suggestions).toEqual(
      ["后端问题"],
    );
  });

  it("keeps polling until backend suggestions are available", async () => {
    vi.useFakeTimers();
    const currentQARef = createCurrentQARef();
    mocks.fetchSuggestions
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce(["后端问题"]);

    renderHarness(currentQARef);

    const pollPromise = hookApi.pollSuggestions();

    await act(async () => {
      await Promise.resolve();
    });
    expect(mocks.fetchSuggestions).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(800);
      await pollPromise;
    });

    expect(mocks.fetchSuggestions).toHaveBeenCalledTimes(2);
    expect(currentQARef.current.response?.cards?.[0]?.data.suggestions).toEqual(
      ["后端问题"],
    );
    expect(mocks.updateMessage).toHaveBeenCalledWith(
      currentQARef.current.response,
    );
  });

  it("does not update stale response when response id changes", async () => {
    const currentQARef = createCurrentQARef();
    let resolveSuggestions!: (suggestions: string[]) => void;
    mocks.fetchSuggestions.mockReturnValue(
      new Promise<string[]>((resolve) => {
        resolveSuggestions = resolve;
      }),
    );

    renderHarness(currentQARef);

    const pollPromise = hookApi.pollSuggestions();
    currentQARef.current.response = {
      ...currentQARef.current.response,
      id: "response-2",
    };

    await act(async () => {
      resolveSuggestions(["过期问题"]);
      await pollPromise;
    });

    expect(mocks.updateMessage).not.toHaveBeenCalled();
    expect(
      currentQARef.current.response?.cards?.[0]?.data.suggestions,
    ).toBeUndefined();
  });

  it("does not call suggestions API when response id is missing", async () => {
    const currentQARef = createCurrentQARef();
    delete currentQARef.current.response?.id;

    renderHarness(currentQARef);

    await act(async () => {
      await hookApi.pollSuggestions();
    });

    expect(mocks.fetchSuggestions).not.toHaveBeenCalled();
    expect(mocks.updateMessage).not.toHaveBeenCalled();
  });
});

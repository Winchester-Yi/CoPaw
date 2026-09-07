import React from "react";
import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import useChatController from "./useChatController";
import type { IAgentScopeRuntimeWebUIInputData } from "@/components/agentscope-chat";
import type { CurrentQARef } from "./currentQARef";
import type { ChatRequestOwner } from "./requestOwnership";

const mocks = vi.hoisted(() => ({
  inputContext: {},
  sessionsContext: {},
  setLoading: vi.fn(),
  getLoading: vi.fn(() => false),
  getSession: vi.fn(async () => ({ generating: false })),
  request: vi.fn(),
  reconnect: vi.fn(),
  cancelActiveRequest: vi.fn(),
  updateMessage: vi.fn(),
  getMessages: vi.fn(() => [{ id: "message-1" }]),
  hasMessage: vi.fn(() => true),
  getHistoryMessages: vi.fn(() => []),
  createRequestMessage: vi.fn(),
  createApprovalMessage: vi.fn(),
  createResponseMessage: vi.fn(),
  removeMessageById: vi.fn(),
  ensureSession: vi.fn(),
  updateSessionName: vi.fn(),
  getCurrentSessionId: vi.fn(() => "chat-b"),
  syncSessionMessages: vi.fn(),
  syncSessionMessagesForSession: vi.fn(),
  pollSuggestions: vi.fn(),
  sleep: vi.fn(async () => {}),
}));

let latestCurrentQARef: CurrentQARef | undefined;
let latestRequestOptions:
  | {
      onFinish: (owner: ChatRequestOwner) => void;
    }
  | undefined;
let latestController:
  | {
      handleSubmit: (data: IAgentScopeRuntimeWebUIInputData) => void;
      handleCancel: () => void;
    }
  | undefined;

function hasMockReset(value: unknown): value is { mockReset: () => void } {
  return (
    typeof value === "object" &&
    value !== null &&
    "mockReset" in value &&
    typeof (value as { mockReset?: unknown }).mockReset === "function"
  );
}

vi.mock("use-context-selector", () => ({
  createContext: vi.fn(() => ({})),
  useContextSelector: (
    context: unknown,
    selector: (value: unknown) => unknown,
  ) => {
    if (context === mocks.inputContext) {
      return selector({
        setLoading: mocks.setLoading,
        getLoading: mocks.getLoading,
      });
    }

    if (context === mocks.sessionsContext) {
      return selector({
        currentSessionId: "chat-b",
      });
    }

    return selector({});
  },
}));

vi.mock("../../Context/ChatAnywhereInputContext", () => ({
  ChatAnywhereInputContext: mocks.inputContext,
}));

vi.mock("../../Context/ChatAnywhereSessionsContext", () => ({
  ChatAnywhereSessionsContext: mocks.sessionsContext,
}));

vi.mock("../../Context/ChatAnywhereOptionsContext", () => ({
  useChatAnywhereOptions: (selector: (value: unknown) => unknown) =>
    selector({
      session: {
        api: {
          getSession: mocks.getSession,
          getLogicalSessionId: (sessionId: string) => `logical:${sessionId}`,
          getChatIdForSession: (sessionId: string) => `chat:${sessionId}`,
        },
      },
    }),
}));

vi.mock("./useChatMessageHandler", () => ({
  __esModule: true,
  default: ({ currentQARef }: { currentQARef: CurrentQARef }) => {
    latestCurrentQARef = currentQARef;
    return {
      updateMessage: mocks.updateMessage,
      getMessages: mocks.getMessages,
      hasMessage: mocks.hasMessage,
      getHistoryMessages: mocks.getHistoryMessages,
      createRequestMessage: mocks.createRequestMessage,
      createApprovalMessage: mocks.createApprovalMessage,
      createResponseMessage: mocks.createResponseMessage.mockImplementation(
        () => {
          currentQARef.current.response = {
            id: "response-a",
            msgStatus: "generating",
            cards: [
              {
                code: "AgentScopeRuntimeResponseCard",
                data: {
                  id: "response-a",
                  status: "created",
                  created_at: 0,
                  output: [],
                },
              },
            ],
          };
        },
      ),
      removeMessageById: mocks.removeMessageById,
    };
  },
}));

vi.mock("./useChatRequest", () => ({
  __esModule: true,
  default: (options: { onFinish: (owner: ChatRequestOwner) => void }) => {
    latestRequestOptions = options;
    return {
      request: mocks.request,
      reconnect: mocks.reconnect,
      cancelActiveRequest: mocks.cancelActiveRequest,
    };
  },
}));

vi.mock("./useChatSessionHandler", () => ({
  __esModule: true,
  default: () => ({
    ensureSession: mocks.ensureSession,
    updateSessionName: mocks.updateSessionName,
    getCurrentSessionId: mocks.getCurrentSessionId,
    syncSessionMessages: mocks.syncSessionMessages,
    syncSessionMessagesForSession: mocks.syncSessionMessagesForSession,
  }),
}));

vi.mock("./useSuggestionsPolling", () => ({
  __esModule: true,
  default: () => ({
    pollSuggestions: mocks.pollSuggestions,
  }),
}));

vi.mock("@/components/agentscope-chat", () => ({
  sleep: (...args: unknown[]) => mocks.sleep(...args),
}));

vi.mock("react-dom", () => ({
  __esModule: true,
  default: {
    flushSync: (callback: () => void) => callback(),
  },
}));

function Harness() {
  latestController = useChatController();
  return null;
}

describe("useChatController", () => {
  beforeEach(() => {
    Object.values(mocks).forEach((value) => {
      if (hasMockReset(value)) {
        value.mockReset();
      }
    });
    mocks.getLoading.mockReturnValue(false);
    mocks.getSession.mockResolvedValue({ generating: false });
    mocks.getMessages.mockReturnValue([{ id: "message-1" }]);
    latestCurrentQARef = undefined;
    latestRequestOptions = undefined;
    latestController = undefined;
  });

  it("syncs completion-time messages back to the request's owning session", async () => {
    render(<Harness />);

    latestCurrentQARef!.current.response = {
      id: "response-a",
      msgStatus: "generating",
      cards: [
        {
          code: "AgentScopeRuntimeResponseCard",
          data: {
            id: "response-a",
            status: "in_progress",
            created_at: 0,
            output: [],
          },
        },
      ],
    };

    await act(async () => {
      latestRequestOptions!.onFinish({
        requestId: "request-a",
        kind: "reconnect",
        sessionId: "chat-a",
        logicalSessionId: "logical:chat-a",
        chatId: "chat:chat-a",
      });
    });

    expect(mocks.syncSessionMessagesForSession).toHaveBeenCalledWith(
      "chat-a",
      [{ id: "message-1" }],
      false,
      { refreshList: false },
    );
  });

  it("clears loading when completion arrives after the response was removed", async () => {
    render(<Harness />);
    latestCurrentQARef!.current.activeRequestOwner = {
      requestId: "request-a",
      kind: "submit",
      sessionId: "chat-b",
      logicalSessionId: "logical:chat-b",
      chatId: "chat:chat-b",
    };

    await act(async () => {
      latestRequestOptions!.onFinish({
        requestId: "request-a",
        kind: "submit",
        sessionId: "chat-b",
        logicalSessionId: "logical:chat-b",
        chatId: "chat:chat-b",
      });
    });

    expect(mocks.setLoading).toHaveBeenCalledWith(false);
    expect(latestCurrentQARef!.current.activeRequestOwner).toBeUndefined();
  });

  it("marks the owning session as generating before waiting for the first frame", async () => {
    let releaseSleep: (() => void) | undefined;
    mocks.sleep.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          releaseSleep = resolve;
        }),
    );

    render(<Harness />);
    expect(latestController).toBeTruthy();

    await act(async () => {
      const submitPromise = latestController!.handleSubmit({
        query: "hello",
        fileList: [],
      });

      await waitFor(() =>
        expect(mocks.syncSessionMessagesForSession).toHaveBeenCalledWith(
          "chat-b",
          [{ id: "message-1" }],
          true,
          { refreshList: false },
        ),
      );

      releaseSleep?.();
      await submitPromise;
    });
  });

  it("keeps first-turn local session updates out of the chat list refresh path", async () => {
    render(<Harness />);

    await act(async () => {
      await latestController!.handleSubmit({
        query: "hello",
        fileList: [],
      });
    });

    expect(mocks.updateSessionName).toHaveBeenCalledWith(
      "hello",
      [{ id: "message-1" }],
      { refreshList: false },
    );
    expect(mocks.syncSessionMessagesForSession).toHaveBeenCalledWith(
      "chat-b",
      [{ id: "message-1" }],
      true,
      { refreshList: false },
    );
  });

  it("cancels the active backend request when the user stops a response", async () => {
    render(<Harness />);

    latestCurrentQARef!.current.response = {
      id: "response-a",
      msgStatus: "generating",
      cards: [
        {
          code: "AgentScopeRuntimeResponseCard",
          data: {
            id: "response-a",
            status: "in_progress",
            created_at: 0,
            output: [],
          },
        },
      ],
    };
    latestCurrentQARef!.current.activeRequestOwner = {
      requestId: "request-a",
      kind: "submit",
      sessionId: "chat-b",
      logicalSessionId: "logical:chat-b",
      chatId: "chat:chat-b",
    };

    await act(async () => {
      await latestController!.handleCancel();
    });

    expect(mocks.cancelActiveRequest).toHaveBeenCalledTimes(1);
    expect(mocks.setLoading).toHaveBeenCalledWith(false);
    expect(latestCurrentQARef!.current.activeRequestOwner).toBeUndefined();
    expect(mocks.syncSessionMessagesForSession).toHaveBeenCalledWith(
      "chat-b",
      [{ id: "message-1" }],
      false,
      { refreshList: false },
    );
  });

  it("keeps the composer locked until the Stop request settles", async () => {
    render(<Harness />);
    mocks.setLoading.mockClear();
    let finishStop: (() => void) | undefined;
    mocks.cancelActiveRequest.mockImplementation(
      () => new Promise<void>((resolve) => (finishStop = resolve)),
    );
    latestCurrentQARef!.current.response = {
      id: "response-a",
      msgStatus: "generating",
      cards: [],
    };

    const stopPromise = latestController!.handleCancel();
    await waitFor(() => expect(mocks.setLoading).toHaveBeenCalledWith(true));
    expect(mocks.setLoading).not.toHaveBeenCalledWith(false);

    finishStop?.();
    await act(async () => {
      await stopPromise;
    });

    expect(mocks.setLoading).toHaveBeenCalledWith(false);
  });
});

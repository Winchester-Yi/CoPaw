import React from "react";
import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import useChatRequest from "./useChatRequest";
import { isChatStreamAbortReason } from "./abortReasons";
import type { CurrentQARef } from "./currentQARef";
import type { ChatRequestOwner } from "./requestOwnership";

const mocks = vi.hoisted(() => {
  const streamGate = {
    promise: Promise.resolve(),
    resolve: () => {},
  };
  const streamChunks = [
    {
      data: JSON.stringify({
        object: "response",
        id: "response-1",
        status: "in_progress",
        created_at: 1,
        output: [
          {
            object: "message",
            id: "message-1",
            role: "assistant",
            type: "message",
            status: "in_progress",
            content: [
              {
                object: "content",
                type: "text",
                text: "hello",
                status: "completed",
              },
            ],
          },
        ],
      }),
    },
    {
      data: JSON.stringify({
        object: "response",
        id: "response-1",
        status: "completed",
        created_at: 1,
        completed_at: 2,
        output: [
          {
            object: "message",
            id: "message-1",
            role: "assistant",
            type: "message",
            status: "completed",
            content: [
              {
                object: "content",
                type: "text",
                text: "hello world",
                status: "completed",
              },
            ],
          },
        ],
      }),
    },
  ];

  return {
    fetch: vi.fn(),
    reconnect: vi.fn(),
    cancel: vi.fn(),
    streamGate,
    streamChunks,
  };
});

vi.mock("@/components/agentscope-chat", () => ({
  sleep: vi.fn(async () => {}),
  uuid: vi.fn(() => "uuid-1"),
  Stream: vi.fn(() => ({
    async *[Symbol.asyncIterator]() {
      yield mocks.streamChunks[0];

      await mocks.streamGate.promise;

      for (const chunk of mocks.streamChunks.slice(1)) {
        yield chunk;
      }
    },
  })),
}));

vi.mock("../../Context/ChatAnywhereOptionsContext", () => ({
  useChatAnywhereOptions: (selector: (value: unknown) => unknown) =>
    selector({
      api: {
        fetch: mocks.fetch,
        reconnect: mocks.reconnect,
        cancel: mocks.cancel,
        responseParser: JSON.parse,
      },
    }),
}));

let hookApi: ReturnType<typeof useChatRequest>;

function createOwner(
  overrides: Partial<ChatRequestOwner> = {},
): ChatRequestOwner {
  return {
    requestId: "request-1",
    kind: "submit",
    sessionId: "chat-a",
    logicalSessionId: "logical-a",
    chatId: "chat-real-a",
    ...overrides,
  };
}

function Harness(props: {
  currentQARef: CurrentQARef;
  updateMessage: (message: unknown) => void;
  hasMessage?: (id: string) => boolean;
  onFinish: (owner: ChatRequestOwner) => void;
}) {
  hookApi = useChatRequest({
    currentQARef: props.currentQARef,
    updateMessage: props.updateMessage,
    hasMessage: props.hasMessage,
    getCurrentSessionId: () => "chat-b",
    onFinish: props.onFinish,
  });

  return null;
}

describe("useChatRequest", () => {
  beforeEach(() => {
    mocks.fetch.mockReset();
    mocks.reconnect.mockReset();
    mocks.cancel.mockReset();
    mocks.streamChunks.splice(
      0,
      mocks.streamChunks.length,
      {
        data: JSON.stringify({
          object: "response",
          id: "response-1",
          status: "in_progress",
          created_at: 1,
          output: [
            {
              object: "message",
              id: "message-1",
              role: "assistant",
              type: "message",
              status: "in_progress",
              content: [
                {
                  object: "content",
                  type: "text",
                  text: "hello",
                  status: "completed",
                },
              ],
            },
          ],
        }),
      },
      {
        data: JSON.stringify({
          object: "response",
          id: "response-1",
          status: "completed",
          created_at: 1,
          completed_at: 2,
          output: [
            {
              object: "message",
              id: "message-1",
              role: "assistant",
              type: "message",
              status: "completed",
              content: [
                {
                  object: "content",
                  type: "text",
                  text: "hello world",
                  status: "completed",
                },
              ],
            },
          ],
        }),
      },
    );
    let resolveGate: () => void = () => {};
    mocks.streamGate.promise = new Promise<void>((resolve) => {
      resolveGate = resolve;
    });
    mocks.streamGate.resolve = resolveGate;
  });

  it("ignores delayed SSE chunks after another request owns the active response", async () => {
    mocks.fetch.mockResolvedValue({
      ok: true,
      body: {},
    } as Response);

    const updateMessage = vi.fn();
    const onFinish = vi.fn();
    const currentQARef = {
      current: {
        response: {
          id: "ui-response-a",
          msgStatus: "generating",
          cards: [
            {
              code: "AgentScopeRuntimeResponseCard",
              data: {
                id: "response-1",
                status: "created",
                created_at: 0,
                output: [],
              },
            },
          ],
        },
        activeRequestOwner: createOwner(),
      },
    } as CurrentQARef;

    render(
      <Harness
        currentQARef={currentQARef}
        updateMessage={updateMessage}
        onFinish={onFinish}
      />,
    );

    const requestPromise = hookApi.request([], undefined, createOwner());

    await waitFor(() => {
      expect(updateMessage).toHaveBeenCalledTimes(1);
    });

    currentQARef.current.activeRequestOwner = createOwner({
      requestId: "request-2",
      sessionId: "chat-b",
      logicalSessionId: "logical-b",
      chatId: "chat-real-b",
    });
    currentQARef.current.response = {
      id: "ui-response-b",
      role: "assistant",
      msgStatus: "generating",
      cards: [
        {
          code: "AgentScopeRuntimeResponseCard",
          data: {
            id: "response-2",
            status: "created",
            created_at: 0,
            output: [],
          },
        },
      ],
    };

    mocks.streamGate.resolve();

    await act(async () => {
      await requestPromise;
    });

    expect(updateMessage).toHaveBeenCalledTimes(1);
    expect(onFinish).not.toHaveBeenCalled();
  });

  it("does not append delayed chunks after the live response leaves the current message list", async () => {
    mocks.fetch.mockResolvedValue({
      ok: true,
      body: {},
    } as Response);

    const updateMessage = vi.fn();
    const onFinish = vi.fn();
    const currentQARef = {
      current: {
        response: {
          id: "ui-response-a",
          msgStatus: "generating",
          cards: [
            {
              code: "AgentScopeRuntimeResponseCard",
              data: {
                id: "response-1",
                status: "created",
                created_at: 0,
                output: [],
              },
            },
          ],
        },
        activeRequestOwner: createOwner(),
      },
    } as CurrentQARef;
    let responseMounted = true;

    render(
      <Harness
        currentQARef={currentQARef}
        updateMessage={updateMessage}
        hasMessage={() => responseMounted}
        onFinish={onFinish}
      />,
    );

    const requestPromise = hookApi.request([], undefined, createOwner());

    await waitFor(() => {
      expect(updateMessage).toHaveBeenCalledTimes(1);
    });

    responseMounted = false;
    mocks.streamGate.resolve();

    await act(async () => {
      await requestPromise;
    });

    expect(updateMessage).toHaveBeenCalledTimes(1);
    expect(onFinish).not.toHaveBeenCalled();
  });

  it("passes the owning session identifiers through fetch", async () => {
    mocks.fetch.mockResolvedValue({
      ok: true,
      body: null,
    } as Response);

    const currentQARef = {
      current: {
        response: {
          id: "ui-response-a",
          msgStatus: "generating",
          cards: [
            {
              code: "AgentScopeRuntimeResponseCard",
              data: {
                id: "response-1",
                status: "created",
                created_at: 0,
                output: [],
              },
            },
          ],
        },
        activeRequestOwner: createOwner(),
      },
    } as CurrentQARef;

    render(
      <Harness
        currentQARef={currentQARef}
        updateMessage={vi.fn()}
        onFinish={vi.fn()}
      />,
    );

    await act(async () => {
      await hookApi.request([], undefined, createOwner());
    });

    expect(mocks.fetch).toHaveBeenCalledWith(
      expect.objectContaining({
        session_id: "chat-a",
        logical_session_id: "logical-a",
        chat_id: "chat-real-a",
        turn_id: "ui-response-a",
      }),
    );
  });

  it("passes the owning session identifiers through reconnect", async () => {
    mocks.reconnect.mockResolvedValue({
      ok: true,
      body: null,
    } as Response);

    const currentQARef = {
      current: {
        response: {
          id: "ui-response-a",
          msgStatus: "generating",
          cards: [
            {
              code: "AgentScopeRuntimeResponseCard",
              data: {
                id: "response-1",
                status: "created",
                created_at: 0,
                output: [],
              },
            },
          ],
        },
        activeRequestOwner: createOwner(),
      },
    } as CurrentQARef;

    render(
      <Harness
        currentQARef={currentQARef}
        updateMessage={vi.fn()}
        onFinish={vi.fn()}
      />,
    );

    await act(async () => {
      await hookApi.reconnect("chat-a", createOwner());
    });

    expect(mocks.reconnect).toHaveBeenCalledWith(
      expect.objectContaining({
        session_id: "chat-a",
        logical_session_id: "logical-a",
        chat_id: "chat-real-a",
      }),
    );
  });

  it("cancels the active backend run with the owning chat identifiers", async () => {
    const abortController = new AbortController();
    const currentQARef = {
      current: {
        abortController,
        activeRequestOwner: createOwner(),
        response: {
          id: "ui-response-a",
          msgStatus: "generating",
          cards: [
            {
              code: "AgentScopeRuntimeResponseCard",
              data: {
                id: "response-1",
                status: "in_progress",
                created_at: 0,
                output: [],
              },
            },
          ],
        },
      },
    } as CurrentQARef;

    render(
      <Harness
        currentQARef={currentQARef}
        updateMessage={vi.fn()}
        onFinish={vi.fn()}
      />,
    );

    await act(async () => {
      await hookApi.cancelActiveRequest();
    });

    expect(mocks.cancel).toHaveBeenCalledWith({
      session_id: "chat-a",
      logical_session_id: "logical-a",
      chat_id: "chat-real-a",
    });
    expect(isChatStreamAbortReason(abortController.signal.reason, "stop")).toBe(
      true,
    );
  });

  it("finishes on completed response frames even when output is empty", async () => {
    mocks.fetch.mockResolvedValue({
      ok: true,
      body: {},
    } as Response);
    mocks.streamChunks[1] = {
      data: JSON.stringify({
        object: "response",
        id: "response-1",
        status: "completed",
        created_at: 1,
        completed_at: 2,
        output: [],
      }),
    };

    const updateMessage = vi.fn();
    const onFinish = vi.fn();
    const currentQARef = {
      current: {
        response: {
          id: "ui-response-a",
          msgStatus: "generating",
          cards: [
            {
              code: "AgentScopeRuntimeResponseCard",
              data: {
                id: "response-1",
                status: "created",
                created_at: 0,
                output: [],
              },
            },
          ],
        },
        activeRequestOwner: createOwner(),
      },
    } as CurrentQARef;

    render(
      <Harness
        currentQARef={currentQARef}
        updateMessage={updateMessage}
        onFinish={onFinish}
      />,
    );

    const requestPromise = hookApi.request([], undefined, createOwner());
    mocks.streamGate.resolve();

    await act(async () => {
      await requestPromise;
    });

    expect(updateMessage).toHaveBeenCalledTimes(1);
    expect(onFinish).toHaveBeenCalledWith(createOwner());
  });

  it("preserves the latest assistant output on terminal empty response frames", async () => {
    mocks.fetch.mockResolvedValue({
      ok: true,
      body: {},
    } as Response);
    mocks.streamChunks[1] = {
      data: JSON.stringify({
        object: "response",
        id: "response-1",
        status: "failed",
        created_at: 1,
        completed_at: 3,
        output: [],
        error: {
          code: "timeout",
          message: "timed out",
        },
      }),
    };

    const updateMessage = vi.fn();
    const onFinish = vi.fn();
    const currentQARef = {
      current: {
        response: {
          id: "ui-response-a",
          msgStatus: "generating",
          cards: [
            {
              code: "AgentScopeRuntimeResponseCard",
              data: {
                id: "response-1",
                status: "created",
                created_at: 0,
                output: [],
              },
            },
          ],
        },
        activeRequestOwner: createOwner(),
      },
    } as CurrentQARef;

    render(
      <Harness
        currentQARef={currentQARef}
        updateMessage={updateMessage}
        onFinish={onFinish}
      />,
    );

    const requestPromise = hookApi.request([], undefined, createOwner());
    mocks.streamGate.resolve();

    await act(async () => {
      await requestPromise;
    });

    const responseCardData = currentQARef.current.response?.cards?.[0]
      ?.data as { output?: Array<{ content?: Array<{ text?: string }> }>; status?: string };

    expect(responseCardData.status).toBe("failed");
    expect(responseCardData.output?.[0]?.content?.[0]?.text).toBe("hello");
    expect(onFinish).toHaveBeenCalledWith(createOwner());
  });

  it("does not render backend task cancellation errors", async () => {
    mocks.fetch.mockResolvedValue({
      ok: true,
      body: {},
    } as Response);
    mocks.streamChunks[1] = {
      data: JSON.stringify({
        object: "message",
        id: "error",
        role: "assistant",
        type: "error",
        status: "failed",
        code: "AGENT_ERROR",
        message: "Task has been cancelled!",
        content: [],
      }),
    };

    const updateMessage = vi.fn();
    const onFinish = vi.fn();
    const currentQARef = {
      current: {
        response: {
          id: "ui-response-a",
          msgStatus: "generating",
          cards: [
            {
              code: "AgentScopeRuntimeResponseCard",
              data: {
                id: "response-1",
                status: "created",
                created_at: 0,
                output: [],
              },
            },
          ],
        },
        activeRequestOwner: createOwner(),
      },
    } as CurrentQARef;

    render(
      <Harness
        currentQARef={currentQARef}
        updateMessage={updateMessage}
        onFinish={onFinish}
      />,
    );

    const requestPromise = hookApi.request([], undefined, createOwner());
    mocks.streamGate.resolve();

    await act(async () => {
      await requestPromise;
    });

    const responseCardData = currentQARef.current.response?.cards?.[0]
      ?.data as { output?: Array<{ type?: string; message?: string }> };

    expect(responseCardData.output).toHaveLength(1);
    expect(responseCardData.output?.[0]?.type).toBe("message");
    expect(responseCardData.output?.[0]?.message).toBeUndefined();
    expect(updateMessage).toHaveBeenCalledTimes(1);
    expect(onFinish).toHaveBeenCalledWith(createOwner());
  });
});

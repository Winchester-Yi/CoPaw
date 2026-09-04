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
  const streamChunks: Array<{ data: string; event?: string }> = [
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
  applyRecoverySnapshot?: (history: unknown, owner: ChatRequestOwner) => void;
  recoverAfterNotFound?: (owner: ChatRequestOwner) => void;
}) {
  hookApi = useChatRequest({
    currentQARef: props.currentQARef,
    updateMessage: props.updateMessage,
    hasMessage: props.hasMessage,
    getCurrentSessionId: () => "chat-b",
    onFinish: props.onFinish,
    applyRecoverySnapshot: props.applyRecoverySnapshot,
    recoverAfterNotFound: props.recoverAfterNotFound,
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

  it("finishes a W+ entry proposal as a structured Chat card", async () => {
    mocks.fetch.mockResolvedValue({
      ok: true,
      body: {},
    } as Response);
    mocks.streamChunks.splice(0, mocks.streamChunks.length, {
      data: JSON.stringify({
        object: "wplus_sop_entry_proposal",
        status: "completed",
        proposal_id: "proposal-1",
        mode: "explicit",
        chat_id: "chat-real-a",
        session_id: "logical-a",
        title: "进入 W+ SOP 工作台",
        message: "确认后进入独立工作台。",
      }),
    });
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
        updateMessage={vi.fn()}
        onFinish={onFinish}
      />,
    );

    mocks.streamGate.resolve();
    await act(async () => {
      await hookApi.request([], undefined, createOwner());
    });

    expect(currentQARef.current.response?.cards).toEqual([
      {
        code: "WPlusSopEntryProposal",
        data: expect.objectContaining({
          proposal_id: "proposal-1",
          mode: "explicit",
        }),
      },
    ]);
    expect(onFinish).toHaveBeenCalledWith(createOwner());
  });

  it("finishes when the stream ends after terminal message frames", async () => {
    mocks.fetch.mockResolvedValue({
      ok: true,
      body: {},
    } as Response);
    mocks.streamChunks.splice(
      0,
      mocks.streamChunks.length,
      {
        data: JSON.stringify({
          object: "response",
          id: "response-1",
          status: "in_progress",
          created_at: 1,
          output: [],
        }),
      },
      {
        data: JSON.stringify({
          object: "message",
          id: "message-1",
          role: "assistant",
          type: "message",
          status: "completed",
          content: [
            {
              object: "content",
              type: "text",
              text: "hello",
              status: "completed",
            },
          ],
        }),
      },
    );
    const onFinish = vi.fn();
    const currentQARef = {
      current: {
        response: {
          id: "ui-response-a",
          msgStatus: "generating",
          cards: [],
        },
        activeRequestOwner: createOwner(),
      },
    } as CurrentQARef;

    render(
      <Harness
        currentQARef={currentQARef}
        updateMessage={vi.fn()}
        onFinish={onFinish}
      />,
    );

    mocks.streamGate.resolve();
    await act(async () => {
      await hookApi.request([], undefined, createOwner());
    });

    expect(onFinish).toHaveBeenCalledWith(createOwner());
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

  it("applies a named terminal chat snapshot without creating an error card", async () => {
    const applyRecoverySnapshot = vi.fn();
    mocks.streamChunks.splice(0, mocks.streamChunks.length, {
      event: "chat.snapshot",
      data: JSON.stringify({
        object: "chat_snapshot",
        chat_id: "chat-real-a",
        msgid: "msg-2",
        turn_status: "completed",
        history: { messages: [{ id: "user-1", role: "user" }] },
      }),
    });
    mocks.reconnect.mockResolvedValue({
      ok: true,
      body: {},
      headers: new Headers(),
    } as Response);

    const currentQARef = {
      current: {
        response: {
          id: "ui-response-a",
          msgStatus: "generating",
          cards: [],
        },
        activeRequestOwner: createOwner({ kind: "reconnect" }),
      },
    } as CurrentQARef;

    const updateMessage = vi.fn();
    const onFinish = vi.fn();
    render(
      <Harness
        currentQARef={currentQARef}
        updateMessage={updateMessage}
        onFinish={onFinish}
        applyRecoverySnapshot={applyRecoverySnapshot}
      />,
    );

    await act(async () => {
      await hookApi.reconnect("chat-a", createOwner({ kind: "reconnect" }));
    });

    expect(applyRecoverySnapshot).toHaveBeenCalledWith(
      expect.objectContaining({ messages: [{ id: "user-1", role: "user" }] }),
      expect.objectContaining({ msgid: "msg-2" }),
    );
    expect(updateMessage).not.toHaveBeenCalled();
    expect(onFinish).not.toHaveBeenCalled();
  });

  it("uses the compatibility refresh callback for a reconnect 404", async () => {
    const recoverAfterNotFound = vi.fn();
    mocks.reconnect.mockResolvedValue({
      ok: false,
      status: 404,
      body: {},
      json: vi.fn(async () => ({ detail: "No running chat for this session" })),
    } as unknown as Response);
    const currentQARef = {
      current: {
        response: {
          id: "ui-response-a",
          msgStatus: "generating",
          cards: [],
        },
        activeRequestOwner: createOwner({ kind: "reconnect" }),
      },
    } as CurrentQARef;

    render(
      <Harness
        currentQARef={currentQARef}
        updateMessage={vi.fn()}
        onFinish={vi.fn()}
        recoverAfterNotFound={recoverAfterNotFound}
      />,
    );

    await act(async () => {
      await hookApi.reconnect("chat-a", createOwner({ kind: "reconnect" }));
    });

    expect(recoverAfterNotFound).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "reconnect" }),
    );
  });

  it("retries transient settlement responses before applying legacy errors", async () => {
    const recoverAfterNotFound = vi.fn();
    mocks.reconnect
      .mockResolvedValueOnce({
        ok: false,
        status: 503,
        body: {},
        headers: new Headers({ "Retry-After": "0" }),
      } as unknown as Response)
      .mockResolvedValueOnce({
        ok: false,
        status: 404,
        body: {},
        headers: new Headers(),
        json: vi.fn(async () => ({ detail: "missing" })),
      } as unknown as Response);
    const currentQARef = {
      current: {
        response: { id: "ui-response-a", msgStatus: "generating", cards: [] },
        activeRequestOwner: createOwner({ kind: "reconnect" }),
      },
    } as CurrentQARef;

    render(
      <Harness
        currentQARef={currentQARef}
        updateMessage={vi.fn()}
        onFinish={vi.fn()}
        recoverAfterNotFound={recoverAfterNotFound}
      />,
    );

    await act(async () => {
      await hookApi.reconnect("chat-a", createOwner({ kind: "reconnect" }));
    });

    expect(mocks.reconnect).toHaveBeenCalledTimes(2);
    expect(recoverAfterNotFound).toHaveBeenCalledOnce();
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

  it("uses the stream turn identity when stopping a started request", async () => {
    const abortController = new AbortController();
    const owner = createOwner({ chatId: null });
    const currentQARef = {
      current: {
        abortController,
        activeRequestOwner: owner,
        response: {
          id: "ui-response-a",
          msgStatus: "generating",
          cards: [],
        },
      },
    } as CurrentQARef;
    mocks.fetch.mockResolvedValue({
      ok: true,
      body: {},
      headers: new Headers({
        "X-Swe-Chatid": "chat-real-a",
        "X-Swe-Msgid": "turn-a",
        "X-Swe-Sessionid": "logical-a",
      }),
    } as Response);

    render(
      <Harness
        currentQARef={currentQARef}
        updateMessage={vi.fn()}
        onFinish={vi.fn()}
      />,
    );

    const requestPromise = hookApi.request([], undefined, owner);
    await waitFor(() => expect(owner.msgid).toBe("turn-a"));
    await act(async () => {
      await hookApi.cancelActiveRequest();
    });
    mocks.streamGate.resolve();
    await requestPromise;

    expect(mocks.cancel).toHaveBeenCalledWith(
      expect.objectContaining({
        chat_id: "chat-real-a",
        msgid: "turn-a",
        logical_session_id: "logical-a",
      }),
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

  it("places plan review before the response feedback card", async () => {
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
        plan_interaction_card: {
          card_type: "plan_review",
          plan_id: "plan-1",
          title: "Implementation plan",
          summary: "Review before execution",
          steps: [],
          risks: [],
          verification: [],
        },
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
                text: "Plan ready",
                status: "completed",
              },
            ],
          },
        ],
      }),
    };

    const currentQARef = {
      current: {
        response: {
          id: "ui-response-a",
          msgStatus: "generating",
          cards: [],
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

    const requestPromise = hookApi.request([], undefined, createOwner());
    mocks.streamGate.resolve();

    await act(async () => {
      await requestPromise;
    });

    expect(
      currentQARef.current.response?.cards?.map((card) => card.code),
    ).toEqual([
      "AgentScopeRuntimeResponseCard",
      "PlanInteraction",
      "ResponseFeedback",
    ]);
    expect(currentQARef.current.response?.cards?.[0]?.data).toMatchObject({
      planReviewCard: {
        card_type: "plan_review",
        plan_id: "plan-1",
      },
    });
  });

  it("finishes exit_plan short-circuit frames without adding assistant content", async () => {
    mocks.fetch.mockResolvedValue({
      ok: true,
      body: {},
    } as Response);
    mocks.streamChunks.splice(0, mocks.streamChunks.length, {
      data: JSON.stringify({
        object: "response",
        id: "response-1",
        status: "completed",
        type: "exit_plan",
        created_at: 1,
        completed_at: 2,
        output: [],
      }),
    });

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
      ?.data as { output?: unknown[]; status?: string };

    expect(onFinish).toHaveBeenCalledWith(createOwner());
    expect(responseCardData.status).toBe("completed");
    expect(responseCardData.output).toEqual([]);
    expect(updateMessage).not.toHaveBeenCalled();
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
      ?.data as {
      output?: Array<{ content?: Array<{ text?: string }> }>;
      status?: string;
    };

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

  it("emits compaction boundaries without building an assistant response", async () => {
    mocks.fetch.mockResolvedValue({
      ok: true,
      body: {},
    } as Response);
    mocks.streamChunks[0] = {
      data: JSON.stringify({
        object: "conversation_compacted",
        chat_id: "chat-real-a",
        boundary: {
          id: "boundary-1",
          archived_message_count: 3,
        },
      }),
    };
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
    const onBoundary = vi.fn();
    document.addEventListener("conversation_compacted", onBoundary);
    const updateMessage = vi.fn();
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
        onFinish={vi.fn()}
      />,
    );

    const requestPromise = hookApi.request([], undefined, createOwner());
    await waitFor(() => {
      expect(onBoundary).toHaveBeenCalledTimes(1);
    });
    mocks.streamGate.resolve();
    await act(async () => {
      await requestPromise;
    });

    expect(onBoundary.mock.calls[0]?.[0].detail).toEqual({
      chat_id: "chat-real-a",
      boundary: {
        id: "boundary-1",
        archived_message_count: 3,
      },
    });
    expect(updateMessage).not.toHaveBeenCalled();
    document.removeEventListener("conversation_compacted", onBoundary);
  });
});

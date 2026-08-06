import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { forwardRef, useImperativeHandle, useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatContentOnlyProvider } from "@/components/agentscope-chat/ChatContentOnlyContext";
import MessageList from ".";

const mocks = vi.hoisted(() => ({
  messagesContext: {},
  sessionsContext: {},
  messages: [] as Array<{
    id: string;
    history?: boolean;
    msgStatus?: "generating" | "finished";
    role?: "assistant" | "user";
    cards?: Array<{ code: string; data: unknown }>;
  }>,
  setMessages: vi.fn(),
  isSessionLoading: false,
  sessionNotFound: false,
  currentSessionId: "chat-1",
  anchorTop: 100,
  animationFrame: undefined as FrameRequestCallback | undefined,
}));

const apiMocks = vi.hoisted(() => ({
  getChatHistory: vi.fn(),
  getChatIdForSession: vi.fn(),
  getSession: vi.fn(),
  scrollToBottom: vi.fn(),
}));

vi.mock("use-context-selector", () => ({
  useContextSelector: (
    context: unknown,
    selector: (value: unknown) => unknown,
  ) => {
    if (context === mocks.messagesContext) {
      return selector({
        messages: mocks.messages,
        setMessages: mocks.setMessages,
      });
    }
    return selector({
      currentSessionId: mocks.currentSessionId,
      isSessionLoading: mocks.isSessionLoading,
      sessionNotFound: mocks.sessionNotFound,
    });
  },
}));

vi.mock("../../Context/ChatAnywhereMessagesContext", () => ({
  ChatAnywhereMessagesContext: mocks.messagesContext,
}));

vi.mock("../../Context/ChatAnywhereSessionsContext", () => ({
  ChatAnywhereSessionsContext: mocks.sessionsContext,
}));

vi.mock("@/components/agentscope-chat", () => ({
  Bubble: {
    List: forwardRef(
      (
        {
          items = [],
          onReachStart,
          onBottomStateChange,
          pagination,
          topContent,
        }: {
          items?: Array<{ id?: string }>;
          onReachStart?: () => void;
          onBottomStateChange?: (isAtBottom: boolean) => void;
          pagination?: boolean;
          topContent?: React.ReactNode;
        },
        ref,
      ) => {
        const scrollRef = useRef<HTMLDivElement | null>(null);
        useImperativeHandle(ref, () => ({
          getScrollElement: () => scrollRef.current,
          scrollToBottom: apiMocks.scrollToBottom,
        }));
        return (
          <div
            data-testid="bubble-list"
            data-item-ids={items.map((item) => item.id).join(",")}
            data-pagination={String(pagination)}
            onPointerMove={onReachStart}
            ref={(element) => {
              scrollRef.current = element;
              if (!element) return;
              if (element.dataset.scrollGeometryReady === "true") return;
              element.dataset.scrollGeometryReady = "true";
              Object.defineProperties(element, {
                clientHeight: { configurable: true, value: 400 },
                scrollHeight: { configurable: true, value: 1000 },
                scrollTop: {
                  configurable: true,
                  value: 0,
                  writable: true,
                },
                getBoundingClientRect: {
                  configurable: true,
                  value: () => ({
                    bottom: 400,
                    height: 400,
                    left: 0,
                    right: 400,
                    toJSON: () => ({}),
                    top: 0,
                    width: 400,
                    x: 0,
                    y: 0,
                  }),
                },
              });
            }}
          >
            {topContent}
            {items.length}
            {items.map((item, index) => (
              <div
                data-role="assistant"
                id={item.id}
                key={item.id || index}
                ref={(element) => {
                  if (!element) return;
                  Object.defineProperty(element, "getBoundingClientRect", {
                    configurable: true,
                    value: () => ({
                      bottom:
                        (item.id === "online-message"
                          ? mocks.anchorTop
                          : -100) + 48,
                      height: 48,
                      left: 0,
                      right: 400,
                      toJSON: () => ({}),
                      top:
                        item.id === "online-message" ? mocks.anchorTop : -100,
                      width: 400,
                      x: 0,
                      y: item.id === "online-message" ? mocks.anchorTop : -100,
                    }),
                  });
                }}
              />
            ))}
            <button onClick={() => onBottomStateChange?.(false)} type="button">
              标记为浏览历史
            </button>
          </div>
        );
      },
    ),
  },
  useProviderContext: () => ({
    getPrefixCls: (name: string) => `copaw-${name}`,
  }),
}));

vi.mock("@/api/modules/chat", () => ({
  chatApi: {
    getChatHistory: apiMocks.getChatHistory,
  },
}));

vi.mock("@/pages/Chat/sessionApi", () => ({
  convertMessages: (messages: Array<{ id: string }>) => messages,
  convertArchivedPage: (
    messages: Array<{ id: string }>,
    boundaries: Array<{ id: string }> = [],
  ) => [
    ...messages,
    ...boundaries.map((boundary) => ({
      id: `conversation-compaction-${boundary.id}`,
    })),
  ],
  default: {
    getChatIdForSession: apiMocks.getChatIdForSession,
    getSession: apiMocks.getSession,
  },
}));

vi.mock("../../Context/ChatAnywhereOptionsContext", () => ({
  useChatAnywhereOptions: (
    selector: (value: { theme: { bubbleList: object } }) => unknown,
  ) => selector({ theme: { bubbleList: {} } }),
}));

vi.mock("../Welcome", () => ({
  default: () => <div data-testid="welcome">welcome</div>,
}));

vi.mock("antd", () => ({
  Result: ({ title, subTitle }: { title: string; subTitle: string }) => (
    <div data-testid="not-found-result">
      <span>{title}</span>
      <span>{subTitle}</span>
    </div>
  ),
  Spin: () => <div data-testid="spin">loading</div>,
}));

describe("MessageList content-only composition", () => {
  beforeEach(() => {
    mocks.messages = [];
    mocks.setMessages.mockReset();
    mocks.setMessages.mockImplementation((update) => {
      mocks.messages =
        typeof update === "function" ? update(mocks.messages) : update;
    });
    apiMocks.getChatHistory.mockReset();
    apiMocks.getChatIdForSession.mockReset();
    apiMocks.getSession.mockReset();
    apiMocks.scrollToBottom.mockReset();
    apiMocks.getChatIdForSession.mockReturnValue("chat-real-1");
    mocks.isSessionLoading = false;
    mocks.sessionNotFound = false;
    mocks.currentSessionId = "chat-1";
    mocks.anchorTop = 100;
    mocks.animationFrame = undefined;
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      mocks.animationFrame = callback;
      return 1;
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    cleanup();
  });

  it("preserves the normal Welcome surface outside content-only mode", () => {
    render(<MessageList onSubmit={vi.fn()} />);

    expect(screen.getByTestId("welcome")).toBeInTheDocument();
  });

  it("does not mount the input-bearing Welcome surface in content-only mode", () => {
    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    expect(screen.queryByTestId("welcome")).not.toBeInTheDocument();
    expect(screen.queryByTestId("bubble-list")).not.toBeInTheDocument();
    expect(screen.queryByTestId("not-found-result")).not.toBeInTheDocument();
  });

  it("renders an unavailable result for an active content-only 404", () => {
    mocks.sessionNotFound = true;

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    expect(screen.getByTestId("not-found-result")).toHaveTextContent(
      "会话不存在",
    );
    expect(screen.getByTestId("not-found-result")).toHaveTextContent(
      "该会话不存在或已被删除",
    );
    expect(screen.queryByTestId("welcome")).not.toBeInTheDocument();
  });

  it("does not replace normal chat with the content-only 404 result", () => {
    mocks.sessionNotFound = true;

    render(<MessageList onSubmit={vi.fn()} />);

    expect(screen.getByTestId("welcome")).toBeInTheDocument();
    expect(screen.queryByTestId("not-found-result")).not.toBeInTheDocument();
  });

  it("keeps the existing session loading state unchanged", () => {
    mocks.isSessionLoading = true;
    mocks.sessionNotFound = true;

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    expect(screen.getByTestId("spin")).toBeInTheDocument();
    expect(screen.queryByTestId("not-found-result")).not.toBeInTheDocument();
  });

  it("keeps loaded messages on the existing Bubble list", () => {
    mocks.messages = [{ id: "message-1" }];

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    expect(screen.getByTestId("bubble-list")).toHaveTextContent("1");
  });

  it("uses cursor history paging instead of BubbleList's local pagination", () => {
    mocks.messages = [{ id: "online-message", history: true }];

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    expect(screen.getByTestId("bubble-list")).toHaveAttribute(
      "data-pagination",
      "false",
    );
  });

  it("renders the newest 10 historical cards initially while keeping live cards visible", () => {
    mocks.messages = [
      ...Array.from({ length: 25 }, (_, index) => ({
        id: `history-${index + 1}`,
        history: true,
      })),
      { id: "live-message" },
    ];

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    const bubbleList = screen.getByTestId("bubble-list");
    expect(bubbleList).toHaveTextContent("11");
    expect(bubbleList).toHaveAttribute(
      "data-item-ids",
      "live-message,history-25,history-24,history-23,history-22,history-21,history-20,history-19,history-18,history-17,history-16",
    );
  });

  it("uses the same progressive window in normal chat mode", () => {
    mocks.messages = Array.from({ length: 25 }, (_, index) => ({
      id: `history-${index + 1}`,
      history: true,
    }));

    render(<MessageList onSubmit={vi.fn()} />);

    expect(screen.getByTestId("bubble-list")).toHaveTextContent("10");
  });

  it("reveals local history in 10-card batches before requesting the archive", async () => {
    mocks.messages = Array.from({ length: 25 }, (_, index) => ({
      id: `history-${index + 1}`,
      history: true,
    }));

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    const bubbleList = screen.getByTestId("bubble-list");
    expect(bubbleList).toHaveTextContent("10");

    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);
    await waitFor(() => expect(bubbleList).toHaveTextContent("20"));
    expect(apiMocks.getChatHistory).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toBeEmptyDOMElement();

    // Remaining inside the preload zone is latched and must not reveal twice.
    fireEvent.scroll(bubbleList);
    expect(bubbleList).toHaveTextContent("20");

    bubbleList.scrollTop = -300;
    fireEvent.scroll(bubbleList);
    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);
    await waitFor(() => expect(bubbleList).toHaveTextContent("25"));
    expect(apiMocks.getChatHistory).not.toHaveBeenCalled();
  });

  it("restores the visible anchor after revealing a local history batch", async () => {
    mocks.messages = [
      ...Array.from({ length: 15 }, (_, index) => ({
        id: `history-${index + 1}`,
        history: true,
      })),
      { id: "online-message" },
    ];

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    const bubbleList = screen.getByTestId("bubble-list");

    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);
    await waitFor(() => expect(bubbleList).toHaveTextContent("16"));

    expect(mocks.animationFrame).toBeTypeOf("function");
    mocks.anchorTop = 140;
    act(() => {
      mocks.animationFrame?.(0);
    });
    expect(bubbleList.scrollTop).toBe(-340);
  });

  it("requests one archive page with limit 20 only after local history is fully visible", async () => {
    mocks.messages = Array.from({ length: 11 }, (_, index) => ({
      id: `history-${index + 1}`,
      history: true,
    }));
    apiMocks.getChatHistory.mockResolvedValue({
      messages: Array.from({ length: 20 }, (_, index) => ({
        id: `archived-${index + 1}`,
      })),
      boundaries: [],
      has_more: true,
      next_cursor: "cursor-2",
    });

    const rendered = render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    const bubbleList = screen.getByTestId("bubble-list");

    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);
    await waitFor(() => expect(bubbleList).toHaveTextContent("11"));
    expect(apiMocks.getChatHistory).not.toHaveBeenCalled();

    bubbleList.scrollTop = -300;
    fireEvent.scroll(bubbleList);
    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);
    await waitFor(() => {
      expect(apiMocks.getChatHistory).toHaveBeenCalledWith(
        "chat-real-1",
        null,
        20,
      );
    });
    rendered.rerender(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    expect(bubbleList).toHaveTextContent("31");
  });

  it("continues archive paging with the returned cursor", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    apiMocks.getChatHistory
      .mockResolvedValueOnce({
        messages: [{ id: "archived-page-1" }],
        boundaries: [],
        has_more: true,
        next_cursor: "cursor-2",
      })
      .mockResolvedValueOnce({
        messages: [{ id: "archived-page-2" }],
        boundaries: [],
        has_more: false,
        next_cursor: null,
      });

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    const bubbleList = screen.getByTestId("bubble-list");

    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);
    await waitFor(() => {
      expect(apiMocks.getChatHistory).toHaveBeenNthCalledWith(
        1,
        "chat-real-1",
        null,
        20,
      );
    });

    bubbleList.scrollTop = -300;
    fireEvent.scroll(bubbleList);
    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);
    await waitFor(() => {
      expect(apiMocks.getChatHistory).toHaveBeenNthCalledWith(
        2,
        "chat-real-1",
        "cursor-2",
        20,
      );
    });
    expect(screen.getByRole("status")).toHaveTextContent("已到达会话开始处");
  });

  it("does not request archived history until normal scrolling enters the preload range", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    const bubbleList = screen.getByTestId("bubble-list");
    bubbleList.scrollTop = -300;
    fireEvent.scroll(bubbleList);

    expect(apiMocks.getChatHistory).not.toHaveBeenCalled();
  });

  it("loads archived history with the resolved backend chat ID while normal scrolling nears the top", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    let resolveHistory!: (page: {
      messages: Array<{ id: string }>;
      boundaries: never[];
      has_more: boolean;
      next_cursor: string | null;
    }) => void;
    apiMocks.getChatHistory.mockReturnValue(
      new Promise((resolve) => {
        resolveHistory = resolve;
      }),
    );

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    const bubbleList = screen.getByTestId("bubble-list");
    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);

    expect(screen.getByRole("status")).toHaveTextContent("正在加载更早的消息…");
    expect(screen.getByRole("status")).toHaveStyle({ flexShrink: "0" });
    expect(screen.getByTestId("bubble-list")).toContainElement(
      screen.getByRole("status"),
    );

    await waitFor(() => {
      expect(apiMocks.getChatHistory).toHaveBeenCalledWith(
        "chat-real-1",
        null,
        20,
      );
    });
    resolveHistory({
      messages: [{ id: "archived-message" }],
      boundaries: [],
      has_more: false,
      next_cursor: null,
    });
    await waitFor(() => {
      expect(mocks.messages).toEqual([
        { id: "archived-message", history: true },
        { id: "online-message", history: true },
      ]);
    });
    expect(screen.getByRole("status")).toHaveTextContent("已到达会话开始处");
    expect(mocks.messages).toEqual([
      { id: "archived-message", history: true },
      { id: "online-message", history: true },
    ]);
  });

  it("restores the visible anchor after a post-commit history layout shift", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    let resolveHistory!: (page: {
      messages: Array<{ id: string }>;
      boundaries: never[];
      has_more: boolean;
      next_cursor: string | null;
    }) => void;
    apiMocks.getChatHistory.mockReturnValue(
      new Promise((resolve) => {
        resolveHistory = resolve;
      }),
    );
    const rendered = render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    const bubbleList = screen.getByTestId("bubble-list");
    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);
    await waitFor(() => {
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(1);
    });

    resolveHistory({
      messages: [{ id: "archived-message" }],
      boundaries: [],
      has_more: true,
      next_cursor: "next-page",
    });
    await waitFor(() => {
      expect(mocks.messages).toHaveLength(2);
    });
    rendered.rerender(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    expect(mocks.animationFrame).toBeTypeOf("function");
    mocks.anchorTop = 140;
    act(() => {
      mocks.animationFrame?.(0);
    });
    expect(bubbleList.scrollTop).toBe(-340);
  });

  it("shows the start-of-conversation state immediately for a terminal page", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    apiMocks.getChatHistory.mockResolvedValue({
      messages: [],
      boundaries: [],
      has_more: false,
      next_cursor: null,
    });

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    const bubbleList = screen.getByTestId("bubble-list");
    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("已到达会话开始处");
    });
    expect(mocks.messages).toEqual([{ id: "online-message", history: true }]);
  });

  it("keeps loaded messages and exposes a retry action when history loading fails", async () => {
    mocks.messages = [{ id: "online-message", history: true }];
    apiMocks.getChatHistory.mockRejectedValueOnce(new Error("offline"));
    apiMocks.getChatHistory.mockResolvedValueOnce({
      messages: [{ id: "archived-message" }],
      boundaries: [],
      has_more: false,
      next_cursor: null,
    });

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    const bubbleList = screen.getByTestId("bubble-list");
    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("历史消息加载失败");
    });
    expect(mocks.messages).toEqual([{ id: "online-message", history: true }]);

    fireEvent.click(screen.getByRole("button", { name: "重试加载历史消息" }));
    await waitFor(() => {
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(2);
    });
    expect(apiMocks.getChatHistory).toHaveBeenNthCalledWith(
      1,
      "chat-real-1",
      null,
      20,
    );
    expect(apiMocks.getChatHistory).toHaveBeenNthCalledWith(
      2,
      "chat-real-1",
      null,
      20,
    );
    expect(mocks.messages).toEqual([
      { id: "archived-message", history: true },
      { id: "online-message", history: true },
    ]);
  });

  it("does not pull the reader back to the latest message after they enter history", () => {
    mocks.messages = [{ id: "online-message" }];
    const rendered = render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "标记为浏览历史" }));
    mocks.messages = [{ id: "online-message" }, { id: "new-message" }];
    rendered.rerender(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    expect(apiMocks.scrollToBottom).not.toHaveBeenCalled();
  });

  it("keeps an existing compaction divider when its archive page is loaded", async () => {
    mocks.messages = [
      { id: "conversation-compaction-boundary-1", history: true },
      { id: "online-message", history: true },
    ];
    apiMocks.getChatHistory.mockResolvedValue({
      messages: [{ id: "archived-message" }],
      boundaries: [{ id: "boundary-1" }],
      has_more: false,
      next_cursor: null,
    });

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    const bubbleList = screen.getByTestId("bubble-list");
    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);

    await waitFor(() => {
      expect(mocks.messages).toEqual([
        { id: "archived-message", history: true },
        { id: "conversation-compaction-boundary-1", history: true },
        { id: "online-message", history: true },
      ]);
    });
  });

  it("keeps archive paging available when compaction invalidates an in-flight page", async () => {
    mocks.messages = [{ id: "online-message" }];
    let resolveHistory!: (value: {
      messages: Array<{ id: string }>;
      boundaries: never[];
      has_more: boolean;
      next_cursor: string | null;
    }) => void;
    apiMocks.getChatHistory
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveHistory = resolve;
        }),
      )
      .mockResolvedValueOnce({
        messages: [],
        boundaries: [],
        has_more: false,
        next_cursor: null,
      });
    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    const bubbleList = screen.getByTestId("bubble-list");
    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);
    await waitFor(() => {
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(1);
    });

    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: { chat_id: "chat-real-1" },
      }),
    );
    expect(apiMocks.getSession).not.toHaveBeenCalled();

    resolveHistory({
      messages: [{ id: "stale-archived-message" }],
      boundaries: [],
      has_more: true,
      next_cursor: "stale-cursor",
    });
    await act(async () => {
      await Promise.resolve();
    });

    bubbleList.scrollTop = -300;
    fireEvent.scroll(bubbleList);
    bubbleList.scrollTop = -380;
    fireEvent.scroll(bubbleList);
    await waitFor(() => {
      expect(apiMocks.getChatHistory).toHaveBeenCalledTimes(2);
    });
  });

  it("does not replace a finished local turn when the compaction snapshot omits its tail", async () => {
    vi.useFakeTimers();
    mocks.messages = [
      {
        id: "local-request",
        role: "user",
        cards: [{ code: "Request", data: { input: "current question" } }],
      },
      {
        id: "local-response",
        role: "assistant",
        msgStatus: "finished",
        cards: [{ code: "Response", data: { output: "current answer" } }],
      },
    ];
    apiMocks.getSession.mockResolvedValue({
      generating: false,
      messages: [
        {
          id: "stale-persisted-message",
          role: "assistant",
          cards: [{ code: "Response", data: { output: "older answer" } }],
        },
      ],
    });

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: { chat_id: "chat-real-1" },
      }),
    );

    expect(apiMocks.getSession).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(apiMocks.getSession).toHaveBeenCalledWith("chat-1");
    expect(mocks.messages).toEqual([
      {
        id: "local-request",
        role: "user",
        cards: [{ code: "Request", data: { input: "current question" } }],
      },
      {
        id: "local-response",
        role: "assistant",
        msgStatus: "finished",
        cards: [{ code: "Response", data: { output: "current answer" } }],
      },
    ]);
    vi.useRealTimers();
  });

  it("requires the ordered local request-response suffix before accepting a compaction snapshot", async () => {
    vi.useFakeTimers();
    mocks.messages = [
      {
        id: "current-request",
        role: "user",
        cards: [{ code: "Request", data: { input: "current question" } }],
      },
      {
        id: "current-response",
        role: "assistant",
        msgStatus: "finished",
        cards: [{ code: "Response", data: { output: "same answer" } }],
      },
    ];
    apiMocks.getSession
      .mockResolvedValueOnce({
        generating: false,
        messages: [
          {
            id: "old-request",
            role: "user",
            cards: [{ code: "Request", data: { input: "older question" } }],
          },
          {
            id: "old-response",
            role: "assistant",
            cards: [{ code: "Response", data: { output: "same answer" } }],
          },
        ],
      })
      .mockResolvedValueOnce({
        generating: false,
        messages: [
          {
            id: "persisted-request",
            role: "user",
            cards: [{ code: "Request", data: { input: "current question" } }],
          },
          {
            id: "persisted-response",
            role: "assistant",
            cards: [{ code: "Response", data: { output: "same answer" } }],
          },
        ],
      });

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: { chat_id: "chat-real-1" },
      }),
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(mocks.messages.map((message) => message.id)).toEqual([
      "current-request",
      "current-response",
    ]);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(mocks.messages.map((message) => message.id)).toEqual([
      "persisted-request",
      "persisted-response",
    ]);
  });

  it("requires the compaction boundary in the snapshot when the online tail is unchanged", async () => {
    vi.useFakeTimers();
    mocks.messages = [
      {
        id: "current-request",
        role: "user",
        cards: [{ code: "Request", data: { input: "current question" } }],
      },
      {
        id: "current-response",
        role: "assistant",
        msgStatus: "finished",
        cards: [{ code: "Response", data: { output: "same answer" } }],
      },
    ];
    apiMocks.getSession
      .mockResolvedValueOnce({
        generating: false,
        messages: [
          {
            id: "stale-request",
            role: "user",
            cards: [{ code: "Request", data: { input: "current question" } }],
          },
          {
            id: "stale-response",
            role: "assistant",
            cards: [{ code: "Response", data: { output: "same answer" } }],
          },
        ],
      })
      .mockResolvedValueOnce({
        generating: false,
        messages: [
          {
            id: "persisted-request",
            role: "user",
            cards: [{ code: "Request", data: { input: "current question" } }],
          },
          {
            id: "persisted-response",
            role: "assistant",
            cards: [{ code: "Response", data: { output: "same answer" } }],
          },
          {
            id: "conversation-compaction-boundary-1",
            role: "assistant",
            cards: [
              {
                code: "ConversationCompactionBoundary",
                data: { id: "boundary-1" },
              },
            ],
          },
        ],
      });

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: {
          chat_id: "chat-real-1",
          boundary: { id: "boundary-1" },
        },
      }),
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(mocks.messages.map((message) => message.id)).toEqual([
      "current-request",
      "current-response",
    ]);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(mocks.messages.map((message) => message.id)).toEqual([
      "persisted-request",
      "persisted-response",
      "conversation-compaction-boundary-1",
    ]);
  });

  it("keeps retrying compaction refresh until a delayed persisted snapshot confirms the local tail", async () => {
    vi.useFakeTimers();
    mocks.messages = [
      {
        id: "current-request",
        role: "user",
        cards: [{ code: "Request", data: { input: "current question" } }],
      },
    ];
    apiMocks.getSession
      .mockResolvedValueOnce({ generating: false, messages: [] })
      .mockResolvedValueOnce({ generating: false, messages: [] })
      .mockResolvedValueOnce({ generating: false, messages: [] })
      .mockResolvedValueOnce({
        generating: false,
        messages: [
          {
            id: "persisted-request",
            role: "user",
            cards: [{ code: "Request", data: { input: "current question" } }],
          },
        ],
      });

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: { chat_id: "chat-real-1" },
      }),
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    expect(apiMocks.getSession).toHaveBeenCalledTimes(4);
    expect(mocks.messages.map((message) => message.id)).toEqual([
      "persisted-request",
    ]);
  });

  it("refreshes a pending stream compaction only after a non-generating snapshot confirms the local tail", async () => {
    vi.useFakeTimers();
    mocks.messages = [
      {
        id: "local-response",
        role: "assistant",
        msgStatus: "generating",
        cards: [{ code: "Response", data: { output: "current answer" } }],
      },
    ];
    apiMocks.getSession
      .mockResolvedValueOnce({ generating: true, messages: [] })
      .mockResolvedValueOnce({
        generating: false,
        messages: [
          {
            id: "persisted-response",
            role: "assistant",
            cards: [{ code: "Response", data: { output: "current answer" } }],
          },
        ],
      });

    const rendered = render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: { chat_id: "chat-real-1" },
      }),
    );
    expect(apiMocks.getSession).not.toHaveBeenCalled();

    mocks.messages = [
      {
        id: "local-response",
        role: "assistant",
        msgStatus: "finished",
        cards: [{ code: "Response", data: { output: "current answer" } }],
      },
    ];
    rendered.rerender(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
      await Promise.resolve();
    });
    expect(apiMocks.getSession).toHaveBeenCalledTimes(1);
    expect(mocks.messages).toEqual([
      {
        id: "local-response",
        role: "assistant",
        msgStatus: "finished",
        cards: [{ code: "Response", data: { output: "current answer" } }],
      },
    ]);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
      await Promise.resolve();
    });
    expect(apiMocks.getSession).toHaveBeenCalledTimes(2);
    expect(mocks.messages).toEqual([
      {
        id: "persisted-response",
        role: "assistant",
        history: true,
        cards: [{ code: "Response", data: { output: "current answer" } }],
      },
    ]);
    vi.useRealTimers();
  });

  it("does not replace a new local request when a compaction snapshot is in flight", async () => {
    vi.useFakeTimers();
    mocks.messages = [
      {
        id: "existing-message",
        role: "user",
        cards: [{ code: "Request", data: { input: "existing" } }],
      },
    ];
    let resolveSession!: (value: {
      generating: boolean;
      messages: unknown[];
    }) => void;
    apiMocks.getSession.mockReturnValue(
      new Promise((resolve) => {
        resolveSession = resolve;
      }),
    );

    const rendered = render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: { chat_id: "chat-real-1" },
      }),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(apiMocks.getSession).toHaveBeenCalledWith("chat-1");

    mocks.messages = [
      {
        id: "existing-message",
        role: "user",
        cards: [{ code: "Request", data: { input: "existing" } }],
      },
      {
        id: "new-local-response",
        role: "assistant",
        msgStatus: "generating",
        cards: [{ code: "Response", data: { output: "new" } }],
      },
    ];
    rendered.rerender(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    resolveSession({ generating: false, messages: [] });

    await act(async () => {
      await Promise.resolve();
    });
    expect(mocks.messages).toHaveLength(2);
    expect(mocks.messages[1]?.id).toBe("new-local-response");
    vi.useRealTimers();
  });

  it("keeps the current session visible when a compaction boundary arrives", () => {
    mocks.messages = [{ id: "old-message" }];

    render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );

    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: { chat_id: "chat-real-1" },
      }),
    );

    expect(apiMocks.getSession).not.toHaveBeenCalled();
    expect(mocks.messages).toEqual([{ id: "old-message" }]);
  });

  it("does not let an earlier compaction refresh overwrite a switched session", async () => {
    vi.useFakeTimers();
    mocks.messages = [
      {
        id: "message-for-chat-1",
        role: "user",
        cards: [{ code: "Request", data: { input: "chat-1" } }],
      },
    ];
    let resolveSession!: (value: {
      generating: boolean;
      messages: unknown[];
    }) => void;
    apiMocks.getSession.mockReturnValue(
      new Promise((resolve) => {
        resolveSession = resolve;
      }),
    );
    apiMocks.getChatIdForSession.mockImplementation((sessionId: string) =>
      sessionId === "chat-1" ? "chat-real-1" : "chat-real-2",
    );

    const rendered = render(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    document.dispatchEvent(
      new CustomEvent("conversation_compacted", {
        detail: { chat_id: "chat-real-1" },
      }),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(apiMocks.getSession).toHaveBeenCalledWith("chat-1");

    mocks.currentSessionId = "chat-2";
    mocks.messages = [{ id: "message-for-chat-2" }];
    rendered.rerender(
      <ChatContentOnlyProvider enabled>
        <MessageList onSubmit={vi.fn()} />
      </ChatContentOnlyProvider>,
    );
    resolveSession({ generating: false, messages: [] });

    await act(async () => {
      await Promise.resolve();
    });
    expect(mocks.messages).toEqual([{ id: "message-for-chat-2" }]);
    vi.useRealTimers();
  });
});

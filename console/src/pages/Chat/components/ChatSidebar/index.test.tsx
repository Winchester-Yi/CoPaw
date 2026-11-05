import React from "react";
import { fireEvent, render, waitFor, within } from "@testing-library/react";
import { Modal } from "antd";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ChatSidebar from ".";
import sessionApi from "../../sessionApi";

interface TestSession {
  id: string;
  name: string;
  messages: unknown[];
  createdAt?: string;
  generating?: boolean;
}

const mocks = vi.hoisted(() => ({
  navigate: vi.fn(),
  setSessions: vi.fn(),
  setSessionLoading: vi.fn(),
  setSelectedAgent: vi.fn(),
  loadMoreSessions: vi.fn(),
  hasMoreSessions: vi.fn(),
  getSessionTotal: vi.fn(),
  getSessions: vi.fn(),
  openExternalLink: vi.fn(),
  context: {
    sessions: [
      {
        id: "chat-1",
        name: "first chat",
        messages: [],
        createdAt: "2026-06-10T00:00:00Z",
      },
    ] as TestSession[],
    setSessions: vi.fn(),
    setSessionLoading: vi.fn(),
    getSessions: vi.fn(),
    isSessionsListLoading: false,
  },
  iframeState: {
    bbk: null as string | null,
    source: null as string | null,
    isOriginY: false,
  },
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => mocks.navigate,
  useLocation: () => ({ pathname: "/chat" }),
}));

vi.mock("antd", () => ({
  Tooltip: ({ children }: { children: React.ReactNode }) => children,
  Image: Object.assign(
    ({ src }: { src?: string }) =>
      src ? (
        <img alt="guide-preview" data-testid="guide-image" src={src} />
      ) : null,
    {
      PreviewGroup: ({
        children,
        preview,
      }: {
        children: React.ReactNode;
        preview?: { visible?: boolean };
      }) => (
        <div
          data-testid="guide-preview"
          data-visible={String(Boolean(preview?.visible))}
        >
          {children}
        </div>
      ),
    },
  ),
  Modal: { confirm: vi.fn() },
}));

vi.mock("../ChatTaskList", () => ({ default: () => null }));
vi.mock("./CollapsedToolbar", () => ({ default: () => null }));
vi.mock("./ExpandablePanel", () => ({ default: () => null }));
vi.mock("./HistorySkeleton", () => ({ HistorySkeleton: () => null }));
vi.mock("./HistorySessionRow", () => ({
  HistorySessionRow: ({
    name,
    session,
    onSessionDelete,
  }: {
    name: string;
    session: { id: string; realId?: string };
    onSessionDelete: (
      sessionId: string,
      backendId: string | null,
      sessionName: string,
    ) => void;
  }) => (
    <button
      type="button"
      onClick={() => onSessionDelete(session.id, session.realId || null, name)}
    >
      {name}
    </button>
  ),
}));

vi.mock("use-context-selector", () => ({
  useContextSelector: (
    _context: unknown,
    selector: (value: never) => unknown,
  ) => selector(mocks.context as never),
}));

vi.mock("@/components/agentscope-chat", () => ({
  ChatAnywhereSessionsContext: {},
  useChatAnywhereSessionsState: () => ({
    currentSessionId: null,
    setSessionLoading: mocks.setSessionLoading,
  }),
}));

vi.mock(
  "@/components/agentscope-chat/AgentScopeRuntimeWebUI/core/Context/useChatAnywhereEventEmitter",
  () => ({ default: vi.fn() }),
);

vi.mock("@/stores/agentStore", () => ({
  useAgentStore: () => ({
    selectedAgent: "default",
    setSelectedAgent: mocks.setSelectedAgent,
  }),
}));

vi.mock("@/stores/iframeStore", () => ({
  useIframeStore: (
    selector: (state: {
      bbk: string | null;
      source: string | null;
      isOriginY: boolean;
    }) => unknown,
  ) => selector(mocks.iframeState),
}));

vi.mock("@/assets/others/note.png", () => ({ default: "guide-image.png" }));
vi.mock("@/assets/others/sh_note.png", () => ({
  default: "sh-guide-image.png",
}));

vi.mock("../../sessionApi", () => ({
  default: {
    getSessionList: vi.fn(),
    loadMoreSessions: mocks.loadMoreSessions,
    hasMoreSessions: mocks.hasMoreSessions,
    getSessionTotal: mocks.getSessionTotal,
    removeSession: vi.fn(),
  },
}));

describe("ChatSidebar infinite history scrolling", () => {
  beforeEach(() => {
    mocks.navigate.mockReset();
    mocks.setSessions.mockReset();
    mocks.setSessionLoading.mockReset();
    mocks.setSelectedAgent.mockReset();
    mocks.loadMoreSessions.mockReset();
    mocks.hasMoreSessions.mockReset();
    mocks.getSessionTotal.mockReset();
    mocks.getSessions.mockReset();
    mocks.openExternalLink.mockReset();
    vi.stubGlobal("open", mocks.openExternalLink);
    vi.mocked(Modal.confirm).mockReset();
    vi.mocked(sessionApi.getSessionList).mockReset();
    vi.mocked(sessionApi.removeSession).mockReset();
    mocks.context.setSessions = mocks.setSessions;
    mocks.context.setSessionLoading = mocks.setSessionLoading;
    mocks.context.getSessions = mocks.getSessions;
    mocks.iframeState.bbk = null;
    mocks.iframeState.source = null;
    mocks.iframeState.isOriginY = false;
    mocks.hasMoreSessions.mockReturnValue(true);
    mocks.getSessionTotal.mockReturnValue(120);
    mocks.getSessions.mockImplementation(() => mocks.context.sessions);
    mocks.loadMoreSessions.mockResolvedValue([
      ...mocks.context.sessions,
      {
        id: "chat-2",
        name: "second chat",
        messages: [],
        createdAt: "2026-06-09T00:00:00Z",
      },
    ]);
  });

  it("loads the next page when the expanded sidebar reaches the bottom", async () => {
    const { container } = render(<ChatSidebar tasks={[]} />);
    const scrollContainer = container.querySelector(
      ".chat-sidebar-content-record-list",
    ) as HTMLDivElement;
    Object.defineProperties(scrollContainer, {
      clientHeight: { configurable: true, value: 300 },
      scrollHeight: { configurable: true, value: 800 },
      scrollTop: { configurable: true, value: 440, writable: true },
    });

    fireEvent.scroll(scrollContainer);

    await waitFor(() => {
      expect(mocks.loadMoreSessions).toHaveBeenCalledTimes(1);
    });
    expect(mocks.setSessions).toHaveBeenCalledWith(
      expect.arrayContaining([expect.objectContaining({ id: "chat-2" })]),
    );
  });

  it("shows the loaded history count without claiming an exact total", () => {
    const { getAllByText } = render(<ChatSidebar tasks={[]} />);

    expect(getAllByText("历史记录(已加载1)").length).toBeGreaterThan(0);
    expect(mocks.getSessionTotal).not.toHaveBeenCalled();
  });

  it("restores the sidebar from the collapsed toolbar", () => {
    const { container } = render(<ChatSidebar tasks={[]} />);
    const view = within(container);
    const collapseButton = view.getByRole("button", { name: "收起导航" });

    expect(collapseButton).toHaveClass(
      "chat-sidebar-collapse-toggle--expanded",
    );
    expect(collapseButton.closest(".chat-sidebar-new-topic")).not.toBeNull();

    fireEvent.click(collapseButton);

    expect(view.getByRole("button", { name: "展开导航" })).toBeInTheDocument();
    expect(container.querySelector(".chat-sidebar")).toBeNull();
  });

  it("does not subtract unrelated task rows from loaded history", () => {
    const tasks = [
      { id: "task-1", task: { unread_execution_count: 0 } },
      { id: "task-2", task: { unread_execution_count: 0 } },
    ];

    const { getAllByText } = render(<ChatSidebar tasks={tasks as never} />);

    expect(getAllByText("历史记录(已加载1)").length).toBeGreaterThan(0);
  });

  it("excludes the deleted session when refreshing after delete", async () => {
    const nextSessions = [
      {
        id: "chat-2",
        name: "second chat",
        messages: [],
        createdAt: "2026-06-09T00:00:00Z",
      },
    ];
    vi.mocked(sessionApi.removeSession).mockResolvedValue(nextSessions);
    vi.mocked(sessionApi.getSessionList).mockResolvedValue(nextSessions);
    mocks.getSessions.mockReturnValue([
      ...mocks.context.sessions,
      ...nextSessions,
    ]);
    mocks.hasMoreSessions.mockReturnValue(false);

    const { getAllByText } = render(<ChatSidebar tasks={[]} />);
    mocks.setSessions.mockClear();

    fireEvent.click(getAllByText("first chat")[0]);
    await vi.mocked(Modal.confirm).mock.calls[0]?.[0]?.onOk?.();

    await waitFor(() => {
      expect(mocks.setSessions).toHaveBeenCalledWith([
        expect.objectContaining({ id: "chat-2" }),
      ]);
    });
    expect(mocks.setSessions).not.toHaveBeenCalledWith(
      expect.arrayContaining([expect.objectContaining({ id: "chat-1" })]),
    );
  });

  it("preserves a concurrently created session and generating state when a page resolves", async () => {
    let resolvePage: (sessions: TestSession[]) => void = () => undefined;
    mocks.loadMoreSessions.mockReturnValueOnce(
      new Promise((resolve) => {
        resolvePage = resolve;
      }),
    );
    const { container } = render(<ChatSidebar tasks={[]} />);
    const scrollContainer = container.querySelector(
      ".chat-sidebar-content-record-list",
    ) as HTMLDivElement;
    Object.defineProperties(scrollContainer, {
      clientHeight: { configurable: true, value: 300 },
      scrollHeight: { configurable: true, value: 800 },
      scrollTop: { configurable: true, value: 440, writable: true },
    });

    fireEvent.scroll(scrollContainer);
    mocks.getSessions.mockReturnValue([
      {
        id: "local-new",
        name: "new chat",
        messages: [],
        generating: true,
        createdAt: "2026-06-11T00:00:00Z",
      },
      {
        ...mocks.context.sessions[0],
        generating: true,
      },
    ]);
    resolvePage([
      { ...mocks.context.sessions[0], generating: false },
      {
        id: "chat-2",
        name: "second chat",
        messages: [],
        createdAt: "2026-06-09T00:00:00Z",
      },
    ]);

    await waitFor(() => {
      expect(mocks.setSessions).toHaveBeenCalledWith(
        expect.arrayContaining([
          expect.objectContaining({ id: "local-new", generating: true }),
          expect.objectContaining({ id: "chat-1", generating: true }),
          expect.objectContaining({ id: "chat-2" }),
        ]),
      );
    });
  });

  it("previews the guide image when bbk is 121 and source is RMASSIST", () => {
    mocks.iframeState.bbk = "121";
    mocks.iframeState.source = "RMASSIST";
    mocks.iframeState.isOriginY = true;

    const { container } = render(<ChatSidebar tasks={[]} />);
    const guide = container.querySelector(
      '[data-testid="guide-image"]',
    ) as HTMLImageElement;

    expect(guide).toHaveAttribute("src", "sh-guide-image.png");
    expect(
      container.querySelector('[data-testid="guide-preview"]'),
    ).toHaveAttribute("data-visible", "false");

    fireEvent.click(
      container.querySelector(".chat-sidebar-footer-item") as HTMLElement,
    );

    expect(
      container.querySelector('[data-testid="guide-preview"]'),
    ).toHaveAttribute("data-visible", "true");
    expect(mocks.openExternalLink).not.toHaveBeenCalled();
  });

  it("opens the placeholder link when the operation guide context does not match", () => {
    mocks.iframeState.bbk = "121";
    mocks.iframeState.source = "OTHER";
    mocks.iframeState.isOriginY = true;

    const { container } = render(<ChatSidebar tasks={[]} />);

    expect(container.querySelector(".chat-sidebar-footer")).toHaveTextContent(
      "操作指南",
    );
    expect(container.querySelector('[data-testid="guide-image"]')).toBeNull();
    fireEvent.click(
      container.querySelector(".chat-sidebar-footer-item") as HTMLElement,
    );
    expect(mocks.openExternalLink).toHaveBeenCalledWith(
      "https://example.com",
      "_blank",
      "noopener,noreferrer",
    );
  });

  it("hides the operation guide when origin is not Y", () => {
    mocks.iframeState.bbk = "100";
    mocks.iframeState.source = "OTHER";

    const { container } = render(<ChatSidebar tasks={[]} />);

    expect(container.querySelector(".chat-sidebar-footer")).toBeNull();
    expect(container.querySelector('[data-testid="guide-image"]')).toBeNull();
    expect(mocks.openExternalLink).not.toHaveBeenCalled();
  });

  it("accounts for the hidden header when source is ruice", () => {
    mocks.iframeState.bbk = "100";
    mocks.iframeState.source = "ruice";
    mocks.iframeState.isOriginY = true;

    const { container } = render(<ChatSidebar tasks={[]} />);
    const recordList = container.querySelector(
      ".chat-sidebar-content-record-list",
    );

    expect(recordList).toHaveClass(
      "chat-sidebar-content-record-list--without-header",
    );
    expect(container.querySelector(".chat-sidebar-footer")).toHaveTextContent(
      "操作指南",
    );
  });
});

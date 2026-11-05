import { act, render, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import UserDetailModal from "./index";

const sessionCardListMock = vi.hoisted(() => vi.fn());
const userStatsHeaderMock = vi.hoisted(() => vi.fn());
const readOnlyChatMock = vi.hoisted(() => vi.fn());

const tracingApiMock = vi.hoisted(() => ({
  getUserStats: vi.fn(),
  getSessions: vi.fn(),
  getUserChats: vi.fn(),
  getUserChatSummaries: vi.fn(),
  getSessionStats: vi.fn(),
}));

vi.mock("../../../../../api/modules/tracing", () => ({
  tracingApi: tracingApiMock,
}));

vi.mock("./UserStatsHeader", () => ({
  default: (props: unknown) => {
    userStatsHeaderMock(props);
    return <div data-testid="user-stats-header" />;
  },
}));

vi.mock("./SessionCardList", () => ({
  default: (props: unknown) => {
    sessionCardListMock(props);
    return <div data-testid="session-card-list" />;
  },
}));

vi.mock("./ReadOnlySessionChat", () => ({
  default: (props: unknown) => {
    readOnlyChatMock(props);
    return <div data-testid="readonly-session-chat" />;
  },
}));

describe("UserDetailModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionCardListMock.mockClear();
    userStatsHeaderMock.mockClear();
    tracingApiMock.getUserStats.mockResolvedValue({});
    tracingApiMock.getSessions.mockResolvedValue({
      items: [
        {
          session_id: "session-older",
          session_name: "历史会话",
          total_traces: 1,
          total_tokens: 12,
          total_skills: 0,
          channel: "web",
          last_active: "2026-05-01T10:00:00Z",
        },
      ],
      total: 1,
    });
    tracingApiMock.getUserChatSummaries.mockReset();
    tracingApiMock.getUserChatSummaries.mockResolvedValue({
      items: [
        {
          id: "chat-001",
          session_id: "session-older",
          user_id: "user-001",
          channel: "web",
        },
      ],
      has_more: false,
      next_cursor: null,
    });
    tracingApiMock.getSessionStats.mockResolvedValue({});
  });

  it("loads sessions and chat mappings for the selected user", async () => {
    render(
      <UserDetailModal
        open
        userId="user-001"
        startDate="2026-05-20"
        endDate="2026-05-20"
        bbkIds="100"
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(tracingApiMock.getSessions).toHaveBeenCalled();
    });

    expect(tracingApiMock.getUserStats).toHaveBeenCalledWith(
      "user-001",
      "2026-05-20",
      "2026-05-20",
      "100",
    );
    expect(tracingApiMock.getSessions).toHaveBeenCalledWith(1, 10, {
      user_id: "user-001",
      bbk_ids: "100",
      start_date: "2026-05-20",
      end_date: "2026-05-20",
      has_error: undefined,
      resource_type: undefined,
      resource_name: undefined,
      mcp_server: undefined,
    });
    await waitFor(() =>
      expect(tracingApiMock.getUserChatSummaries).toHaveBeenCalledWith(
        "user-001",
        { session_id: "session-older", page_size: 100, cursor: null },
      ),
    );
    expect(tracingApiMock.getUserChats).not.toHaveBeenCalled();

    await waitFor(() => {
      expect(tracingApiMock.getSessionStats).toHaveBeenCalled();
    });

    expect(tracingApiMock.getSessionStats).toHaveBeenCalledWith(
      "session-older",
      undefined,
      undefined,
      "100",
    );
  });

  it("uses the changed page size when loading another sessions page", async () => {
    render(
      <UserDetailModal
        open
        userId="user-001"
        startDate="2026-05-20"
        endDate="2026-05-20"
        bbkIds="100"
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(sessionCardListMock).toHaveBeenCalled();
    });

    const latestProps =
      sessionCardListMock.mock.calls[
        sessionCardListMock.mock.calls.length - 1
      ][0] as {
        onPageChange: (page: number, pageSize: number) => void;
      };

    await act(async () => {
      latestProps.onPageChange(2, 20);
    });

    await waitFor(() => {
      expect(tracingApiMock.getSessions).toHaveBeenCalledWith(2, 20, {
        user_id: "user-001",
        bbk_ids: "100",
        start_date: "2026-05-20",
        end_date: "2026-05-20",
        has_error: undefined,
        resource_type: undefined,
        resource_name: undefined,
        mcp_server: undefined,
      });
    });
  });

  it("replaces and clears the active resource filter while keeping error filtering", async () => {
    render(
      <UserDetailModal
        open
        userId="user-001"
        startDate="2026-05-20"
        endDate="2026-05-20"
        bbkIds="100"
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => expect(userStatsHeaderMock).toHaveBeenCalled());
    let headerProps = userStatsHeaderMock.mock.calls[
      userStatsHeaderMock.mock.calls.length - 1
    ][0] as {
      onResourceFilterChange: (filter: {
        type: "skill" | "model";
        name: string;
      }) => void;
    };

    await act(async () => {
      headerProps.onResourceFilterChange({ type: "skill", name: "risk-check" });
    });

    await waitFor(() => {
      expect(tracingApiMock.getSessions).toHaveBeenLastCalledWith(1, 10, {
        user_id: "user-001",
        bbk_ids: "100",
        start_date: "2026-05-20",
        end_date: "2026-05-20",
        has_error: undefined,
        resource_type: "skill",
        resource_name: "risk-check",
        mcp_server: undefined,
      });
    });

    const listProps = sessionCardListMock.mock.calls[
      sessionCardListMock.mock.calls.length - 1
    ][0] as {
      onToggleErrorFilter: () => void;
    };
    await act(async () => listProps.onToggleErrorFilter());

    await waitFor(() => {
      expect(tracingApiMock.getSessions).toHaveBeenLastCalledWith(1, 10, {
        user_id: "user-001",
        bbk_ids: "100",
        start_date: "2026-05-20",
        end_date: "2026-05-20",
        has_error: true,
        resource_type: "skill",
        resource_name: "risk-check",
        mcp_server: undefined,
      });
    });

    headerProps = userStatsHeaderMock.mock.calls[
      userStatsHeaderMock.mock.calls.length - 1
    ][0] as typeof headerProps;
    await act(async () => {
      headerProps.onResourceFilterChange({ type: "model", name: "gpt-5" });
    });
    await waitFor(() => {
      expect(tracingApiMock.getSessions).toHaveBeenLastCalledWith(1, 10, {
        user_id: "user-001",
        bbk_ids: "100",
        start_date: "2026-05-20",
        end_date: "2026-05-20",
        has_error: true,
        resource_type: "model",
        resource_name: "gpt-5",
        mcp_server: undefined,
      });
    });

    headerProps = userStatsHeaderMock.mock.calls[
      userStatsHeaderMock.mock.calls.length - 1
    ][0] as typeof headerProps;
    await act(async () => {
      headerProps.onResourceFilterChange({ type: "model", name: "gpt-5" });
    });
    await waitFor(() => {
      expect(tracingApiMock.getSessions).toHaveBeenLastCalledWith(1, 10, {
        user_id: "user-001",
        bbk_ids: "100",
        start_date: "2026-05-20",
        end_date: "2026-05-20",
        has_error: true,
        resource_type: undefined,
        resource_name: undefined,
        mcp_server: undefined,
      });
    });
  });

  it("falls back to an empty chat mapping when user chats response is not an array", async () => {
    tracingApiMock.getUserChatSummaries.mockResolvedValue({});

    render(
      <UserDetailModal
        open
        userId="user-001"
        startDate="2026-05-20"
        endDate="2026-05-20"
        bbkIds="100"
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(tracingApiMock.getUserChatSummaries).toHaveBeenCalledWith(
        "user-001",
        { session_id: "session-older", page_size: 100, cursor: null },
      );
    });

    await waitFor(() => {
      expect(sessionCardListMock).toHaveBeenCalled();
    });
  });

  it("preserves last mapping in recency order across summary pages", async () => {
    tracingApiMock.getUserChatSummaries
      .mockResolvedValueOnce({
        items: [{ id: "new", session_id: "session-older" }],
        has_more: true,
        next_cursor: "next",
      })
      .mockResolvedValueOnce({
        items: [{ id: "old", session_id: "session-older" }],
        has_more: false,
        next_cursor: null,
      });
    render(<UserDetailModal open userId="user-001" onClose={vi.fn()} />);
    await waitFor(() =>
      expect(tracingApiMock.getUserChatSummaries).toHaveBeenLastCalledWith(
        "user-001",
        { session_id: "session-older", page_size: 100, cursor: "next" },
      ),
    );
    await waitFor(() =>
      expect(readOnlyChatMock.mock.lastCall?.[0].chatIdBySessionId).toEqual({
        "session-older": "old",
      }),
    );
  });

  it("ignores a late mapping from the previous selected session", async () => {
    let finish: (value: unknown) => void = () => undefined;
    tracingApiMock.getUserChatSummaries.mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    render(<UserDetailModal open userId="user-001" onClose={vi.fn()} />);
    await waitFor(() =>
      expect(tracingApiMock.getUserChatSummaries).toHaveBeenCalled(),
    );
    tracingApiMock.getUserChatSummaries.mockResolvedValue({
      items: [{ id: "second", session_id: "session-second" }],
      has_more: false,
      next_cursor: null,
    });
    await act(async () => {
      sessionCardListMock.mock.lastCall?.[0].onSelect("session-second");
    });
    await waitFor(() =>
      expect(readOnlyChatMock.mock.lastCall?.[0].chatIdBySessionId).toEqual({
        "session-second": "second",
      }),
    );
    await act(async () => {
      finish({
        items: [{ id: "stale", session_id: "session-older" }],
        has_more: false,
      });
    });
    expect(readOnlyChatMock.mock.lastCall?.[0].chatIdBySessionId).toEqual({
      "session-second": "second",
    });
  });
});

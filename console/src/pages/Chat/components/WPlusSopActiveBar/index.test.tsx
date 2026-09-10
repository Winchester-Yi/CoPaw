import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import WPlusSopActiveBar from "./index";

const apiMock = vi.hoisted(() => ({
  getActiveSession: vi.fn(),
  getSession: vi.fn(),
  confirmEntry: vi.fn(),
  rejectEntry: vi.fn(),
}));

vi.mock("@/api/modules/wplusSop", () => ({
  wplusSopApi: apiMock,
}));

describe("WPlusSopActiveBar", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getActiveSession.mockRejectedValue(
      Object.assign(new Error("no active session"), { status: 404 }),
    );
    apiMock.getSession.mockRejectedValue(
      Object.assign(new Error("session not found"), { status: 404 }),
    );
  });

  afterEach(() => {
    cleanup();
  });

  it("keeps ordinary Chat unlocked when active-session returns null", async () => {
    apiMock.getActiveSession.mockResolvedValue(null);
    const onLocksChatInputChange = vi.fn();
    render(
      <MemoryRouter>
        <WPlusSopActiveBar
          chatId="chat-1"
          onLocksChatInputChange={onLocksChatInputChange}
        />
      </MemoryRouter>,
    );

    await waitFor(() => expect(apiMock.getActiveSession).toHaveBeenCalled());
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    expect(onLocksChatInputChange).toHaveBeenLastCalledWith(false);
  });

  it("clears the active bar and unlocks Chat when a refresh returns null", async () => {
    apiMock.getActiveSession.mockResolvedValueOnce({
      session_id: "sop-1",
      title: "客户经营 SOP",
      state: "GeneratingStageProposal",
      state_version: 1,
    });
    apiMock.getActiveSession.mockResolvedValue(null);
    const onLocksChatInputChange = vi.fn();
    render(
      <MemoryRouter>
        <WPlusSopActiveBar
          chatId="chat-1"
          onLocksChatInputChange={onLocksChatInputChange}
        />
      </MemoryRouter>,
    );

    expect(
      await screen.findByText("这个 Chat 正在进行 W+ SOP"),
    ).toBeInTheDocument();
    expect(onLocksChatInputChange).toHaveBeenLastCalledWith(true);
    fireEvent.focus(window);

    await waitFor(() => {
      expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
      expect(onLocksChatInputChange).toHaveBeenLastCalledWith(false);
    });
  });

  it("restores a terminal workspace card from Chat metadata", async () => {
    render(
      <MemoryRouter initialEntries={["/chat/chat-1"]}>
        <Routes>
          <Route
            path="/chat/:chatId"
            element={
              <WPlusSopActiveBar
                chatId="chat-1"
                projection={{
                  entryProposal: null,
                  session: {
                    session_id: "sop-1",
                    title: "客户经营 SOP",
                    state: "Completed",
                    state_version: 20,
                  },
                }}
              />
            }
          />
          <Route path="/wplus-sop/:sessionId" element={<h1>历史工作台</h1>} />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText("这个 Chat 的 W+ SOP 已结束")).toBeInTheDocument();
    expect(screen.getByText("已完成")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看工作台" }));
    expect(
      await screen.findByRole("heading", { name: "历史工作台" }),
    ).toBeInTheDocument();
  });

  it("restores pending confirmation controls from Chat metadata", () => {
    render(
      <MemoryRouter>
        <WPlusSopActiveBar
          chatId="chat-1"
          logicalSessionId="logical-1"
          projection={{
            entryProposal: {
              proposal_id: "proposal-1",
              mode: "implicit",
              status: "pending",
            },
            session: null,
          }}
        />
      </MemoryRouter>,
    );

    expect(
      screen.getByRole("button", { name: "进入工作台" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "留在 Chat" }),
    ).toBeInTheDocument();
  });

  it("prioritizes a new pending proposal over an older terminal session", () => {
    render(
      <MemoryRouter>
        <WPlusSopActiveBar
          chatId="chat-1"
          projection={{
            entryProposal: {
              proposal_id: "proposal-new",
              mode: "explicit",
              status: "pending",
            },
            session: {
              session_id: "sop-old",
              title: "旧 SOP",
              state: "Completed",
              state_version: 20,
            },
          }}
        />
      </MemoryRouter>,
    );

    expect(
      screen.getByRole("button", { name: "进入工作台" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("旧 SOP")).not.toBeInTheDocument();
  });

  it("refreshes on focus and releases the Chat input lock when paused", async () => {
    const onLocksChatInputChange = vi.fn();
    apiMock.getSession
      .mockResolvedValueOnce({
        session_id: "sop-1",
        chat_id: "chat-1",
        title: "客户经营 SOP",
        state: "GeneratingQuestions",
        state_version: 5,
        revision: 1,
        round: 1,
        stages: [],
        current_stage_id: null,
        updated_at: "2026-07-29T00:00:00Z",
      })
      .mockResolvedValueOnce({
        session_id: "sop-1",
        chat_id: "chat-1",
        title: "客户经营 SOP",
        state: "Paused",
        state_version: 6,
        revision: 1,
        round: 1,
        stages: [],
        current_stage_id: null,
        updated_at: "2026-07-29T00:01:00Z",
      });
    render(
      <MemoryRouter>
        <WPlusSopActiveBar
          chatId="chat-1"
          projection={{
            entryProposal: null,
            session: {
              session_id: "sop-1",
              title: "客户经营 SOP",
              state: "GeneratingQuestions",
              state_version: 5,
            },
          }}
          onLocksChatInputChange={onLocksChatInputChange}
        />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(onLocksChatInputChange).toHaveBeenLastCalledWith(true),
    );
    fireEvent.focus(window);
    await screen.findByText("已暂停");
    await waitFor(() =>
      expect(onLocksChatInputChange).toHaveBeenLastCalledWith(false),
    );
  });

  it("shows and navigates the resume entry from an active-session paused response", async () => {
    apiMock.getActiveSession.mockResolvedValue({
      session_id: "sop-paused",
      chat_id: "chat-1",
      title: "客户经营 SOP",
      state: "Paused",
      state_version: 6,
      revision: 1,
      round: 1,
      stages: [],
      current_stage_id: null,
      updated_at: "2026-07-29T00:01:00Z",
    });
    render(
      <MemoryRouter initialEntries={["/chat/chat-1"]}>
        <Routes>
          <Route
            path="/chat/:chatId"
            element={<WPlusSopActiveBar chatId="chat-1" />}
          />
          <Route path="/wplus-sop/:sessionId" element={<h1>继续工作页</h1>} />
        </Routes>
      </MemoryRouter>,
    );

    expect(
      await screen.findByText("这个 Chat 的 W+ SOP 已暂停"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "继续工作" }));
    expect(
      await screen.findByRole("heading", { name: "继续工作页" }),
    ).toBeInTheDocument();
  });
});

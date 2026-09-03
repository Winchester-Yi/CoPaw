import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type React from "react";
import { buildChatShareUrl } from "./shareUrl";
import { isShareableTurn } from "./shareSelection";

vi.mock("../../chatShareContext", () => ({
  useChatShareSelection: () => ({
    active: true,
    turns: [],
    selectedTurnIds: ["turn-1"],
    selectableTurnIds: ["turn-1"],
    turnByMessageId: {},
    shareUrl: null,
    open: vi.fn(),
    close: vi.fn(),
    toggleTurn: vi.fn(),
    selectAll: vi.fn(),
    setShareUrl: vi.fn(),
  }),
}));

vi.mock("@/api/modules/chat", () => ({ chatApi: {} }));

vi.mock("@agentscope-ai/design", () => ({
  IconButton: ({ icon, onClick }: { icon: React.ReactNode; onClick?: () => void }) => (
    <button onClick={onClick}>{icon}</button>
  ),
}));

vi.mock("@agentscope-ai/icons", () => ({
  SparkShareLine: () => <span aria-label="spark-share-line" />,
}));

describe("ChatActionGroup turn selection", () => {
  it("only enables turns with an authoritative completed status", () => {
    const statuses = { completed: "completed", running: "running" };
    expect(isShareableTurn("completed", statuses)).toBe(true);
    expect(isShareableTurn("running", statuses)).toBe(false);
    expect(isShareableTurn("missing", statuses)).toBe(false);
  });
});

describe("ChatActionGroup share URL", () => {
  it("preserves the console basename when building a public link", () => {
    expect(
      buildChatShareUrl("/chat-share/token-1", {
        origin: "https://example.test",
        pathname: "/console/chat/abc",
      }),
    ).toBe("https://example.test/console/chat-share/token-1");
  });

  it("does not duplicate an already-prefixed share path", () => {
    expect(
      buildChatShareUrl("/console/chat-share/token-1", {
        origin: "https://example.test",
        pathname: "/console/chat/abc",
      }),
    ).toBe("https://example.test/console/chat-share/token-1");
  });
});

describe("ChatActionGroup placement", () => {
  it("mounts the active toolbar at document body level and aligns it to chat width", async () => {
    const chatArea = document.createElement("div");
    chatArea.dataset.chatMessagesArea = "true";
    chatArea.getBoundingClientRect = () =>
      ({ left: 240, width: 900 } as DOMRect);
    document.body.appendChild(chatArea);

    const { default: ChatActionGroup } = await import("./index");
    render(<ChatActionGroup chatId="chat-1" />);
    const toolbar = screen.getByRole("region", { name: "分享选择操作" });
    expect(toolbar.parentElement).toBe(document.body);
    expect(screen.getByLabelText("spark-share-line")).toBeInTheDocument();
    await waitFor(() =>
      expect(toolbar).toHaveStyle({ left: "240px", width: "900px" }),
    );
  });
});

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Message } from "@/api/types";
import {
  ChatShareSelectionProvider,
  useChatShareSelection,
} from "./chatShareContext";

const messages: Message[] = [
  { id: "q1", role: "user", content: "question" },
  { id: "a1", role: "assistant", content: "answer" },
  { id: "q2", role: "user", content: "next" },
  { id: "a2", role: "assistant", content: "next answer" },
];

function Probe() {
  const selection = useChatShareSelection();
  return (
    <>
      <button
        onClick={() =>
          selection.open(messages, { q1: "completed", q2: "completed" })
        }
      >
        open
      </button>
      <button onClick={() => selection.toggleTurn("q1")}>toggle-q1</button>
      <button
        onClick={() => selection.setShareUrl("https://example.test/share")}
      >
        set-url
      </button>
      <output>{selection.selectedTurnIds.join(",")}</output>
      <output>{selection.turnByMessageId.a1 || "missing"}</output>
      <output>{selection.shareUrl || "no-url"}</output>
    </>
  );
}

describe("ChatShareSelectionProvider", () => {
  it("defaults to all complete answer turns and toggles a whole pair", async () => {
    render(
      <ChatShareSelectionProvider>
        <Probe />
      </ChatShareSelectionProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "open" }));
    expect(screen.getByText("q1,q2")).toBeInTheDocument();
    expect(screen.getByText("q1", { selector: "output" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "toggle-q1" }));
    expect(screen.getByText("q2")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "set-url" }));
    expect(screen.getByText("https://example.test/share")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "toggle-q1" }));
    expect(screen.getByText("no-url")).toBeInTheDocument();
  });
});

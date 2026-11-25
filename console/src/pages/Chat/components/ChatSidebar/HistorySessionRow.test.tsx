import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HistorySessionRow } from "./HistorySessionRow";

vi.mock("../ChatSessionItem", () => ({
  __esModule: true,
  default: (props: {
    name: string;
    onClick?: () => void;
    titleAction?: React.ReactNode;
  }) => (
    <div>
      <button type="button" onClick={props.onClick}>
        {props.name}
      </button>
      {props.titleAction}
    </div>
  ),
}));

describe("HistorySessionRow", () => {
  afterEach(() => {
    cleanup();
  });

  it("uses the resolved chat id as the click target when available", () => {
    const onSessionClick = vi.fn();

    render(
      <HistorySessionRow
        session={{
          id: "1777001065201000",
          realId: "chat-real-1",
          name: "running chat",
          messages: [],
          createdAt: "2026-04-24T00:00:00Z",
        }}
        active={false}
        onSessionClick={onSessionClick}
        onSessionRename={vi.fn()}
        onSessionDelete={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByText("running chat"));

    expect(onSessionClick).toHaveBeenCalledWith("chat-real-1");
  });

  it("passes the resolved chat id and session name when deleting", () => {
    const onSessionDelete = vi.fn();
    const onSessionRename = vi.fn();

    render(
      <HistorySessionRow
        session={{
          id: "1777001065201000",
          realId: "chat-real-2",
          name: "待删除会话",
          messages: [],
          createdAt: "2026-04-24T00:00:00Z",
        }}
        active={false}
        onSessionClick={vi.fn()}
        onSessionRename={onSessionRename}
        onSessionDelete={onSessionDelete}
      />,
    );

    const menu = screen.getByRole("button", { name: /更多操作/ });
    fireEvent.click(menu);
    fireEvent.click(screen.getByRole("menuitem", { name: "编辑标题" }));
    expect(onSessionRename).toHaveBeenCalledWith(
      "chat-real-2",
      "待删除会话",
      menu,
    );

    fireEvent.click(menu);
    fireEvent.click(screen.getByRole("menuitem", { name: "删除" }));

    expect(onSessionDelete).toHaveBeenCalledWith(
      "chat-real-2",
      "chat-real-2",
      "待删除会话",
    );
  });
});

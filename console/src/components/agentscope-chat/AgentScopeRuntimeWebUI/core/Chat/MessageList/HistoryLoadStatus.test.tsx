import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import HistoryLoadStatus from "./HistoryLoadStatus";

describe("HistoryLoadStatus", () => {
  afterEach(cleanup);

  it("keeps a quiet stable status region while idle", () => {
    render(<HistoryLoadStatus state="idle" onRetry={vi.fn()} />);

    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveAttribute("data-state", "idle");
    expect(status).toBeEmptyDOMElement();
  });

  it("announces network loading without exposing a retry action", () => {
    render(<HistoryLoadStatus state="loading" onRetry={vi.fn()} />);

    expect(screen.getByRole("status")).toHaveTextContent("正在加载更早的消息…");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("shows an accessible compact error and keyboard-focusable retry", () => {
    const onRetry = vi.fn();
    render(<HistoryLoadStatus state="error" onRetry={onRetry} />);

    expect(screen.getByRole("alert")).toHaveTextContent("历史消息加载失败");
    const retry = screen.getByRole("button", { name: "重试加载历史消息" });
    retry.focus();
    expect(retry).toHaveFocus();
    fireEvent.click(retry);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("keeps retry visible but disabled while the retry request is running", () => {
    render(<HistoryLoadStatus state="loading" retrying onRetry={vi.fn()} />);

    expect(screen.getByRole("alert")).toHaveTextContent(
      "正在重新加载历史消息…",
    );
    expect(
      screen.getByRole("button", { name: "正在重试加载历史消息" }),
    ).toBeDisabled();
  });

  it("announces the terminal state quietly", () => {
    render(<HistoryLoadStatus state="exhausted" onRetry={vi.fn()} />);

    expect(screen.getByRole("status")).toHaveTextContent("已到达会话开始处");
  });
});

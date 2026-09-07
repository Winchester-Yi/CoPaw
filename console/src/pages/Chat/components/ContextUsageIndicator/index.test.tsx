import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ContextUsageIndicator from ".";
import type { ContextUsageSnapshot } from "@/api/types/contextUsage";

const availableSnapshot: ContextUsageSnapshot = {
  available: true,
  schema_version: 1,
  used_tokens: 266_000,
  max_tokens: 1_000_000,
  remaining_tokens: 734_000,
  usage_ratio: 0.266,
  system_context_tokens: 18_700,
  tool_definition_tokens: 240,
  conversation_tokens: 247_060,
  governance_threshold_ratio: 0.5,
  active_threshold_ratio: 0.7,
  emergency_threshold_ratio: 0.9,
  status: "normal",
  estimated: true,
  stale: false,
  as_of: "2026-09-02T08:00:00Z",
};

describe("ContextUsageIndicator", () => {
  afterEach(cleanup);

  it("hides until a snapshot is available and hides again for a new chat", () => {
    const refresh = vi.fn();
    const { rerender } = render(
      <ContextUsageIndicator error={false} refresh={refresh} />,
    );

    expect(screen.queryByRole("button")).not.toBeInTheDocument();

    rerender(
      <ContextUsageIndicator
        snapshot={{ available: false }}
        error={false}
        refresh={refresh}
      />,
    );
    expect(screen.queryByRole("button")).not.toBeInTheDocument();

    rerender(
      <ContextUsageIndicator
        snapshot={availableSnapshot}
        error={false}
        refresh={refresh}
      />,
    );

    expect(
      screen.getByRole("button", { name: /上下文占用 27%/ }),
    ).toBeVisible();

    rerender(<ContextUsageIndicator error={false} refresh={refresh} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("shows estimated totals, progress semantics, and all three categories", async () => {
    render(
      <ContextUsageIndicator
        snapshot={availableSnapshot}
        error={false}
        refresh={vi.fn()}
      />,
    );
    const trigger = screen.getByRole("button", {
      name: /上下文占用 27%.*状态正常/,
    });

    fireEvent.click(trigger);

    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(
      await screen.findByRole("dialog", { name: "上下文占用详情" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("progressbar", { name: "上下文占用" }),
    ).toHaveAttribute("aria-valuenow", "27");
    expect(screen.getByText(/约 266K \/ 1M/)).toBeInTheDocument();
    expect(screen.getByText(/剩余约 734K/)).toBeInTheDocument();
    expect(screen.getByText("系统上下文")).toBeInTheDocument();
    expect(screen.getByText("工具定义")).toBeInTheDocument();
    expect(screen.getByText("对话消息")).toBeInTheDocument();
    expect(screen.getByText(/18,700/)).toBeInTheDocument();
    expect(screen.getAllByText(/240/).length).toBeGreaterThan(0);
    expect(screen.getByText(/247,060/)).toBeInTheDocument();
    expect(
      screen.queryByText(/估算值，不是模型供应商账单/),
    ).not.toBeInTheDocument();
  });

  it("keeps the last value while exposing the shared error state", async () => {
    render(
      <ContextUsageIndicator
        snapshot={availableSnapshot}
        error
        refresh={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /上下文占用 27%/ }));
    expect(
      await screen.findByText(/刷新失败.*显示上次结果/),
    ).toBeInTheDocument();
  });

  it("stays hidden when the initial snapshot request fails", () => {
    const refresh = vi.fn();
    render(<ContextUsageIndicator error refresh={refresh} />);

    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(refresh).not.toHaveBeenCalled();
  });
});

import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ToolCall from "./ToolCall";

vi.mock("@/components/agentscope-chat", () => {
  const OperateCard = ({
    header,
  }: {
    header: Record<string, React.ReactNode>;
  }) =>
    React.createElement(
      "div",
      { "data-testid": "operate-card" },
      header.icon,
      header.title,
      header.extra,
    );
  OperateCard.LineBody = ({ children }: { children: React.ReactNode }) =>
    React.createElement("div", null, children);
  return {
    OperateCard,
    useProviderContext: () => ({
      getPrefixCls: (name: string) => `swe-${name}`,
    }),
  };
});

vi.mock("@agentscope-ai/icons", () => ({
  SparkCheckCircleFill: () => React.createElement("span"),
  SparkCopyLine: () => React.createElement("span"),
  SparkErrorCircleFill: () => React.createElement("span"),
  SparkLoadingLine: () => React.createElement("span"),
  SparkLockFill: () => React.createElement("span"),
  SparkStopCircleLine: () => React.createElement("span"),
  SparkTimeLine: () => React.createElement("span"),
  SparkToolLine: () => React.createElement("span"),
  SparkTrueLine: () => React.createElement("span"),
  SparkWarningCircleFill: () => React.createElement("span"),
}));

vi.mock("@agentscope-ai/design", () => ({
  CodeBlock: () => null,
  IconButton: () => null,
}));

vi.mock("../../Util/copy", () => ({
  copy: vi.fn(async () => {}),
}));

afterEach(() => {
  cleanup();
});

describe("ToolCall governance badges", () => {
  it("shows pending approval without a loading icon", () => {
    render(
      <ToolCall
        input={{}}
        loading={false}
        msgStatus="pending"
        output={{}}
        title="执行操作"
      />,
    );

    expect(screen.getByText("待审批")).toBeInTheDocument();
  });

  it("distinguishes a policy block from a user rejection", () => {
    render(
      <ToolCall
        input={{}}
        loading={false}
        msgStatus="blocked"
        output={{}}
        title="执行操作"
      />,
    );

    expect(screen.getByText("已拦截")).toBeInTheDocument();
    expect(screen.queryByText("已拒绝")).not.toBeInTheDocument();
  });
});

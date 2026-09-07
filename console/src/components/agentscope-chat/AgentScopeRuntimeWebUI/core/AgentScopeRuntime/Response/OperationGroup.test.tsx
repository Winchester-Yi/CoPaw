import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import OperationGroup from "./OperationGroup";
import type { OperationGroupEntry } from "./operationGrouping";
import {
  AgentScopeRuntimeContentType,
  AgentScopeRuntimeMessageType,
  AgentScopeRuntimeRunStatus,
  type IAgentScopeRuntimeMessage,
} from "../types";

vi.mock("@/components/agentscope-chat", () => ({
  useProviderContext: () => ({
    getPrefixCls: (name: string) => "swe-" + name,
  }),
}));

vi.mock("@agentscope-ai/icons", () => {
  return {
    SparkCheckCircleFill: () =>
      React.createElement("span", { "data-testid": "icon-success" }),
    SparkDownLine: () =>
      React.createElement("span", { "data-testid": "chevron-down" }),
    SparkErrorCircleFill: () =>
      React.createElement("span", { "data-testid": "icon-failed" }),
    SparkLoadingLine: () =>
      React.createElement("span", { "data-testid": "icon-running" }, "run"),
    SparkLockFill: () =>
      React.createElement("span", { "data-testid": "icon-blocked" }),
    SparkStopCircleLine: () =>
      React.createElement("span", { "data-testid": "icon-canceled" }),
    SparkTimeLine: () =>
      React.createElement("span", { "data-testid": "icon-pending" }),
    SparkUpLine: () =>
      React.createElement("span", { "data-testid": "chevron-up" }),
    SparkWarningCircleFill: () =>
      React.createElement("span", { "data-testid": "icon-warning" }),
  };
});

vi.mock("./style", () => ({
  default: () => null,
}));

vi.mock("./Tool", () => ({
  default: ({ data }: { data: IAgentScopeRuntimeMessage }) =>
    React.createElement(
      "button",
      {
        "data-message-id": data.id,
        "data-testid": "tool-detail",
        type: "button",
      },
      "工具详情",
    ),
}));

vi.mock("./Reasoning", () => ({
  default: ({ data }: { data: IAgentScopeRuntimeMessage }) =>
    React.createElement(
      "div",
      { "data-testid": "group-reasoning" },
      data.content[0]?.type === AgentScopeRuntimeContentType.TEXT
        ? data.content[0].text
        : "",
    ),
}));

afterEach(() => {
  cleanup();
});

function toolMessage(options: {
  id: string;
  toolName?: string;
  inputStatus?: string;
  outputStatus?: string;
  governance?: string;
}): IAgentScopeRuntimeMessage {
  const toolName = options.toolName || "execute_shell_command";
  const inputData: Record<string, unknown> = {
    name: toolName,
    arguments: "{}",
    summary: "开始执行操作",
    operation_group: { id: "inspect", title: "检查图片" },
  };
  if (options.inputStatus) inputData.tool_status = options.inputStatus;
  const content: IAgentScopeRuntimeMessage["content"] = [
    {
      type: AgentScopeRuntimeContentType.DATA,
      status: AgentScopeRuntimeRunStatus.Completed,
      data: inputData,
    },
  ];
  if (options.outputStatus || options.governance) {
    const outputData: Record<string, unknown> = { name: toolName };
    if (options.outputStatus) outputData.tool_status = options.outputStatus;
    if (options.governance) {
      outputData.tool_governance = options.governance;
    }
    content.push({
      type: AgentScopeRuntimeContentType.DATA,
      status: AgentScopeRuntimeRunStatus.Completed,
      data: outputData,
    });
  }
  return {
    id: options.id,
    object: "message",
    role: "assistant",
    type: AgentScopeRuntimeMessageType.PLUGIN_CALL,
    status: AgentScopeRuntimeRunStatus.InProgress,
    content,
  };
}

function reasoningMessage(id: string, text: string): IAgentScopeRuntimeMessage {
  return {
    id,
    object: "message",
    role: "assistant",
    type: AgentScopeRuntimeMessageType.REASONING,
    status: AgentScopeRuntimeRunStatus.Completed,
    content: [
      {
        type: AgentScopeRuntimeContentType.TEXT,
        status: AgentScopeRuntimeRunStatus.Completed,
        text,
      },
    ],
  };
}

let sequence = 0;

function entry(
  steps: IAgentScopeRuntimeMessage[],
  id = "inspect",
  title = "检查图片",
): OperationGroupEntry {
  sequence += 1;
  const key = id + "#" + sequence;
  return {
    kind: "group",
    key,
    group: { id, title, instanceKey: key },
    steps,
  };
}

function headerIcon(container: HTMLElement) {
  return container.querySelector<HTMLElement>(
    ".swe-response-operation-group-trigger [data-testid]",
  );
}

describe("OperationGroup", () => {
  it("renders collapsed by default with only the title and a status icon", () => {
    const { container } = render(
      React.createElement(OperationGroup, {
        entry: entry([
          toolMessage({ id: "t1", inputStatus: "running" }),
          toolMessage({ id: "t2", inputStatus: "running" }),
        ]),
      }),
    );

    const trigger = screen.getByRole("button");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("检查图片")).toBeInTheDocument();
    expect(headerIcon(container)?.getAttribute("data-testid")).toBe(
      "icon-running",
    );
    const toolCards = screen.getAllByTestId("tool-detail");
    expect(toolCards).toHaveLength(2);
    for (const toolCard of toolCards) {
      expect(toolCard).not.toBeVisible();
    }
    expect(trigger.textContent).not.toContain("工具详情");
  });

  it("expands on click and shows the original tool cards", () => {
    const { container } = render(
      React.createElement(OperationGroup, {
        entry: entry([
          toolMessage({ id: "t1", inputStatus: "running" }),
          toolMessage({
            id: "t2",
            inputStatus: "running",
            outputStatus: "success",
          }),
        ]),
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: /操作组/ }));

    expect(screen.getByRole("button", { name: /操作组/ })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    for (const toolCard of screen.getAllByTestId("tool-detail")) {
      expect(toolCard).toBeVisible();
    }
    expect(headerIcon(container)?.getAttribute("data-testid")).toBe(
      "icon-running",
    );
  });

  it("shows a distinct pending icon while awaiting approval", () => {
    const { container } = render(
      React.createElement(OperationGroup, {
        entry: entry([
          toolMessage({
            id: "t1",
            inputStatus: "running",
            governance: "pending",
          }),
        ]),
      }),
    );

    expect(headerIcon(container)?.getAttribute("data-testid")).toBe(
      "icon-pending",
    );
  });

  it("shows the failed icon when any step really failed", () => {
    const { container } = render(
      React.createElement(OperationGroup, {
        entry: entry([
          toolMessage({
            id: "t1",
            inputStatus: "running",
            outputStatus: "success",
          }),
          toolMessage({
            id: "t2",
            inputStatus: "running",
            outputStatus: "failed",
          }),
        ]),
      }),
    );

    expect(headerIcon(container)?.getAttribute("data-testid")).toBe(
      "icon-failed",
    );
  });

  it("does not share expansion state across response-card instances", () => {
    const duplicateKey = entry([
      toolMessage({ id: "t1", inputStatus: "running" }),
    ]);
    render(
      React.createElement(
        React.Fragment,
        null,
        React.createElement(OperationGroup, { entry: duplicateKey }),
        React.createElement(OperationGroup, { entry: duplicateKey }),
      ),
    );

    const triggers = screen.getAllByRole("button", { name: /操作组/ });
    fireEvent.click(triggers[0]);

    expect(triggers[0]).toHaveAttribute("aria-expanded", "true");
    expect(triggers[1]).toHaveAttribute("aria-expanded", "false");
  });

  it("uses the original independently expandable tool cards", () => {
    render(
      React.createElement(OperationGroup, {
        entry: entry([
          toolMessage({ id: "t1", inputStatus: "running" }),
          toolMessage({ id: "t2", outputStatus: "success" }),
        ]),
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: /操作组/ }));

    const toolCards = screen.getAllByTestId("tool-detail");
    expect(toolCards).toHaveLength(2);
    expect(toolCards[0]).toBeVisible();
    expect(toolCards[0]).toHaveAttribute("data-message-id", "t1");
    expect(toolCards[1]).toHaveAttribute("data-message-id", "t2");
    expect(screen.queryByText("命令行操作已完成")).not.toBeInTheDocument();
  });

  it("renders multiple interleaved reasoning messages inside one group", () => {
    render(
      React.createElement(OperationGroup, {
        entry: entry([
          toolMessage({ id: "t1", inputStatus: "running" }),
          reasoningMessage("reason-1", "检查完目录后继续读取"),
          reasoningMessage("reason-2", "读取后继续核对结果"),
          toolMessage({ id: "t2", inputStatus: "running" }),
        ]),
      }),
    );

    for (const reasoning of screen.getAllByTestId("group-reasoning")) {
      expect(reasoning).not.toBeVisible();
    }
    fireEvent.click(screen.getByRole("button", { name: /操作组/ }));

    const body = screen.getByRole("list");
    expect(body.children).toHaveLength(4);
    expect(body.children[0].querySelector("button")).toHaveAttribute(
      "data-message-id",
      "t1",
    );
    expect(body.children[1]).toHaveTextContent("检查完目录后继续读取");
    expect(body.children[2]).toHaveTextContent("读取后继续核对结果");
    expect(body.children[3].querySelector("button")).toHaveAttribute(
      "data-message-id",
      "t2",
    );
  });
});

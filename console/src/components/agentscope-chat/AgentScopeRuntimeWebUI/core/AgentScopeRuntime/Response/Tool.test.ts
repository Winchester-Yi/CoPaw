import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  AgentScopeRuntimeContentType,
  AgentScopeRuntimeMessageType,
  AgentScopeRuntimeRunStatus,
  type IAgentScopeRuntimeMessage,
} from "../types";
import Tool from "./Tool";
import {
  buildToolTitle,
  getToolDisplayName,
  resolveServerLabel,
  resolveToolName,
} from "./ToolTitle";

const mockOptions = vi.hoisted(() => ({
  customToolRenderConfig: {} as Record<string, any>,
}));

vi.mock("@/components/agentscope-chat", () => ({
  ToolCall: ({
    loading,
    msgStatus,
    output,
  }: {
    loading: boolean;
    msgStatus: string;
    output: unknown;
  }) =>
    React.createElement("div", {
      "data-loading": String(loading),
      "data-msg-status": msgStatus,
      "data-output":
        typeof output === "string" ? output : JSON.stringify(output),
      "data-testid": "tool-call",
    }),
}));

vi.mock("../../Context/ChatAnywhereOptionsContext", () => ({
  useChatAnywhereOptions: (selector: (value: any) => any) =>
    selector({
      customToolRenderConfig: mockOptions.customToolRenderConfig,
    }),
}));

vi.mock("./Approval", () => ({
  default: () => React.createElement("div", { "data-testid": "approval" }),
}));

function toolMessage({
  toolName = "execute_shell_command",
  toolStatus,
  governance,
  liveOutput,
}: {
  toolName?: string;
  toolStatus?: "running" | "success" | "failed";
  governance?: "pending" | "rejected" | "blocked";
  liveOutput?: string;
}): IAgentScopeRuntimeMessage {
  return {
    id: "tool-message",
    object: "message",
    role: "assistant",
    type: AgentScopeRuntimeMessageType.PLUGIN_CALL_OUTPUT,
    status: AgentScopeRuntimeRunStatus.Completed,
    content: [
      {
        type: AgentScopeRuntimeContentType.DATA,
        status: AgentScopeRuntimeRunStatus.Completed,
        data: {
          name: toolName,
          arguments: { command: "echo ok" },
          summary: "开始执行操作",
          tool_status: "running",
          live_output: liveOutput,
        },
      },
      {
        type: AgentScopeRuntimeContentType.DATA,
        status: AgentScopeRuntimeRunStatus.Completed,
        data: {
          name: toolName,
          output: "Error: this text is controlled by backend status",
          output_summary: "输出摘要",
          tool_error: toolStatus === "failed" ? "Tool error" : null,
          tool_status: toolStatus,
          tool_governance: governance,
        },
      },
    ],
  };
}

describe("tool call title", () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    mockOptions.customToolRenderConfig = {};
  });

  it("keeps a clean backend summary with the concrete object", () => {
    const title = buildToolTitle({
      loading: true,
      toolName: "read_file",
      defaultTitle: getToolDisplayName("read_file"),
      input: '{"file_path": "/tmp/demo.txt"}',
      summary: "正在读取 demo.txt",
    });

    expect(title).toBe("正在读取 demo.txt");
  });

  it("extracts the basename for file-read arguments when summary is unsafe", () => {
    const title = buildToolTitle({
      loading: false,
      toolName: "read_file",
      defaultTitle: getToolDisplayName("read_file"),
      input: '{"file_path": "/workspace/reports/demo.txt", "limit": 2000}',
      summary:
        '正在工具操作：{"file_path": "/workspace/reports/demo.txt", "limit": 2000}',
    });

    expect(title).toBe("读取文件：demo.txt");
    expect(title).not.toContain("file_path");
    expect(title).not.toContain("limit");
  });

  it("shows the glob pattern without exposing the whole JSON payload", () => {
    const title = buildToolTitle({
      loading: true,
      toolName: "glob_search",
      defaultTitle: getToolDisplayName("glob_search"),
      input: '{"pattern": "src/**/*.tsx", "root": "/workspace"}',
      summary: '{"pattern": "src/**/*.tsx", "root": "/workspace"}',
    });

    expect(title).toBe("正在查找文件：src/**/*.tsx");
    expect(title).not.toContain("root");
  });

  it("falls back to the tool label when there is no safe object hint", () => {
    const title = buildToolTitle({
      loading: false,
      toolName: "execute_shell_command",
      defaultTitle: getToolDisplayName("execute_shell_command"),
      input: '{"command": "cat /etc/passwd"}',
      summary: '{"command": "cat /etc/passwd"}',
    });

    expect(title).toBe("调用工具：执行操作");
    expect(title).not.toContain("cat");
  });

  it("keeps unknown tool names visible while hiding opaque business params", () => {
    const title = buildToolTitle({
      loading: true,
      toolName: "query_business_opportunity",
      defaultTitle: getToolDisplayName("query_business_opportunity"),
      input: '{"bbkOrgId": "V00", "brnOrgId": "V00001"}',
      summary: '{"bbkOrgId": "V00", "brnOrgId": "V00001"}',
    });

    expect(title).toBe("正在调用：query_business_opportunity");
    expect(title).not.toContain("bbkOrgId");
    expect(title).not.toContain("V00001");
  });

  it("does not use vague browser summaries as the visible title", () => {
    const title = buildToolTitle({
      loading: true,
      toolName: "browser_use",
      defaultTitle: getToolDisplayName("browser_use"),
      input: '{"action": "type", "text": "chengdu"}',
      summary: "正在 chengdu",
    });

    expect(title).toBe("正在网页操作：chengdu");
  });

  it("keeps the browser action label for URL path hints", () => {
    const title = buildToolTitle({
      loading: true,
      toolName: "browser_use",
      defaultTitle: getToolDisplayName("browser_use"),
      input:
        '{"action": "open", "url": "https://weather.com.cn/weather/101270101.shtml"}',
      summary: "正在 101270101.shtml",
    });

    expect(title).toBe("正在网页操作：weather.com.cn/weather/101270101.shtml");
  });

  it("resolves MCP tool aliases instead of falling back to generic labels", () => {
    const data = {
      tool_name: "fetch_customer_profile",
      mcp_server: "crm",
    };

    expect(resolveToolName(data)).toBe("fetch_customer_profile");
    expect(resolveServerLabel(data)).toBe("crm");
    expect(
      getToolDisplayName(resolveToolName(data), resolveServerLabel(data)),
    ).toBe("[crm] fetch_customer_profile");
  });

  it("ignores generic summaries and shows the resolved MCP tool name", () => {
    const data = {
      tool: {
        name: "search_customer_cases",
      },
    };
    const toolName = resolveToolName(data);
    const title = buildToolTitle({
      loading: true,
      toolName,
      defaultTitle: getToolDisplayName(toolName),
      input: "{}",
      summary: "正在工具操作",
    });

    expect(toolName).toBe("search_customer_cases");
    expect(title).toBe("正在调用：search_customer_cases");
  });

  it("passes backend failed tool_status to the default tool card", () => {
    render(
      React.createElement(Tool, {
        data: toolMessage({ toolStatus: "failed" }),
      }),
    );

    expect(screen.getByTestId("tool-call")).toHaveAttribute(
      "data-msg-status",
      AgentScopeRuntimeRunStatus.Failed,
    );
    expect(screen.getByTestId("tool-call")).toHaveAttribute(
      "data-loading",
      "false",
    );
  });

  it("renders pending governance without keeping the tool loading", () => {
    render(
      React.createElement(Tool, {
        data: toolMessage({ governance: "pending" }),
      }),
    );

    expect(screen.getByTestId("tool-call")).toHaveAttribute(
      "data-msg-status",
      "pending",
    );
    expect(screen.getByTestId("tool-call")).toHaveAttribute(
      "data-loading",
      "false",
    );
  });

  it("renders blocked governance distinctly from a rejection", () => {
    render(
      React.createElement(Tool, {
        data: toolMessage({ governance: "blocked" }),
      }),
    );

    expect(screen.getByTestId("tool-call")).toHaveAttribute(
      "data-msg-status",
      "blocked",
    );
    expect(screen.getByTestId("tool-call")).toHaveAttribute(
      "data-loading",
      "false",
    );
  });

  it("renders a rejected governance decision distinctly", () => {
    render(
      React.createElement(Tool, {
        data: toolMessage({ governance: "rejected" }),
      }),
    );

    expect(screen.getByTestId("tool-call")).toHaveAttribute(
      "data-msg-status",
      AgentScopeRuntimeRunStatus.Rejected,
    );
    expect(screen.getByTestId("tool-call")).toHaveAttribute(
      "data-loading",
      "false",
    );
  });

  it("uses live output before a final tool output arrives", () => {
    const message = toolMessage({ liveOutput: "running tests\n" });
    message.content = message.content.slice(0, 1);
    message.type = AgentScopeRuntimeMessageType.PLUGIN_CALL;
    message.status = AgentScopeRuntimeRunStatus.InProgress;

    render(
      React.createElement(Tool, {
        data: message,
      }),
    );

    expect(screen.getByTestId("tool-call")).toHaveAttribute(
      "data-output",
      "running tests\n",
    );
  });

  it("uses final output instead of live output when both are present", () => {
    render(
      React.createElement(Tool, {
        data: toolMessage({
          liveOutput: "partial output\n",
          toolStatus: "success",
        }),
      }),
    );

    expect(screen.getByTestId("tool-call")).toHaveAttribute(
      "data-output",
      "Error: this text is controlled by backend status",
    );
  });

  it("passes normalized status to custom tool renders", () => {
    mockOptions.customToolRenderConfig = {
      custom_tool: ({ data }: { data: IAgentScopeRuntimeMessage }) =>
        React.createElement("div", {
          "data-status": data.status,
          "data-testid": "custom-tool",
        }),
    };

    render(
      React.createElement(Tool, {
        data: toolMessage({
          toolName: "custom_tool",
          toolStatus: "failed",
        }),
      }),
    );

    expect(screen.getByTestId("custom-tool")).toHaveAttribute(
      "data-status",
      AgentScopeRuntimeRunStatus.Failed,
    );
  });
});

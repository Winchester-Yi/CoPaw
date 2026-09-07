import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AgentScopeRuntimeResponseCard from "./Card";
import {
  AgentScopeRuntimeContentType,
  AgentScopeRuntimeMessageRole,
  AgentScopeRuntimeMessageType,
  AgentScopeRuntimeRunStatus,
  IAgentScopeRuntimeMessage,
  IAgentScopeRuntimeResponse,
} from "../types";

vi.mock("@/components/agentscope-chat", () => ({
  Bubble: {
    Spin: () => <div data-testid="spin" />,
  },
  Markdown: ({ content }: { content: string }) => (
    <div data-testid="markdown">{content}</div>
  ),
  useProviderContext: () => ({
    getPrefixCls: (name: string) => `swe-${name}`,
  }),
}));

vi.mock("@agentscope-ai/icons", () => ({
  SparkCheckCircleFill: () => <span data-testid="success-icon" />,
  SparkDownLine: () => <span data-testid="chevron-down" />,
  SparkErrorCircleFill: () => <span data-testid="error-icon" />,
  SparkLoadingLine: () => <span data-testid="loading-icon" />,
  SparkLockFill: () => <span data-testid="lock-icon" />,
  SparkStopCircleLine: () => <span data-testid="stop-icon" />,
  SparkTimeLine: () => <span data-testid="time-icon" />,
  SparkTodoListLine: () => <span data-testid="todo-list-icon" />,
  SparkToolLine: () => <span data-testid="tool-icon" />,
  SparkUpLine: () => <span data-testid="chevron-up" />,
  SparkWarningCircleFill: () => <span data-testid="warning-icon" />,
}));

vi.mock("./style", () => ({
  default: () => null,
}));

vi.mock("./Message", () => ({
  default: ({ data }: { data: IAgentScopeRuntimeMessage }) => (
    <div data-testid="message">
      {data.content
        ?.map((content) =>
          content.type === AgentScopeRuntimeContentType.TEXT
            ? content.text
            : content.type,
        )
        .join("\n")}
    </div>
  ),
}));

vi.mock("./Reasoning", () => ({
  default: ({ data }: { data: IAgentScopeRuntimeMessage }) => (
    <div data-testid="reasoning">
      {data.content
        ?.map((content) =>
          content.type === AgentScopeRuntimeContentType.TEXT
            ? content.text
            : content.type,
        )
        .join("\n")}
    </div>
  ),
}));

vi.mock("./Tool", () => ({
  default: ({ data }: { data: IAgentScopeRuntimeMessage }) => (
    <div data-testid="tool">{data.id}</div>
  ),
}));

vi.mock("./Error", () => ({
  default: ({ data }: { data: IAgentScopeRuntimeMessage }) => (
    <div data-testid="error">{data.message || data.code || data.id}</div>
  ),
}));

vi.mock("./Actions", () => ({
  default: ({ hideReplace }: { hideReplace?: boolean }) => (
    <div data-testid="actions" data-hide-replace={String(Boolean(hideReplace))}>
      actions
    </div>
  ),
}));

vi.mock("./Suggestions", () => ({
  default: () => <div data-testid="suggestions" />,
}));

vi.mock("./RetryStatusMessage", () => ({
  default: ({ data }: { data: IAgentScopeRuntimeMessage }) => (
    <div data-testid="retry-status">{data.id}</div>
  ),
}));

afterEach(() => {
  cleanup();
});

function textMessage(
  id: string,
  text: string,
  type = AgentScopeRuntimeMessageType.MESSAGE,
  timestamp?: string,
): IAgentScopeRuntimeMessage {
  return {
    id,
    object: "message",
    role: AgentScopeRuntimeMessageRole.ASSISTANT,
    type,
    status: AgentScopeRuntimeRunStatus.Completed,
    content: [
      {
        object: "content",
        type: AgentScopeRuntimeContentType.TEXT,
        text,
        status: AgentScopeRuntimeRunStatus.Completed,
      },
    ],
    timestamp,
  } as IAgentScopeRuntimeMessage;
}

function response(
  output: IAgentScopeRuntimeMessage[],
  status = AgentScopeRuntimeRunStatus.Completed,
  completedAt?: number,
): IAgentScopeRuntimeResponse {
  return {
    id: "response-1",
    object: "response",
    status,
    created_at: 1,
    completed_at: completedAt,
    output,
  };
}

function toolMessage(
  id: string,
  status = AgentScopeRuntimeRunStatus.Completed,
  data: Record<string, unknown> = {},
  type = AgentScopeRuntimeMessageType.MCP_CALL,
  timestamp?: string,
): IAgentScopeRuntimeMessage {
  return {
    id,
    object: "message",
    role: AgentScopeRuntimeMessageRole.ASSISTANT,
    type,
    status,
    content: [
      {
        object: "content",
        type: AgentScopeRuntimeContentType.DATA,
        status,
        data: {
          tool_name: "query_customer",
          arguments: {},
          ...data,
        },
      },
    ],
    timestamp,
  } as IAgentScopeRuntimeMessage;
}

function groupedToolMessage(
  id: string,
  status = AgentScopeRuntimeRunStatus.InProgress,
  toolStatus: "running" | "success" | "failed" = "running",
): IAgentScopeRuntimeMessage {
  return toolMessage(id, status, {
    call_id: id,
    summary: "正在查看工作目录文件",
    tool_status: toolStatus,
    operation_group: { id: "inspect", title: "查看工作目录文件" },
  });
}

function errorMessage(id: string, message: string): IAgentScopeRuntimeMessage {
  return {
    id,
    object: "message",
    role: AgentScopeRuntimeMessageRole.ASSISTANT,
    type: AgentScopeRuntimeMessageType.ERROR,
    status: AgentScopeRuntimeRunStatus.Failed,
    content: [],
    code: "runtime_error",
    message,
  };
}

function getDisclosureBody() {
  return document.querySelector(".swe-response-process-disclosure-body");
}

describe("AgentScopeRuntimeResponseCard", () => {
  it("renders content before response actions", () => {
    const props = {
      data: response([textMessage("message-1", "正文")]),
      beforeActions: <div data-testid="plan-review">plan-review</div>,
    } as React.ComponentProps<typeof AgentScopeRuntimeResponseCard> & {
      beforeActions: React.ReactNode;
    };
    const { container } = render(<AgentScopeRuntimeResponseCard {...props} />);

    expect(container.textContent).toBe("正文plan-reviewactions");
  });

  it("renders fallback markdown when the final visible output is reasoning", () => {
    render(
      <AgentScopeRuntimeResponseCard
        data={response([
          textMessage("message-1", "前置正文"),
          textMessage(
            "reason-1",
            "最后被误归类到 Thinking 的正文",
            AgentScopeRuntimeMessageType.REASONING,
          ),
        ])}
      />,
    );

    expect(screen.getByTestId("markdown")).toHaveTextContent(
      "最后被误归类到 Thinking 的正文",
    );
  });

  it("does not render fallback markdown when normal body text is the final output", () => {
    render(
      <AgentScopeRuntimeResponseCard
        data={response([
          textMessage(
            "reason-1",
            "正常思考",
            AgentScopeRuntimeMessageType.REASONING,
          ),
          textMessage("message-1", "最终正文"),
        ])}
      />,
    );

    expect(screen.queryByTestId("markdown")).not.toBeInTheDocument();
  });

  it("collapses completed process content by default while showing the final answer", () => {
    render(
      <AgentScopeRuntimeResponseCard
        data={response([
          textMessage(
            "reason-1",
            "已完成的思考过程",
            AgentScopeRuntimeMessageType.REASONING,
          ),
          toolMessage("tool-1"),
          textMessage("message-1", "最终正文"),
        ])}
      />,
    );

    expect(screen.getByText("最终正文")).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: /展开执行过程 · 2 个步骤 · 工具调用 1 次/,
      }),
    ).toBeInTheDocument();
    expect(getDisclosureBody()).toHaveAttribute("hidden");
  });

  it("folds earlier answer text and process content while keeping only the final answer visible", () => {
    render(
      <AgentScopeRuntimeResponseCard
        data={response([
          textMessage("message-1", "正文 A"),
          textMessage(
            "reason-1",
            "执行过程 B",
            AgentScopeRuntimeMessageType.REASONING,
          ),
          textMessage("message-2", "正文 C"),
        ])}
      />,
    );

    expect(
      screen.getByRole("button", {
        name: /展开执行过程 · 1 个步骤/,
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("正文 C")).toBeVisible();
    expect(screen.getByText("正文 A")).not.toBeVisible();
    expect(screen.getByText("执行过程 B")).not.toBeVisible();
  });

  it("does not count folded intermediate answer text as a process step", () => {
    render(
      <AgentScopeRuntimeResponseCard
        data={response([
          textMessage("message-1", "中间正文"),
          textMessage("message-2", "最终正文"),
        ])}
      />,
    );

    const trigger = screen.getByRole("button", {
      name: "展开执行过程",
    });
    expect(trigger).toBeInTheDocument();
    expect(trigger).not.toHaveAccessibleName(/0 个步骤/);
    expect(screen.getByText("最终正文")).toBeVisible();
    expect(screen.getByText("中间正文")).not.toBeVisible();
  });

  it("does not show duration from response-level timestamps alone", () => {
    render(
      <AgentScopeRuntimeResponseCard
        data={response(
          [toolMessage("tool-1"), textMessage("message-1", "最终正文")],
          AgentScopeRuntimeRunStatus.Completed,
          19,
        )}
      />,
    );

    expect(
      screen.getByRole("button", {
        name: /展开执行过程 · 1 个步骤 · 工具调用 1 次$/,
      }),
    ).toBeInTheDocument();
  });

  it("shows duration from first and last message timestamps", () => {
    render(
      <AgentScopeRuntimeResponseCard
        data={response(
          [
            textMessage(
              "reason-1",
              "开始思考",
              AgentScopeRuntimeMessageType.REASONING,
              "2026-05-06 15:51:23.097",
            ),
            toolMessage(
              "tool-1",
              AgentScopeRuntimeRunStatus.Completed,
              {},
              AgentScopeRuntimeMessageType.MCP_CALL,
              "2026-05-06 15:51:26.097",
            ),
            textMessage(
              "reason-2",
              "结束思考",
              AgentScopeRuntimeMessageType.REASONING,
              "2026-05-06 15:51:34.047",
            ),
            textMessage(
              "message-1",
              "最终正文",
              AgentScopeRuntimeMessageType.MESSAGE,
              "2026-05-06 15:51:34.047",
            ),
          ],
          AgentScopeRuntimeRunStatus.Completed,
          1,
        )}
      />,
    );

    expect(
      screen.getByRole("button", {
        name: /展开执行过程 · 3 个步骤 · 工具调用 1 次 · 总耗时 11s/,
      }),
    ).toBeInTheDocument();
  });

  it("shows sub-second duration instead of zero seconds", () => {
    render(
      <AgentScopeRuntimeResponseCard
        data={response([
          toolMessage(
            "tool-1",
            AgentScopeRuntimeRunStatus.Completed,
            {},
            AgentScopeRuntimeMessageType.MCP_CALL,
            "2026-05-06 15:51:23.097",
          ),
          textMessage(
            "message-1",
            "最终正文",
            AgentScopeRuntimeMessageType.MESSAGE,
            "2026-05-06 15:51:23.197",
          ),
        ])}
      />,
    );

    expect(
      screen.getByRole("button", {
        name: /展开执行过程 · 1 个步骤 · 工具调用 1 次 · 总耗时 <1s/,
      }),
    ).toBeInTheDocument();
  });

  it("allows the user to expand and collapse process content", () => {
    render(
      <AgentScopeRuntimeResponseCard
        data={response([
          textMessage(
            "reason-1",
            "可查看的思考过程",
            AgentScopeRuntimeMessageType.REASONING,
          ),
          textMessage("message-1", "最终正文"),
        ])}
      />,
    );

    const trigger = screen.getByRole("button", {
      name: /展开执行过程 · 1 个步骤/,
    });
    fireEvent.click(trigger);

    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(getDisclosureBody()).not.toHaveAttribute("hidden");

    fireEvent.click(trigger);

    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(getDisclosureBody()).toHaveAttribute("hidden");
  });

  it("keeps generating process content visible", () => {
    render(
      <AgentScopeRuntimeResponseCard
        data={response(
          [
            textMessage(
              "reason-1",
              "正在思考",
              AgentScopeRuntimeMessageType.REASONING,
            ),
            toolMessage("tool-1", AgentScopeRuntimeRunStatus.InProgress),
          ],
          AgentScopeRuntimeRunStatus.InProgress,
        )}
      />,
    );

    expect(screen.queryByRole("button", { name: /执行过程/ })).toBeNull();
    expect(screen.getByText("正在思考")).toBeInTheDocument();
    expect(screen.getByText("tool-1")).toBeInTheDocument();
  });

  it("renders the first running operation group immediately", () => {
    render(
      <AgentScopeRuntimeResponseCard
        data={response(
          [groupedToolMessage("tool-group-1")],
          AgentScopeRuntimeRunStatus.InProgress,
        )}
      />,
    );

    expect(screen.queryByRole("button", { name: /执行过程/ })).toBeNull();
    expect(
      screen.getByRole("button", { name: /展开操作组：查看工作目录文件/ }),
    ).toBeVisible();
  });

  it("folds a completed operation group into process disclosure", () => {
    render(
      <AgentScopeRuntimeResponseCard
        data={response([
          groupedToolMessage(
            "tool-group-1",
            AgentScopeRuntimeRunStatus.Completed,
            "success",
          ),
          textMessage("message-1", "最终正文"),
        ])}
      />,
    );

    const processTrigger = screen.getByRole("button", {
      name: /展开执行过程 · 1 个步骤 · 工具调用 1 次/,
    });
    expect(screen.getByText("查看工作目录文件")).not.toBeVisible();

    fireEvent.click(processTrigger);
    expect(
      screen.getByRole("button", { name: /展开操作组：查看工作目录文件/ }),
    ).toBeVisible();
  });

  it("counts grouped reasoning and tools inside process disclosure", () => {
    render(
      <AgentScopeRuntimeResponseCard
        data={response([
          groupedToolMessage(
            "tool-group-1",
            AgentScopeRuntimeRunStatus.Completed,
            "success",
          ),
          textMessage(
            "reason-1",
            "继续确认目录内容",
            AgentScopeRuntimeMessageType.REASONING,
          ),
          groupedToolMessage(
            "tool-group-2",
            AgentScopeRuntimeRunStatus.Completed,
            "success",
          ),
          textMessage("message-1", "最终正文"),
        ])}
      />,
    );

    expect(
      screen.getByRole("button", {
        name: /展开执行过程 · 3 个步骤 · 工具调用 2 次/,
      }),
    ).toBeInTheDocument();
  });

  it("keeps approval requests visible when a final answer exists", () => {
    render(
      <AgentScopeRuntimeResponseCard
        data={response([
          toolMessage(
            "approval-1",
            AgentScopeRuntimeRunStatus.InProgress,
            { tool_name: "write_file" },
            AgentScopeRuntimeMessageType.MCP_APPROVAL_REQUEST,
          ),
          textMessage("message-1", "最终正文"),
        ])}
      />,
    );

    expect(screen.queryByRole("button", { name: /执行过程/ })).toBeNull();
    expect(screen.getByText("approval-1")).toBeInTheDocument();
    expect(screen.getByText("最终正文")).toBeInTheDocument();
  });

  it("keeps no-answer errors visible", () => {
    render(
      <AgentScopeRuntimeResponseCard
        data={response([errorMessage("error-1", "执行失败")])}
      />,
    );

    expect(screen.queryByRole("button", { name: /执行过程/ })).toBeNull();
    expect(screen.getByText("执行失败")).toBeInTheDocument();
  });

  it("keeps model_call_failed response errors directly visible with partial output", () => {
    render(
      <AgentScopeRuntimeResponseCard
        isLast
        data={{
          ...response(
            [textMessage("message-1", "partial answer")],
            AgentScopeRuntimeRunStatus.Failed,
          ),
          error: {
            code: "model_call_failed",
            message: "provider diagnostic",
          },
        }}
      />,
    );

    expect(screen.getByText("partial answer")).toBeInTheDocument();
    expect(screen.getByText("provider diagnostic")).toBeInTheDocument();
    expect(screen.getByTestId("actions")).toHaveAttribute(
      "data-hide-replace",
      "true",
    );
    expect(screen.queryByRole("button", { name: /执行过程/ })).toBeNull();
  });

  it("summarizes failed process when a final answer exists", () => {
    render(
      <AgentScopeRuntimeResponseCard
        data={response([
          toolMessage("tool-1", AgentScopeRuntimeRunStatus.Failed, {
            tool_status: "failed",
            tool_error: "查询失败",
          }),
          textMessage("message-1", "最终正文"),
        ])}
      />,
    );

    expect(
      screen.getByRole("button", {
        name: /展开执行过程 · 1 个步骤 · 工具调用 1 次 · 操作失败 1 次/,
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("最终正文")).toBeInTheDocument();
  });

  it("keeps fallback reasoning answer outside the process disclosure", () => {
    render(
      <AgentScopeRuntimeResponseCard
        data={response([
          textMessage("message-1", "前置正文"),
          textMessage(
            "reason-1",
            "最后被误归类到 Thinking 的正文",
            AgentScopeRuntimeMessageType.REASONING,
          ),
        ])}
      />,
    );

    expect(screen.getByTestId("markdown")).toHaveTextContent(
      "最后被误归类到 Thinking 的正文",
    );
    expect(
      screen.getByRole("button", { name: /展开执行过程 · 1 个步骤/ }),
    ).toBeInTheDocument();
    expect(screen.getByText("前置正文")).not.toBeVisible();
  });
});

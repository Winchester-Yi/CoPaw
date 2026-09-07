import { describe, expect, it, vi } from "vitest";
import Builder from "./Builder";
import {
  AgentScopeRuntimeContentType,
  AgentScopeRuntimeMessageType,
  AgentScopeRuntimeRunStatus,
  type IAgentScopeRuntimeMessage,
} from "../types";
import {
  OPERATION_GROUP_SAFE_TITLE,
  aggregateGroupStatus,
  extractOperationGroup,
  getToolStepKey,
  getToolStepStatus,
  groupOperationMessages,
} from "./operationGrouping";

vi.mock("@/components/agentscope-chat", () => ({ uuid: () => "test-uuid" }));

function toolMessage(options: {
  id: string;
  toolName?: string;
  group?: { id: string; title: string } | null;
  inputStatus?: string;
  outputStatus?: string;
  governance?: string;
  outputSummary?: string;
  summary?: string;
  callId?: string;
  messageStatus?: AgentScopeRuntimeRunStatus;
}): IAgentScopeRuntimeMessage {
  const toolName = options.toolName || "execute_shell_command";
  const inputData: Record<string, unknown> = {
    name: toolName,
    arguments: "{}",
    summary: options.summary || "开始执行操作",
  };
  if (options.callId) inputData.call_id = options.callId;
  if (options.group) {
    inputData.operation_group = options.group;
  }
  if (options.inputStatus) inputData.tool_status = options.inputStatus;

  const content: IAgentScopeRuntimeMessage["content"] = [
    {
      type: AgentScopeRuntimeContentType.DATA,
      status: AgentScopeRuntimeRunStatus.Completed,
      data: inputData,
    },
  ];
  if (options.outputStatus || options.governance || options.outputSummary) {
    const outputData: Record<string, unknown> = { name: toolName };
    if (options.outputStatus) outputData.tool_status = options.outputStatus;
    if (options.governance) {
      outputData.tool_governance = options.governance;
    }
    if (options.outputSummary)
      outputData.output_summary = options.outputSummary;
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
    status: options.messageStatus || AgentScopeRuntimeRunStatus.InProgress,
    content,
  };
}

function textMessage(id: string): IAgentScopeRuntimeMessage {
  return {
    id,
    object: "message",
    role: "assistant",
    type: AgentScopeRuntimeMessageType.MESSAGE,
    status: AgentScopeRuntimeRunStatus.InProgress,
    content: [
      {
        type: AgentScopeRuntimeContentType.TEXT,
        status: AgentScopeRuntimeRunStatus.Completed,
        text: "正在处理",
      },
    ],
  };
}

function emptyMessage(id: string): IAgentScopeRuntimeMessage {
  return {
    id,
    object: "message",
    role: "assistant",
    type: AgentScopeRuntimeMessageType.MESSAGE,
    status: AgentScopeRuntimeRunStatus.InProgress,
    content: [],
  };
}

function reasoningMessage(
  id: string,
  text = "正在判断下一步",
  status = AgentScopeRuntimeRunStatus.Completed,
): IAgentScopeRuntimeMessage {
  return {
    id,
    object: "message",
    role: "assistant",
    type: AgentScopeRuntimeMessageType.REASONING,
    status,
    content: [
      {
        type: AgentScopeRuntimeContentType.TEXT,
        status,
        text,
      },
    ],
  };
}

const GROUP_A = { id: "inspect", title: "检查图片" };
const GROUP_B = { id: "verify", title: "校验结果" };

describe("extractOperationGroup", () => {
  it("reads the backend-provided group from a tool message", () => {
    const message = toolMessage({ id: "t1", group: GROUP_A });
    expect(extractOperationGroup(message)).toEqual({
      id: "inspect",
      title: "检查图片",
      instanceKey: "inspect",
    });
  });

  it("returns null for messages without a declaration", () => {
    expect(extractOperationGroup(toolMessage({ id: "t1" }))).toBeNull();
    expect(extractOperationGroup(textMessage("m1"))).toBeNull();
  });

  it("falls back to the generic safe title", () => {
    const message = toolMessage({
      id: "t1",
      group: { id: "g1", title: "" },
    });
    expect(extractOperationGroup(message)?.title).toBe(
      OPERATION_GROUP_SAFE_TITLE,
    );
  });
});

describe("getToolStepStatus", () => {
  it("uses terminal output status when present", () => {
    const message = toolMessage({
      id: "t1",
      group: GROUP_A,
      inputStatus: "running",
      outputStatus: "failed",
    });
    expect(getToolStepStatus(message)).toBe("failed");
  });

  it("falls back to the running input status", () => {
    const message = toolMessage({ id: "t1", group: GROUP_A });
    expect(getToolStepStatus(message)).toBe("running");
  });

  it("prefers governance status over execution status", () => {
    const message = toolMessage({
      id: "t1",
      group: GROUP_A,
      inputStatus: "running",
      governance: "pending",
    });
    expect(getToolStepStatus(message)).toBe("pending");
  });

  it("maps message-level canceled to canceled", () => {
    const message = toolMessage({
      id: "t1",
      group: GROUP_A,
      messageStatus: AgentScopeRuntimeRunStatus.Canceled,
    });
    expect(getToolStepStatus(message)).toBe("canceled");
  });
});

describe("getToolStepKey", () => {
  it("uses the stable call id instead of the replaceable message id", () => {
    const message = toolMessage({
      id: "output-message-id",
      callId: "call-1",
      group: GROUP_A,
    });

    expect(getToolStepKey(message)).toBe("call-1");
  });
});

describe("aggregateGroupStatus", () => {
  it("returns success when every step succeeded", () => {
    const steps = [
      toolMessage({ id: "t1", group: GROUP_A, outputStatus: "success" }),
      toolMessage({ id: "t2", group: GROUP_A, outputStatus: "success" }),
    ];
    expect(aggregateGroupStatus(steps)).toBe("success");
  });

  it("returns failed when any step really failed", () => {
    const steps = [
      toolMessage({ id: "t1", group: GROUP_A, outputStatus: "success" }),
      toolMessage({ id: "t2", group: GROUP_A, outputStatus: "failed" }),
    ];
    expect(aggregateGroupStatus(steps)).toBe("failed");
  });

  it("returns pending when a step awaits approval and nothing failed", () => {
    const steps = [
      toolMessage({ id: "t1", group: GROUP_A, outputStatus: "success" }),
      toolMessage({ id: "t2", group: GROUP_A, governance: "pending" }),
    ];
    expect(aggregateGroupStatus(steps)).toBe("pending");
  });

  it("returns warning when only rejected/blocked steps exist", () => {
    const steps = [
      toolMessage({ id: "t1", group: GROUP_A, governance: "blocked" }),
      toolMessage({ id: "t2", group: GROUP_A, governance: "rejected" }),
    ];
    expect(aggregateGroupStatus(steps)).toBe("warning");
  });

  it("keeps a real failure above governance warnings", () => {
    const steps = [
      toolMessage({ id: "t1", group: GROUP_A, governance: "blocked" }),
      toolMessage({ id: "t2", group: GROUP_A, outputStatus: "failed" }),
    ];
    expect(aggregateGroupStatus(steps)).toBe("failed");
  });

  it("returns running while any step is still running", () => {
    const steps = [
      toolMessage({ id: "t1", group: GROUP_A, inputStatus: "running" }),
      toolMessage({ id: "t2", group: GROUP_A, outputStatus: "success" }),
    ];
    expect(aggregateGroupStatus(steps)).toBe("running");
  });

  it("ignores reasoning status when aggregating tool status", () => {
    const steps = [
      toolMessage({ id: "t1", group: GROUP_A, outputStatus: "success" }),
      reasoningMessage(
        "reason-1",
        "思考失败字样",
        AgentScopeRuntimeRunStatus.Failed,
      ),
    ];

    expect(aggregateGroupStatus(steps)).toBe("success");
  });
});

describe("groupOperationMessages", () => {
  it("renders a group from the first still-running tool call", () => {
    const message = toolMessage({
      id: "t1",
      group: GROUP_A,
      inputStatus: "running",
      messageStatus: AgentScopeRuntimeRunStatus.InProgress,
    });

    const { items, groups } = groupOperationMessages([message]);

    expect(groups).toHaveLength(1);
    expect(groups[0].steps).toEqual([message]);
    expect(items).toEqual([groups[0]]);
  });

  it("groups consecutive tool calls sharing the same explicit id", () => {
    const messages = [
      toolMessage({ id: "t1", group: GROUP_A }),
      toolMessage({ id: "t2", group: GROUP_A }),
      toolMessage({ id: "t3", group: GROUP_A }),
      textMessage("m1"),
    ];
    const { items, groups } = groupOperationMessages(messages);

    expect(groups).toHaveLength(1);
    expect(groups[0].steps).toHaveLength(3);
    expect(items).toHaveLength(2);
    expect(items[0]).toEqual(groups[0]);
    expect(items[1]).toMatchObject({ kind: "message", message: messages[3] });
  });

  it("splits groups on a user-facing text boundary (R4)", () => {
    const messages = [
      toolMessage({ id: "t1", group: GROUP_A }),
      textMessage("m1"),
      toolMessage({ id: "t2", group: GROUP_A }),
    ];
    const { groups } = groupOperationMessages(messages);

    expect(groups).toHaveLength(2);
    expect(groups[0].group.instanceKey).toBe("inspect:t1");
    expect(groups[1].group.instanceKey).toBe("inspect:t2");
    expect(groups[1].group.title).toBe("检查图片");
  });

  it("splits groups when the declared group id changes", () => {
    const messages = [
      toolMessage({ id: "t1", group: GROUP_A }),
      toolMessage({ id: "t2", group: GROUP_B }),
      toolMessage({ id: "t3", group: GROUP_A }),
    ];
    const { groups } = groupOperationMessages(messages);

    expect(groups).toHaveLength(3);
  });

  it("keeps multiple reasoning messages between same-group tools", () => {
    const firstTool = toolMessage({ id: "t1", group: GROUP_A });
    const firstReasoning = reasoningMessage("reason-1", "先检查目录");
    const secondReasoning = reasoningMessage("reason-2", "再检查文件");
    const secondTool = toolMessage({ id: "t2", group: GROUP_A });

    const { groups } = groupOperationMessages([
      firstTool,
      firstReasoning,
      secondReasoning,
      secondTool,
    ]);

    expect(groups).toHaveLength(1);
    expect(groups[0].steps).toEqual([
      firstTool,
      firstReasoning,
      secondReasoning,
      secondTool,
    ]);
  });

  it("ignores an invisible assistant boundary after reasoning", () => {
    const firstTool = toolMessage({ id: "t1", group: GROUP_A });
    const reasoning = reasoningMessage("reason-1", "继续执行同一阶段");
    const invisibleBoundary = emptyMessage("assistant-boundary-1");
    const secondTool = toolMessage({ id: "t2", group: GROUP_A });

    const { items, groups } = groupOperationMessages([
      firstTool,
      reasoning,
      invisibleBoundary,
      secondTool,
    ]);

    expect(groups).toHaveLength(1);
    expect(items).toEqual([groups[0]]);
    expect(groups[0].steps).toEqual([firstTool, reasoning, secondTool]);
  });

  it.each([
    AgentScopeRuntimeRunStatus.Created,
    AgentScopeRuntimeRunStatus.InProgress,
    AgentScopeRuntimeRunStatus.Completed,
  ])("ignores empty and whitespace-only assistant content in %s", (status) => {
    for (const texts of [[], [""], ["\n\n\n", ""], [" \t\r\n"]]) {
      const boundary = emptyMessage("blank");
      boundary.status = status;
      boundary.content = texts.map((text) => ({
        type: AgentScopeRuntimeContentType.TEXT,
        status,
        text,
      }));
      const { items, groups } = groupOperationMessages([
        toolMessage({ id: "t1", group: GROUP_A }),
        boundary,
        toolMessage({ id: "t2", group: GROUP_A }),
      ]);
      expect(groups).toHaveLength(1);
      expect(items).toHaveLength(1);
    }
  });

  it.each(["user", "system"])(
    "preserves empty %s message boundaries",
    (role) => {
      const boundary = { ...emptyMessage("boundary"), role };
      expect(
        groupOperationMessages([
          toolMessage({ id: "t1", group: GROUP_A }),
          boundary,
          toolMessage({ id: "t2", group: GROUP_A }),
        ]).groups,
      ).toHaveLength(2);
    },
  );

  it.each([
    AgentScopeRuntimeRunStatus.Failed,
    AgentScopeRuntimeRunStatus.Canceled,
    AgentScopeRuntimeRunStatus.Rejected,
  ])("preserves an empty %s message", (status) => {
    const boundary = { ...emptyMessage("boundary"), status };
    expect(
      groupOperationMessages([
        toolMessage({ id: "t1", group: GROUP_A }),
        boundary,
        toolMessage({ id: "t2", group: GROUP_A }),
      ]).groups,
    ).toHaveLength(2);
  });

  it.each([
    AgentScopeRuntimeContentType.IMAGE,
    AgentScopeRuntimeContentType.FILE,
    AgentScopeRuntimeContentType.REFUSAL,
    AgentScopeRuntimeContentType.DATA,
  ])("preserves %s blocks even without text", (type) => {
    const boundary = emptyMessage("boundary");
    boundary.content = [
      { type, status: AgentScopeRuntimeRunStatus.Completed },
    ] as IAgentScopeRuntimeMessage["content"];
    expect(
      groupOperationMessages([
        toolMessage({ id: "t1", group: GROUP_A }),
        boundary,
        toolMessage({ id: "t2", group: GROUP_A }),
      ]).groups,
    ).toHaveLength(2);
  });

  it.each(["approval_action", "retry_status", "plan_interaction_card"])(
    "preserves empty messages with direct or history-nested %s",
    (key) => {
      for (const metadata of [{ [key]: true }, { metadata: { [key]: true } }]) {
        const boundary = { ...emptyMessage("boundary"), metadata };
        const { items, groups } = groupOperationMessages([
          toolMessage({ id: "t1", group: GROUP_A }),
          boundary,
          toolMessage({ id: "t2", group: GROUP_A }),
        ]);
        expect(groups).toHaveLength(2);
        expect(items[1]).toEqual({ kind: "message", message: boundary });
      }
    },
  );

  it("keeps the SSE null -> whitespace -> completed boundary invisible", () => {
    const builder = new Builder({
      id: "response",
      status: AgentScopeRuntimeRunStatus.InProgress,
      created_at: 1,
    });
    const firstTool = toolMessage({
      id: "glob",
      callId: "call-glob",
      toolName: "glob_search",
      group: GROUP_A,
    });
    builder.handle(firstTool);
    builder.handle(reasoningMessage("reason"));
    const project = () =>
      groupOperationMessages(Builder.mergeToolMessages(builder.data.output));
    builder.handle({
      ...emptyMessage("blank"),
      content: null,
    } as unknown as IAgentScopeRuntimeMessage);
    expect(project().items).toHaveLength(1);
    builder.handle({
      object: "content",
      type: AgentScopeRuntimeContentType.TEXT,
      status: AgentScopeRuntimeRunStatus.InProgress,
      delta: true,
      msg_id: "blank",
      text: "\n\n\n",
    });
    expect(project().items).toHaveLength(1);
    builder.handle({
      ...emptyMessage("blank"),
      status: AgentScopeRuntimeRunStatus.Completed,
      content: [
        {
          type: AgentScopeRuntimeContentType.TEXT,
          status: AgentScopeRuntimeRunStatus.Completed,
          text: "\n\n\n",
        },
        {
          type: AgentScopeRuntimeContentType.TEXT,
          status: AgentScopeRuntimeRunStatus.Completed,
          text: "",
        },
      ],
    });
    builder.handle(
      toolMessage({
        id: "read-profile",
        toolName: "read_file",
        group: GROUP_A,
      }),
    );
    builder.handle(
      toolMessage({ id: "read-agents", toolName: "read_file", group: GROUP_A }),
    );
    expect(project().groups).toHaveLength(1);
    expect(project().groups[0].steps.map((step) => step.id)).toEqual([
      "glob",
      "reason",
      "read-profile",
      "read-agents",
    ]);
    // A later real text update must restore the boundary, not stay suppressed.
    builder.handle(textMessage("blank"));
    expect(project().groups).toHaveLength(2);
  });

  it("keeps ungrouped tool messages as individual items (R16)", () => {
    const messages = [
      toolMessage({ id: "t1", group: GROUP_A }),
      toolMessage({ id: "t2" }),
      toolMessage({ id: "t3", group: GROUP_A }),
    ];
    const { items, groups } = groupOperationMessages(messages);

    expect(groups).toHaveLength(2);
    expect(items.filter((item) => item.kind === "group")).toHaveLength(2);
    expect(items.filter((item) => item.kind !== "group")).toEqual([
      { kind: "message", message: messages[1] },
    ]);
  });
});

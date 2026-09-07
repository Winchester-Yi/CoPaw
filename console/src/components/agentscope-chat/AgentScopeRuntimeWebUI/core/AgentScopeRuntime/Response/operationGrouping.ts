import type { IAgentScopeRuntimeMessage, IDataContent } from "../types";
import {
  AgentScopeRuntimeContentType,
  AgentScopeRuntimeMessageType,
  AgentScopeRuntimeRunStatus,
} from "../types";

/**
 * Tool call grouping presentation helpers.
 *
 * The backend attaches a validated operation_group object ({ id, title })
 * to tool call messages.  Consecutive tool messages that share the same
 * explicit group id form one default-collapsed operation group in the
 * Console.  Grouping is explicit only: no inference from time, adjacency
 * or tool type (R1), and messages without a declaration keep the legacy
 * individual-card rendering (R16).
 */

export type ToolStepStatus =
  | "running"
  | "success"
  | "failed"
  | "pending"
  | "rejected"
  | "blocked"
  | "canceled";

export type GroupSummaryStatus =
  | "running"
  | "success"
  | "failed"
  | "pending"
  | "warning"
  | "canceled";

export interface OperationGroupInfo {
  id: string;
  title: string;
  instanceKey: string;
}

export interface OperationGroupEntry {
  kind: "group";
  key: string;
  group: OperationGroupInfo;
  steps: IAgentScopeRuntimeMessage[];
}

export interface OperationGroupMessageItem {
  kind: "message";
  message: IAgentScopeRuntimeMessage;
}

export type OperationGroupedItem =
  | OperationGroupEntry
  | OperationGroupMessageItem;

export const OPERATION_GROUP_SAFE_TITLE = "任务操作";

const TOOL_MESSAGE_TYPES = new Set<AgentScopeRuntimeMessageType>([
  AgentScopeRuntimeMessageType.FUNCTION_CALL,
  AgentScopeRuntimeMessageType.FUNCTION_CALL_OUTPUT,
  AgentScopeRuntimeMessageType.PLUGIN_CALL,
  AgentScopeRuntimeMessageType.PLUGIN_CALL_OUTPUT,
  AgentScopeRuntimeMessageType.MCP_CALL,
  AgentScopeRuntimeMessageType.MCP_CALL_OUTPUT,
  AgentScopeRuntimeMessageType.COMPONENT_CALL,
  AgentScopeRuntimeMessageType.COMPONENT_CALL_OUTPUT,
]);

const GOVERNANCE_STATUSES: ToolStepStatus[] = [
  "pending",
  "rejected",
  "blocked",
];

const TOOL_STATUSES: ToolStepStatus[] = ["running", "success", "failed"];

export function isOperationGroupToolMessage(
  message: IAgentScopeRuntimeMessage,
): boolean {
  return TOOL_MESSAGE_TYPES.has(message.type);
}

function isReasoningMessage(message: IAgentScopeRuntimeMessage): boolean {
  return message.type === AgentScopeRuntimeMessageType.REASONING;
}

function hasBoundaryMetadata(value: unknown): boolean {
  if (!value || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  return (
    Boolean(
      record.approval_action ||
        record.retry_status ||
        record.plan_interaction_card,
    ) || hasBoundaryMetadata(record.metadata)
  );
}

function isInvisibleAssistantBoundary(
  message: IAgentScopeRuntimeMessage,
): boolean {
  if (
    message.type !== AgentScopeRuntimeMessageType.MESSAGE ||
    message.role !== "assistant" ||
    ![
      AgentScopeRuntimeRunStatus.Created,
      AgentScopeRuntimeRunStatus.InProgress,
      AgentScopeRuntimeRunStatus.Completed,
    ].includes(message.status) ||
    message.code ||
    message.message ||
    hasBoundaryMetadata(message)
  )
    return false;

  // A reasoning boundary can complete with newline-only text blocks.
  return (message.content ?? []).every(
    (block) =>
      block.type === AgentScopeRuntimeContentType.TEXT &&
      typeof block.text === "string" &&
      !block.text.trim(),
  );
}

function isToolStatus(value: unknown): value is ToolStepStatus {
  return (
    typeof value === "string" && TOOL_STATUSES.includes(value as ToolStepStatus)
  );
}

function isGovernance(
  value: unknown,
): value is "pending" | "rejected" | "blocked" {
  return (
    typeof value === "string" &&
    GOVERNANCE_STATUSES.includes(value as ToolStepStatus)
  );
}

function dataBlocks(message: IAgentScopeRuntimeMessage): IDataContent[] {
  return (message.content || []).filter(
    (content): content is IDataContent =>
      content.type === AgentScopeRuntimeContentType.DATA,
  );
}

function blockData(message: IAgentScopeRuntimeMessage, index: number) {
  const data = dataBlocks(message)[index]?.data;
  return data && typeof data === "object" ? data : undefined;
}

export function extractOperationGroup(
  message: IAgentScopeRuntimeMessage,
): OperationGroupInfo | null {
  if (!isOperationGroupToolMessage(message)) return null;
  const data = blockData(message, 0);
  const group = data?.operation_group;
  if (!group || typeof group !== "object") return null;
  const raw = group as Record<string, unknown>;
  const id = typeof raw.id === "string" ? raw.id.trim() : "";
  if (!id) return null;
  const title =
    typeof raw.title === "string" && raw.title.trim()
      ? raw.title.trim()
      : OPERATION_GROUP_SAFE_TITLE;
  return { id, title, instanceKey: id };
}

export function getToolStepStatus(
  message: IAgentScopeRuntimeMessage,
): ToolStepStatus {
  const blocks = dataBlocks(message);
  const terminalData = blocks[1]?.data;
  const inputData = blocks[0]?.data;

  for (const data of [terminalData, inputData]) {
    if (!data || typeof data !== "object") continue;
    const record = data as Record<string, unknown>;
    if (isGovernance(record.tool_governance)) return record.tool_governance;
    if (isToolStatus(record.tool_status)) return record.tool_status;
  }

  switch (message.status) {
    case AgentScopeRuntimeRunStatus.InProgress:
    case AgentScopeRuntimeRunStatus.Created:
      return "running";
    case AgentScopeRuntimeRunStatus.Completed:
      return "success";
    case AgentScopeRuntimeRunStatus.Failed:
      return "failed";
    case AgentScopeRuntimeRunStatus.Canceled:
      return "canceled";
    case AgentScopeRuntimeRunStatus.Rejected:
      return "rejected";
    default:
      return "running";
  }
}

export function getToolStepKey(message: IAgentScopeRuntimeMessage): string {
  for (const data of [blockData(message, 0), blockData(message, 1)]) {
    if (data && typeof data === "object") {
      const record = data as Record<string, unknown>;
      const key = record.call_id || record.id || record.tool_call_id;
      if (typeof key === "string" && key) return key;
    }
  }
  return message.id;
}

const SUMMARY_PRECEDENCE: GroupSummaryStatus[] = [
  "failed",
  "pending",
  "running",
  "warning",
  "canceled",
  "success",
];

function toSummaryStatus(status: ToolStepStatus): GroupSummaryStatus {
  if (status === "rejected" || status === "blocked") return "warning";
  return status;
}

export function aggregateGroupStatus(
  steps: IAgentScopeRuntimeMessage[],
): GroupSummaryStatus {
  let best: GroupSummaryStatus = "success";
  for (const step of steps) {
    if (!isOperationGroupToolMessage(step)) continue;
    const summary = toSummaryStatus(getToolStepStatus(step));
    if (
      SUMMARY_PRECEDENCE.indexOf(summary) < SUMMARY_PRECEDENCE.indexOf(best)
    ) {
      best = summary;
    }
  }
  return best;
}

export function groupOperationMessages(messages: IAgentScopeRuntimeMessage[]): {
  items: OperationGroupedItem[];
  groups: OperationGroupEntry[];
} {
  const items: OperationGroupedItem[] = [];
  const groups: OperationGroupEntry[] = [];
  let openSteps: IAgentScopeRuntimeMessage[] = [];
  let openGroupId = "";

  const flush = () => {
    if (openSteps.length === 0) return;
    const first = openSteps[0];
    const info = extractOperationGroup(first);
    if (!info) {
      for (const step of openSteps) {
        items.push({ kind: "message", message: step });
      }
      openSteps = [];
      openGroupId = "";
      return;
    }
    const instanceKey = info.id + ":" + getToolStepKey(first);
    const entry: OperationGroupEntry = {
      kind: "group",
      key: instanceKey,
      group: { ...info, instanceKey },
      steps: openSteps,
    };
    groups.push(entry);
    items.push(entry);
    openSteps = [];
    openGroupId = "";
  };

  for (const message of messages) {
    const groupInfo = isOperationGroupToolMessage(message)
      ? extractOperationGroup(message)
      : null;
    if (groupInfo) {
      if (openSteps.length === 0) {
        openGroupId = groupInfo.id;
        openSteps.push(message);
      } else if (openGroupId === groupInfo.id) {
        openSteps.push(message);
      } else {
        flush();
        openGroupId = groupInfo.id;
        openSteps.push(message);
      }
      continue;
    }
    if (openSteps.length > 0 && isReasoningMessage(message)) {
      openSteps.push(message);
      continue;
    }
    if (openSteps.length > 0 && isInvisibleAssistantBoundary(message)) {
      continue;
    }
    // User-facing text, errors and ungrouped tool calls close the open group.
    // Reasoning and its invisible assistant boundary are handled above.
    flush();
    items.push({ kind: "message", message });
  }
  flush();

  return { items, groups };
}

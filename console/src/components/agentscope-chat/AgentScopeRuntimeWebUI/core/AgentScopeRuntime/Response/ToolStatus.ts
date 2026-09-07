import { AgentScopeRuntimeRunStatus } from "../types";

type ToolStatusValue = "running" | "success" | "failed";
type ToolGovernanceValue = "pending" | "rejected" | "blocked";

type ToolStatusData = {
  tool_status?: unknown;
  tool_governance?: unknown;
  [key: string]: unknown;
};

type ResolveToolStatusOptions = {
  messageStatus: AgentScopeRuntimeRunStatus;
  hasOutputContent?: boolean;
  inputData?: ToolStatusData;
  outputData?: ToolStatusData;
};

function getToolGovernance(
  data?: ToolStatusData,
): ToolGovernanceValue | undefined {
  const governance = data?.tool_governance;
  if (
    governance === "pending" ||
    governance === "rejected" ||
    governance === "blocked"
  ) {
    return governance;
  }
  return undefined;
}

function getToolStatus(data?: ToolStatusData): ToolStatusValue | undefined {
  const status = data?.tool_status;
  if (status === "running" || status === "success" || status === "failed") {
    return status;
  }
  return undefined;
}

export function resolveToolGovernanceStatus({
  hasOutputContent = false,
  inputData,
  outputData,
}: Omit<ResolveToolStatusOptions, "messageStatus">):
  | ToolGovernanceValue
  | undefined {
  return (
    getToolGovernance(outputData) ||
    (hasOutputContent ? undefined : getToolGovernance(inputData))
  );
}

export function resolveToolMessageStatus({
  messageStatus,
  hasOutputContent = false,
  inputData,
  outputData,
}: ResolveToolStatusOptions): AgentScopeRuntimeRunStatus {
  const outputToolStatus = getToolStatus(outputData);
  const inputToolStatus = hasOutputContent
    ? undefined
    : getToolStatus(inputData);
  const governance = resolveToolGovernanceStatus({
    hasOutputContent,
    inputData,
    outputData,
  });
  const toolStatus = governance || outputToolStatus || inputToolStatus;

  switch (toolStatus) {
    case "running":
    case "pending":
      return AgentScopeRuntimeRunStatus.InProgress;
    case "failed":
      return AgentScopeRuntimeRunStatus.Failed;
    case "rejected":
    case "blocked":
      return AgentScopeRuntimeRunStatus.Rejected;
    case "success":
      return AgentScopeRuntimeRunStatus.Completed;
    default:
      return messageStatus;
  }
}

export function isToolMessageLoading(status?: string) {
  return status === AgentScopeRuntimeRunStatus.InProgress;
}

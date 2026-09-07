import React from "react";
import { IAgentScopeRuntimeMessage, IDataContent } from "../types";
import { ToolCall } from "@/components/agentscope-chat";
import { useChatAnywhereOptions } from "../../Context/ChatAnywhereOptionsContext";
import Approval from "./Approval";
import {
  buildToolTitle,
  getToolDisplayName,
  resolveServerLabel,
  resolveToolName,
} from "./ToolTitle";
import {
  isToolMessageLoading,
  resolveToolGovernanceStatus,
  resolveToolMessageStatus,
} from "./ToolStatus";

const HIDDEN_TOOL_NAMES = new Set(["update_task_progress"]);

const Tool = React.memo(function ({
  data,
  isApproval = false,
}: {
  data: IAgentScopeRuntimeMessage;
  isApproval?: boolean;
}) {
  const customToolRenderConfig =
    useChatAnywhereOptions((v) => v.customToolRenderConfig) || {};

  if (!data.content?.length) return null;
  const content = data.content as IDataContent<{
    name: string;
    server_label?: string;
    arguments: Record<string, any>;
    output: Record<string, any>;
    summary?: string;
    output_summary?: string;
    tool_status?: "running" | "success" | "failed";
    tool_error?: string | null;
    live_output?: string;
    live_output_truncated?: boolean;
  }>[];
  const inputData = (content[0]?.data || {}) as Record<string, any>;
  const outputData = (content[1]?.data || {}) as Record<string, any>;
  const statusOptions = {
    messageStatus: data.status,
    hasOutputContent: content.length > 1,
    inputData,
    outputData,
  };
  const governanceStatus = resolveToolGovernanceStatus(statusOptions);
  const msgStatus = resolveToolMessageStatus(statusOptions);
  const loading =
    governanceStatus === undefined && isToolMessageLoading(msgStatus);
  const displayStatus = governanceStatus || msgStatus;
  const toolName = resolveToolName(inputData) || resolveToolName(outputData);
  if (HIDDEN_TOOL_NAMES.has(toolName)) return null;

  const serverLabel =
    resolveServerLabel(inputData) || resolveServerLabel(outputData);
  const defaultTitle = getToolDisplayName(toolName, serverLabel);
  const input = inputData.arguments ?? outputData.arguments;
  const summary = inputData.summary ?? outputData.summary;
  const output = outputData.output ?? inputData.live_output ?? inputData.output;
  const outputSummary =
    outputData.output_summary ??
    inputData.output_summary ??
    (inputData.live_output_truncated ? "早期实时输出已省略" : undefined);
  const title = buildToolTitle({
    loading,
    toolName,
    defaultTitle,
    input,
    summary,
  });

  let node;
  const renderedData =
    msgStatus === data.status ? data : { ...data, status: msgStatus };

  if (customToolRenderConfig[toolName]) {
    const C = customToolRenderConfig[toolName];
    node = <C data={renderedData} />;
  } else {
    node = (
      <ToolCall
        loading={loading}
        msgStatus={displayStatus}
        defaultOpen={false}
        title={title}
        input={input}
        output={output}
        outputSummary={outputSummary}
      ></ToolCall>
    );
  }

  return (
    <>
      {node}
      {isApproval && <Approval data={data} />}
    </>
  );
});

export default Tool;

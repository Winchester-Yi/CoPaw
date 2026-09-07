import { memo, useState } from "react";
import type { ReactNode } from "react";
import {
  SparkCheckCircleFill,
  SparkDownLine,
  SparkErrorCircleFill,
  SparkLoadingLine,
  SparkStopCircleLine,
  SparkTimeLine,
  SparkUpLine,
  SparkWarningCircleFill,
} from "@agentscope-ai/icons";
import { useProviderContext } from "@/components/agentscope-chat";
import Style from "./style";
import Reasoning from "./Reasoning";
import Tool from "./Tool";
import type {
  GroupSummaryStatus,
  OperationGroupEntry,
} from "./operationGrouping";
import {
  aggregateGroupStatus,
  getToolStepKey,
  isOperationGroupToolMessage,
} from "./operationGrouping";

function getGroupStatusIcon(
  status: GroupSummaryStatus,
): (props: { spin?: boolean }) => ReactNode {
  switch (status) {
    case "running":
      return SparkLoadingLine;
    case "pending":
      return SparkTimeLine;
    case "warning":
      return SparkWarningCircleFill;
    case "failed":
      return SparkErrorCircleFill;
    case "canceled":
      return SparkStopCircleLine;
    default:
      return SparkCheckCircleFill;
  }
}

const GROUP_STATUS_LABEL: Record<GroupSummaryStatus, string> = {
  running: "执行中",
  success: "成功",
  failed: "失败",
  pending: "待审批",
  warning: "治理警告",
  canceled: "已取消",
};

function OperationGroupComponent({ entry }: { entry: OperationGroupEntry }) {
  const { getPrefixCls } = useProviderContext();
  const prefixCls = getPrefixCls("response-operation-group");
  const [open, setOpen] = useState(false);
  const summary = aggregateGroupStatus(entry.steps);
  const Icon = getGroupStatusIcon(summary);
  const statusLabel = GROUP_STATUS_LABEL[summary];

  const toggle = () => {
    setOpen((current) => !current);
  };

  return (
    <>
      <Style />
      <div className={prefixCls}>
        <button
          type="button"
          className={prefixCls + "-trigger"}
          aria-expanded={open}
          aria-label={
            (open ? "收起" : "展开") +
            "操作组：" +
            entry.group.title +
            "，" +
            statusLabel
          }
          data-status={summary}
          onClick={toggle}
        >
          <span
            className={prefixCls + "-icon"}
            data-status={summary}
            aria-hidden="true"
          >
            <Icon spin={summary === "running"} />
          </span>
          <span className={prefixCls + "-title"}>{entry.group.title}</span>
          <span className={prefixCls + "-chevron"} aria-hidden="true">
            {open ? <SparkUpLine /> : <SparkDownLine />}
          </span>
        </button>
        <div
          className={
            prefixCls + "-body" + (open ? " " + prefixCls + "-body-open" : "")
          }
          hidden={!open}
          role="list"
        >
          {entry.steps.map((message) => {
            const key = entry.key + ":" + getToolStepKey(message);
            if (isOperationGroupToolMessage(message)) {
              return (
                <div key={key} className={prefixCls + "-tool"} role="listitem">
                  <Tool data={message} />
                </div>
              );
            }
            return (
              <div
                key={key}
                className={prefixCls + "-reasoning"}
                role="listitem"
              >
                <Reasoning data={message} />
              </div>
            );
          })}
        </div>
      </div>
    </>
  );
}

const OperationGroup = memo(OperationGroupComponent);
export default OperationGroup;

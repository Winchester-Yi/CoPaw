import { useState } from "react";
import { Popover, Progress } from "antd";
import type {
  ContextUsageAvailableSnapshot,
  ContextUsageSnapshot,
  ContextUsageStatus,
} from "@/api/types/contextUsage";
import styles from "./index.module.less";

const STATUS_LABELS: Record<ContextUsageStatus, string> = {
  normal: "正常",
  governance: "接近治理阈值",
  active: "已进入压缩区间",
  emergency: "紧急",
  overflow: "已超出上限",
};
export interface ContextUsageIndicatorProps {
  snapshot?: ContextUsageSnapshot;
  error: boolean;
  refresh: () => void;
}

function formatCompactTokens(value: number): string {
  const compact = (divisor: number, suffix: string) => {
    const scaled = value / divisor;
    const digits = scaled >= 100 ? 0 : scaled >= 10 ? 1 : 2;
    return `${Number(scaled.toFixed(digits))}${suffix}`;
  };

  if (value >= 1_000_000) return compact(1_000_000, "M");
  if (value >= 1_000) return compact(1_000, "K");
  return String(value);
}

function formatFullTokens(value: number): string {
  return value.toLocaleString("en-US");
}

function getPercentage(snapshot: ContextUsageAvailableSnapshot): number {
  return Math.max(0, Math.round(snapshot.usage_ratio * 100));
}

function ContextUsageDetails({
  error,
  snapshot,
}: {
  error: boolean;
  snapshot: ContextUsageAvailableSnapshot;
}) {
  const percentage = getPercentage(snapshot);
  const progressPercentage = Math.min(percentage, 100);
  const categories = [
    ["系统上下文", snapshot.system_context_tokens],
    ["工具定义", snapshot.tool_definition_tokens],
    ["对话消息", snapshot.conversation_tokens],
  ] as const;

  return (
    <div className={styles.details} role="dialog" aria-label="上下文占用详情">
      <div className={styles.header}>
        <div>
          <div className={styles.title}>上下文占用</div>
          <div className={styles.total}>
            约 {formatCompactTokens(snapshot.used_tokens)} /{" "}
            {formatCompactTokens(snapshot.max_tokens)} · {percentage}%
          </div>
        </div>
        <span className={`${styles.status} ${styles[snapshot.status]}`}>
          {STATUS_LABELS[snapshot.status]}
        </span>
      </div>
      <div
        className={styles.progress}
        role="progressbar"
        aria-label="上下文占用"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progressPercentage}
      >
        <Progress
          percent={progressPercentage}
          showInfo={false}
          strokeColor="currentColor"
          trailColor="#e8edf5"
          aria-hidden="true"
        />
      </div>
      <div className={styles.remaining}>
        剩余约 {formatCompactTokens(snapshot.remaining_tokens)}
      </div>
      <div className={styles.categories}>
        {categories.map(([label, value]) => {
          const compactValue = formatCompactTokens(value);
          const fullValue = formatFullTokens(value);
          return (
            <div className={styles.category} key={label}>
              <span>{label}</span>
              <span className={styles.value}>
                ~{compactValue}
                {compactValue !== fullValue ? (
                  <span className={styles.fullValue}>{fullValue}</span>
                ) : null}
              </span>
            </div>
          );
        })}
      </div>
      {snapshot.stale ? (
        <p className={styles.notice}>正在生成，显示上次保存结果。</p>
      ) : null}
      {error ? (
        <p className={styles.notice}>刷新失败，继续显示上次结果。</p>
      ) : null}
    </div>
  );
}

export default function ContextUsageIndicator({
  snapshot,
  error,
}: ContextUsageIndicatorProps) {
  const [open, setOpen] = useState(false);
  if (!snapshot?.available) return null;

  const percentage = getPercentage(snapshot);
  const statusLabel = STATUS_LABELS[snapshot.status];
  const ringPercentage = Math.min(100, Math.max(0, snapshot.usage_ratio * 100));

  return (
    <Popover
      content={<ContextUsageDetails snapshot={snapshot} error={error} />}
      trigger={["hover", "click"]}
      placement="topRight"
      open={open}
      onOpenChange={setOpen}
      overlayClassName={styles.popover}
    >
      <button
        type="button"
        className={`${styles.trigger} ${styles[snapshot.status]}`}
        aria-label={`上下文占用 ${percentage}%，状态${statusLabel}`}
        aria-haspopup="dialog"
        aria-expanded={open}
      >
        <svg
          className={styles.ring}
          viewBox="0 0 20 20"
          aria-hidden="true"
          focusable="false"
        >
          <circle className={styles.ringTrack} cx="10" cy="10" r="7" />
          <circle
            className={styles.ringFill}
            cx="10"
            cy="10"
            r="7"
            pathLength="100"
            strokeDasharray={`${ringPercentage} 100`}
            transform="rotate(-90 10 10)"
          />
        </svg>
      </button>
    </Popover>
  );
}

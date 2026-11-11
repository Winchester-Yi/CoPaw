import { Button, Tag, Tooltip, Typography } from "antd";
import { CopyOutlined } from "@ant-design/icons";
import type { ManagedJob } from "@/api/types/schedulerCron";
import { isBroadcastChildJob } from "../CronJobs/components/columns";
import { beijingRunTime, RESULT_LABELS } from "./presentation";
import { copyToClipboard } from "@/utils/clipboard";
import styles from "./management.module.less";

export function TaskIdentity({
  job,
  onCopy,
}: {
  job: ManagedJob;
  onCopy: (success: boolean) => void;
}) {
  const text = (value: string, label: string) => (
    <div className={styles.identityRow}>
      <Typography.Text
        className={styles.identityText}
        ellipsis={{ tooltip: <span className={styles.fullText}>{value}</span> }}
      >
        {value}
      </Typography.Text>
      <Button
        type="text"
        size="small"
        icon={<CopyOutlined />}
        aria-label={`复制${label}`}
        onClick={() => void copyToClipboard(value).then(onCopy)}
      />
    </div>
  );
  return (
    <div className={styles.identity}>
      {text(job.name, "任务名称")}
      <div className={styles.identifier}>{text(job.id, "任务ID")}</div>
      {isBroadcastChildJob(job) ? (
        <Tag color="blue">分发子任务</Tag>
      ) : job.has_distributed_children ? (
        <Tag>父任务</Tag>
      ) : null}
    </div>
  );
}

export function ExecutionResult({ job }: { job: ManagedJob }) {
  const code = job.task?.is_running
    ? "running"
    : job.state?.last_status || "idle";
  const label = RESULT_LABELS[code] ?? code;
  const content =
    code === "idle" ? (
      "暂无执行记录"
    ) : (
      <div className={styles.fullText}>
        <div>
          最近执行时间：{beijingRunTime(job.state?.last_run_at)}（北京时间）
        </div>
        <div>执行结果：{label}</div>
        {code === "error" && (
          <div>
            错误详情：
            {job.state?.last_error?.trim() || "暂无失败详情，请查看执行日志"}
          </div>
        )}
      </div>
    );
  return (
    <Tooltip title={content}>
      <span className={styles.result}>
        <span
          className={`${styles.dot} ${
            code === "error" || code === "cancelled"
              ? styles.error
              : code === "success"
              ? styles.success
              : code === "running"
              ? styles.processing
              : ""
          }`}
        />
        {label}
      </span>
    </Tooltip>
  );
}

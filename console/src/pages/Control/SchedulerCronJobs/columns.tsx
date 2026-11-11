import { Tag, Tooltip } from "antd";
import type { ColumnsType, ColumnType } from "antd/es/table";
import type { MenuProps } from "antd";
import { Button, Dropdown, Modal } from "@agentscope-ai/design";
import { MoreOutlined, QuestionCircleOutlined } from "@ant-design/icons";
import type { TFunction } from "i18next";
import type { ManagedJob } from "@/api/types/schedulerCron";
import type { ExecutionModelOption } from "@/hooks/useExecutionModelOptions";
import { isBroadcastChildJob } from "../CronJobs/components/columns";
import { isBatchDispatchMode, MODE_LABELS, scheduleLabel } from "./helpers";
import { dateRangeLabel, isDatePause } from "./dateRange";
import { managementLabel, schedulingReasons } from "./presentation";
import { ExecutionResult, TaskIdentity } from "./ManagedJobCells";
import styles from "../CronJobs/index.module.less";
import layout from "./management.module.less";

export interface ManagedJobPanel {
  kind: "edit" | "broadcast" | "children" | "progress";
  job: ManagedJob;
}
interface ColumnOptions {
  t: TFunction;
  models: { options: ExecutionModelOption[]; tenantDefaultLabel: string };
  message: {
    success: (text: string) => unknown;
    error: (text: string) => unknown;
  };
  perform: (
    kind: "toggle" | "remove" | "run" | "pause",
    job: ManagedJob,
  ) => Promise<void>;
  setPanel: (panel: ManagedJobPanel) => void;
  busy: boolean;
}
const STATE_HELP =
  "可调度：当前策略允许自动调度，不代表正在运行或平台同步成功。\n未到生效日期／已过期：当前北京时间日期不在区间内。\n本任务已停用：仅本任务的开关关闭。\n自动暂停：例如未读结果过多。\n批入口已暂停：不再接受新批次，不代表所有成员都已停用。\n已删除：不再接受新执行，清理进度另查。多个限制会逐行显示。";

export function createManagedColumns({
  t,
  message,
  perform,
  setPanel,
  busy,
}: ColumnOptions): ColumnsType<ManagedJob> {
  const actions: ColumnType<ManagedJob> = {
    title: t("common.actions"),
    key: "action",
    fixed: "right",
    width: 235,
    render: (_, job) => {
      const child = isBroadcastChildJob(job);
      const items: MenuProps["items"] = [
        {
          key: "edit",
          label: "任务设置",
          disabled: job.deleted || busy,
          onClick: () => setPanel({ kind: "edit", job }),
        },
        { type: "divider" },
        ...(!child
          ? [
              {
                key: "broadcast",
                label: "分发",
                disabled: job.deleted || busy,
                onClick: () => setPanel({ kind: "broadcast", job }),
              },
              {
                key: "children",
                label: "子任务管理",
                onClick: () => setPanel({ kind: "children", job }),
              },
              { type: "divider" as const },
            ]
          : []),
        {
          key: "progress",
          label: "操作进度",
          onClick: () => setPanel({ kind: "progress", job }),
        },
        { type: "divider" },
        ...(!child
          ? [
              {
                key: "pause",
                label: "暂停任务及全部子任务",
                disabled: job.deleted || busy,
                onClick: () =>
                  Modal.confirm({
                    title: `暂停任务「${job.name}」及其子任务？`,
                    content:
                      "未开始的执行跳过，已运行的继续；若开启批调度，则暂停本组批入口并异步同步平台关闭。",
                    onOk: () => perform("pause", job),
                  }),
              },
            ]
          : []),
        {
          key: "delete",
          label: "删除",
          danger: true,
          disabled: job.deleted || busy,
          onClick: () =>
            Modal.confirm({
              title: `删除任务「${job.name}」？`,
              content: "先逻辑删除，已入队但未开始的执行将跳过，在途执行继续。",
              onOk: () => perform("remove", job),
            }),
        },
      ];
      return (
        <div className={styles.actionColumn}>
          <Button
            type="link"
            size="small"
            disabled={busy || job.deleted}
            onClick={() => void perform("toggle", job)}
          >
            {job.enabled &&
            (!job.task?.is_paused || isDatePause(job.task?.pause_reason))
              ? "停用本任务"
              : "启用本任务"}
          </Button>
          <Button
            type="link"
            size="small"
            disabled={
              busy ||
              job.deleted ||
              !job.enabled ||
              Boolean(job.task?.is_running || job.task?.is_paused)
            }
            onClick={() =>
              Modal.confirm({
                title: t("cronJobs.executeNowTitle"),
                content: t("cronJobs.executeNowContent", { name: job.name }),
                okText: t("cronJobs.executeNowConfirm"),
                cancelText: t("cronJobs.cancelText"),
                onOk: () => perform("run", job),
              })
            }
          >
            立即执行
          </Button>
          <Dropdown
            menu={{ items }}
            placement="bottomRight"
            trigger={["click"]}
          >
            <Button
              type="text"
              size="small"
              aria-label={`更多操作 ${job.name}`}
              icon={<MoreOutlined />}
            />
          </Dropdown>
        </div>
      );
    },
  };
  return [
    {
      title: "任务",
      key: "name",
      width: 270,
      fixed: "left",
      render: (_, job) => (
        <TaskIdentity
          job={job}
          onCopy={(ok) =>
            ok
              ? message.success(t("common.copied"))
              : message.error(t("common.copyFailed"))
          }
        />
      ),
    },
    {
      title: "可执行日期",
      key: "run_date_range",
      width: 200,
      render: (_, job) => (
        <Tooltip title="北京时间，包含首尾两天">
          <span className={layout.dateRange}>
            {dateRangeLabel(job.run_date_range)}
            {isBroadcastChildJob(job) && (
              <>
                <br />
                <Tag>继承父任务</Tag>
              </>
            )}
          </span>
        </Tooltip>
      ),
    },
    {
      title: "执行时间",
      key: "cron",
      dataIndex: ["schedule", "cron"],
      width: 180,
      render: (_, job) => (
        <Tooltip
          title={
            <div className={layout.fullText}>
              Cron 表达式：{job.schedule.cron}
              <br />
              时区：{job.schedule.timezone || "未记录"}
            </div>
          }
        >
          <span className={styles.cronText}>
            {isBatchDispatchMode(job.dispatch_state) && (
              <Tag color="processing">批调度</Tag>
            )}
            <span>{scheduleLabel(job.schedule.cron, t)}</span>
          </span>
        </Tooltip>
      ),
    },
    {
      title: (
        <span>
          当前调度状态
          <Tooltip
            title={<span className={layout.fullText}>{STATE_HELP}</span>}
          >
            <QuestionCircleOutlined
              tabIndex={0}
              aria-label="调度状态说明"
              className={layout.hint}
            />
          </Tooltip>
        </span>
      ),
      key: "enabled",
      width: 160,
      render: (_, job) => (
        <div className={layout.reasons}>
          {schedulingReasons(job).map((reason) => (
            <span key={reason}>{reason}</span>
          ))}
        </div>
      ),
    },
    {
      title: "调度方式",
      key: "dispatch_state",
      width: 130,
      render: (_, job) => MODE_LABELS[job.dispatch_state],
    },
    {
      title: "管理状态",
      key: "management_status",
      width: 130,
      render: (_, job) => (
        <Tooltip title={job.management_status}>
          <Button
            type="link"
            danger={job.management_status.endsWith("_failed")}
            onClick={() => setPanel({ kind: "progress", job })}
          >
            {managementLabel(job.management_status)}
          </Button>
        </Tooltip>
      ),
    },
    {
      title: "最近执行结果",
      key: "last_status",
      width: 130,
      render: (_, job) => <ExecutionResult job={job} />,
    },
    actions,
  ];
}

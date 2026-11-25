import { useCallback, useState } from "react";
import { validate as isUuid } from "uuid";
import {
  Alert,
  Button,
  Space,
  message,
  Input,
  Pagination,
  Select,
  Spin,
  Table,
  Tabs,
  Tooltip,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  managedBatchBoard,
  type ManagedBatch,
  type ManagedBatchEvent,
  type ManagedBatchMember,
  type ManagedRetryResult,
  type ManagedSkippedMember,
} from "../../../api/modules/managedBatchBoard";
import styles from "../CronBatchDispatch/index.module.less";
import { labels, statusTag, settledStatusTag, timeText } from "./format";
import hoverStyles from "./hoverDetails.module.less";
import { useBoardResource } from "./useBoardResource";
import FailurePanel from "./FailurePanel";
import PriorityOrder from "./PriorityOrder";
import MemberAttempts from "./MemberAttempts";
import { getBbkDisplayName } from "../../../constants/bbk";

const skipReasonLabels: Record<string, string> = {
  auto_unread_threshold: "未读过多自动暂停",
  manual: "手动停用",
  job_disabled: "任务未启用",
  job_paused: "任务已暂停",
  batch_paused: "批调度已暂停",
  job_deleted: "任务已删除",
  job_not_ready: "任务管理状态未就绪",
  job_not_in_batch_mode: "任务未开启批调度",
  date_before_start: "尚未到任务开始日期",
  date_expired: "已超过任务结束日期",
  date_configuration_invalid: "任务执行日期配置不完整",
};

const skippedColumns: ColumnsType<ManagedSkippedMember> = [
  {
    title: "角色 / 用户",
    width: 160,
    ellipsis: true,
    render: (_, item) =>
      `${item.member_role === "parent" ? "父任务" : "子任务"} · ${
        item.tenant_id
      }`,
  },
  {
    title: "任务",
    ellipsis: true,
    render: (_, item) => (
      <Tooltip title={`${item.job_name || item.job_id} · ${item.job_id}`}>
        <span>{item.job_name || item.job_id}</span>
      </Tooltip>
    ),
  },
  {
    title: "跳过原因",
    width: 178,
    render: (_, item) => skipReasonLabels[item.reason] ?? "其他原因",
  },
];

function eventLabel(event: ManagedBatchEvent) {
  if (event.event_type === "auto_retry_blocked") {
    if (event.details.reason === "manual") return "手动停用，重试未入队";
    if (event.details.reason === "auto_unread_threshold")
      return "未读暂停，重试未入队";
  }
  return event.event_type;
}

const memberColumns = (
  source: string,
  batchId: string,
): ColumnsType<ManagedBatchMember> => [
  {
    title: "顺位",
    dataIndex: "member_order",
    width: 60,
    render: (_, item) => <PriorityOrder item={item} />,
  },
  {
    title: "角色 / 用户",
    width: 145,
    render: (_, item) => (
      <div className={styles.stackCell}>
        <strong>
          {item.member_role === "parent" ? "父任务" : "子任务"} ·{" "}
          {item.tenant_id}
        </strong>
        <span>
          {item.bbk_id ? getBbkDisplayName(item.bbk_id) : "未记录分行"}
        </span>
      </div>
    ),
  },
  {
    title: "任务 / 执行",
    width: 220,
    render: (_, item) => (
      <div className={styles.stackCell}>
        <strong>{item.job_name || item.job_id}</strong>
        <Tooltip title={`定时任务 ID：${item.job_id}`}>
          <span>{item.job_id}</span>
        </Tooltip>
        <Tooltip title={item.execution_id} trigger={["hover", "focus"]}>
          <span tabIndex={0}>执行 ID：{item.execution_id || "-"}</span>
        </Tooltip>
        <MemberAttempts
          source={source}
          batchId={batchId}
          memberId={item.member_id}
          count={Math.max(0, item.business_attempt - 1)}
        />
      </div>
    ),
  },
  {
    title: "状态 / 子任务",
    width: 160,
    render: (_, item) => (
      <>
        {statusTag(item.status)}
        <div>{settledStatusTag(item.status, item.settled_status)}</div>
      </>
    ),
  },
  {
    title: "模型 / 热度",
    width: 180,
    render: (_, item) => (
      <div className={styles.stackCell}>
        <strong title={`${item.provider_id || "-"} / ${item.model_id || "-"}`}>
          {item.provider_id || "-"} / {item.model_id || "-"}
        </strong>
        <span>热度 {item.viewer_heat_score ?? "未记录"}</span>
      </div>
    ),
  },
  {
    title: "时间",
    width: 245,
    render: (_, item) => (
      <div className={styles.stackCell}>
        <span>最早 {timeText(item.not_before_at)}</span>
        <span>开始 {timeText(item.started_at)}</span>
        <span>完成 {timeText(item.finished_at)}</span>
        <span title="Scheduler 确认子任务状态的时间">
          子任务{" "}
          {timeText(
            item.status === "success" &&
              item.settled_error_code !== "cron_async_subtasks_timeout"
              ? item.settled_at
              : null,
          )}
        </span>
      </div>
    ),
  },
  {
    title: "error_type",
    dataIndex: "error_type",
    width: 130,
    ellipsis: true,
    render: (value: string | null | undefined) => (
      <Tooltip title={value ?? "-"}>{value ?? "-"}</Tooltip>
    ),
  },
  {
    title: "结果 / 错误",
    width: 200,
    render: (_, item) => {
      const failed =
        ["error", "dispatch_failed"].includes(item.status) ||
        item.settled_status === "error";
      const message = failed
        ? item.error_message ||
          item.settled_error_code ||
          item.error_code ||
          "-"
        : "-";
      return (
        <Tooltip title={message}>
          <span className={styles.errorText}>{message}</span>
        </Tooltip>
      );
    },
  },
];

export default function BatchDetail({
  source,
  actor,
  batch,
  refresh,
  onQueued,
  onPauseChanged,
}: {
  source: string;
  actor: string;
  batch: ManagedBatch;
  refresh: number;
  onPauseChanged?: () => void;
  onQueued: (result: ManagedRetryResult) => void;
}) {
  const [filters, setFilters] = useState({
    member_page: 1,
    member_limit: 50,
    member_status: "",
    subtask_status: "",
    member_role: "",
    member_query: "",
    event_page: 1,
    event_limit: 50,
    event_query: "",
  });
  const load = useCallback(
    (signal: AbortSignal) => {
      void refresh;
      const { event_query, ...params } = filters;
      const query = event_query.trim();
      const eventFilter = isUuid(query)
        ? { event_execution_id: query.toLowerCase() }
        : { event_type: query };
      return managedBatchBoard.detail(
        source,
        batch.batch_id,
        { ...params, ...eventFilter },
        signal,
      );
    },
    [source, batch.batch_id, filters, refresh],
  );
  const [pausing, setPausing] = useState(false);
  const detail = useBoardResource(load);
  const data = detail.value;
  const skipped = data?.skipped_members;
  const current = data?.batch || batch;
  const emptyBatchDetail = data && current.member_count === 0 && (
    <Alert
      type="info"
      showIcon
      message={skipped ? "无可执行成员" : "该批次未记录跳过明细"}
    />
  );
  const skippedTab = skipped?.length
    ? {
        key: "skipped",
        label: `入队前跳过 (${skipped.length})`,
        children: (
          <Table
            size="small"
            rowKey={(item) =>
              JSON.stringify([item.tenant_id, item.agent_id, item.job_id])
            }
            columns={skippedColumns}
            dataSource={skipped}
            pagination={false}
            scroll={{ x: 510, y: 320 }}
          />
        ),
      }
    : null;
  return (
    <div className={styles.detailContent}>
      <div className={styles.detailHead}>
        <div>
          <h2>{batch.job_name || "未命名定时任务"}</h2>
          <p>
            <Tooltip title={batch.batch_id}>
              <span className={styles.mono}>{batch.batch_id}</span>
            </Tooltip>
            <span title="父任务本次执行的冻结模型；各成员模型见成员列表">
              {current.provider_id || "-"} / {current.model_id || "-"}
            </span>
          </p>
        </div>
        <Space>
          {statusTag(data?.batch.status || batch.status)}
          {current.batch_paused && <span>本批已暂停</span>}
          <Tooltip title="暂停本批后续派发和重试；已交接的执行继续完成，后续新批次不受影响">
            <Button
              aria-label={current.batch_paused ? "恢复本批" : "暂停本批"}
              loading={pausing}
              onClick={async () => {
                setPausing(true);
                try {
                  await managedBatchBoard.pause(
                    source,
                    batch.batch_id,
                    !current.batch_paused,
                  );
                  setFilters((value) => ({ ...value }));
                  onPauseChanged?.();
                } catch {
                  message.error("批次暂停状态更新失败，请重试");
                } finally {
                  setPausing(false);
                }
              }}
            >
              {current.batch_paused ? "恢复本批" : "暂停本批"}
            </Button>
          </Tooltip>
        </Space>
      </div>
      <div
        className={styles.detailMeta}
        style={{ gridTemplateColumns: "repeat(3, minmax(0, 1fr))" }}
      >
        <span>父任务 {batch.job_id}</span>
        <span>计划 {timeText(batch.logical_fire_at)}</span>
        <span>回调 {timeText(batch.created_at)}</span>
      </div>
      {detail.error && <Alert type="error" showIcon message={detail.error} />}
      <Tabs
        defaultActiveKey="members"
        className={`${styles.detailTabs} ${styles.managedDetailTabs}`}
        items={[
          {
            key: "failures",
            label: "失败类型与重试",
            className: styles.failureTab,
            children: (
              <FailurePanel
                source={source}
                actor={actor}
                batchId={batch.batch_id}
                onQueued={onQueued}
              />
            ),
          },
          {
            key: "members",
            label: `执行成员 (${data?.member_total ?? batch.member_count})`,
            children: emptyBatchDetail || (
              <div className={styles.intentTab}>
                <div className={styles.intentFilters}>
                  <Input
                    aria-label="筛选新版执行成员"
                    placeholder="筛选用户、任务或名称"
                    value={filters.member_query}
                    onChange={(event) =>
                      setFilters({
                        ...filters,
                        member_query: event.target.value,
                        member_page: 1,
                      })
                    }
                  />
                  <Select
                    aria-label="新版成员角色"
                    value={filters.member_role}
                    options={[
                      { value: "", label: "全部角色" },
                      { value: "parent", label: "父任务" },
                      { value: "child", label: "子任务" },
                    ]}
                    onChange={(value) =>
                      setFilters({
                        ...filters,
                        member_role: value,
                        member_page: 1,
                      })
                    }
                  />
                  <Select
                    aria-label="新版成员状态"
                    value={filters.member_status}
                    options={[
                      { value: "", label: "全部状态" },
                      ...[
                        "queued",
                        "dispatching",
                        "accepted",
                        "running",
                        "success",
                        "error",
                        "dispatch_failed",
                        "skipped",
                        "cancelled",
                      ].map((value) => ({ value, label: labels[value] })),
                    ]}
                    onChange={(value) =>
                      setFilters({
                        ...filters,
                        member_status: value,
                        member_page: 1,
                      })
                    }
                  />
                  <Select
                    aria-label="批次内子任务状态"
                    value={filters.subtask_status}
                    options={[
                      { value: "", label: "全部子任务状态" },
                      { value: "waiting_subtasks", label: "等待子任务完成" },
                      { value: "success", label: "子任务成功" },
                      { value: "error", label: "子任务失败" },
                      { value: "skipped", label: "子任务已跳过" },
                      { value: "cancelled", label: "子任务已取消" },
                      { value: "unconfirmed", label: "子任务状态未确认" },
                    ]}
                    onChange={(value) =>
                      setFilters({
                        ...filters,
                        subtask_status: value,
                        member_page: 1,
                      })
                    }
                  />
                  <strong>{data?.member_total ?? "-"} 条匹配结果</strong>
                </div>
                <Table
                  loading={detail.loading}
                  size="small"
                  rowKey="member_id"
                  columns={memberColumns(source, batch.batch_id)}
                  dataSource={data?.members || []}
                  pagination={false}
                  scroll={{ x: 1410, y: 292 }}
                  expandable={{
                    expandedRowRender: (item) => (
                      <div>
                        任务版本 {item.definition_version} / 当前版本{" "}
                        {item.current_definition_version}；操作者{" "}
                        {item.actor_id}；管理状态 {item.management_status}
                        ；调度模式 {item.dispatch_state}；受理{" "}
                        {timeText(item.accepted_at)}；子任务完成{" "}
                        {timeText(item.settled_at)}
                      </div>
                    ),
                  }}
                />
                <nav
                  aria-label="新版成员分页"
                  className={styles.detailPagination}
                >
                  <Pagination
                    current={filters.member_page}
                    pageSize={50}
                    total={data?.member_total || 0}
                    showSizeChanger={false}
                    size="small"
                    onChange={(value) =>
                      setFilters({ ...filters, member_page: value })
                    }
                  />
                </nav>
              </div>
            ),
          },
          ...(skippedTab ? [skippedTab] : []),
          {
            key: "events",
            label: `调度事件 (${data?.event_total ?? "-"})`,
            children: (
              <>
                <Input
                  allowClear
                  aria-label="筛选调度事件"
                  maxLength={64}
                  placeholder="输入事件类型或完整执行 ID"
                  style={{ maxWidth: 360, marginBottom: 8 }}
                  value={filters.event_query}
                  onChange={(event) =>
                    setFilters({
                      ...filters,
                      event_query: event.target.value,
                      event_page: 1,
                    })
                  }
                />
                <Spin spinning={detail.loading}>
                  <div className={styles.eventList}>
                    {data?.events.map((event) => (
                      <div className={styles.eventItem} key={event.event_id}>
                        <time>{timeText(event.occurred_at)}</time>
                        <strong title={event.event_type}>
                          {eventLabel(event)}
                        </strong>
                        <Tooltip title={event.execution_id}>
                          <p>
                            执行 {event.execution_id || "-"} · 操作者{" "}
                            {event.actor_id}
                          </p>
                        </Tooltip>
                        <Tooltip
                          trigger={["hover", "focus"]}
                          placement="topRight"
                          overlayStyle={{
                            maxWidth: "min(640px, calc(100vw - 32px))",
                          }}
                          title={
                            <pre
                              className={hoverStyles.eventTooltip}
                              aria-label="完整事件详情"
                            >
                              {JSON.stringify(event.details, null, 2)}
                            </pre>
                          }
                        >
                          <em tabIndex={0} aria-label="查看完整事件详情">
                            {JSON.stringify(event.details)}
                          </em>
                        </Tooltip>
                      </div>
                    ))}
                  </div>
                </Spin>
                <small>仅展示实际记录事件；升级前缺失的事件不会补造。</small>
                <nav
                  aria-label="新版事件分页"
                  className={styles.detailPagination}
                >
                  <Pagination
                    current={filters.event_page}
                    pageSize={50}
                    total={data?.event_total || 0}
                    showSizeChanger={false}
                    size="small"
                    onChange={(value) =>
                      setFilters({ ...filters, event_page: value })
                    }
                  />
                </nav>
              </>
            ),
          },
        ]}
      />
    </div>
  );
}

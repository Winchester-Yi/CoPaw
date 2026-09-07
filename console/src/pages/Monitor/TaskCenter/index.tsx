import { useCallback, useEffect, useState } from "react";
import {
  Button,
  Empty,
  Input,
  Modal,
  Pagination,
  Select,
  Spin,
  Table,
  Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import { useTranslation } from "react-i18next";
import { RefreshCw, Search } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import {
  monitorApi,
  type AsyncTaskDetailRecord,
  type AsyncTaskRecord,
} from "../../../api/modules/monitor";
import { getBbkDisplayName } from "../../../constants/bbk";
import { DEFAULT_SOURCE_ID } from "../../../constants/identity";
import { useIframeStore } from "../../../stores/iframeStore";
import styles from "./index.module.less";

const STATUS_OPTIONS = [
  { label: "全部状态", value: "" },
  { label: "排队中", value: "queued" },
  { label: "运行中", value: "running" },
  { label: "已成功", value: "succeeded" },
  { label: "部分失败", value: "partial_failed" },
  { label: "失败", value: "failed" },
];

const STATUS_COLOR: Record<string, string> = {
  queued: "default",
  running: "processing",
  succeeded: "success",
  created: "success",
  skipped: "default",
  partial_failed: "warning",
  failed: "error",
};

const STATUS_LABEL: Record<string, string> = {
  queued: "排队",
  running: "运行",
  succeeded: "成功",
  created: "创建",
  skipped: "跳过",
  partial_failed: "部分失败",
  failed: "失败",
};

const ITEM_STATUS_ORDER: Record<string, number> = {
  failed: 0,
  running: 1,
  queued: 2,
  created: 3,
  succeeded: 4,
  skipped: 5,
};

const TASK_TYPE_TITLE_MAP: Record<string, string> = {
  "cron.broadcast.distribute": "定时任务分发",
  "market.mcp.distribute": "MCP 分发",
  "market.skill.distribute": "技能分发",
  "monitor.high.freq.question": "用户高频问题分析",
  "provider.active_model.distribute": "模型分发",
  "provider.providers.distribute": "供应商分发",
  "tenant.bootstrap": "用户初始化",
};

const TASK_TYPE_OPTIONS = [
  { label: "全部类型", value: "" },
  ...Object.entries(TASK_TYPE_TITLE_MAP).map(([value, label]) => ({
    label,
    value,
  })),
];
const PAGE_SIZE_OPTIONS = ["10", "20", "50", "100"];

function formatDateTime(value?: string | null) {
  return value ? dayjs(value).format("YYYY-MM-DD HH:mm:ss") : "-";
}

function formatCount(done: number, total: number, failed: number) {
  return `${done}/${total} · 失败 ${failed}`;
}

function isHighFrequencyQuestionTask(
  record?: Pick<AsyncTaskRecord, "task_type" | "title"> | null,
) {
  return (
    record?.task_type === "monitor.high.freq.question" ||
    record?.title === "用户高频问题分析"
  );
}

function readTaskRequest(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const request = (value as Record<string, unknown>).request;
  return request && typeof request === "object" && !Array.isArray(request)
    ? (request as Record<string, unknown>)
    : null;
}

function normalizeRequestText(value: unknown) {
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

function formatHighFrequencyQuestionSummary(record: AsyncTaskRecord) {
  if (!isHighFrequencyQuestionTask(record)) {
    return record.summary || record.task_id;
  }

  const request = readTaskRequest(record.result_json);
  if (request) {
    const startDate = normalizeRequestText(request.start_date);
    const endDate = normalizeRequestText(request.end_date);
    const bbkId = normalizeRequestText(request.bbk_id);
    const scopeType = normalizeRequestText(request.scope_type);
    const scopeText =
      scopeType === "ALL" || bbkId === "ALL"
        ? "全部机构"
        : getBbkDisplayName(bbkId);
    if (startDate && endDate) {
      return `${startDate} 至 ${endDate}，${scopeText}`;
    }
  }

  return (record.summary || record.task_id).replace(
    /，(\d{3,})$/,
    (_, bbkId: string) => `，${getBbkDisplayName(bbkId)}`,
  );
}

function StatusTag({ status }: { status: string }) {
  return (
    <Tag className={styles.statusTag} color={STATUS_COLOR[status] || "default"}>
      {STATUS_LABEL[status] || status || "-"}
    </Tag>
  );
}

function getItemStatusRank(status: string) {
  return ITEM_STATUS_ORDER[status] ?? Number.MAX_SAFE_INTEGER;
}

export default function TaskCenterPage() {
  const { t } = useTranslation();
  const sourceId = useIframeStore((state) => state.source) || DEFAULT_SOURCE_ID;
  const [items, setItems] = useState<AsyncTaskRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [status, setStatus] = useState("");
  const [taskType, setTaskType] = useState("");
  const [searchText, setSearchText] = useState("");
  const [submittedKeyword, setSubmittedKeyword] = useState("");
  const [loading, setLoading] = useState(false);
  const [selectedTask, setSelectedTask] =
    useState<AsyncTaskDetailRecord | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const fetchTasks = useCallback(
    async (nextPage: number, nextPageSize: number) => {
      setLoading(true);
      try {
        const response = await monitorApi.getAsyncTasks({
          source_id: sourceId,
          status: status || undefined,
          task_type: taskType || undefined,
          keyword: submittedKeyword || undefined,
          page: nextPage,
          page_size: nextPageSize,
        });
        setItems(response.items);
        setTotal(response.total);
        setPage(response.page);
        setPageSize(response.page_size);
      } finally {
        setLoading(false);
      }
    },
    [sourceId, status, taskType, submittedKeyword],
  );

  useEffect(() => {
    void fetchTasks(1, pageSize);
  }, [fetchTasks, pageSize]);

  const openTaskDetail = async (taskId: string) => {
    setDetailLoading(true);
    try {
      setSelectedTask(await monitorApi.getAsyncTaskDetail(taskId, sourceId));
    } finally {
      setDetailLoading(false);
    }
  };

  const selectedTaskIsHighFrequency = isHighFrequencyQuestionTask(selectedTask);

  const columns: ColumnsType<AsyncTaskRecord> = [
    {
      title: "任务标题",
      dataIndex: "title",
      key: "title",
      render: (_value, record) => (
        <button
          type="button"
          className={styles.linkButton}
          onClick={() => {
            void openTaskDetail(record.task_id);
          }}
        >
          <span>{record.title}</span>
          <small>{formatHighFrequencyQuestionSummary(record)}</small>
        </button>
      ),
    },
    {
      title: "任务ID",
      dataIndex: "task_id",
      key: "task_id",
      width: 300,
      render: (value) => <span className={styles.idCell}>{value || "-"}</span>,
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 60,
      render: (value) => <StatusTag status={String(value)} />,
    },
    {
      title: "进度",
      key: "progress",
      width: 120,
      render: (_, record) => (
        <span>
          {formatCount(
            record.done_count,
            record.target_count,
            record.failed_count,
          )}
        </span>
      ),
    },
    {
      title: "操作人",
      key: "actor",
      width: 130,
      render: (_, record) => (
        <span className={styles.idCell}>
          {record.actor_user_id || record.actor_user_name
            ? `${record.actor_user_id || "-"}/${record.actor_user_name || "-"}`
            : "-"}
        </span>
      ),
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 150,
      render: (value) => formatDateTime(value),
    },
    {
      title: "完成时间",
      dataIndex: "finished_at",
      key: "finished_at",
      width: 150,
      render: (value) => formatDateTime(value),
    },
  ];

  return (
    <div className={styles.page}>
      <PageHeader
        items={[
          { title: t("nav.insightCenter", "洞察中心") },
          { title: t("nav.monitorTaskCenter", "异步任务中心") },
        ]}
        extra={
          <Button
            icon={<RefreshCw size={16} />}
            onClick={() => {
              void fetchTasks(page, pageSize);
            }}
          >
            刷新
          </Button>
        }
      />

      <div className={styles.content}>
        <section className={styles.toolbar}>
          <div className={styles.filters}>
            <Select
              className={styles.select}
              options={STATUS_OPTIONS}
              value={status}
              onChange={setStatus}
            />
            <Select
              className={styles.taskTypeSelect}
              options={TASK_TYPE_OPTIONS}
              value={taskType}
              onChange={setTaskType}
            />
          </div>
          <div className={styles.searchBox}>
            <Input
              className={styles.searchInput}
              prefix={<Search size={14} />}
              placeholder="按标题、摘要、任务ID搜索"
              value={searchText}
              onChange={(event) => setSearchText(event.target.value)}
              allowClear
            />
            <Button
              type="primary"
              icon={<Search size={16} />}
              onClick={() => {
                const nextKeyword = searchText.trim();
                if (nextKeyword === submittedKeyword) {
                  void fetchTasks(1, pageSize);
                  return;
                }
                setSubmittedKeyword(nextKeyword);
              }}
            >
              查询
            </Button>
          </div>
        </section>

        <Spin spinning={loading}>
          <Table<AsyncTaskRecord>
            rowKey="task_id"
            columns={columns}
            dataSource={items}
            pagination={false}
            locale={{
              emptyText: <Empty description="暂无任务" />,
            }}
            size="middle"
          />
        </Spin>

        <div className={styles.footer}>
          <Pagination
            current={page}
            pageSize={pageSize}
            total={total}
            showSizeChanger
            pageSizeOptions={PAGE_SIZE_OPTIONS}
            showTotal={(count) => `共 ${count} 条`}
            onChange={(nextPage, nextPageSize) => {
              void fetchTasks(nextPage, nextPageSize);
            }}
          />
        </div>
      </div>

      <Modal
        title={selectedTask?.title || "任务详情"}
        open={selectedTask !== null}
        onCancel={() => setSelectedTask(null)}
        footer={null}
        width={760}
        destroyOnClose
      >
        <Spin spinning={detailLoading}>
          {selectedTask ? (
            <div className={styles.detail}>
              <section className={styles.detailGrid}>
                <div>
                  <span>任务ID</span>
                  <strong>{selectedTask.task_id}</strong>
                </div>
                <div>
                  <span>状态</span>
                  <strong>
                    <StatusTag status={selectedTask.status} />
                  </strong>
                </div>
                <div>
                  <span>进度</span>
                  <strong>
                    {formatCount(
                      selectedTask.done_count,
                      selectedTask.target_count,
                      selectedTask.failed_count,
                    )}
                  </strong>
                </div>
                <div>
                  <span>创建时间</span>
                  <strong>{formatDateTime(selectedTask.created_at)}</strong>
                </div>
                {selectedTask.status === "succeeded" ? (
                  <div>
                    <span>完成时间</span>
                    <strong>{formatDateTime(selectedTask.finished_at)}</strong>
                  </div>
                ) : null}
              </section>

              <section className={styles.detailBlock}>
                <h3>摘要</h3>
                <p>{formatHighFrequencyQuestionSummary(selectedTask)}</p>
              </section>

              {selectedTaskIsHighFrequency ? null : (
                <section className={styles.detailBlock}>
                  <h3>分发明细</h3>
                  <Table
                    rowKey="target_id"
                    size="small"
                    pagination={{
                      defaultPageSize: 10,
                      showSizeChanger: true,
                      pageSizeOptions: PAGE_SIZE_OPTIONS,
                      showTotal: (count) => `共 ${count} 条`,
                    }}
                    dataSource={[...selectedTask.items].sort(
                      (left, right) =>
                        getItemStatusRank(left.status) -
                          getItemStatusRank(right.status) ||
                        left.target_id.localeCompare(right.target_id),
                    )}
                    columns={[
                      {
                        title: "目标",
                        dataIndex: "target_id",
                        key: "target_id",
                        render: (value, record) => (
                          <div className={styles.metaCell}>
                            <strong>{value}</strong>
                            <span>{record.target_name || "-"}</span>
                          </div>
                        ),
                      },
                      {
                        title: "状态",
                        dataIndex: "status",
                        key: "status",
                        width: 120,
                        sorter: (left, right) =>
                          getItemStatusRank(left.status) -
                            getItemStatusRank(right.status) ||
                          left.target_id.localeCompare(right.target_id),
                        defaultSortOrder: "ascend",
                        render: (value) => <StatusTag status={String(value)} />,
                      },
                      {
                        title: "错误信息",
                        dataIndex: "error_message",
                        key: "error_message",
                        render: (value) => value || "-",
                      },
                    ]}
                  />
                </section>
              )}
            </div>
          ) : null}
        </Spin>
      </Modal>
    </div>
  );
}

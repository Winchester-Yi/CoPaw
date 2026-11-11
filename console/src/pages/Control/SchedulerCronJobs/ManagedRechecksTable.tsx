import { useEffect, useState } from "react";
import { Alert, Space, Tabs, Tag } from "antd";
import { Button, Card, Table } from "@agentscope-ai/design";
import dayjs from "dayjs";
import { Link } from "react-router-dom";
import { schedulerCronApi } from "@/api/modules/schedulerCron";
import type {
  ManagedCronContext,
  ManagedPage,
  ManagedRecheck,
  ManagedRecheckView,
} from "@/api/types/schedulerCron";
import styles from "../CronJobs/index.module.less";
import { failureMessage } from "./helpers";

const statuses: Record<ManagedRecheck["status"], [string, string]> = {
  queued: ["待执行", "default"],
  dispatching: ["派发中", "processing"],
  accepted: ["派发中", "processing"],
  running: ["执行中", "processing"],
  success: ["成功", "success"],
  error: ["失败", "error"],
  cancelled: ["已取消", "default"],
};

function formatTime(value: string | null | undefined) {
  return value && dayjs(value).isValid()
    ? dayjs(value).format("YYYY-MM-DD HH:mm:ss")
    : "—";
}

function RecheckPage({
  context,
  view,
}: {
  context: ManagedCronContext;
  view: ManagedRecheckView;
}) {
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const [revision, setRevision] = useState(0);
  const [page, setPage] = useState<ManagedPage<ManagedRecheck>>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();
  const cursor = cursors[cursors.length - 1];

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setPage(undefined);
    setError(undefined);
    void schedulerCronApi
      .listRechecks(context, view, cursor, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) setPage(result);
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(failureMessage(failure));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [context, cursor, revision, view]);

  const history = view === "history";
  const next = page?.next_cursor;
  const columns = [
    { title: "原任务", dataIndex: "job_name", key: "job_name" },
    { title: "回检 ID", dataIndex: "recheck_id", key: "recheck_id" },
    ...(history
      ? [
          {
            title: "原执行 ID",
            dataIndex: "parent_execution_id",
            key: "parent_execution_id",
          },
        ]
      : []),
    {
      title: "计划执行时间",
      dataIndex: "run_at",
      key: "run_at",
      render: formatTime,
    },
    {
      title: "开始时间",
      dataIndex: "started_at",
      key: "started_at",
      render: formatTime,
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      render: (status: ManagedRecheck["status"]) => {
        const [label, color] = statuses[status] ?? [status, "default"];
        return <Tag color={color}>{label}</Tag>;
      },
    },
    ...(history
      ? [
          {
            title: "结束时间",
            dataIndex: "finished_at",
            key: "finished_at",
            render: formatTime,
          },
          {
            title: "原因",
            dataIndex: "reason_code",
            key: "reason_code",
            render: (reason: string | null) => reason || "—",
          },
          {
            title: "会话",
            dataIndex: "chat_id",
            key: "chat_id",
            render: (id: string | null) =>
              id ? (
                <Link to={`/chat/${encodeURIComponent(id)}`}>查看原会话</Link>
              ) : (
                "—"
              ),
          },
        ]
      : []),
  ];

  return (
    <Card className={styles.tableCard}>
      <Space>
        <Button
          aria-label="刷新"
          loading={loading}
          onClick={() => {
            setCursors([undefined]);
            setRevision((value) => value + 1);
          }}
        >
          刷新
        </Button>
        <Button
          disabled={loading || cursors.length <= 1}
          onClick={() => setCursors((value) => value.slice(0, -1))}
        >
          上一页
        </Button>
        <Button
          disabled={loading || !next}
          onClick={() => next && setCursors((value) => [...value, next])}
        >
          下一页
        </Button>
      </Space>
      {error ? (
        <Alert type="error" showIcon message={error} />
      ) : (
        <Table
          columns={columns}
          dataSource={page?.items ?? []}
          loading={loading}
          locale={{ emptyText: history ? "暂无回检历史" : "暂无回检任务" }}
          pagination={false}
          rowKey="recheck_id"
          scroll={{ x: history ? 1320 : 920 }}
        />
      )}
    </Card>
  );
}

export function ManagedRechecksTable({
  context,
}: {
  context: ManagedCronContext;
}) {
  return (
    <Tabs
      defaultActiveKey="active"
      destroyOnHidden
      items={[
        {
          key: "active",
          label: "当前任务",
          children: (
            <RecheckPage key="active" context={context} view="active" />
          ),
        },
        {
          key: "history",
          label: "历史记录",
          children: (
            <RecheckPage key="history" context={context} view="history" />
          ),
        },
      ]}
    />
  );
}

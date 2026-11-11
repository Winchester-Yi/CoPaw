import { useCallback, useState } from "react";
import { Alert, Button, Modal, Spin, Table, Tag } from "antd";
import { managedBatchBoard, type ManagedMemberAttempt } from "@/api/modules/managedBatchBoard";
import { labels, timeText } from "./format";
import { useBoardResource } from "./useBoardResource";

type Target = { source: string; batchId: string; memberId: string };

function History({ source, batchId, memberId }: Target) {
  const load = useCallback(
    (signal: AbortSignal) => managedBatchBoard.memberAttempts(source, batchId, memberId, signal),
    [source, batchId, memberId],
  );
  const resource = useBoardResource(load);
  if (resource.loading) return <Spin aria-label="加载尝试历史" />;
  if (resource.error) return <Alert type="error" message="尝试历史加载失败，请关闭后重试" />;
  return <Table<ManagedMemberAttempt>
    size="small"
    rowKey="execution_id"
    dataSource={resource.value?.items ?? []}
    columns={[
      { title: "尝试", render: (_, row) => <>
        {row.business_attempt === 1 ? "首次执行" : `重试 ${row.business_attempt - 1}`}
        {row.current && <Tag color="blue">当前</Tag>}
      </> },
      { title: "执行 ID", dataIndex: "execution_id" },
      { title: "开始 / 完成", render: (_, row) => <>
        <div>{timeText(row.started_at)}</div><div>{timeText(row.finished_at)}</div>
      </> },
      { title: "结果", render: (_, row) => {
        const status = row.settled_status || row.status;
        return labels[status] || status;
      } },
      { title: "错误类型", render: (_, row) => row.error_type || "—" },
      { title: "错误详情", render: (_, row) => row.error_message || "—" },
    ]}
    pagination={{ pageSize: 10, hideOnSinglePage: true }}
  />;
}

export default function MemberAttempts({ count, ...target }: Target & { count: number }) {
  const [open, setOpen] = useState(false);
  if (count === 0) return <span>重试次数：0</span>;
  return <>
    <span>重试次数：<Button type="link" size="small" onClick={() => setOpen(true)}
      aria-label={`重试次数 ${count}，查看尝试历史`}>{count}</Button></span>
    <Modal title="尝试历史" width={900} open={open} footer={null} onCancel={() => setOpen(false)}>
      {open && <History {...target} />}
    </Modal>
  </>;
}

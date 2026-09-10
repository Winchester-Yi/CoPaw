import { useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Select, Space, Spin, Table } from "antd";
import {
  batchOperations,
  type FailurePreview,
} from "../../../api/modules/batchOperations";

export default function FailurePanel({
  batchId,
  onQueued,
}: {
  batchId: string;
  onQueued: () => void;
}) {
  const [types, setTypes] = useState<string[]>([]);
  const [preview, setPreview] = useState<FailurePreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [resolved, setResolved] = useState(false);
  const [stopped, setStopped] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [notice, setNotice] = useState<{
    type: "success" | "error";
    text: string;
  } | null>(null);
  const alive = useRef(true);
  const submitLock = useRef(false);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setPreview(null);
    setResolved(false);
    setStopped(false);
    batchOperations
      .failures(batchId, types)
      .then((result) => {
        if (active) setPreview(result);
      })
      .catch((error) => {
        if (active)
          setNotice({
            type: "error",
            text: error instanceof Error ? error.message : "失败分类加载失败",
          });
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [batchId, types, refresh]);
  const needsResolved = preview?.items.some((item) =>
    ["auth_expired", "configuration"].includes(item.failure_type),
  );
  const needsStopped = preview?.items.some((item) =>
    [
      "outcome_unknown",
      "execution_timeout",
      "subtask_timeout",
      "other",
    ].includes(item.failure_type),
  );
  const blocked =
    loading ||
    submitting ||
    !types.length ||
    !preview?.items.length ||
    (needsResolved && !resolved) ||
    (needsStopped && !stopped);
  const retry = async () => {
    if (blocked || !preview || submitLock.current) return;
    submitLock.current = true;
    setSubmitting(true);
    setNotice(null);
    try {
      const result = await batchOperations.retry(
        batchId,
        preview.items,
        resolved,
        stopped,
      );
      if (!alive.current) return;
      setNotice({
        type: "success",
        text: `已加入重试队列 ${result.queued} 个，状态已变化跳过 ${
          result.skipped
        } 个${result.dispatch_paused ? "；批调度已暂停，等待恢复" : ""}`,
      });
      setRefresh((v) => v + 1);
      onQueued();
    } catch (error) {
      if (!alive.current) return;
      setNotice({
        type: "error",
        text:
          error instanceof Error
            ? error.message
            : "重试提交失败，请刷新核对状态",
      });
      setRefresh((v) => v + 1);
      onQueued();
    } finally {
      submitLock.current = false;
      if (alive.current) setSubmitting(false);
    }
  };
  return (
    <section aria-label="失败类型与重试">
      <Space direction="vertical" style={{ width: "100%" }}>
        <strong>失败类型统计（整个批次当前失败的 Intent）</strong>
        <Space wrap>
          {preview?.counts.map((item) => (
            <span key={item.failure_type}>
              {item.label}：{item.count}
            </span>
          ))}
        </Space>
        <Select
          mode="multiple"
          aria-label="选择重试失败类型"
          style={{ width: "100%" }}
          placeholder="选择需要重试的失败类型"
          value={types}
          disabled={submitting}
          options={preview?.counts.map((item) => ({
            value: item.failure_type,
            label: `${item.label} (${item.count})`,
          }))}
          onChange={(values) => {
            setLoading(true);
            setPreview(null);
            setTypes(values);
            setNotice(null);
          }}
        />
        <Alert
          type="info"
          showIcon
          message="重试会重新执行整个任务，包括 Agent 和子任务。每次仅增加一次执行机会，按原优先顺序等待共享 Worker 名额。"
        />
        {preview?.has_more && (
          <Alert
            type="warning"
            message="本次预览前 200 个匹配任务，提交后可继续处理剩余任务。"
          />
        )}
        <Spin spinning={loading}>
          <Table
            size="small"
            rowKey="id"
            dataSource={preview?.items || []}
            pagination={{ pageSize: 5 }}
            scroll={{ x: 620 }}
            columns={[
              { title: "Intent", dataIndex: "id", width: 80 },
              { title: "用户", dataIndex: "tenant_id", width: 140 },
              { title: "已尝试", dataIndex: "attempt_count", width: 80 },
              {
                title: "原始失败原因",
                dataIndex: "error_message",
                ellipsis: true,
              },
            ]}
          />
        </Spin>
        {needsResolved && (
          <Checkbox
            disabled={submitting}
            checked={resolved}
            onChange={(e) => setResolved(e.target.checked)}
          >
            已刷新鉴权或修复配置
          </Checkbox>
        )}
        {needsStopped && (
          <Checkbox
            disabled={submitting}
            checked={stopped}
            onChange={(e) => setStopped(e.target.checked)}
          >
            已核对旧执行及其子任务，确认已停止，可重新执行
          </Checkbox>
        )}
        <Space>
          <Button
            type="primary"
            disabled={Boolean(blocked)}
            loading={submitting}
            onClick={retry}
          >
            重试预览的 {preview?.items.length || 0} 个失败任务
          </Button>
          <Button
            disabled={loading || submitting}
            onClick={() => setRefresh((v) => v + 1)}
          >
            刷新失败统计
          </Button>
        </Space>
        {notice && <Alert type={notice.type} message={notice.text} showIcon />}
      </Space>
    </section>
  );
}

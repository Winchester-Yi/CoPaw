import { useEffect, useRef, useState } from "react";
import { Alert, Button, Modal, Select, Space, Table, Typography } from "antd";
import { v4 as uuid } from "uuid";
import { cronMigrationApi } from "@/api/modules/cronMigration";
import type { MigrationProgress } from "@/api/modules/cronMigration";
import { cronAccessKey, currentCronContext } from "@/api/cronTaskContext";
import { useAgentStore } from "@/stores/agentStore";
import { useIframeStore } from "@/stores/iframeStore";

const labels: Record<string, string> = {
  previewing: "采集快照中",
  awaiting_confirmation: "等待确认",
  preview_ready: "可迁移",
  preview_failed: "采集失败",
  running: "迁移中",
  completed: "已完成",
  failed: "失败，可重试",
};
export default function SelectedMigration({
  selectedIds,
  clearSelection,
  refreshJobs,
}: {
  selectedIds: string[];
  clearSelection: () => void;
  refreshJobs?: () => void;
}) {
  useAgentStore((state) => state.selectedAgent);
  useIframeStore((state) => state.userId);
  useIframeStore((state) => state.source);
  useIframeStore((state) => state.manager);
  useIframeStore((state) => state.isSuperManager);
  const scope = cronAccessKey();
  const latestScope = useRef(scope);
  latestScope.current = scope;
  const mounted = useRef(true);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<MigrationProgress | null>(null);
  const [recent, setRecent] = useState<MigrationProgress[]>([]);
  const currentOperation = useRef<string | null>(null);
  const serial = useRef(0);
  const submission = useRef<{ selection: string; id: string } | null>(null);
  const progressId = progress?.operation_id;
  const progressStatus = progress?.status;
  const refresh = useRef(refreshJobs);
  refresh.current = refreshJobs;
  const migrated = progress?.groups.some((group) => group.status === "completed");
  const valid = (key: string) => mounted.current && key === latestScope.current;

  useEffect(() => {
    if (migrated && ["completed", "failed"].includes(progressStatus ?? ""))
      refresh.current?.();
  }, [migrated, progressStatus, progressId]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    setOpen(false);
    setProgress(null);
    setRecent([]);
    setError(null);
    setBusy(false);
    currentOperation.current = null;
    serial.current += 1;
    submission.current = null;
    clearSelection();
  }, [scope, clearSelection]);

  useEffect(() => {
    if (
      !open ||
      !progressId ||
      !progressStatus ||
      !["running", "previewing"].includes(progressStatus)
    )
      return;
    const id = progressId;
    const context = currentCronContext();
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await cronMigrationApi.get(context, id);
        if (!cancelled && valid(scope) && currentOperation.current === id)
          setProgress(value);
      } catch {
        if (!cancelled && valid(scope))
          setError("进度查询失败，迁移可能仍在继续，请刷新查询");
      }
      if (!cancelled) timer = setTimeout(poll, 2500);
    };
    timer = setTimeout(poll, 1500);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [open, progressId, progressStatus, scope]);

  const act = async (action: () => Promise<MigrationProgress>) => {
    const key = scope;
    const ticket = ++serial.current;
    const current = () => valid(key) && serial.current === ticket;
    setBusy(true);
    setError(null);
    try {
      const value = await action();
      if (!current()) return;
      currentOperation.current = value.operation_id;
      setProgress(value);
      setOpen(true);
    } catch (err) {
      if (current())
        setError(err instanceof Error ? err.message : "请求失败，请重试");
    } finally {
      if (current()) setBusy(false);
    }
  };
  const showRecent = async () => {
    setOpen(true);
    setError(null);
    setBusy(true);
    const key = scope;
    const ticket = ++serial.current;
    const current = () => valid(key) && serial.current === ticket;
    try {
      const value = await cronMigrationApi.recent(currentCronContext());
      if (!current()) return;
      setRecent(value.items);
      currentOperation.current = value.items[0]?.operation_id ?? null;
      setProgress(value.items[0] ?? null);
    } catch {
      if (current()) setError("迁移记录加载失败");
    } finally {
      if (current()) setBusy(false);
    }
  };
  return (
    <>
      <Button
        disabled={!selectedIds.length || selectedIds.length > 100 || busy}
        loading={busy && !open}
        onClick={() => {
          setOpen(true);
          setProgress(null);
          currentOperation.current = null;
          const selection = JSON.stringify([...selectedIds].sort());
          if (submission.current?.selection !== selection)
            submission.current = { selection, id: uuid() };
          const requestId = submission.current.id;
          void act(async () => {
            const result = await cronMigrationApi.submit(
              currentCronContext(),
              selectedIds,
              requestId,
            );
            if (submission.current?.id === requestId) submission.current = null;
            return result;
          });
        }}
      >
        迁移到新版{selectedIds.length ? `（${selectedIds.length}）` : ""}
      </Button>
      <Button onClick={() => void showRecent()}>迁移进度</Button>
      <Modal
        open={open}
        width={800}
        title="迁移到新版定时任务"
        onCancel={() => setOpen(false)}
        footer={null}
      >
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Alert
            type="warning"
            showIcon
            message="保留原任务和会话 ID；父任务连同子任务迁移。确认后先暂停已识别的旧平台定时器，再切换新版；已入队或运行中的任务不取消。"
          />
          <Typography.Text type="secondary">
            每次最多选择 100
            个父任务或独立任务，子任务自动纳入。默认非工作日不执行；删除新版任务会保留原会话资源。关闭此窗口不会取消后台迁移。
          </Typography.Text>
          {recent.length > 0 && (
            <Select
              aria-label="选择迁移记录"
              style={{ width: "100%" }}
              value={progress?.operation_id}
              options={recent.map((item) => ({
                value: item.operation_id,
                label: `${item.operation_id} · ${
                  labels[item.status] || item.status
                }`,
              }))}
              onChange={(id) => {
                currentOperation.current = id;
                void act(() => cronMigrationApi.get(currentCronContext(), id));
              }}
            />
          )}
          {error && <Alert type="error" showIcon message={error} />}
          {progress ? (
            <>
              <Typography.Text>
                状态：{labels[progress.status] || progress.status}；已采集{" "}
                {progress.groups.length}/{progress.root_count} 组，共{" "}
                {progress.member_count} 个任务
              </Typography.Text>
              <Typography.Text copyable>
                {progress.operation_id}
              </Typography.Text>
              <Table
                size="small"
                rowKey="job_id"
                dataSource={progress.groups}
                pagination={{ pageSize: 5 }}
                columns={[
                  {
                    title: "父任务 / 独立任务 ID",
                    dataIndex: "job_id",
                    ellipsis: true,
                    render: (id: string) => (
                      <Typography.Text ellipsis={{ tooltip: id }} copyable>
                        {id}
                      </Typography.Text>
                    ),
                  },
                  {
                    title: "实际范围",
                    width: 130,
                    render: (_, row) =>
                      row.member_count === 1
                        ? "1 个独立任务"
                        : row.member_count
                        ? `1 个父任务，${row.member_count - 1} 个子任务`
                        : "待采集",
                  },
                  {
                    title: "进度",
                    dataIndex: "status",
                    width: 130,
                    render: (status: string) => labels[status] || status,
                  },
                  {
                    title: "说明",
                    render: (_, row) => {
                      const warnings = (row.warnings ?? []).map((warning) => {
                        if (warning === "memory_state_missing")
                          return "当前实例无内存状态，使用可恢复快照";
                        const [code, job, origin, field] = warning.split(":");
                        if (code === "cron_migration_binding_conflict")
                          return `${job}：${origin} 的 ${field} 不一致，采用 jobs.json`;
                        if (code === "cron_migration_parent_conflict")
                          return `${job}：${origin} 的 ${field} 不一致，采用本组父任务 ${row.job_id}`;
                        return warning;
                      });
                      const lines = [row.error_code, ...warnings].filter(Boolean);
                      return lines.length ? lines.map((line) => <div key={line}>{line}</div>) : "—";
                    },
                  },
                ]}
              />
              <Space>
                {progress.status === "awaiting_confirmation" && (
                  <Button
                    type="primary"
                    loading={busy}
                    onClick={() =>
                      void act(() =>
                        cronMigrationApi.confirm(
                          currentCronContext(),
                          progress.operation_id,
                        ),
                      )
                    }
                  >
                    确认迁移上述范围
                  </Button>
                )}
                {(progress.status === "failed" || progress.can_retry) && (
                  <Button
                    loading={busy}
                    onClick={() =>
                      void act(() =>
                        cronMigrationApi.confirm(
                          currentCronContext(),
                          progress.operation_id,
                        ),
                      )
                    }
                  >
                    重试未完成部分
                  </Button>
                )}
                <Button
                  loading={busy}
                  onClick={() =>
                    void act(() =>
                      cronMigrationApi.get(
                        currentCronContext(),
                        progress.operation_id,
                      ),
                    )
                  }
                >
                  刷新进度
                </Button>
              </Space>
            </>
          ) : (
            <Typography.Text>
              {busy ? "正在加载…" : "暂无迁移记录"}
            </Typography.Text>
          )}
        </Space>
      </Modal>
    </>
  );
}

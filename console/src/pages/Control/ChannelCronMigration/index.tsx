import { useCallback, useRef, useState } from "react";
import { Alert, Button, Card, Input, Select, Space, Table, Tag } from "antd";
import { v4 as uuid } from "uuid";
import { cronAccessKey, currentCronContext } from "@/api/cronTaskContext";
import { cronMigrationApi, type ChannelMigrationJob, type ChannelMigrationOperation } from "@/api/modules/cronMigration";
import { useIframeStore } from "@/stores/iframeStore";
import { useAgentStore } from "@/stores/agentStore";
import { useBoardResource } from "../../Monitor/ManagedBatchDispatch/useBoardResource";

const states: Record<string, string> = {
  not_migrated: "未迁移", in_progress: "已登记，查看进度",
  migrated: "已迁移", migrated_deleted: "已迁移 · 新版已删除",
  unknown_agent: "未识别 Agent",
  previewing: "采集快照中", awaiting_confirmation: "等待确认",
  preview_ready: "可迁移", preview_failed: "采集失败", running: "迁移中",
  completed: "已完成", failed: "失败",
};

function MigrationPage() {
  const [selected, setSelected] = useState<string[]>([]);
  const [filters, setFilters] = useState({ tenant: "", query: "", status: "" });
  const [cursors, setCursors] = useState([""]);
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const requestId = useRef<{ key: string; id: string }>();
  const cursor = cursors[cursors.length - 1];
  const context = currentCronContext();
  const scope = cronAccessKey(context);
  const load = useCallback((signal: AbortSignal) => {
    void refresh;
    const params = new URLSearchParams({ cursor });
    for (const [key, value] of Object.entries(filters)) if (value) params.set(key, value);
    return cronMigrationApi.channelJobs(currentCronContext(), params, signal);
  }, [refresh, cursor, filters]);
  const loadProgress = useCallback((signal: AbortSignal) => {
    void refresh;
    return cronMigrationApi.channelRecent(currentCronContext(), signal);
  }, [refresh]);
  const jobs = useBoardResource(load);
  const operations = useBoardResource(loadProgress);
  const changeFilter = (key: keyof typeof filters, value: string) => {
    setFilters((previous) => ({ ...previous, [key]: value }));
    setCursors([""]);
    setSelected([]);
  };
  const act = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError(undefined);
    try {
      await action();
      if (cronAccessKey() === scope) setRefresh((value) => value + 1);
    } catch (err) {
      if (cronAccessKey() === scope)
        setError(err instanceof Error ? err.message : "迁移操作失败");
    } finally {
      if (cronAccessKey() === scope) setBusy(false);
    }
  };
  const submit = () => {
    const key = JSON.stringify([...selected].sort());
    if (requestId.current?.key !== key) requestId.current = { key, id: uuid() };
    const id = requestId.current.id;
    void act(async () => {
      await cronMigrationApi.channelSubmit(context, selected, id);
      if (cronAccessKey() === scope) {
        requestId.current = undefined;
        setSelected([]);
      }
    });
  };
  return <Space direction="vertical" size="middle" style={{ width: "100%" }}>
    <Card title={`定时任务迁移 · ${context.source_id}`} extra={
      <Button onClick={() => setRefresh((value) => value + 1)}>刷新列表及进度</Button>
    }>
      <Alert type="info" showIcon message="只勾选父任务或独立任务；分发子任务随父任务整组迁移。预览不修改任务。" />
      {error && <Alert type="error" message={error} />}
      {jobs.error && <Alert type="error" message="旧任务清单加载失败，请刷新重试" />}
      <Space wrap style={{ marginBlock: 16 }}>
        <Input.Search placeholder="租户 ID" aria-label="租户 ID" allowClear onSearch={(value) => changeFilter("tenant", value.trim())} />
        <Input.Search placeholder="任务名或 ID" aria-label="任务名或 ID" allowClear onSearch={(value) => changeFilter("query", value.trim())} />
        <Select value={filters.status} aria-label="迁移状态" style={{ width: 170 }} onChange={(value) => changeFilter("status", value)}
          options={["", "not_migrated", "in_progress", "migrated", "migrated_deleted", "unknown_agent"]
            .map((value) => ({ value, label: states[value] || "全部状态" }))} />
        <Button type="primary" disabled={!selected.length || jobs.loading} loading={busy} onClick={submit}>
          预览迁移（{selected.length}）
        </Button>
      </Space>
      <Table<ChannelMigrationJob> rowKey="job_id" dataSource={jobs.value?.items ?? []} loading={jobs.loading}
        pagination={false} rowSelection={{
          selectedRowKeys: selected, onChange: (keys) => setSelected(keys.map(String)),
          getCheckboxProps: (row) => ({ disabled: Boolean(row.parent_job_id) || row.migration_status !== "not_migrated" }),
        }}
        columns={[
          { title: "任务名称", dataIndex: "name" },
          { title: "任务 ID", dataIndex: "job_id" },
          { title: "租户", dataIndex: "tenant_id" },
          { title: "Agent", render: (_, row) => row.agent_id || "—" },
          { title: "父任务 ID", render: (_, row) => row.parent_job_id || "—" },
          { title: "状态", render: (_, row) => <Tag>{states[row.migration_status]}</Tag> },
        ]} />
      <Space style={{ marginTop: 12 }}>
        <Button disabled={cursors.length < 2 || jobs.loading} onClick={() => {
          setCursors((value) => value.slice(0, -1));
          setSelected([]);
        }}>上一页</Button>
        <Button disabled={!jobs.value?.next_cursor || jobs.loading} onClick={() => {
          const next = jobs.value?.next_cursor;
          if (next) setCursors((value) => [...value, next]);
          setSelected([]);
        }}>下一页</Button>
      </Space>
    </Card>
    <Card title="近期迁移进度">
      {operations.error && <Alert type="error" message="迁移进度加载失败，请刷新重试" />}
      <Table<ChannelMigrationOperation> rowKey="operation_id" size="small"
        dataSource={operations.value?.items ?? []} loading={operations.loading}
        columns={[
          { title: "操作 ID", dataIndex: "operation_id" },
          { title: "租户", dataIndex: "tenant_id" },
          { title: "状态", render: (_, row) => states[row.status] || row.status },
          { title: "任务组", render: (_, row) => row.groups.map((group) => (
            <div key={group.job_id}>
              {group.job_id} · {states[group.status] || group.status}
              {group.member_count !== undefined && ` · ${group.member_count} 个任务`}
              {group.error_code && <div>{group.error_code}</div>}
              {group.warnings?.map((warning) => <div key={warning}>{warning}</div>)}
            </div>
          )) },
          { title: "操作", render: (_, row) => row.can_confirm &&
            (["awaiting_confirmation", "failed"].includes(row.status) || row.can_retry) &&
            <Button disabled={busy} onClick={() => void act(() =>
              cronMigrationApi.channelConfirm(context, row.operation_id)
            )}>{row.status === "awaiting_confirmation" ? "确认迁移" : "重试未完成部分"}</Button> },
        ]} />
    </Card>
  </Space>;
}

export default function ChannelCronMigration() {
  const manager = useIframeStore((state) => state.manager);
  const admin = useIframeStore((state) => state.isSuperManager);
  useIframeStore((state) => state.source);
  useIframeStore((state) => state.userId);
  useAgentStore((state) => state.selectedAgent);
  if (!manager && !admin)
    return <Alert type="warning" message="仅渠道管理员可查看迁移清单" />;
  return <MigrationPage key={cronAccessKey()} />;
}

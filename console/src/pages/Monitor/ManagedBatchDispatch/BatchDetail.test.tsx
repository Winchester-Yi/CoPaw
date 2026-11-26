import {
  cleanup,
  render,
  screen,
  fireEvent,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import BatchDetail from "./BatchDetail";
import type { ManagedBatch } from "@/api/modules/managedBatchBoard";

const api = vi.hoisted(() => ({
  detail: vi.fn(),
  pause: vi.fn(),
}));
vi.mock("../../../api/modules/managedBatchBoard", () => ({
  managedBatchBoard: api,
}));
vi.mock("./FailurePanel", () => ({ default: () => null }));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("当前批次可按异步子任务状态查询成员", async () => {
  const batch = {
    batch_id: "batch-ledger",
    job_id: "parent",
    member_count: 1,
    status: "running",
  } as ManagedBatch;
  api.detail.mockResolvedValue({
    batch,
    members: [],
    member_total: 0,
    events: [],
    event_total: 0,
  });
  render(
    <BatchDetail
      source="bank"
      actor="manager"
      batch={batch}
      refresh={0}
      onQueued={() => {}}
    />,
  );
  fireEvent.mouseDown(
    screen.getByRole("combobox", { name: "批次内子任务状态" }),
  );
  fireEvent.click(await screen.findByText("等待子任务完成", { exact: true }));
  await waitFor(() =>
    expect(api.detail).toHaveBeenLastCalledWith(
      "bank",
      "batch-ledger",
      expect.objectContaining({
        subtask_status: "waiting_subtasks",
        member_page: 1,
      }),
      expect.any(AbortSignal),
    ),
  );
});

it.each([
  {
    skipped: [
      {
        tenant_id: "alice",
        agent_id: "default",
        job_id: "parent",
        job_name: "保险经营专家",
        member_role: "parent",
        reason: "auto_unread_threshold",
      },
    ],
    text: "无可执行成员",
  },
  { skipped: [], text: "无可执行成员" },
  { skipped: null, text: "该批次未记录跳过明细" },
])("零执行成员时展示入队前跳过信息 $text", async ({ skipped, text }) => {
  const batch = {
    batch_id: "empty-batch",
    job_id: "parent",
    member_count: 0,
    status: "received",
  } as ManagedBatch;
  api.detail.mockResolvedValue({
    batch,
    members: [],
    member_total: 0,
    skipped_members: skipped,
    events: [],
    event_total: 0,
  });
  render(
    <BatchDetail
      source="bank"
      actor="manager"
      batch={batch}
      refresh={0}
      onQueued={() => {}}
    />,
  );
  expect(await screen.findByText(text)).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "执行成员 (0)" })).toBeInTheDocument();
  if (skipped?.length) {
    fireEvent.click(screen.getByRole("tab", { name: "入队前跳过 (1)" }));
    const panel = screen.getByRole("tabpanel", { name: "入队前跳过 (1)" });
    expect(within(panel).getByText("保险经营专家")).toBeInTheDocument();
    expect(within(panel).getByText("父任务 · alice")).toBeInTheDocument();
  } else {
    expect(screen.queryByRole("tab", { name: /入队前跳过/ })).toBeNull();
  }
});

it("同时有执行成员和跳过成员时，通过独立页签查看跳过明细", async () => {
  const batch = {
    batch_id: "mixed-batch",
    job_id: "parent",
    member_count: 1,
    status: "failed",
  } as ManagedBatch;
  api.detail.mockResolvedValue({
    batch,
    members: [
      {
        member_id: "member",
        member_role: "child",
        member_order: 0,
        job_id: "child",
        job_name: "已执行任务",
        status: "error",
        business_attempt: 1,
        dispatch_priority: {},
      },
    ],
    member_total: 1,
    skipped_members: [
      {
        tenant_id: "alice",
        agent_id: "default",
        job_id: "parent",
        member_role: "parent",
        reason: "auto_unread_threshold",
      },
    ],
    events: [],
    event_total: 0,
  });
  render(
    <BatchDetail
      source="bank"
      actor="manager"
      batch={batch}
      refresh={0}
      onQueued={() => {}}
    />,
  );
  expect(await screen.findByText("已执行任务")).toBeVisible();
  expect(screen.queryByText("未读过多自动暂停")).toBeNull();
  fireEvent.click(screen.getByRole("tab", { name: "入队前跳过 (1)" }));
  const panel = screen.getByRole("tabpanel", { name: "入队前跳过 (1)" });
  expect(within(panel).getByText("未读过多自动暂停")).toBeInTheDocument();
});

it("详情不展示任务数说明行，成员保留子任务状态文案", async () => {
  const batch = {
    batch_id: "batch",
    job_id: "parent",
    job_name: "日报",
    member_count: 1,
    status: "running",
  } as ManagedBatch;
  api.detail.mockResolvedValue({
    batch,
    members: [
      {
        member_id: "member",
        member_role: "parent",
        member_order: 0,
        job_id: "parent",
        job_name: "日报任务",
        tenant_id: "owner",
        execution_id: "exec",
        business_attempt: 1,
        dispatch_attempt: 1,
        status: "success",
        settled_status: "success",
        settled_at: "2026-11-12T00:17:06",
        dispatch_priority: {},
        definition_version: 1,
        current_definition_version: 1,
      },
    ],
    member_total: 1,
    events: [],
    event_total: 0,
  });
  render(
    <BatchDetail
      source="bank"
      actor="manager"
      batch={batch}
      refresh={0}
      onQueued={() => {}}
    />,
  );
  await screen.findByText("日报任务");
  expect(screen.getByText("重试次数：0")).toBeInTheDocument();
  expect(screen.queryByText(/次投递|业务尝试/)).toBeNull();
  expect(screen.queryByText(/主执行状态与异步收敛状态分别展示/)).toBeNull();
  expect(screen.queryByText(/任务数.*含父任务/)).toBeNull();
  expect(screen.getByRole("tab", { name: "执行成员 (1)" })).toBeInTheDocument();
  expect(screen.getByText("子任务成功")).toHaveClass("ant-tag-success");
  expect(screen.getByText("子任务 2026-11-12 00:17:06")).toBeInTheDocument();
  expect(screen.getByText("parent")).toBeInTheDocument();
  expect(screen.getByText("执行 ID：exec")).toBeInTheDocument();
  expect(screen.queryByText(/收敛/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /expand|展开/i }));
  expect(screen.queryByText(/；收敛/)).toBeNull();
  expect(screen.getByText(/子任务完成/)).toBeInTheDocument();
});

it("异步子任务失败单独显示红色标签，不把主执行成功当作整轮成功", async () => {
  const batch = {
    batch_id: "batch",
    job_id: "parent",
    status: "failed",
    member_count: 1,
  } as ManagedBatch;
  api.detail.mockResolvedValue({
    batch,
    member_total: 1,
    events: [],
    event_total: 0,
    members: [
      {
        member_id: "child",
        member_order: 0,
        job_id: "child",
        job_name: "异步报告",
        member_role: "child",
        business_attempt: 1,
        status: "success",
        settled_status: "error",
        error_message: "子任务执行失败",
        dispatch_priority: {},
      },
    ],
  });
  render(
    <BatchDetail
      source="bank"
      actor="manager"
      batch={batch}
      refresh={0}
      onQueued={() => {}}
    />,
  );
  expect(await screen.findByText("子任务失败")).toHaveClass("ant-tag-error");
  expect(screen.getByText("主执行成功")).toHaveClass("ant-tag-success");
});

it.each([
  ["auto_unread_threshold", "未读暂停，重试未入队"],
  ["manual", "手动停用，重试未入队"],
])("展示 %s 的拦截原因和完整事件详情", async (reason, label) => {
  const executionId = "548b6d5d-9a4a-5461-b0c9-38c12605426f";
  const batch = {
    batch_id: "batch",
    job_id: "parent",
    status: "failed",
    member_count: 0,
  } as ManagedBatch;
  const details = {
    reason,
    nested: { description: "<b>这只是文本</b>".repeat(100) },
  };
  api.detail.mockResolvedValue({
    batch,
    members: [],
    member_total: 0,
    event_total: 1,
    events: [
      {
        event_id: "event",
        event_type: "auto_retry_blocked",
        execution_id: executionId,
        actor_id: "alice",
        occurred_at: "2026-11-05T01:00:00Z",
        details,
      },
    ],
  });
  render(
    <BatchDetail
      source="bank"
      actor="manager"
      batch={batch}
      refresh={0}
      onQueued={() => {}}
    />,
  );
  fireEvent.click(await screen.findByRole("tab", { name: "调度事件 (1)" }));
  const events = within(screen.getByRole("tabpanel", { name: /调度事件/ }));
  const search = events.getByRole("textbox", { name: "筛选调度事件" });
  expect(events.getAllByRole("textbox")).toHaveLength(1);
  expect(events.queryByText("执行 ID")).not.toBeInTheDocument();
  fireEvent.change(search, { target: { value: " auto_retry_blocked " } });
  expect(api.detail).toHaveBeenLastCalledWith(
    "bank",
    "batch",
    expect.objectContaining({
      event_type: "auto_retry_blocked",
      event_page: 1,
    }),
    expect.any(AbortSignal),
  );
  fireEvent.change(search, {
    target: { value: ` ${executionId.toUpperCase()} ` },
  });
  expect(api.detail).toHaveBeenLastCalledWith(
    "bank",
    "batch",
    expect.objectContaining({
      event_execution_id: executionId,
      event_page: 1,
    }),
    expect.any(AbortSignal),
  );
  expect(await screen.findByText(label)).toHaveAttribute(
    "title",
    "auto_retry_blocked",
  );
  fireEvent.mouseOver(screen.getByLabelText("查看完整事件详情"));
  const full = await screen.findByLabelText("完整事件详情");
  expect(full.textContent).toBe(JSON.stringify(details, null, 2));
  expect(full.querySelector("b")).toBeNull();
});

it("父任务模型和错误说明沿用旧版展示，不展示Owner或优先JSON", async () => {
  const batch = {
    batch_id: "batch",
    job_id: "parent",
    job_name: "日报",
    member_count: 1,
    status: "failed",
    provider_id: "frozen-provider",
    model_id: "frozen-model",
  } as ManagedBatch;
  api.detail.mockResolvedValue({
    batch,
    member_total: 1,
    events: [],
    event_total: 0,
    members: [
      {
        member_id: "one",
        member_role: "child",
        member_order: 0,
        tenant_id: "user",
        job_name: "子任务报告",
        status: "error",
        error_code: "cron_execution_failed",
        settled_at: "2026-11-12T00:17:06",
        error_message: "子任务执行失败: 报告生成失败",
        error_type: "Network,Prompt",
        dispatch_priority: { basis: "user", user_rank: 1, branch_id: "100" },
        viewer_heat_score: 8,
        business_attempt: 3,
        dispatch_attempt: 1,
      },
    ],
  });
  render(
    <BatchDetail
      source="bank"
      actor="manager"
      batch={batch}
      refresh={0}
      onQueued={() => {}}
    />,
  );
  await screen.findByText("子任务报告");
  expect(
    screen.getByRole("button", { name: "重试次数 2，查看尝试历史" }),
  ).toBeInTheDocument();
  expect(screen.getByText("Network,Prompt")).toBeInTheDocument();
  expect(screen.getByText("子任务 -")).toBeInTheDocument();
  expect(screen.queryByText(/次投递|业务尝试/)).toBeNull();
  expect(
    screen.getByText("frozen-provider / frozen-model"),
  ).toBeInTheDocument();
  expect(screen.queryByText(/Owner/)).toBeNull();
  expect(screen.getByText("子任务执行失败: 报告生成失败")).toBeInTheDocument();
  expect(screen.queryByText(/\{"basis"/)).toBeNull();
  fireEvent.mouseOver(screen.getByLabelText("批内顺位 1，查看优先详情"));
  expect(
    await screen.findByText("优先依据：用户优先 · 第1位"),
  ).toBeInTheDocument();
  expect(screen.getByText("热度：8")).toBeInTheDocument();
});

it("暂停当前批并刷新，恢复使用同一批次身份", async () => {
  const batch = {
    batch_id: "batch",
    job_id: "parent",
    job_name: "日报",
    member_count: 1,
    status: "running",
  } as ManagedBatch;
  let paused = false;
  api.detail.mockImplementation(async () => ({
    batch: { ...batch, batch_paused: paused },
    members: [],
    member_total: 0,
    events: [],
    event_total: 0,
  }));
  api.pause.mockImplementation(async (_source, _batch, value) => {
    paused = value;
    return { batch_id: "batch", batch_paused: value };
  });
  const changed = vi.fn();
  render(
    <BatchDetail
      source="bank"
      actor="manager"
      batch={batch}
      refresh={0}
      onQueued={() => {}}
      onPauseChanged={changed}
    />,
  );
  fireEvent.click(await screen.findByRole("button", { name: "暂停本批" }));
  fireEvent.click(await screen.findByRole("button", { name: "恢复本批" }));
  await screen.findByRole("button", { name: "暂停本批" });
  expect(api.pause.mock.calls).toEqual([
    ["bank", "batch", true],
    ["bank", "batch", false],
  ]);
  expect(changed).toHaveBeenCalledTimes(2);
});

it("详情信息条在批结束后展示完成时间", async () => {
  const batch = {
    batch_id: "batch-done",
    job_id: "parent",
    job_name: "日报",
    member_count: 1,
    status: "completed",
    logical_fire_at: "2026-11-25T01:00:00Z",
    created_at: "2026-11-25T01:00:05Z",
    finished_at: "2026-11-25T01:20:00Z",
  } as ManagedBatch;
  api.detail.mockResolvedValue({
    batch,
    members: [],
    member_total: 0,
    events: [],
    event_total: 0,
  });
  render(
    <BatchDetail
      source="bank"
      actor="manager"
      batch={batch}
      refresh={0}
      onQueued={() => {}}
    />,
  );
  expect(await screen.findByText(/^完成 /)).toBeVisible();
});

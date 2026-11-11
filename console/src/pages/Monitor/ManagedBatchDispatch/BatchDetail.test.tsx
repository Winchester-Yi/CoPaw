import { cleanup, render, screen, fireEvent } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import BatchDetail from "./BatchDetail";
import type { ManagedBatch } from "@/api/modules/managedBatchBoard";

const api = vi.hoisted(() => ({ detail: vi.fn(), pause: vi.fn() }));
vi.mock("../../../api/modules/managedBatchBoard", () => ({
  managedBatchBoard: api,
}));
vi.mock("./FailurePanel", () => ({ default: () => null }));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
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
  expect(screen.getByText("parent")).toBeInTheDocument();
  expect(screen.queryByText("exec")).toBeNull();
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

it("悬停事件详情展示完整且格式化的JSON，而不是截断的预览", async () => {
  const batch = {
    batch_id: "batch",
    job_id: "parent",
    status: "failed",
    member_count: 0,
  } as ManagedBatch;
  const details = {
    reason: "完整原因".repeat(100),
    nested: { description: "<b>这只是文本</b>" },
  };
  api.detail.mockResolvedValue({
    batch,
    members: [],
    member_total: 0,
    event_total: 1,
    events: [
      {
        event_id: "event",
        event_type: "retry_skipped",
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
  expect(screen.getByRole("button", { name: "重试次数 2，查看尝试历史" })).toBeInTheDocument();
  expect(screen.getByText("Network,Prompt")).toBeInTheDocument();
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

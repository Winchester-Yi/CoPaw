import { cleanup, render, screen, fireEvent } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { TFunction } from "i18next";
import type { ColumnType } from "antd/es/table";
import type {
  ManagedDispatchMode,
  ManagedJob,
} from "@/api/types/schedulerCron";
import { createManagedColumns } from "./columns";
import { copyToClipboard } from "@/utils/clipboard";
vi.mock("@/utils/clipboard", () => ({
  copyToClipboard: vi.fn().mockResolvedValue(true),
}));
vi.mock("@agentscope-ai/design", async (original) => ({
  ...(await original<object>()),
  Dropdown: (await import("antd")).Dropdown,
}));

afterEach(cleanup);
const job = (mode: ManagedDispatchMode, child: boolean): ManagedJob =>
  ({
    id: "new-job",
    name: "任务",
    tenant_id: "alice",
    source_id: "bank",
    agent_id: "default",
    dispatch_state: mode,
    schedule: {
      type: "cron",
      cron: child ? "30 10 * * *" : "0 9 * * *",
      timezone: "Asia/Shanghai",
    },
    meta: child ? { broadcast_source_job_id: "parent" } : {},
  }) as ManagedJob;

function column(key: string): ColumnType<ManagedJob> {
  return createManagedColumns({
    t: ((key: string) => key) as TFunction,
    models: { options: [], tenantDefaultLabel: "默认模型" },
    message: { success: vi.fn(), error: vi.fn() },
    perform: vi.fn(),
    setPanel: vi.fn(),
    busy: false,
  }).find((item) => item.key === key)! as ColumnType<ManagedJob>;
}

it("仅保留八个管理列，不继承旧页面的技术字段", () => {
  const columns = createManagedColumns({
    t: ((key: string) => key) as TFunction,
    models: { options: [], tenantDefaultLabel: "默认模型" },
    message: { success: vi.fn(), error: vi.fn() },
    perform: vi.fn(),
    setPanel: vi.fn(),
    busy: false,
  });
  expect(columns.map((item) => item.key)).toEqual([
    "name",
    "run_date_range",
    "cron",
    "enabled",
    "dispatch_state",
    "management_status",
    "last_status",
    "action",
  ]);
});

it("名称和ID各自复制完整原文，不复制截断值", async () => {
  const record = {
    ...job("ordinary", false),
    name: "超长任务名称".repeat(30),
    id: "long-id-".repeat(30),
  };
  render(<>{column("name").render?.(null, record, 0)}</>);
  fireEvent.click(screen.getByRole("button", { name: "复制任务名称" }));
  fireEvent.click(screen.getByRole("button", { name: "复制任务ID" }));
  expect(copyToClipboard).toHaveBeenCalledWith(record.name);
  expect(copyToClipboard).toHaveBeenCalledWith(record.id);
});
it("子任务菜单不展示分发、子任务管理或重复暂停入口", async () => {
  render(<>{column("action").render?.(null, job("batch_enabled", true), 0)}</>);
  fireEvent.click(screen.getByRole("button", { name: "更多操作 任务" }));
  await screen.findByText("任务设置");
  expect(screen.getByText("操作进度")).toBeInTheDocument();
  expect(screen.getByText("删除")).toBeInTheDocument();
  expect(screen.queryByText("分发")).toBeNull();
  expect(screen.queryByText("子任务管理")).toBeNull();
  expect(screen.queryByText("暂停任务")).toBeNull();
});
it("失败悬浮同时包含北京时间和完整错误", async () => {
  const record = {
    ...job("ordinary", false),
    state: {
      last_status: "error",
      last_run_at: "2026-11-03T01:00:00",
      last_error: "用户信息缺失\nworkflow sapId does not match target user",
    },
  } as ManagedJob;
  render(<>{column("last_status").render?.(null, record, 0)}</>);
  fireEvent.mouseEnter(screen.getByText("失败"));
  const tip = await screen.findByRole("tooltip");
  expect(tip).toHaveTextContent("2026-11-03 09:00:00");
  expect(tip).toHaveTextContent("workflow sapId does not match target user");
});

it("列表展示可执行时间区间，未配置时显示不限日期段", () => {
  const record = {
    ...job("ordinary", false),
    run_date_range: { start_date: "2026-11-01", end_date: "2026-11-30" },
  };
  const dateColumn = column("run_date_range");
  expect(dateColumn).toBeDefined();
  const view = render(<>{dateColumn.render?.(null, record, 0)}</>);
  expect(screen.getByText(/2026-11-01.*2026-11-30/)).toBeVisible();
  view.unmount();
  render(<>{dateColumn.render?.(null, job("ordinary", true), 0)}</>);
  expect(screen.getByText("不限日期段")).toBeVisible();
  expect(screen.getByText("继承父任务")).toBeVisible();
});

it.each(["batch_enabled", "batch_paused"] as const)(
  "%s 的父任务和子任务都在自己的执行时间前显示批调度",
  (mode) => {
    for (const child of [false, true]) {
      const record = job(mode, child);
      const view = render(
        <>{column("cron").render?.(record.schedule.cron, record, 0)}</>,
      );
      const badge = screen.getByText("批调度");
      expect(badge).toHaveClass("ant-tag-processing");
      expect(badge.nextElementSibling).toHaveTextContent(
        child ? "10:30" : "09:00",
      );
      expect(view.container.textContent).not.toContain("05:00");
      view.unmount();
    }
  },
);

it("普通任务即使残留旧批标记也不显示，普通子任务保留自己的时间", () => {
  const record = job("ordinary", true);
  record.meta!.broadcast_dispatch_intents_enabled = true;
  render(<>{column("cron").render?.(record.schedule.cron, record, 0)}</>);
  expect(screen.queryByText("批调度")).toBeNull();
  expect(screen.getByText(/10:30/)).toBeVisible();
});

it("增加执行时间标识不移除任务名旁的分发子任务标签", () => {
  const record = job("batch_enabled", true);
  render(<>{column("name").render?.(record.name, record, 0)}</>);
  expect(screen.getByText("任务")).toBeVisible();
  expect(screen.getByText("分发子任务")).toBeVisible();
});

it("没有实际分发过的独立任务不显示父任务标识", () => {
  const record = job("batch_enabled", false);
  const { rerender } = render(<>{column("name").render?.(record.name, record, 0)}</>);
  expect(screen.queryByText("父任务")).toBeNull();
  const parent = { ...record, has_distributed_children: true };
  rerender(<>{column("name").render?.(parent.name, parent, 0)}</>);
  expect(screen.getByText("父任务")).toBeVisible();
});

it.each(["技能任务执行失败 [workflow=http_status, HTTP=502]", ""])(
  "新版error悬浮展示当前失败详情或明确缺失提示：%s",
  async (detail) => {
    const record = {
      ...job("ordinary", false),
      state: { last_status: "error", last_error: detail },
    } as ManagedJob;
    render(<>{column("last_status").render?.(null, record, 0)}</>);
    fireEvent.mouseEnter(screen.getByText("失败"));
    expect(await screen.findByRole("tooltip")).toHaveTextContent(
      detail || "暂无失败详情，请查看执行日志",
    );
  },
);

it.each([false, true])(
  "成功或正在运行时不展示旧错误，运行优先=%s",
  async (running) => {
    const record = {
      ...job("ordinary", false),
      state: {
        last_status: running ? "error" : "success",
        last_error: "过期错误",
      },
      task: { is_running: running },
    } as ManagedJob;
    render(<>{column("last_status").render?.(null, record, 0)}</>);
    fireEvent.mouseEnter(screen.getByText(running ? "运行中" : "成功"));
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip).toHaveTextContent("最近执行时间");
    expect(tooltip).not.toHaveTextContent("过期错误");
  },
);

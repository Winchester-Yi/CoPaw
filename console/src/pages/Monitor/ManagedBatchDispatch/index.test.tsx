import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { CronDispatchCapacityItem } from "@/api/modules/monitor";
import ManagedBatchDispatchPage from "./index";
import WorkerTrendDialog from "./WorkerTrendDialog";

const api = vi.hoisted(() => ({
  batches: vi.fn(),
  detail: vi.fn(),
  workers: vi.fn(),
  workerOccupants: vi.fn(),
  workerTrends: vi.fn(),
  failureSummary: vi.fn(),
  failures: vi.fn(),
}));
const context = vi.hoisted(() => ({
  source: "bank",
  userId: "manager",
  manager: true,
  isSuperManager: false,
}));
const chartOptions = vi.hoisted(() => vi.fn());
vi.mock("../../../api/modules/managedBatchBoard", () => ({
  managedBatchBoard: api,
}));
vi.mock("../../../stores/iframeStore", () => ({
  useIframeStore: (selector: (state: typeof context) => unknown) =>
    selector(context),
}));
vi.mock("echarts-for-react", () => ({
  default: ({ option }: { option: unknown }) => {
    chartOptions(option);
    return <div>测试容量曲线</div>;
  },
}));
beforeEach(() => {
  vi.clearAllMocks();
  api.workerOccupants.mockResolvedValue({
    total: 0,
    page: 1,
    page_size: 10,
    sampled_at: "2026-11-12T01:00:00Z",
    items: [],
  });
  context.manager = true;
  api.batches.mockResolvedValue({
    items: [],
    total: 0,
    stats: {
      total_batches: 0,
      total_members: 0,
      done_members: 0,
      failed_members: 0,
      running_batches: 0,
    },
  });
  api.workers.mockResolvedValue({
    source_id: "bank",
    policies: [],
    current_capacity: [],
    capacity_events: [],
    capacity_events_next_cursor: null,
  });
});
afterEach(cleanup);

it("点模型后只查询到当前时刻，曲线和联动时间轴同步截断", async () => {
  api.workers.mockResolvedValue({
    source_id: "bank",
    policies: [],
    capacity_events: [],
    current_capacity: [
      {
        id: 1,
        provider_id: "p",
        model_id: "m",
        effective_workers: 4,
        max_workers: 8,
      },
    ],
  });
  api.workerTrends.mockImplementation(
    async (_source, { end_time }: { end_time: string }) => ({
      bucket_seconds: 600,
      points: [
        {
          at: end_time,
          total: 2,
          success: 1,
          failed: 0,
          requests: 3,
          retries: 1,
        },
        {
          at: new Date(Date.parse(end_time) + 3_600_000).toISOString(),
          total: 10,
          success: 1,
          failed: 0,
          requests: 11,
          retries: 1,
        },
      ],
    }),
  );
  render(<ManagedBatchDispatchPage />);
  fireEvent.click(
    await screen.findByRole("button", {
      name: "查看 p / m 的 Worker 调整过程",
    }),
  );
  const dialog = await screen.findByRole("dialog");
  await waitFor(() => expect(api.workerTrends).toHaveBeenCalledOnce());
  const cutoff = Date.parse(api.workerTrends.mock.lastCall?.[1].end_time);
  expect(cutoff).toBeLessThanOrEqual(Date.now());
  const chart = await within(dialog).findByText("测试容量曲线");
  expect(chart).toBeInTheDocument();
  const option = chartOptions.mock.lastCall?.[0] as {
    xAxis: { max: number }[];
    axisPointer: { link: { xAxisIndex: number[] }[] };
    dataZoom: { xAxisIndex: number[] }[];
    tooltip: {
      formatter: (params: { axisIndex: number; axisValue: number }[]) => string;
    };
    series: { name: string; data: [number, number][] }[];
  };
  expect(option.xAxis.map((axis) => axis.max)).toEqual(Array(4).fill(cutoff));
  expect(option.axisPointer.link).toEqual([
    { xAxisIndex: [0, 1] },
    { xAxisIndex: [2, 3] },
  ]);
  expect(option.dataZoom.map((zoom) => zoom.xAxisIndex)).toEqual([
    [0, 1],
    [0, 1],
    [2, 3],
    [2, 3],
  ]);
  const left = option.tooltip.formatter([{ axisIndex: 1, axisValue: cutoff }]);
  expect(left).toContain("有效 Worker");
  expect(left).toContain("待运行任务");
  expect(left).not.toContain("累计执行数");
  expect(left).not.toContain("发起请求总数");
  const right = option.tooltip.formatter([{ axisIndex: 3, axisValue: cutoff }]);
  expect(right).toContain("累计执行数 2");
  expect(right).toContain("发起请求总数 3");
  expect(right).not.toContain("有效 Worker");
  expect(right).not.toContain("待运行任务");
  for (const name of ["累计执行数", "发起请求总数"]) {
    const data = option.series.find((series) => series.name === name)?.data;
    expect(data?.slice(-1)[0]).toEqual([cutoff, name === "累计执行数" ? 2 : 3]);
  }
  expect(api.workerTrends).toHaveBeenCalledWith(
    "bank",
    expect.objectContaining({ provider_id: "p", model_id: "m" }),
    expect.any(AbortSignal),
  );
});

it("整个时间区间在未来时不请求趋势接口", async () => {
  const now = Date.now();
  render(
    <WorkerTrendDialog
      pool={{ provider_id: "p", model_id: "m" } as CronDispatchCapacityItem}
      history={[]}
      scope={{
        source: "bank",
        start: new Date(now + 3_600_000).toISOString(),
        end: new Date(now + 7_200_000).toISOString(),
        refresh: 0,
      }}
      onClose={() => {}}
    />,
  );
  expect(await screen.findByText("当前时间范围内暂无趋势记录")).toBeInTheDocument();
  expect(api.workerTrends).not.toHaveBeenCalled();
});

it("看板顶部不按子任务状态筛选", async () => {
  api.failureSummary.mockResolvedValue({
    total: 0,
    groups: [],
    queried_at: "2026-11-20T03:00:00Z",
  });
  render(<ManagedBatchDispatchPage />);
  const summary = await screen.findByRole("button", {
    name: /查看失败类型及详情/,
  });
  expect(
    screen.queryByRole("combobox", { name: /^子任务状态$/ }),
  ).not.toBeInTheDocument();
  expect(api.batches.mock.lastCall?.[1]).not.toHaveProperty("child_status");
  fireEvent.click(summary);
  await waitFor(() => expect(api.failureSummary).toHaveBeenCalledOnce());
  expect(api.failureSummary.mock.lastCall?.[1]).not.toHaveProperty(
    "child_status",
  );
});
it("非管理员不加载跨用户看板数据", () => {
  context.manager = false;
  render(<ManagedBatchDispatchPage />);
  expect(screen.getByText("仅管理员可访问批调度看板（新）")).toBeTruthy();
  expect(api.batches).not.toHaveBeenCalled();
  expect(api.workers).not.toHaveBeenCalled();
  expect(api.workerOccupants).not.toHaveBeenCalled();
});

it("成员筛选传给批次清单和失败汇总，并回到第一页", async () => {
  api.batches.mockResolvedValue({
    items: [],
    total: 8,
    stats: {
      total_batches: 8,
      total_members: 0,
      done_members: 0,
      failed_members: 0,
      running_batches: 0,
    },
  });
  api.failureSummary.mockResolvedValue({
    total: 0,
    groups: [],
    queried_at: "2026-11-24T01:00:00Z",
  });
  render(<ManagedBatchDispatchPage />);
  fireEvent.click(await screen.findByTitle("2"));
  await waitFor(() =>
    expect(api.batches).toHaveBeenLastCalledWith(
      "bank",
      expect.objectContaining({ page: 2 }),
      expect.any(AbortSignal),
    ),
  );
  const filter = screen.getByRole("combobox", { name: "可执行成员" });
  fireEvent.mouseDown(filter);
  fireEvent.click(await screen.findByText("有可执行成员", { exact: true }));
  await waitFor(() =>
    expect(api.batches).toHaveBeenLastCalledWith(
      "bank",
      expect.objectContaining({ has_members: true, page: 1 }),
      expect.any(AbortSignal),
    ),
  );
  fireEvent.mouseDown(filter);
  fireEvent.click(await screen.findByText("无可执行成员", { exact: true }));
  await waitFor(() =>
    expect(api.batches).toHaveBeenLastCalledWith(
      "bank",
      expect.objectContaining({ has_members: false, page: 1 }),
      expect.any(AbortSignal),
    ),
  );
  fireEvent.click(
    await screen.findByRole("button", { name: /查看失败类型及详情/ }),
  );
  await waitFor(() =>
    expect(api.failureSummary).toHaveBeenCalledWith(
      "bank",
      expect.objectContaining({ has_members: false }),
      expect.any(AbortSignal),
    ),
  );
});

it("当前 Worker 占用独立查询并展示任务明细", async () => {
  api.workerOccupants.mockResolvedValue({
    total: 1,
    page: 1,
    page_size: 10,
    sampled_at: "2026-11-12T01:00:00Z",
    items: [
      {
        id: "managed:one",
        provider_id: "provider",
        model_id: "model",
        tenant_id: "80074361",
        tenant_name: "测试人员",
        job_id: "worker-job",
        job_name: "日报定时任务",
        started_at: null,
        status: "accepted",
        execution_type: "daily_failure_retry",
      },
    ],
  });
  render(<ManagedBatchDispatchPage />);
  expect(
    await screen.findByRole("heading", { name: "当前 Worker 占用（1）" }),
  ).toBeVisible();
  expect(screen.getByText("测试人员")).toBeInTheDocument();
  expect(screen.getByText("80074361")).toBeInTheDocument();
  expect(screen.getByText("日报定时任务")).toBeInTheDocument();
  expect(screen.getByText("worker-job")).toBeInTheDocument();
  expect(screen.getByText("尚未开始")).toBeInTheDocument();
  expect(screen.getByText("整点补跑")).toBeInTheDocument();
  expect(api.workerOccupants).toHaveBeenCalledWith(
    "bank",
    { page: 1, page_size: 10 },
    expect.any(AbortSignal),
  );
});
it("零成员不伪造百分之百完成率，空数据没有旧接口回退", async () => {
  render(<ManagedBatchDispatchPage />);
  await waitFor(() => expect(api.batches).toHaveBeenCalled());
  const overview = screen.getByRole("region", { name: "新版批调度概览" });
  await waitFor(() =>
    expect(
      within(overview).getByText("任务完成率").parentElement?.textContent,
    ).toContain("-"),
  );
  expect(within(overview).queryByText("100%")).toBeNull();
  expect(await screen.findByText("当前范围内暂无 Batch")).toBeTruthy();
  expect(api.detail).not.toHaveBeenCalled();
});
it("共享容量中途失败明确标记不完整，不显示成功曲线", async () => {
  api.workers
    .mockResolvedValueOnce({
      source_id: "bank",
      policies: [],
      current_capacity: [],
      capacity_events: [],
      capacity_events_next_cursor: "next-page",
    })
    .mockRejectedValueOnce(new Error("history_unavailable"));
  render(<ManagedBatchDispatchPage />);
  expect(
    await screen.findByText("容量历史不完整：history_unavailable"),
  ).toBeTruthy();
  expect(screen.queryByText("测试容量曲线")).toBeNull();
});

it("批次列表显示父任务模型、入口暂停、成功失败及跳过数量", async () => {
  const batch = {
    batch_id: "batch",
    job_id: "parent",
    job_name: "日报",
    member_count: 4,
    done_count: 4,
    failed_count: 1,
    success_count: 2,
    skipped_count: 1,
    status: "failed",
    completion_rate: 1,
    dispatch_paused: true,
    provider_id: "provider",
    model_id: "model",
  };
  api.batches.mockResolvedValue({ items: [batch], total: 1, stats: {} });
  api.detail.mockResolvedValue({
    batch,
    members: [],
    member_total: 4,
    events: [],
    event_total: 0,
  });
  render(<ManagedBatchDispatchPage />);
  const row = await screen.findByRole("button", { name: /日报.*parent/ });
  expect(within(row).getByText("provider / model")).toBeInTheDocument();
  expect(within(row).getByText("成功 2 个")).toBeInTheDocument();
  expect(within(row).getByText("失败 1 个")).toBeInTheDocument();
  expect(within(row).getByText("跳过／取消 1 个")).toBeInTheDocument();
});

it("运行中批次只提示入口暂停，不误称已入队任务暂停", async () => {
  const batch = {
    batch_id: "batch",
    job_id: "parent",
    job_name: "日报",
    member_count: 4,
    done_count: 1,
    failed_count: 0,
    success_count: 1,
    skipped_count: 0,
    status: "running",
    completion_rate: 0.25,
    dispatch_paused: true,
  };
  api.batches.mockResolvedValue({ items: [batch], total: 1, stats: {} });
  api.detail.mockResolvedValue({
    batch,
    members: [],
    member_total: 4,
    events: [],
    event_total: 0,
  });
  render(<ManagedBatchDispatchPage />);
  expect(await screen.findByText("批调度入口已暂停")).toBeInTheDocument();
  expect(screen.queryByText("后续调度已暂停")).toBeNull();
});

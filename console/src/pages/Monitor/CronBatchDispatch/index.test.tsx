import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  CronDispatchBatchDetailResponse,
  CronDispatchBatchesResponse,
  CronDispatchWorkersResponse,
} from "../../../api/modules/monitor";
import CronBatchDispatchPage from "./index";

const monitorApiMock = vi.hoisted(() => ({
  getCronDispatchBatches: vi.fn(),
  getCronDispatchBatchDetail: vi.fn(),
  getCronDispatchWorkers: vi.fn(),
}));

const iframeState = vi.hoisted(() => ({
  source: "CMB-MALL",
  isSuperManager: false,
  manager: true,
}));

vi.mock("../../../api/modules/monitor", async () => {
  const actual = await vi.importActual<
    typeof import("../../../api/modules/monitor")
  >("../../../api/modules/monitor");
  return {
    ...actual,
    monitorApi: monitorApiMock,
  };
});

vi.mock("../../../stores/iframeStore", () => ({
  useIframeStore: (selector: (state: typeof iframeState) => unknown) =>
    selector(iframeState),
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

async function selectOption(label: string, option: string) {
  fireEvent.mouseDown(screen.getByLabelText(label));
  const target = (await screen.findAllByText(option)).find((element) =>
    element.closest(".ant-select-item-option"),
  );
  expect(target).toBeDefined();
  fireEvent.click(target!);
}

describe("CronBatchDispatchPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    iframeState.source = "CMB-MALL";
    iframeState.isSuperManager = false;
    iframeState.manager = true;

    monitorApiMock.getCronDispatchBatches.mockResolvedValue({
      source_id: "CMB-MALL",
      start_time: "2026-07-08T00:00:00",
      end_time: "2026-07-08T23:59:59",
      stats: {
        total_batches: 1,
        running_batches: 1,
        completed_batches: 0,
        failed_batches: 0,
        total_intents: 20,
        completed_intents: 12,
        failed_intents: 1,
        pending_intents: 7,
      },
      items: [
        {
          batch_id: "cron:batch-a",
          parent_job_id: "parent-a",
          parent_job_name: "展示定时任务名",
          parent_external_job_id: "external-a",
          tenant_id: "tenant-a",
          source_id: "CMB-MALL",
          provider_id: "aaa",
          model_id: "bbb",
          agent_id: "agent-a",
          scheduled_fire_at: "2026-07-08T12:00:00",
          callback_received_at: "2026-07-08T08:00:00",
          status: "running",
          lock_owner: "worker-a",
          locked_at: "2026-07-08T08:00:20",
          total_count: 20,
          completed_count: 12,
          failed_count: 1,
          error_message: "",
          completed_at: null,
          created_at: "2026-07-08T08:00:00",
          updated_at: "2026-07-08T08:01:00",
        },
        {
          batch_id: "cron:batch-b",
          parent_job_id: "parent-b",
          parent_job_name: "另一个定时任务",
          parent_external_job_id: "external-b",
          tenant_id: "tenant-b",
          source_id: "CMB-MALL",
          provider_id: "provider-z",
          model_id: "model-z",
          agent_id: "agent-b",
          scheduled_fire_at: "2026-07-08T13:00:00",
          callback_received_at: "2026-07-08T09:00:00",
          status: "failed",
          lock_owner: "worker-b",
          locked_at: "2026-07-08T09:00:20",
          total_count: 5,
          completed_count: 2,
          failed_count: 3,
          error_message: "failed",
          completed_at: "2026-07-08T09:10:00",
          created_at: "2026-07-08T09:00:00",
          updated_at: "2026-07-08T09:10:00",
        },
      ],
      total: 2,
      page: 1,
      page_size: 20,
    });

    monitorApiMock.getCronDispatchBatchDetail.mockResolvedValue({
      batch: {
        batch_id: "cron:batch-a",
        parent_job_id: "parent-a",
        parent_job_name: "展示定时任务名",
        parent_external_job_id: "external-a",
        tenant_id: "tenant-a",
        source_id: "CMB-MALL",
        provider_id: "aaa",
        model_id: "bbb",
        agent_id: "agent-a",
        scheduled_fire_at: "2026-07-08T12:00:00",
        callback_received_at: "2026-07-08T08:00:00",
        status: "running",
        lock_owner: "worker-a",
        locked_at: "2026-07-08T08:00:20",
        total_count: 20,
        completed_count: 12,
        failed_count: 1,
        error_message: "",
        completed_at: null,
        created_at: "2026-07-08T08:00:00",
        updated_at: "2026-07-08T08:01:00",
      },
      intent_total: 3,
      intent_filtered_total: 3,
      intent_page: 1,
      intent_page_size: 50,
      intents: [
        {
          id: 1001,
          batch_id: "cron:batch-a",
          intent_role: "child",
          status: "pending",
          source_id: "CMB-MALL",
          provider_id: "aaa",
          model_id: "bbb",
          tenant_id: "tenant-a",
          agent_id: "agent-a",
          job_id: "job-a",
          parent_job_id: "parent-a",
          scheduled_fire_at: "2026-07-08T12:00:00",
          due_at: "2026-07-08T08:05:00",
          dispatch_order: 1,
          viewer_heat_score: 0,
          attempt_count: 0,
          max_attempts: 3,
          lock_owner: "",
          locked_at: null,
          acked_at: null,
          completed_at: null,
          error_message: "",
          created_at: "2026-07-08T08:00:00",
          updated_at: "2026-07-08T08:00:00",
        },
        {
          id: 1002,
          batch_id: "cron:batch-a",
          intent_role: "parent",
          status: "completed",
          source_id: "CMB-MALL",
          provider_id: "aaa",
          model_id: "bbb",
          tenant_id: "tenant-parent",
          agent_id: "agent-a",
          job_id: "job-parent",
          parent_job_id: "parent-a",
          scheduled_fire_at: "2026-07-08T12:00:00",
          due_at: "2026-07-08T08:04:00",
          dispatch_order: 0,
          viewer_heat_score: 0,
          attempt_count: 1,
          max_attempts: 3,
          lock_owner: "worker-a",
          locked_at: null,
          acked_at: null,
          completed_at: "2026-07-08T08:04:00",
          error_message: "",
          created_at: "2026-07-08T08:00:00",
          updated_at: "2026-07-08T08:04:00",
        },
        {
          id: 1003,
          batch_id: "cron:batch-a",
          intent_role: "child",
          status: "failed",
          source_id: "CMB-MALL",
          provider_id: "aaa",
          model_id: "bbb",
          tenant_id: "tenant-match",
          agent_id: "agent-a",
          job_id: "job-matching",
          parent_job_id: "parent-a",
          scheduled_fire_at: "2026-07-08T12:00:00",
          due_at: "2026-07-08T08:06:00",
          dispatch_order: 2,
          viewer_heat_score: 0,
          attempt_count: 3,
          max_attempts: 3,
          lock_owner: "worker-a",
          locked_at: null,
          acked_at: null,
          completed_at: "2026-07-08T08:06:00",
          error_message: "timeout",
          created_at: "2026-07-08T08:00:00",
          updated_at: "2026-07-08T08:06:00",
        },
      ],
      events: [
        {
          id: 1,
          batch_id: "cron:batch-a",
          intent_id: 1001,
          event_type: "retry_scheduled",
          worker_id: "worker-a",
          job_id: "job-a",
          tenant_id: "tenant-a",
          source_id: "CMB-MALL",
          details: { error: "timeout" },
          created_at: "2026-07-08T08:06:00",
        },
      ],
      event_total: 1,
      event_page: 1,
      event_page_size: 50,
    });

    monitorApiMock.getCronDispatchWorkers.mockResolvedValue({
      source_id: "CMB-MALL",
      policies: [
        {
          source_id: "CMB-MALL",
          provider_id: "aaa",
          model_id: "bbb",
          default_strategy_id: "strategy-a",
          strategy_schedule: [
            { start_time: "16:00", end_time: "21:00", strategy_id: "peak_1" },
          ],
          enabled: true,
          strategy: {
            min_workers: 5,
            baseline_workers: 5,
            max_workers: 999,
            adjust_interval_seconds: 20,
            feedback_window_seconds: 20,
            error_rate_rules: { success_100: "double" },
          },
          created_at: "2026-07-08T07:00:00",
          updated_at: "2026-07-08T07:00:00",
        },
      ],
      current_capacity: [
        {
          id: 10,
          worker_id: "worker-a",
          source_id: "CMB-MALL",
          provider_id: "aaa",
          model_id: "bbb",
          strategy_id: "strategy-a",
          previous_workers: 5,
          baseline_workers: 5,
          min_workers: 5,
          max_workers: 999,
          effective_workers: 10,
          pending_count: 7,
          claimed_count: 2,
          running_count: 1,
          success_count: 12,
          failure_count: 1,
          error_rate: 0.08,
          matched_rule: { reason: "success_70_90_add_1" },
          avg_latency_ms: 1200,
          decision_reason: "success_70_90_add_1",
          created_at: "2026-07-08T08:10:00",
        },
      ],
      capacity_events: [
        {
          id: 11,
          worker_id: "worker-history",
          source_id: "CMB-MALL",
          provider_id: "aaa",
          model_id: "bbb",
          strategy_id: "strategy-a",
          previous_workers: 10,
          baseline_workers: 5,
          min_workers: 5,
          max_workers: 999,
          effective_workers: 5,
          pending_count: 3,
          claimed_count: 1,
          running_count: 1,
          success_count: 3,
          failure_count: 7,
          error_rate: 0.7,
          matched_rule: { reason: "success_below_30_halve" },
          avg_latency_ms: 1600,
          decision_reason: "success_below_30_halve",
          created_at: "2026-07-08T08:20:00",
        },
        {
          id: 12,
          worker_id: "worker-older",
          source_id: "CMB-MALL",
          provider_id: "aaa",
          model_id: "bbb",
          strategy_id: "strategy-a",
          previous_workers: 11,
          baseline_workers: 5,
          min_workers: 5,
          max_workers: 999,
          effective_workers: 10,
          pending_count: 2,
          claimed_count: 1,
          running_count: 1,
          success_count: 5,
          failure_count: 5,
          error_rate: 0.5,
          matched_rule: { reason: "success_50_70_sub_1" },
          avg_latency_ms: 1400,
          decision_reason: "success_50_70_sub_1",
          created_at: "2026-07-08T08:15:00",
        },
      ],
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    cleanup();
  });

  it("renders current source, batches, details, policies and workers", async () => {
    render(<CronBatchDispatchPage />);

    expect(screen.getByText("批调度监控")).toBeInTheDocument();
    expect(screen.getByText("当前渠道 CMB-MALL")).toBeInTheDocument();

    await waitFor(() => {
      expect(monitorApiMock.getCronDispatchBatches).toHaveBeenCalledTimes(1);
      expect(monitorApiMock.getCronDispatchWorkers).toHaveBeenCalledTimes(1);
    });

    await waitFor(() => {
      expect(monitorApiMock.getCronDispatchBatchDetail).toHaveBeenCalledWith(
        "cron:batch-a",
        {
          intent_page: "1",
          intent_limit: "50",
          event_page: "1",
          event_limit: "50",
        },
      );
    });

    expect(screen.getAllByText("parent-a").length).toBeGreaterThan(0);
    expect(screen.getAllByText("展示定时任务名").length).toBeGreaterThanOrEqual(
      2,
    );
    expect(
      screen.getByRole("heading", { name: "展示定时任务名" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("aaa").length).toBeGreaterThan(0);
    expect(screen.getAllByText("bbb").length).toBeGreaterThan(0);
    expect(screen.getByText("success_70_90_add_1")).toBeInTheDocument();
  });

  it("uses a fixed four-row page without an inner scrolling region", async () => {
    render(<CronBatchDispatchPage />);

    await waitFor(() => {
      expect(monitorApiMock.getCronDispatchBatches).toHaveBeenCalledWith(
        1,
        4,
        expect.any(Object),
      );
    });
    expect(screen.queryByRole("region", { name: "Batch 列表" })).toBeNull();
    expect(screen.queryByText(/条\/页/)).toBeNull();
  });

  it("uses the unnamed label instead of IDs when the cron job name is unavailable", async () => {
    const response =
      (await monitorApiMock.getCronDispatchBatches()) as CronDispatchBatchesResponse;
    const detail =
      (await monitorApiMock.getCronDispatchBatchDetail()) as CronDispatchBatchDetailResponse;
    const unnamedBatch = {
      ...response.items[0],
      parent_job_name: "",
    };
    monitorApiMock.getCronDispatchBatches.mockResolvedValue({
      ...response,
      items: [unnamedBatch],
      total: 1,
    });
    monitorApiMock.getCronDispatchBatchDetail.mockResolvedValue({
      ...detail,
      batch: unnamedBatch,
      intents: [],
      intent_total: 0,
      events: [],
    });

    render(<CronBatchDispatchPage />);

    const unnamedLabels = await screen.findAllByText("未命名定时任务");
    fireEvent.click(unnamedLabels[0].closest("button")!);
    expect(
      await screen.findByRole("heading", { name: "未命名定时任务" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("未命名定时任务").length).toBeGreaterThanOrEqual(
      2,
    );
    expect(
      screen.queryByRole("heading", { name: "external-a" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "parent-a" }),
    ).not.toBeInTheDocument();
    expect(screen.getAllByText("parent-a").length).toBeGreaterThan(0);
  });

  it("refetches the last valid page when the current page becomes out of range", async () => {
    const baseResponse =
      (await monitorApiMock.getCronDispatchBatches()) as CronDispatchBatchesResponse;
    monitorApiMock.getCronDispatchBatches.mockClear();
    const firstBatch = baseResponse.items[0];
    const returnedBatch = baseResponse.items[1];
    expect(firstBatch).toBeDefined();
    expect(returnedBatch).toBeDefined();

    const validPageRequest = deferred<CronDispatchBatchesResponse>();
    monitorApiMock.getCronDispatchBatches
      .mockResolvedValueOnce({
        ...baseResponse,
        items: [firstBatch!],
        total: 8,
        page: 1,
        page_size: 4,
      })
      .mockResolvedValueOnce({
        ...baseResponse,
        items: [],
        total: 4,
        page: 2,
        page_size: 4,
      })
      .mockReturnValueOnce(validPageRequest.promise);

    render(<CronBatchDispatchPage />);
    expect(await screen.findByText("展示定时任务名")).toBeInTheDocument();

    fireEvent.click(screen.getByTitle("2"));
    await waitFor(() => {
      expect(monitorApiMock.getCronDispatchBatches).toHaveBeenLastCalledWith(
        1,
        4,
        expect.any(Object),
      );
      expect(monitorApiMock.getCronDispatchBatches).toHaveBeenCalledTimes(3);
    });

    expect(screen.getAllByText("展示定时任务名").length).toBeGreaterThan(0);
    expect(screen.queryByText("当前范围内暂无 Batch")).not.toBeInTheDocument();

    validPageRequest.resolve({
      ...baseResponse,
      items: [returnedBatch!],
      total: 4,
      page: 1,
      page_size: 4,
    });
    expect(await screen.findByText("另一个定时任务")).toBeInTheDocument();
  });

  it("shows one worker adjustment at a time, navigates and expands details", async () => {
    const workersResponse = (await monitorApiMock.getCronDispatchWorkers()) as
      | CronDispatchWorkersResponse
      | undefined;
    expect(workersResponse).toBeDefined();
    monitorApiMock.getCronDispatchWorkers.mockClear();

    render(<CronBatchDispatchPage />);

    const scheduleSummary = await screen.findByText(/^schedule=/);
    fireEvent.mouseEnter(scheduleSummary);
    expect(
      await screen.findByText(/"start_time": "16:00"/),
    ).toBeInTheDocument();

    const previous = screen.getByRole("button", { name: "上一条调整记录" });
    const next = screen.getByRole("button", { name: "下一条调整记录" });
    expect(previous).toBeDisabled();
    expect(next).toBeEnabled();
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
    expect(screen.getByLabelText("查看调整记录 11")).toBeInTheDocument();
    expect(screen.queryByLabelText("查看调整记录 12")).not.toBeInTheDocument();

    fireEvent.click(next);

    expect(previous).toBeEnabled();
    expect(next).toBeDisabled();
    expect(screen.getByText("2 / 2")).toBeInTheDocument();
    expect(screen.queryByLabelText("查看调整记录 11")).not.toBeInTheDocument();
    const toggle = screen.getByLabelText("查看调整记录 12");
    const details = toggle.closest("details");
    expect(details).not.toHaveAttribute("open");

    fireEvent.click(toggle);
    expect(details).toHaveAttribute("open");
    expect(screen.getByText("Worker ID")).toBeInTheDocument();
    expect(screen.getByText("worker-older")).toBeInTheDocument();
    expect(screen.getByText("50.00%")).toBeInTheDocument();

    monitorApiMock.getCronDispatchWorkers.mockResolvedValue({
      ...workersResponse!,
      capacity_events: [
        {
          ...workersResponse!.capacity_events[0],
          id: 13,
          worker_id: "worker-latest",
          created_at: "2026-07-08T08:25:00",
        },
        ...workersResponse!.capacity_events,
      ],
    });
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));

    expect(await screen.findByLabelText("查看调整记录 13")).toBeInTheDocument();
    expect(screen.getByText("1 / 3")).toBeInTheDocument();
    expect(previous).toBeDisabled();
    expect(next).toBeEnabled();
    expect(screen.queryByText("worker-older")).not.toBeInTheDocument();
  }, 30_000);

  it("sends a global Batch query, resets to page one, and does not filter locally", async () => {
    const response =
      (await monitorApiMock.getCronDispatchBatches()) as CronDispatchBatchesResponse;
    monitorApiMock.getCronDispatchBatches.mockClear();
    monitorApiMock.getCronDispatchBatches.mockResolvedValue({
      ...response,
      total: 8,
    });
    render(<CronBatchDispatchPage />);

    await waitFor(() => {
      expect(monitorApiMock.getCronDispatchBatchDetail).toHaveBeenCalledWith(
        "cron:batch-a",
        {
          intent_page: "1",
          intent_limit: "50",
          event_page: "1",
          event_limit: "50",
        },
      );
    });

    fireEvent.click(screen.getByTitle("2"));
    await waitFor(() => {
      expect(monitorApiMock.getCronDispatchBatches).toHaveBeenLastCalledWith(
        2,
        4,
        expect.any(Object),
      );
    });

    vi.useFakeTimers();
    const requestCountBeforeSearch =
      monitorApiMock.getCronDispatchBatches.mock.calls.length;
    fireEvent.change(screen.getByLabelText("全局筛选 Batch"), {
      target: { value: "m" },
    });
    fireEvent.change(screen.getByLabelText("全局筛选 Batch"), {
      target: { value: "model" },
    });
    fireEvent.change(screen.getByLabelText("全局筛选 Batch"), {
      target: { value: "model-z" },
    });

    expect(screen.getAllByText("展示定时任务名").length).toBeGreaterThan(0);
    expect(screen.getAllByText("另一个定时任务").length).toBeGreaterThan(0);
    expect(monitorApiMock.getCronDispatchBatches).toHaveBeenCalledTimes(
      requestCountBeforeSearch,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(299);
    });
    expect(monitorApiMock.getCronDispatchBatches).toHaveBeenCalledTimes(
      requestCountBeforeSearch,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(monitorApiMock.getCronDispatchBatches).toHaveBeenLastCalledWith(
      1,
      4,
      expect.objectContaining({ query: "model-z" }),
    );
    expect(screen.getAllByText("共 8 个全局结果").length).toBeGreaterThan(0);
  });

  it("ignores the pending Batch response while a global query is debouncing", async () => {
    const baseResponse =
      (await monitorApiMock.getCronDispatchBatches()) as CronDispatchBatchesResponse;
    monitorApiMock.getCronDispatchBatches.mockClear();
    const oldBatch = baseResponse.items[0];
    const filteredBatch = baseResponse.items[1];
    expect(oldBatch).toBeDefined();
    expect(filteredBatch).toBeDefined();

    const initialRequest = deferred<CronDispatchBatchesResponse>();
    const filteredRequest = deferred<CronDispatchBatchesResponse>();
    monitorApiMock.getCronDispatchBatches
      .mockReturnValueOnce(initialRequest.promise)
      .mockReturnValueOnce(filteredRequest.promise);
    vi.useFakeTimers();

    render(<CronBatchDispatchPage />);
    expect(monitorApiMock.getCronDispatchBatches).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByLabelText("全局筛选 Batch"), {
      target: { value: "model-z" },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(250);
      initialRequest.resolve({
        ...baseResponse,
        items: [oldBatch!],
        total: 37,
      });
      await Promise.resolve();
    });

    expect(monitorApiMock.getCronDispatchBatches).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("展示定时任务名")).not.toBeInTheDocument();
    expect(screen.queryByText("共 37 个全局结果")).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(49);
    });
    expect(monitorApiMock.getCronDispatchBatches).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(monitorApiMock.getCronDispatchBatches).toHaveBeenCalledTimes(2);
    expect(monitorApiMock.getCronDispatchBatches).toHaveBeenLastCalledWith(
      1,
      4,
      expect.objectContaining({ query: "model-z" }),
    );

    await act(async () => {
      filteredRequest.resolve({
        ...baseResponse,
        items: [filteredBatch!],
        total: 1,
      });
      await Promise.resolve();
    });

    expect(screen.getByText("另一个定时任务")).toBeInTheDocument();
    expect(screen.queryByText("展示定时任务名")).not.toBeInTheDocument();
    expect(screen.getAllByText("共 1 个全局结果").length).toBeGreaterThan(0);
  });

  it("combines Intent text, role and status filters", async () => {
    render(<CronBatchDispatchPage />);

    await screen.findByText("job-matching");
    vi.useFakeTimers();
    fireEvent.change(screen.getByLabelText("筛选 Intent"), {
      target: { value: "job" },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    vi.useRealTimers();
    expect(monitorApiMock.getCronDispatchBatchDetail).toHaveBeenLastCalledWith(
      "cron:batch-a",
      expect.objectContaining({ intent_query: "job", intent_page: "1" }),
    );

    await selectOption("Intent 角色", "子任务");
    expect(monitorApiMock.getCronDispatchBatchDetail).toHaveBeenLastCalledWith(
      "cron:batch-a",
      expect.objectContaining({ intent_query: "job", intent_role: "child" }),
    );

    await selectOption("Intent 状态", "失败");
    expect(monitorApiMock.getCronDispatchBatchDetail).toHaveBeenLastCalledWith(
      "cron:batch-a",
      expect.objectContaining({
        intent_query: "job",
        intent_role: "child",
        intent_status: "failed",
      }),
    );
  });

  it("pages through all Intents and dispatch events", async () => {
    const detail =
      (await monitorApiMock.getCronDispatchBatchDetail()) as CronDispatchBatchDetailResponse;
    monitorApiMock.getCronDispatchBatchDetail.mockClear();
    monitorApiMock.getCronDispatchBatchDetail.mockResolvedValue({
      ...detail,
      intent_total: 650,
      intent_filtered_total: 650,
      event_total: 725,
    });

    render(<CronBatchDispatchPage />);

    const intentPagination = await screen.findByRole("navigation", {
      name: "Intent 分页",
    });
    fireEvent.click(within(intentPagination).getByTitle("2"));
    await waitFor(() => {
      expect(
        monitorApiMock.getCronDispatchBatchDetail,
      ).toHaveBeenLastCalledWith(
        "cron:batch-a",
        expect.objectContaining({ intent_page: "2", event_page: "1" }),
      );
    });

    await selectOption("Intent 角色", "子任务");
    expect(monitorApiMock.getCronDispatchBatchDetail).toHaveBeenLastCalledWith(
      "cron:batch-a",
      expect.objectContaining({ intent_page: "1", intent_role: "child" }),
    );

    fireEvent.click(screen.getByRole("tab", { name: /调度事件/ }));
    const eventPagination = await screen.findByRole("navigation", {
      name: "调度事件分页",
    });
    fireEvent.click(within(eventPagination).getByTitle("2"));
    await waitFor(() => {
      expect(
        monitorApiMock.getCronDispatchBatchDetail,
      ).toHaveBeenLastCalledWith(
        "cron:batch-a",
        expect.objectContaining({ intent_page: "1", event_page: "2" }),
      );
    });
  });

  it("ignores stale Batch responses after the date filter changes", async () => {
    const initialResponse =
      (await monitorApiMock.getCronDispatchBatches()) as CronDispatchBatchesResponse;
    monitorApiMock.getCronDispatchBatches.mockClear();
    const secondBatch = initialResponse.items[1];
    expect(secondBatch).toBeDefined();

    const firstRequest = deferred<CronDispatchBatchesResponse>();
    const secondRequest = deferred<CronDispatchBatchesResponse>();
    let requestCount = 0;
    monitorApiMock.getCronDispatchBatches.mockImplementation(() => {
      requestCount += 1;
      return requestCount === 1 ? firstRequest.promise : secondRequest.promise;
    });

    render(<CronBatchDispatchPage />);
    await waitFor(() => {
      expect(monitorApiMock.getCronDispatchBatches).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByText("近24h"));
    await waitFor(() => {
      expect(monitorApiMock.getCronDispatchBatches).toHaveBeenCalledTimes(2);
    });

    secondRequest.resolve({
      ...initialResponse,
      items: [secondBatch!],
      total: 1,
    });
    expect(await screen.findByText("另一个定时任务")).toBeInTheDocument();

    firstRequest.resolve(initialResponse);
    await waitFor(() => {
      expect(screen.getByText("另一个定时任务")).toBeInTheDocument();
      expect(screen.queryByText("展示定时任务名")).not.toBeInTheDocument();
    });
  });

  it("ignores stale detail responses after switching Batch", async () => {
    const batchesResponse =
      (await monitorApiMock.getCronDispatchBatches()) as CronDispatchBatchesResponse;
    monitorApiMock.getCronDispatchBatches.mockClear();
    monitorApiMock.getCronDispatchBatches.mockImplementation(
      (_page: number, _pageSize: number, filters?: { query?: string }) =>
        Promise.resolve(
          filters?.query
            ? {
                ...batchesResponse,
                items: [batchesResponse.items[1]],
                total: 1,
              }
            : batchesResponse,
        ),
    );
    const batchADetail = (await monitorApiMock.getCronDispatchBatchDetail(
      "cron:batch-a",
    )) as CronDispatchBatchDetailResponse;
    monitorApiMock.getCronDispatchBatchDetail.mockClear();

    const batchARequest = deferred<CronDispatchBatchDetailResponse>();
    const batchBRequest = deferred<CronDispatchBatchDetailResponse>();
    const batchBDetail: CronDispatchBatchDetailResponse = {
      ...batchADetail,
      batch: {
        ...batchADetail.batch,
        batch_id: "cron:batch-b",
        parent_job_id: "parent-b",
        parent_job_name: "另一个定时任务",
        parent_external_job_id: "external-b",
        provider_id: "provider-z",
        model_id: "model-z",
      },
      intents: [],
      intent_total: 0,
      events: [],
    };
    monitorApiMock.getCronDispatchBatchDetail.mockImplementation(
      (batchId: string) =>
        batchId === "cron:batch-a"
          ? batchARequest.promise
          : batchBRequest.promise,
    );

    render(<CronBatchDispatchPage />);
    await waitFor(() => {
      expect(monitorApiMock.getCronDispatchBatchDetail).toHaveBeenCalledWith(
        "cron:batch-a",
        {
          intent_page: "1",
          intent_limit: "50",
          event_page: "1",
          event_limit: "50",
        },
      );
    });

    fireEvent.change(screen.getByLabelText("全局筛选 Batch"), {
      target: { value: "model-z" },
    });
    await new Promise((resolve) => window.setTimeout(resolve, 350));
    await waitFor(() => {
      expect(monitorApiMock.getCronDispatchBatchDetail).toHaveBeenCalledWith(
        "cron:batch-b",
        {
          intent_page: "1",
          intent_limit: "50",
          event_page: "1",
          event_limit: "50",
        },
      );
    });

    batchBRequest.resolve(batchBDetail);
    expect(
      await screen.findByRole("heading", { name: "另一个定时任务" }),
    ).toBeInTheDocument();

    batchARequest.resolve(batchADetail);
    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "另一个定时任务" }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("heading", { name: "展示定时任务名" }),
      ).not.toBeInTheDocument();
    });
  }, 10_000);

  it("switches between Intent and dispatch event tabs", async () => {
    render(<CronBatchDispatchPage />);

    await screen.findByText("job-matching");
    expect(screen.queryByText("retry_scheduled")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /调度事件/ }));
    expect(await screen.findByText("retry_scheduled")).toBeInTheDocument();
    expect(screen.queryByText("job-matching")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /Intent/ }));
    expect(await screen.findByText("job-matching")).toBeInTheDocument();
  });

  it("allows a super manager without the regular manager flag", async () => {
    iframeState.manager = false;
    iframeState.isSuperManager = true;

    render(<CronBatchDispatchPage />);

    expect(screen.getByText("批调度监控")).toBeInTheDocument();
    await waitFor(() => {
      expect(monitorApiMock.getCronDispatchBatches).toHaveBeenCalledTimes(1);
    });
  });

  it("blocks non-admin users", () => {
    iframeState.manager = false;
    iframeState.isSuperManager = false;

    render(<CronBatchDispatchPage />);

    expect(
      screen.getByText("仅管理员可访问批调度监控页面"),
    ).toBeInTheDocument();
    expect(monitorApiMock.getCronDispatchBatches).not.toHaveBeenCalled();
  });
});

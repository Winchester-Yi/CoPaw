import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import TaskCenterPage from "./index";

const monitorApiMock = vi.hoisted(() => ({
  getAsyncTasks: vi.fn(),
  getAsyncTaskDetail: vi.fn(),
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
  useIframeStore: (selector: (state: { source: string }) => unknown) =>
    selector({ source: "CMB-MALL" }),
}));

describe("TaskCenterPage", () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    monitorApiMock.getAsyncTasks.mockResolvedValue({
      items: [
        {
          task_id: "task-1",
          service: "swe",
          task_type: "provider.providers.distribute",
          status: "running",
          title: "供应商分发",
          summary: "",
          source_id: "CMB-MALL",
          target_count: 6,
          done_count: 3,
          failed_count: 1,
          created_at: "2026-07-08T08:00:00",
          updated_at: "2026-07-08T08:10:00",
          finished_at: null,
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
    });
    monitorApiMock.getAsyncTaskDetail.mockResolvedValue({
      task_id: "task-1",
      service: "swe",
      task_type: "provider.providers.distribute",
      status: "running",
      title: "供应商分发",
      summary: "",
      source_id: "CMB-MALL",
      target_count: 6,
      done_count: 3,
      failed_count: 1,
      created_at: "2026-07-08T08:00:00",
      updated_at: "2026-07-08T08:10:00",
      finished_at: null,
      items: [
        {
          task_id: "task-1",
          target_id: "t-1",
          status: "skipped",
          target_name: "跳过",
          error_message: null,
        },
        {
          task_id: "task-1",
          target_id: "t-2",
          status: "failed",
          target_name: "失败",
          error_message: "err",
        },
        {
          task_id: "task-1",
          target_id: "t-3",
          status: "succeeded",
          target_name: "成功",
          error_message: null,
        },
        {
          task_id: "task-1",
          target_id: "t-4",
          status: "running",
          target_name: "运行",
          error_message: null,
        },
        {
          task_id: "task-1",
          target_id: "t-5",
          status: "queued",
          target_name: "排队",
          error_message: null,
        },
        {
          task_id: "task-1",
          target_id: "t-6",
          status: "created",
          target_name: "创建",
          error_message: null,
        },
      ],
    });
  });

  it("orders detail rows by status in the task detail modal", async () => {
    render(<TaskCenterPage />);

    await waitFor(() => {
      expect(monitorApiMock.getAsyncTasks).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole("button", { name: /供应商分发/ }));

    await screen.findByText("分发明细");
    const targetCells = screen.getAllByText(/^t-/).map((node) => node.textContent);
    expect(targetCells).toEqual(["t-2", "t-4", "t-5", "t-6", "t-3", "t-1"]);
  });
});

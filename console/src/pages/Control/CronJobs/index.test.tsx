import React from "react";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CronJobSpecOutput } from "@/api/types";
import CronJobsPage from "./index";

const mocks = vi.hoisted(() => {
  const job: CronJobSpecOutput = {
    id: "job-source",
    name: "ark",
    enabled: true,
    schedule: {
      type: "cron",
      cron: "0 5 * * thu,fri,sat,sun",
      timezone: "Asia/Shanghai",
    },
    dispatch: {
      type: "channel",
      target: {
        user_id: "source-user",
        session_id: "session-1",
      },
    },
  };
  return {
    job,
    getUserTimezone: vi.fn(),
    getCurrentCronBroadcastTask: vi.fn(),
    getCronBroadcastTask: vi.fn(),
    getCronJob: vi.fn(),
    listSweSkills: vi.fn(),
    broadcastCronJob: vi.fn(),
    enableCronBatchDispatch: vi.fn(),
    disableCronBatchDispatch: vi.fn(),
    setBatchDispatch: vi.fn(),
    fetchJobs: vi.fn(),
    message: {
      error: vi.fn(),
      info: vi.fn(),
      success: vi.fn(),
      warning: vi.fn(),
    },
  };
});

vi.mock("../../../api", () => ({
  default: {
    getUserTimezone: mocks.getUserTimezone,
    getCurrentCronBroadcastTask: mocks.getCurrentCronBroadcastTask,
    getCronBroadcastTask: mocks.getCronBroadcastTask,
    getCronJob: mocks.getCronJob,
    listSweSkills: mocks.listSweSkills,
    broadcastCronJob: mocks.broadcastCronJob,
    enableCronBatchDispatch: mocks.enableCronBatchDispatch,
    disableCronBatchDispatch: mocks.disableCronBatchDispatch,
  },
}));

vi.mock("../../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: mocks.message }),
}));

vi.mock("../../../utils/identity", () => ({
  getUserId: () => "current-tenant",
}));

vi.mock("@/hooks/useExecutionModelOptions", () => ({
  buildExecutionModelKey: () => "tenant-default",
  useExecutionModelOptions: () => ({
    loading: false,
    options: [],
    tenantDefaultLabel: "Tenant default",
  }),
}));

vi.mock("@/components/PageHeader", () => ({
  PageHeader: ({ extra }: { extra?: React.ReactNode }) => <div>{extra}</div>,
}));

vi.mock("@/components/TenantSelector", () => ({
  TenantSelector: ({
    onChange,
    onSelectionInfoChange,
  }: {
    onChange: (tenantIds: string[]) => void;
    onSelectionInfoChange?: (
      targets: Array<{
        tenant_id: string;
        tenant_name?: string | null;
        bbk_id?: string | null;
      }>,
    ) => void;
  }) => (
    <button
      type="button"
      onClick={() => {
        onChange(["tenant-a"]);
        onSelectionInfoChange?.([
          {
            tenant_id: "tenant-a",
            tenant_name: "Tenant A",
            bbk_id: "bbk-a",
          },
        ]);
      }}
    >
      Select tenant
    </button>
  ),
}));

vi.mock("./components", () => ({
  DEFAULT_FORM_VALUES: {
    schedule: {},
  },
  JobDrawer: ({
    open,
    form,
    skillOptions,
  }: {
    open: boolean;
    form: { getFieldValue: (name: string) => unknown };
    skillOptions: Array<{ value: string; label: string }>;
  }) => (
    <div
      data-testid="skill-options"
      data-open={String(open)}
      data-values={skillOptions.map((option) => option.value).join(",")}
      data-skill-ids={String(form.getFieldValue("skillIds") ?? "")}
    >
      {skillOptions.length}
    </div>
  ),
  BroadcastChildrenModal: () => null,
  isBroadcastChildJob: () => false,
  useCronJobs: () => ({
    jobs: [mocks.job],
    loading: false,
    fetchJobs: mocks.fetchJobs,
    createJob: vi.fn(),
    updateJob: vi.fn(),
    deleteJob: vi.fn(),
    toggleEnabled: vi.fn(),
    setBatchDispatch: mocks.setBatchDispatch,
    executeNow: vi.fn(),
  }),
  createColumns: (handlers: {
    onBroadcast: (job: CronJobSpecOutput) => void;
    onEdit: (job: CronJobSpecOutput) => void;
  }) => [
    {
      title: "名称",
      dataIndex: "name",
      key: "name",
    },
    {
      title: "操作",
      key: "actions",
      render: (_: unknown, job: CronJobSpecOutput) => (
        <>
          <button type="button" onClick={() => handlers.onBroadcast(job)}>
            广播到租户
          </button>
          <button type="button" onClick={() => handlers.onEdit(job)}>
            编辑
          </button>
        </>
      ),
    },
  ],
}));

describe("CronJobsPage broadcast task refresh", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getUserTimezone.mockResolvedValue({ timezone: "UTC" });
    mocks.getCronJob.mockResolvedValue({ spec: mocks.job });
    mocks.listSweSkills.mockResolvedValue({
      source_id: "default",
      count: 0,
      skills: [],
    });
    mocks.job.meta = {};
    mocks.getCurrentCronBroadcastTask.mockResolvedValue({
      task: {
        task_id: "task-1",
        status: "running",
        tenant_count: 5,
        completed_count: 2,
        failed_count: 0,
        results: [],
        reused: true,
      },
    });
    mocks.getCronBroadcastTask.mockResolvedValue({
      task_id: "task-1",
      status: "running",
      tenant_count: 5,
      completed_count: 4,
      failed_count: 0,
      results: [],
      reused: true,
    });
    mocks.enableCronBatchDispatch.mockResolvedValue({
      ...mocks.job,
      meta: {
        broadcast_dispatch_intents_enabled: true,
        batch_dispatch_offset_window_hours: 4,
      },
    });
    mocks.disableCronBatchDispatch.mockResolvedValue({
      ...mocks.job,
      meta: {},
    });
    mocks.setBatchDispatch.mockImplementation(
      async (
        job: CronJobSpecOutput,
        enabled: boolean,
        options?: { offset_window_hours?: number },
      ) =>
        enabled
          ? mocks.enableCronBatchDispatch(job.id, options)
          : mocks.disableCronBatchDispatch(job.id),
    );
  });

  afterEach(() => {
    cleanup();
  });

  it("refreshes the visible progress for a running broadcast task", async () => {
    render(<CronJobsPage />);

    fireEvent.click(screen.getByRole("button", { name: "广播到租户" }));

    expect(
      await screen.findByText("Broadcasting 2/5 tenants"),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "刷新进度" }));

    await waitFor(() => {
      expect(mocks.getCronBroadcastTask).toHaveBeenCalledWith(
        "job-source",
        "task-1",
      );
    });
    expect(
      await screen.findByText("Broadcasting 4/5 tenants"),
    ).toBeInTheDocument();
  }, 30000);

  it("deduplicates loaded skill options before passing them to the drawer", async () => {
    mocks.listSweSkills.mockResolvedValue({
      source_id: "default",
      count: 2,
      skills: [
        {
          skill_id: "same-skill-id",
          skill_name: "first_skill_name",
          cn_name: "首次展示",
        },
        {
          skill_id: "same-skill-id",
          skill_name: "second_skill_name",
          cn_name: "重复展示",
        },
      ],
    });

    render(<CronJobsPage />);

    await waitFor(() => {
      expect(screen.getByTestId("skill-options")).toHaveTextContent("1");
    });
    expect(screen.getByTestId("skill-options")).toHaveAttribute(
      "data-values",
      "same-skill-id",
    );
  });

  it("rehydrates bound skill ids after async skill options load while editing", async () => {
    let resolveSkills: (value: {
      source_id: string;
      count: number;
      skills: Array<{
        skill_id: string;
        skill_name: string;
        cn_name?: string | null;
      }>;
    }) => void = () => {};
    mocks.job = {
      ...mocks.job,
      enabled: false,
      skill_ids: "skill-a",
    };
    mocks.listSweSkills.mockReturnValue(
      new Promise((resolve) => {
        resolveSkills = resolve;
      }),
    );

    render(<CronJobsPage />);

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));

    expect(screen.getByTestId("skill-options")).toHaveAttribute(
      "data-skill-ids",
      "skill-a",
    );

    resolveSkills({
      source_id: "default",
      count: 1,
      skills: [
        {
          skill_id: "skill-a",
          skill_name: "analysis_skill",
          cn_name: "分析技能",
        },
      ],
    });

    await waitFor(() => {
      expect(screen.getByTestId("skill-options")).toHaveAttribute(
        "data-values",
        "skill-a",
      );
    });
    expect(screen.getByTestId("skill-options")).toHaveAttribute(
      "data-skill-ids",
      "skill-a",
    );
  });

  it("prevents a second broadcast from the visible completed result", async () => {
    mocks.getCurrentCronBroadcastTask.mockResolvedValue({ task: null });
    mocks.broadcastCronJob.mockResolvedValue({
      task_id: "task-completed",
      status: "completed",
      tenant_count: 1,
      completed_count: 1,
      failed_count: 0,
      results: [
        {
          tenant_id: "tenant-a",
          success: true,
          job_id: "job-copy",
          cron: "0 5 * * thu,fri,sat,sun",
          timezone: "Asia/Shanghai",
          offset_minutes: 0,
          notification_timezone: "Asia/Shanghai",
          error: "",
          warning: "",
        },
      ],
      reused: false,
    });

    render(<CronJobsPage />);

    fireEvent.click(screen.getByRole("button", { name: "广播到租户" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Select tenant" }),
    );

    const confirmButton = screen.getByRole("button", { name: /OK/ });
    await waitFor(() => {
      expect(confirmButton).not.toBeDisabled();
    });
    fireEvent.click(confirmButton);

    await waitFor(() => {
      expect(mocks.broadcastCronJob).toHaveBeenCalledTimes(1);
    });
    expect(mocks.broadcastCronJob).toHaveBeenCalledWith(
      "job-source",
      [
        {
          tenant_id: "tenant-a",
          tenant_name: "Tenant A",
          bbk_id: "bbk-a",
        },
      ],
      {
        enable_offset: true,
        offset_window_hours: 4,
      },
    );
    expect(
      await screen.findByText("Broadcast completed 1/1 tenants"),
    ).toBeInTheDocument();
    const disabledConfirmButton = screen.getByRole("button", { name: /OK/ });
    expect(disabledConfirmButton).toBeDisabled();

    fireEvent.click(disabledConfirmButton);

    expect(mocks.broadcastCronJob).toHaveBeenCalledTimes(1);
  }, 30000);

  it("shows cron broadcast task id after submitting async distribution", async () => {
    mocks.getCurrentCronBroadcastTask.mockResolvedValue({ task: null });
    mocks.broadcastCronJob.mockResolvedValue({
      task_id: "task-running",
      status: "running",
      tenant_count: 1,
      completed_count: 0,
      failed_count: 0,
      results: [],
      reused: false,
    });

    render(<CronJobsPage />);

    fireEvent.click(screen.getByRole("button", { name: "广播到租户" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Select tenant" }),
    );

    const confirmButton = screen.getByRole("button", { name: /OK/ });
    await waitFor(() => {
      expect(confirmButton).not.toBeDisabled();
    });
    fireEvent.click(confirmButton);

    await waitFor(() => {
      expect(mocks.message.info).toHaveBeenCalledWith(
        "定时任务分发任务已提交：task-running",
      );
    });
  }, 30000);

  it("requests batch dispatch after broadcasting with the shared offset window", async () => {
    mocks.getCurrentCronBroadcastTask.mockResolvedValue({ task: null });
    mocks.broadcastCronJob.mockResolvedValue({
      task_id: "task-completed",
      status: "completed",
      tenant_count: 1,
      completed_count: 1,
      failed_count: 0,
      results: [
        {
          tenant_id: "tenant-a",
          success: true,
          job_id: "job-copy",
          cron: "0 3 * * thu,fri,sat,sun",
          timezone: "Asia/Shanghai",
          offset_minutes: 120,
          notification_timezone: "Asia/Shanghai",
          error: "",
          warning: "",
        },
      ],
      reused: false,
    });

    render(<CronJobsPage />);

    fireEvent.click(screen.getByRole("button", { name: "广播到租户" }));
    fireEvent.click(await screen.findByText("批调度"));
    fireEvent.change(screen.getByRole("spinbutton"), {
      target: { value: "2" },
    });
    fireEvent.click(
      await screen.findByRole("button", { name: "Select tenant" }),
    );

    const confirmButton = screen.getByRole("button", { name: /OK/ });
    await waitFor(() => {
      expect(confirmButton).not.toBeDisabled();
    });
    fireEvent.click(confirmButton);

    await waitFor(() => {
      expect(mocks.broadcastCronJob).toHaveBeenCalledWith(
        "job-source",
        [
          {
            tenant_id: "tenant-a",
            tenant_name: "Tenant A",
            bbk_id: "bbk-a",
          },
        ],
        {
          enable_offset: true,
          enable_batch_dispatch: true,
          offset_window_hours: 2,
        },
      );
    });
    expect(mocks.enableCronBatchDispatch).not.toHaveBeenCalled();
  }, 30000);

  it("requests normal dispatch after broadcasting with the shared offset window", async () => {
    mocks.job.meta = {
      broadcast_dispatch_intents_enabled: true,
      batch_dispatch_offset_window_hours: 3,
    };
    mocks.getCurrentCronBroadcastTask.mockResolvedValue({ task: null });
    mocks.broadcastCronJob.mockResolvedValue({
      task_id: "task-completed",
      status: "completed",
      tenant_count: 1,
      completed_count: 1,
      failed_count: 0,
      results: [
        {
          tenant_id: "tenant-a",
          success: true,
          job_id: "job-copy",
          cron: "0 2 * * thu,fri,sat,sun",
          timezone: "Asia/Shanghai",
          offset_minutes: 180,
          notification_timezone: "Asia/Shanghai",
          error: "",
          warning: "",
        },
      ],
      reused: false,
    });

    render(<CronJobsPage />);

    fireEvent.click(screen.getByRole("button", { name: "广播到租户" }));
    fireEvent.click(await screen.findByText("正常调度"));
    fireEvent.click(
      await screen.findByRole("button", { name: "Select tenant" }),
    );

    const confirmButton = screen.getByRole("button", { name: /OK/ });
    await waitFor(() => {
      expect(confirmButton).not.toBeDisabled();
    });
    fireEvent.click(confirmButton);

    await waitFor(() => {
      expect(mocks.broadcastCronJob).toHaveBeenCalledWith(
        "job-source",
        [
          {
            tenant_id: "tenant-a",
            tenant_name: "Tenant A",
            bbk_id: "bbk-a",
          },
        ],
        {
          enable_offset: true,
          enable_batch_dispatch: false,
          offset_window_hours: 3,
        },
      );
    });
    expect(mocks.disableCronBatchDispatch).not.toHaveBeenCalled();
  }, 30000);

  it("enables batch dispatch without broadcasting when no tenant is selected", async () => {
    mocks.getCurrentCronBroadcastTask.mockResolvedValue({ task: null });

    render(<CronJobsPage />);

    fireEvent.click(screen.getByRole("button", { name: "广播到租户" }));
    const confirmButton = screen.getByRole("button", { name: /OK/ });
    expect(confirmButton).toBeDisabled();

    fireEvent.click(await screen.findByText("批调度"));

    await waitFor(() => {
      expect(confirmButton).not.toBeDisabled();
    });
    fireEvent.click(confirmButton);

    await waitFor(() => {
      expect(mocks.enableCronBatchDispatch).toHaveBeenCalledWith("job-source", {
        offset_window_hours: 4,
      });
    });
    expect(mocks.broadcastCronJob).not.toHaveBeenCalled();
  }, 30000);

  it("disables batch dispatch without broadcasting when no tenant is selected", async () => {
    mocks.job.meta = {
      broadcast_dispatch_intents_enabled: true,
      batch_dispatch_offset_window_hours: 3,
    };
    mocks.getCurrentCronBroadcastTask.mockResolvedValue({ task: null });

    render(<CronJobsPage />);

    fireEvent.click(screen.getByRole("button", { name: "广播到租户" }));
    const confirmButton = screen.getByRole("button", { name: /OK/ });
    expect(confirmButton).toBeDisabled();

    fireEvent.click(await screen.findByText("正常调度"));

    await waitFor(() => {
      expect(confirmButton).not.toBeDisabled();
    });
    fireEvent.click(confirmButton);

    await waitFor(() => {
      expect(mocks.disableCronBatchDispatch).toHaveBeenCalledWith("job-source");
    });
    expect(mocks.broadcastCronJob).not.toHaveBeenCalled();
  }, 30000);
});

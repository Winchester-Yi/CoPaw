import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { systemCronApi } from "@/api/modules/systemCron";
import type { SystemCronJob } from "@/api/types/systemCron";
import { SystemCronSyncStatus } from ".";

vi.mock("@/api/modules/systemCron", () => ({
  systemCronApi: { jobs: vi.fn(), refresh: vi.fn(), retry: vi.fn() },
}));
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  vi.useRealTimers();
});
const context = {
  actor_id: "alice",
  tenant_id: "alice",
  source_id: "bank",
  agent_id: "default",
};
const job: SystemCronJob = {
  job_id: "sys-1",
  scope_kind: "agent",
  task_type: "heartbeat",
  tenant_id: "alice",
  source_id: "bank",
  agent_id: "default",
  management_owner: "scheduler",
  enabled: true,
  desired_config: { cron: "0 10 * * *", enabled: true },
  effective_config: { cron: "0 9 * * *", enabled: true },
  binding_known: true,
  has_effective_binding: true,
  sync_status: "failed",
  sync_stage: "update",
  error_code: "system_platform_update_failed",
  version: 7,
  generation: 2,
  synced_generation: 1,
  active_execution_id: null,
  last_status: "success",
  needs_verification: false,
};
const props = {
  context,
  scope: "agent" as const,
  taskTypes: ["heartbeat"] as const,
};

it("原设置页区分失败与已有生效绑定，并只重试配置同步", async () => {
  vi.mocked(systemCronApi.jobs).mockResolvedValue({ items: [job] });
  vi.mocked(systemCronApi.retry).mockResolvedValue({
    ...job,
    version: 8,
    sync_status: "pending",
  });
  render(<SystemCronSyncStatus {...props} />);
  expect(await screen.findByText("平台同步失败")).toBeInTheDocument();
  expect(screen.getByText(/已有生效绑定/)).toBeInTheDocument();
  expect(screen.getByText(/0 10 \* \* \*/)).toBeInTheDocument();
  expect(screen.getByText(/0 9 \* \* \*/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "重试心跳同步" }));
  await waitFor(() =>
    expect(systemCronApi.retry).toHaveBeenCalledWith(
      context,
      job,
      expect.any(String),
    ),
  );
  expect(systemCronApi.refresh).not.toHaveBeenCalled();
});

it("一次重试已受理后再次同步失败，可用新版本发起下一次重试", async () => {
  vi.mocked(systemCronApi.jobs)
    .mockResolvedValueOnce({ items: [job] })
    .mockResolvedValue({ items: [{ ...job, version: 9 }] });
  vi.mocked(systemCronApi.retry).mockResolvedValue({
    ...job,
    version: 8,
    sync_status: "pending",
  });
  render(<SystemCronSyncStatus {...props} />);
  fireEvent.click(await screen.findByRole("button", { name: "重试心跳同步" }));
  await waitFor(() => expect(systemCronApi.retry).toHaveBeenCalledTimes(1));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "重试心跳同步" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "重试心跳同步" }));
  await waitFor(() => expect(systemCronApi.retry).toHaveBeenCalledTimes(2));
  const calls = vi.mocked(systemCronApi.retry).mock.calls;
  expect(calls[1][1].version).toBe(9);
  expect(calls[1][2]).not.toBe(calls[0][2]);
});

it("重试回执未知后，查询确认新版本已失败，可开始下一次重试", async () => {
  vi.mocked(systemCronApi.jobs)
    .mockResolvedValueOnce({ items: [job] })
    .mockResolvedValue({ items: [{ ...job, version: 9 }] });
  vi.mocked(systemCronApi.retry)
    .mockRejectedValueOnce(new Error("网络未确认"))
    .mockResolvedValue({ ...job, version: 10, sync_status: "pending" });
  render(<SystemCronSyncStatus {...props} />);
  fireEvent.click(await screen.findByRole("button", { name: "重试心跳同步" }));
  await screen.findByText(/网络未确认/);
  fireEvent.click(screen.getByRole("button", { name: "刷新同步状态" }));
  await waitFor(() =>
    expect(screen.queryByText(/网络未确认/)).not.toBeInTheDocument(),
  );
  fireEvent.click(screen.getByRole("button", { name: "重试心跳同步" }));
  await waitFor(() => expect(systemCronApi.retry).toHaveBeenCalledTimes(2));
  const calls = vi.mocked(systemCronApi.retry).mock.calls;
  expect(calls[1][1].version).toBe(9);
  expect(calls[1][2]).not.toBe(calls[0][2]);
});

it("同步意图重新提交已确认受理后，不继续显示本次保存未受理", async () => {
  vi.mocked(systemCronApi.jobs)
    .mockResolvedValueOnce({ items: [] })
    .mockResolvedValue({ items: [{ ...job, sync_status: "synced" }] });
  vi.mocked(systemCronApi.refresh).mockResolvedValue({
    ...job,
    sync_status: "pending",
  });
  render(
    <SystemCronSyncStatus
      {...props}
      submission={{ submission_status: "failed", request_id: "save-ack" }}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "重新提交心跳同步" }),
  );
  await waitFor(() => expect(systemCronApi.refresh).toHaveBeenCalledTimes(1));
  await waitFor(() =>
    expect(screen.queryByText(/本次保存的同步/)).not.toBeInTheDocument(),
  );
  expect(
    screen.queryByRole("button", { name: "重新提交心跳同步" }),
  ).not.toBeInTheDocument();
});

it("新保存已展示后，旧提交响应不能覆盖更新的同步状态", async () => {
  let finish!: (value: SystemCronJob) => void;
  const newer = {
    ...job,
    desired_config: { ...job.desired_config, cron: "0 12 * * *" },
    effective_config: { ...job.effective_config, cron: "0 11 * * *" },
    sync_status: "synced" as const,
  };
  vi.mocked(systemCronApi.jobs)
    .mockResolvedValueOnce({ items: [] })
    .mockResolvedValueOnce({ items: [newer] })
    .mockRejectedValue(new Error("查询临时失败"));
  vi.mocked(systemCronApi.refresh).mockReturnValue(
    new Promise((resolve) => {
      finish = resolve;
    }),
  );
  const view = render(
    <SystemCronSyncStatus
      {...props}
      submission={{ submission_status: "failed", request_id: "old-save" }}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "重新提交心跳同步" }),
  );
  view.rerender(
    <SystemCronSyncStatus
      {...props}
      submission={{ submission_status: "accepted", request_id: "new-save" }}
    />,
  );
  await screen.findByText("0 12 * * *");
  await act(async () => {
    finish({
      ...job,
      desired_config: { cron: "0 6 * * *" },
      sync_status: "synced",
    });
  });
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "刷新同步状态" })).toBeEnabled(),
  );
  expect(screen.getByText("0 12 * * *")).toBeInTheDocument();
  expect(screen.queryByText("0 6 * * *")).not.toBeInTheDocument();
});

it("配置已保存但未受理时，重提复用原请求号，网络失败后不换号", async () => {
  vi.mocked(systemCronApi.jobs)
    .mockResolvedValueOnce({ items: [] })
    .mockResolvedValue({ items: [{ ...job, sync_status: "synced" }] });
  vi.mocked(systemCronApi.refresh)
    .mockRejectedValueOnce(new Error("网络未确认"))
    .mockResolvedValueOnce({ ...job, sync_status: "pending" });
  render(
    <SystemCronSyncStatus
      {...props}
      submission={{ submission_status: "failed", request_id: "save-1" }}
    />,
  );
  expect(
    await screen.findByText(/本次保存的同步受理未确认/),
  ).toBeInTheDocument();
  fireEvent.click(
    await screen.findByRole("button", { name: "重新提交心跳同步" }),
  );
  await screen.findByText(/网络未确认/);
  fireEvent.click(screen.getByRole("button", { name: "重新提交心跳同步" }));
  await waitFor(() => expect(systemCronApi.refresh).toHaveBeenCalledTimes(2));
  expect(
    vi
      .mocked(systemCronApi.refresh)
      .mock.calls.every((args) => args[2] === "save-1"),
  ).toBe(true);
});

it("来源规范化空身份只用于记录，不替代当前真实Header，未知仅展示", async () => {
  const sourceJob = {
    ...job,
    scope_kind: "source" as const,
    task_type: "archive_maintenance" as const,
    tenant_id: "",
    agent_id: "",
    needs_verification: true,
    active_execution_id: "unknown-1",
    last_status: "unknown",
    sync_status: "synced" as const,
  };
  vi.mocked(systemCronApi.jobs).mockResolvedValue({ items: [sourceJob] });
  render(
    <SystemCronSyncStatus
      context={context}
      scope="source"
      taskTypes={["archive_maintenance"]}
    />,
  );
  expect(await screen.findByText(/执行结果待核实/)).toBeInTheDocument();
  expect(screen.getByText("unknown-1")).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: /解除|重跑|确认结束/ }),
  ).toBeNull();
  expect(systemCronApi.jobs).toHaveBeenCalledWith(context, "source");
  expect(systemCronApi.refresh).not.toHaveBeenCalled();
});

it("没有系统任务时只展示未登记，不自动初始化", async () => {
  vi.mocked(systemCronApi.jobs).mockResolvedValue({ items: [] });
  render(<SystemCronSyncStatus {...props} />);
  expect(await screen.findByText("尚未登记系统调度")).toBeInTheDocument();
  expect(systemCronApi.refresh).not.toHaveBeenCalled();
});

it("存储不可用不伪装为未登记", async () => {
  vi.mocked(systemCronApi.jobs).mockRejectedValue(new Error("存储不可用"));
  render(<SystemCronSyncStatus {...props} />);
  expect(await screen.findByText(/存储不可用/)).toBeInTheDocument();
  expect(screen.queryByText("尚未登记系统调度")).toBeNull();
});

it("身份切换作废旧响应，不能把上个来源状态贴到新来源", async () => {
  let finish!: (value: { items: SystemCronJob[] }) => void;
  vi.mocked(systemCronApi.jobs)
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    )
    .mockResolvedValueOnce({
      items: [
        {
          ...job,
          job_id: "new-source-job",
          source_id: "other",
          sync_status: "synced",
        },
      ],
    });
  const view = render(<SystemCronSyncStatus {...props} />);
  await waitFor(() => expect(systemCronApi.jobs).toHaveBeenCalledTimes(1));
  view.rerender(
    <SystemCronSyncStatus
      {...props}
      context={{ ...context, source_id: "other" }}
    />,
  );
  await screen.findByText("已同步");
  await act(async () => finish({ items: [job] }));
  expect(screen.queryByText("平台同步失败")).toBeNull();
});

it("服务返回不匹配身份时明确报错，不伪装未登记或提供越界重试", async () => {
  vi.mocked(systemCronApi.jobs).mockResolvedValue({
    items: [{ ...job, source_id: "foreign" }],
  });
  render(<SystemCronSyncStatus {...props} />);
  expect(await screen.findByText(/系统同步状态身份不一致/)).toBeInTheDocument();
  expect(screen.queryByText("尚未登记系统调度")).toBeNull();
  expect(screen.queryByRole("button", { name: "重试心跳同步" })).toBeNull();
});

it("已受理待办只轮询已登记状态，不自动提交新刷新或业务执行", async () => {
  vi.useFakeTimers();
  vi.mocked(systemCronApi.jobs)
    .mockResolvedValueOnce({ items: [{ ...job, sync_status: "pending" }] })
    .mockResolvedValue({ items: [{ ...job, sync_status: "synced" }] });
  await act(async () => {
    render(<SystemCronSyncStatus {...props} />);
  });
  expect(screen.getByText("待同步")).toBeInTheDocument();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(3000);
  });
  expect(screen.getByText("已同步")).toBeInTheDocument();
  expect(systemCronApi.jobs).toHaveBeenCalledTimes(2);
  expect(systemCronApi.refresh).not.toHaveBeenCalled();
  expect(systemCronApi.retry).not.toHaveBeenCalled();
});

import type { ReactNode } from "react";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import CaseDetailDrawer from ".";
import { schedulerCronApi } from "@/api/modules/schedulerCron";
import { cronJobApi } from "@/api/modules/cronjob";
import type { FeaturedCase } from "@/api/types/featuredCases";
import type { ManagedJobDetail } from "@/api/types/schedulerCron";
import type { ScheduledTaskPopupProps } from "@/components/ScheduledTaskPopup";

const popupState = vi.hoisted(() => ({
  current: undefined as ScheduledTaskPopupProps | undefined,
}));

vi.mock("antd", () => ({
  Drawer: ({ open, children }: { open: boolean; children: ReactNode }) =>
    open ? <div>{children}</div> : null,
  Spin: () => null,
}));
vi.mock("./style", () => ({ default: () => null }));
vi.mock("@/utils/identity", () => ({
  getUserId: () => "alice",
  getChannel: () => "console",
}));
vi.mock("@/hooks/useCronContext", () => ({
  useCronContext: () => ({
    actor_id: "alice",
    tenant_id: "alice",
    source_id: "bank",
    agent_id: "default",
  }),
  getCurrentCronContext: () => ({
    actor_id: "alice",
    tenant_id: "alice",
    source_id: "bank",
    agent_id: "default",
  }),
}));
vi.mock("@/api/modules/schedulerCron", () => ({
  schedulerCronApi: {
    create: vi.fn(),
    operation: vi.fn(),
    detail: vi.fn(),
    retry: vi.fn(),
  },
}));
vi.mock("@/api/modules/cronjob", () => ({
  cronJobApi: { createCronJob: vi.fn() },
}));
vi.mock("@/components/ScheduledTaskPopup", () => ({
  default: (props: ScheduledTaskPopupProps) => {
    popupState.current = props;
    return props.open ? (
      <button
        onClick={() =>
          void props
            .onConfirm(
              "0 10 * * *",
              { frequency: "daily", hour: 10, minute: 0 },
              undefined,
              0,
              {
                type: "cron",
                cron: "0 10 * * *",
                timezone: "Asia/Shanghai",
                run_on_non_workday: true,
              },
            )
            .catch(() => undefined)
        }
      >
        确认新任务
      </button>
    ) : null;
  },
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("精选案例入口实际调用Scheduler，准备完成后才刷新新旧任务列表", async () => {
  const created = vi.fn();
  document.addEventListener("taskCreated", created);
  vi.mocked(schedulerCronApi.create).mockResolvedValue({
    operation: {
      operation_id: "operation-1",
      job_id: "job-1",
      action: "create",
      status: "completed",
      stage: "completed",
      version: 3,
      expected_version: 1,
      logical_delete_applied: false,
    },
  });
  vi.mocked(schedulerCronApi.detail).mockResolvedValue({
    management_owner: "scheduler",
    management_status: "ready",
    agent_id: "default",
    spec: { id: "job-1", tenant_id: "alice", source_id: "bank" },
  } as ManagedJobDetail);
  render(
    <CaseDetailDrawer
      visible
      onClose={vi.fn()}
      caseData={
        { id: 12, label: "案例", value: "查询进展", steps: [] } as FeaturedCase
      }
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "订阅为定时任务" }));
  expect(popupState.current?.showCalendarOptions).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "确认新任务" }));
  await waitFor(() => expect(created).toHaveBeenCalledTimes(1));
  expect(schedulerCronApi.create).toHaveBeenCalledWith(
    {
      actor_id: "alice",
      tenant_id: "alice",
      source_id: "bank",
      agent_id: "default",
    },
    expect.objectContaining({
      name: "案例",
      schedule: {
        type: "cron",
        cron: "0 10 * * *",
        timezone: "Asia/Shanghai",
        run_on_non_workday: false,
      },
      meta: expect.objectContaining({ subscription_key: "featured_case:12" }),
    }),
    expect.any(String),
  );
  expect(cronJobApi.createCronJob).not.toHaveBeenCalled();
  document.removeEventListener("taskCreated", created);
});

it("网络未知后案例正文后台更新，重试仍发送最初那份快照", async () => {
  const operation = {
    operation_id: "operation-2",
    job_id: "job-2",
    action: "create",
    status: "completed" as const,
    stage: "completed",
    version: 3,
    expected_version: 1,
    logical_delete_applied: false,
  };
  vi.mocked(schedulerCronApi.create)
    .mockRejectedValueOnce(new Error("网络未知"))
    .mockResolvedValue({ operation });
  vi.mocked(schedulerCronApi.detail).mockResolvedValue({
    management_owner: "scheduler",
    management_status: "ready",
    agent_id: "default",
    spec: { id: "job-2", tenant_id: "alice", source_id: "bank" },
  } as ManagedJobDetail);
  const caseData = {
    id: 12,
    source_id: "bank",
    label: "原案例",
    value: "原正文",
    steps: [],
  } as FeaturedCase;
  const view = render(
    <CaseDetailDrawer visible onClose={vi.fn()} caseData={caseData} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "订阅为定时任务" }));
  fireEvent.click(screen.getByRole("button", { name: "确认新任务" }));
  await waitFor(() => expect(schedulerCronApi.create).toHaveBeenCalledTimes(1));
  view.rerender(
    <CaseDetailDrawer
      visible
      onClose={vi.fn()}
      caseData={{ ...caseData, label: "新标题", value: "新正文" }}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "确认新任务" }));
  await waitFor(() => expect(schedulerCronApi.create).toHaveBeenCalledTimes(2));
  const calls = vi.mocked(schedulerCronApi.create).mock.calls;
  expect(calls[1]).toEqual(calls[0]);
});

it("外层关闭并清空案例时也关闭快捷弹窗，不能在没有案例时假报创建成功", () => {
  const view = render(
    <CaseDetailDrawer
      visible
      onClose={vi.fn()}
      caseData={{ id: 12, label: "案例", value: "原正文" } as FeaturedCase}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "订阅为定时任务" }));
  expect(
    screen.getByRole("button", { name: "确认新任务" }),
  ).toBeInTheDocument();
  view.rerender(
    <CaseDetailDrawer visible={false} onClose={vi.fn()} caseData={null} />,
  );
  expect(
    screen.queryByRole("button", { name: "确认新任务" }),
  ).not.toBeInTheDocument();
});

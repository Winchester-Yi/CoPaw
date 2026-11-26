import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { schedulerCronApi } from "@/api/modules/schedulerCron";
import { ManagedModeModal } from "./ManagedModeModal";
import type { ManagedJob } from "@/api/types/schedulerCron";
import { useIframeStore } from "@/stores/iframeStore";
vi.mock("../CronJobs/components/BatchPriorityEditor", () => ({
  default: () => <div>优先策略</div>,
}));
vi.mock("@/api/modules/schedulerCron", () => ({
  schedulerCronApi: {
    dispatchState: vi.fn(),
    priority: vi.fn(),
    setDispatchState: vi.fn(),
    setBatchFollowLevel: vi.fn(),
    retry: vi.fn(),
  },
}));
const context = {
  actor_id: "u",
  tenant_id: "u",
  source_id: "s",
  agent_id: "a",
};
const job = { id: "j", name: "任务" } as ManagedJob;
afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  useIframeStore.setState({ manager: false, isSuperManager: false });
  vi.mocked(schedulerCronApi.priority).mockResolvedValue({
    priority: { user_ids: [], branch_ids: [] },
    management_version: 1,
  });
});

it.each([
  [false, "completed"],
  [true, "completed"],
  [true, "failed"],
] as const)(
  "任务调度详情读取关注等级，管理员可编辑（管理员=%s，同步=%s）",
  async (manager, syncStatus) => {
    useIframeStore.setState({ manager });
    const dispatchState = {
      job_id: "j",
      state: "batch_enabled",
      sync_status: syncStatus,
      version: 1,
      effective_state: syncStatus === "failed" ? "mixed" : "batch_enabled",
      batch_follow_level: 3,
      pending_count: 0,
      completed_count: 1,
      failed_count: syncStatus === "failed" ? 1 : 0,
    } as const;
    vi.mocked(schedulerCronApi.dispatchState).mockResolvedValue(dispatchState);
    vi.mocked(schedulerCronApi.setBatchFollowLevel).mockResolvedValue({
      batch_follow_level: 5,
    });
    render(
      <ManagedModeModal
        context={context}
        job={job}
        onClose={() => {}}
        onChanged={() => {}}
      />,
    );
    await screen.findByText("关注 L3", { exact: false });
    if (!manager) {
      expect(
        screen.queryByRole("combobox", { name: "批调度优先级" }),
      ).toBeNull();
      return;
    }
    const select = screen.getByRole("combobox", { name: "批调度优先级" });
    expect(select).toBeEnabled();
    fireEvent.mouseDown(select.closest(".ant-select-selector")!);
    fireEvent.click(await screen.findByText("关注 L5"));
    await waitFor(() =>
      expect(schedulerCronApi.setBatchFollowLevel).toHaveBeenCalledWith(
        context,
        "j",
        5,
      ),
    );
    expect(schedulerCronApi.dispatchState).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(
        select
          .closest(".ant-select-selector")
          ?.querySelector('[title="关注 L5"]'),
      ).toBeInTheDocument(),
    );
  },
);
it("切换未完成或外部失败不能换目标，只能刷新或重试", async () => {
  vi.mocked(schedulerCronApi.dispatchState).mockResolvedValue({
    job_id: "j",
    state: "batch_enabled",
    sync_status: "failed",
    operation_id: "op",
    version: 2,
    effective_state: "mixed",
  } as never);
  render(
    <ManagedModeModal
      context={context}
      job={job}
      onClose={() => {}}
      onChanged={() => {}}
    />,
  );
  await screen.findByText(/实际生效.*混合/);
  expect(screen.getByRole("button", { name: "提交切换" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "重试原切换" }));
  await waitFor(() =>
    expect(schedulerCronApi.retry).toHaveBeenCalledWith(context, "j", "op"),
  );
  expect(schedulerCronApi.setDispatchState).not.toHaveBeenCalled();
});

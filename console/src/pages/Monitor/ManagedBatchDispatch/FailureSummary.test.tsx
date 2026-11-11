import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import FailureSummary from "./FailureSummary";
import { managedBatchBoard } from "@/api/modules/managedBatchBoard";

vi.mock("@/api/modules/managedBatchBoard", () => ({ managedBatchBoard: { failureSummary: vi.fn() } }));
afterEach(cleanup);
beforeEach(() => vi.resetAllMocks());
const result = { total: 3, queried_at: "2026-11-05T01:00:00Z", groups: [{
  failure_type: "configuration", label: "配置错误", count: 3,
  details: [{ message: "缺少用户信息", count: 2 }, { message: "绑定配置不存在", count: 1 }],
}] };
const filters = { start_time: "2026-11-05T00:00:00+08:00", end_time: "2026-11-05T23:59:59+08:00", status: "failed", query: "日报" };

it("只在hover时按完整全局筛选加载，展示每种具体错误和数量", async () => {
  vi.mocked(managedBatchBoard.failureSummary).mockResolvedValue(result);
  render(<FailureSummary source="bank" filters={filters} refresh={0} value={3} />);
  expect(managedBatchBoard.failureSummary).not.toHaveBeenCalled();
  const trigger = screen.getByRole("button", { name: "失败任务 3 个，查看失败类型及详情" });
  expect(within(trigger).getByText("3")).toHaveStyle({ color: "#d92d20" });
  fireEvent.mouseOver(trigger);
  expect(await screen.findByText("缺少用户信息")).toBeInTheDocument();
  expect(screen.getByText("绑定配置不存在")).toBeInTheDocument();
  expect(screen.getByText("配置错误 · 3")).toBeInTheDocument();
  expect(managedBatchBoard.failureSummary).toHaveBeenCalledWith("bank", filters, expect.any(AbortSignal));
  expect(screen.getByText("缺少用户信息").parentElement).toHaveTextContent("2");
});

it("查询失败不是零失败，支持原条件重试", async () => {
  vi.mocked(managedBatchBoard.failureSummary).mockRejectedValueOnce(new Error("unavailable")).mockResolvedValueOnce(result);
  render(<FailureSummary source="bank" filters={filters} refresh={0} value={3} />);
  fireEvent.mouseOver(screen.getByRole("button", { name: /失败任务 3/ }));
  expect(await screen.findByText("失败明细加载失败")).toBeInTheDocument();
  expect(screen.queryByText("当前筛选范围暂无失败任务")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "重试查询" }));
  expect(await screen.findByText("缺少用户信息")).toBeInTheDocument();
});

it("切换来源丢弃迟到明细，不把上一来源错误展示到当前来源", async () => {
  let resolve!: (value: typeof result) => void;
  vi.mocked(managedBatchBoard.failureSummary).mockReturnValueOnce(new Promise(done => { resolve = done; }))
    .mockResolvedValueOnce({ total: 0, queried_at: result.queried_at, groups: [] });
  const { rerender } = render(<FailureSummary source="bank" filters={filters} refresh={0} value={3} />);
  fireEvent.mouseOver(screen.getByRole("button", { name: /失败任务 3/ }));
  await waitFor(() => expect(managedBatchBoard.failureSummary).toHaveBeenCalledTimes(1));
  rerender(<FailureSummary source="other" filters={filters} refresh={0} value={0} />);
  await act(async () => resolve(result));
  fireEvent.mouseOver(screen.getByRole("button", { name: /失败任务 0/ }));
  expect(await screen.findByText("当前筛选范围暂无失败任务")).toBeInTheDocument();
  expect(screen.queryByText("缺少用户信息")).toBeNull();
});

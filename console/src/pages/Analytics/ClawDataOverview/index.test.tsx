import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import ClawDataOverview from "./index";
import {
  getTaskTypeReport,
  getTaskReportOptions,
  type ReportRow,
} from "../../../api/modules/taskTypeReport";
const auth = vi.hoisted(() => ({ bbk: "100" }));
vi.mock("../../../api/modules/taskTypeReport", () => ({
  getTaskReportOptions: vi.fn(),
  getTaskReportBbk: () => auth.bbk,
  REPORT_PAGE_SIZE: 20,
  taskReportError: (error: Error) => error.message,
  exportTaskReport: vi.fn(),
  getTaskTypeReport: vi.fn(),
}));
vi.mock("../../../stores/iframeStore", () => ({
  useIframeStore: (
    selector: (state: { bbk: string; source: string }) => unknown,
  ) => selector({ bbk: "100", source: "RMASSIST" }),
}));
const row: ReportRow = {
  first_bbk_id: "001",
  first_bbk_name: "测试分行",
  org_id: null,
  org_name: null,
  user_id: null,
  user_name: null,
  sapid: null,
  pst_lvl: null,
  skill_id: null,
  cn_name: null,
  task_type: "push_plan",
  task_type_name: "推送（名单+方案）",
  skill_count: 2,
  permission_manager_count: 20,
  active_manager_count: 10,
  suc_execute_job: 10,
  read_tasks: 20,
  read_rate: 200,
  recommended_customers: 10,
  read_customer_count: 5,
  plan_read_rate: 50,
  insight_customer_count: 3,
  click_to_insight_rate: 30,
  insight_count: 5,
  phone_customer_count: 0,
  click_to_phone_rate: null,
  phone_count: 2,
};
beforeEach(() => {
  auth.bbk = "100";
  vi.mocked(getTaskReportOptions).mockResolvedValue({
    sync_date: "2026-09-01",
    items: [
      { value: "110", label: "北京分行" },
      { value: "120", label: "广州分行" },
    ],
  });
  vi.mocked(getTaskTypeReport).mockImplementation(async (params) => ({
    sync_date: "2026-09-01",
    warnings: [],
    page: params.page || null,
    page_size: params.page_size || null,
    total: 1,
    has_more: false,
    items: params.skill_detail
      ? [{ ...row, skill_id: "skill-a", cn_name: "测试技能" }]
      : [row],
  }));
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
describe("技能运行看板", () => {
  it("按后端百分比显示并在原报表下展开技能明细", async () => {
    render(<ClawDataOverview />);
    expect(await screen.findByText("200.00%")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "查看测试分行的技能明细" }),
    );
    const details = await screen.findByRole("region", { name: "技能明细" });
    expect(await within(details).findByText("测试技能")).toBeInTheDocument();
    expect(getTaskTypeReport).toHaveBeenLastCalledWith(
      expect.objectContaining({
        group_by: "branch",
        skill_detail: true,
        first_bbk_id: "001",
      }),
      expect.any(AbortSignal),
    );
  }, 30000);
  it("失败保留刷新入口并能恢复为空结果", async () => {
    vi.mocked(getTaskTypeReport).mockRejectedValueOnce(
      new Error("服务暂不可用"),
    );
    render(<ClawDataOverview />);
    expect(await screen.findByText("服务暂不可用")).toBeInTheDocument();
    vi.mocked(getTaskTypeReport).mockResolvedValueOnce({
      sync_date: null,
      warnings: ["empty_roster"],
      page: null,
      page_size: null,
      total: 0,
      has_more: false,
      items: [],
    });
    fireEvent.click(screen.getByRole("button", { name: /刷新报表/ }));
    expect(
      await screen.findByText("当前条件下暂无数据，请调整筛选条件"),
    ).toBeInTheDocument();
  }, 30000);
  it("非总行分行固定且只有本行，缺失身份不发送请求", async () => {
    auth.bbk = "110";
    const view = render(<ClawDataOverview />);
    await screen.findByText("北京分行");
    expect(screen.getByRole("combobox", { name: "分行" })).toBeDisabled();
    expect(screen.queryByText("广州分行")).not.toBeInTheDocument();
    expect(getTaskTypeReport).toHaveBeenCalledWith(
      expect.objectContaining({ first_bbk_id: "110" }),
      expect.any(AbortSignal),
    );
    cleanup();
    vi.mocked(getTaskTypeReport).mockClear();
    auth.bbk = "";
    view.unmount();
    render(<ClawDataOverview />);
    expect(
      screen.getByText("缺少分行身份，请从业务系统重新进入看板"),
    ).toBeInTheDocument();
    expect(getTaskTypeReport).not.toHaveBeenCalled();
  }, 30000);
  it("缺失机构标识时不发送扩大范围的明细查询", async () => {
    vi.mocked(getTaskTypeReport).mockResolvedValueOnce({
      sync_date: "2026-09-01",
      items: [{ ...row, first_bbk_id: null }],
      warnings: [],
      page: null,
      page_size: null,
      total: 1,
      has_more: false,
    });
    render(<ClawDataOverview />);
    const button = await screen.findByRole("button", {
      name: "查看测试分行的技能明细",
    });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(getTaskTypeReport).toHaveBeenCalledTimes(1);
  }, 30000);
});

import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  mapCronJobOverviewPageData,
  monitorApi,
  type CronBranchErrorResponse,
  type CronBranchRankingResponse,
  type CronOverviewStatsResponse,
} from "./monitor";

const requestMock = vi.hoisted(() => vi.fn());

vi.mock("../request", () => ({
  request: requestMock,
}));

beforeEach(() => {
  requestMock.mockReset();
});

describe("mapCronJobOverviewPageData", () => {
  it("maps report rate and report detail counts into summary metrics", () => {
    const stats: CronOverviewStatsResponse = {
      start_date: "2026-06-30",
      end_date: "2026-06-30",
      total_tasks: 320,
      new_cron_tasks: 12,
      total_executions: 2480,
      branch_count: 12,
      tenant_count: 86,
      success_rate: 93.2,
      success_count: 2112,
      running_count: 24,
      read_tasks: 1525,
      read_rate: 61.5,
      error_count: 154,
      error_rate: 6.2,
      report_rate: 34.8,
      report_count: 863,
      insight_count: 512,
      phone_count: 221,
    };
    const ranking: CronBranchRankingResponse = {
      start_date: "2026-06-30",
      end_date: "2026-06-30",
      items: [],
    };
    const branchError: CronBranchErrorResponse = {
      start_date: "2026-06-30",
      end_date: "2026-06-30",
      affected_branch_count: 0,
      affected_manager_count: 0,
      error_reasons: [],
      branch_error_rank: [],
    };

    const result = mapCronJobOverviewPageData(stats, ranking, branchError);

    expect(result.summaryMetrics).toEqual(
      expect.arrayContaining([
        { key: "report", value: "34.80" },
        { key: "report_count", value: "863" },
        { key: "insight_count", value: "512" },
        { key: "phone_count", value: "221" },
      ]),
    );
  });

  it("calculates branch report ratio columns from existing ranking counts", () => {
    const stats: CronOverviewStatsResponse = {
      start_date: "2026-06-30",
      end_date: "2026-06-30",
      total_tasks: 0,
      new_cron_tasks: 0,
      total_executions: 0,
      branch_count: 1,
      tenant_count: 5,
      success_rate: 0,
      success_count: 0,
      running_count: 0,
      read_tasks: 0,
      read_rate: 0,
      error_count: 0,
      error_rate: 0,
      report_rate: 0,
      report_count: 0,
      insight_count: 0,
      phone_count: 0,
    };
    const ranking: CronBranchRankingResponse = {
      start_date: "2026-06-30",
      end_date: "2026-06-30",
      items: [
        {
          bbk_id: "100",
          bbk_name: "测试分行",
          skill_count: 3,
          total_tasks: 20,
          success_count: 18,
          read_tasks: 11,
          involved_managers: 5,
          result_view_managers: 4,
          plan_managers: 3,
          insight_managers: 2,
          phone_managers: 1,
          recommended_customers: 30,
          viewed_customers: 12,
          contacted_customers: 8,
          contact_rate: 0.4,
          insight_customers: 5,
          phone_customers: 2,
        },
      ],
    };
    const branchError: CronBranchErrorResponse = {
      start_date: "2026-06-30",
      end_date: "2026-06-30",
      affected_branch_count: 0,
      affected_manager_count: 0,
      error_reasons: [],
      branch_error_rank: [],
    };

    const [row] = mapCronJobOverviewPageData(
      stats,
      ranking,
      branchError,
    ).branchRankingRows;

    expect(row.resultViewManagerRate).toBe("80.00%");
    expect(row.planManagerRate).toBe("75.00%");
    expect(row.insightManagerRate).toBe("66.67%");
    expect(row.phoneManagerRate).toBe("33.33%");
    expect(row.viewedCustomerRate).toBe("40.00%");
    expect(row.contactRate).toBe("40.00%");
  });
});

describe("monitorApi schedule distribution", () => {
  it("maps definition_revision to the API expected_revision parameter", async () => {
    requestMock.mockResolvedValue({
      items: [],
      total: 0,
      page: 2,
      page_size: 20,
    });

    await monitorApi.getScheduleDistributionDetails({
      start_time: "2026-07-27T02:00:00Z",
      end_time: "2026-07-27T02:15:00Z",
      task_type: "agent",
      page: 2,
      page_size: 20,
      definition_revision: "revision-1",
    });

    expect(requestMock).toHaveBeenCalledTimes(1);
    const path = requestMock.mock.calls[0][0] as string;
    const url = new URL(path, "http://monitor.test");
    expect(url.pathname).toBe("/monitor/cron/schedule-distribution/details");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      start_time: "2026-07-27T02:00:00Z",
      end_time: "2026-07-27T02:15:00Z",
      page: "2",
      page_size: "20",
      task_type: "agent",
      expected_revision: "revision-1",
    });
    expect(url.searchParams.has("definition_revision")).toBe(false);
  });
});

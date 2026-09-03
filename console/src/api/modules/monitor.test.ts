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

describe("monitorApi cron batch detail", () => {
  it("serializes independent Intent and event pagination filters", async () => {
    requestMock.mockResolvedValue({});

    await monitorApi.getCronDispatchBatchDetail("cron:batch/a", {
      intent_page: "2",
      intent_limit: "50",
      intent_query: "job-a",
      intent_role: "child",
      intent_status: "pending",
      event_page: "3",
      event_limit: "50",
    });

    const path = requestMock.mock.calls[0][0] as string;
    const url = new URL(path, "http://monitor.test");
    expect(url.pathname).toBe(
      "/monitor/cron/dispatch/batches/cron%3Abatch%2Fa",
    );
    expect(Object.fromEntries(url.searchParams)).toEqual({
      intent_page: "2",
      intent_limit: "50",
      intent_query: "job-a",
      intent_role: "child",
      intent_status: "pending",
      event_page: "3",
      event_limit: "50",
    });
  });
});

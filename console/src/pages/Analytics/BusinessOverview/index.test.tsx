import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { message } from "antd";
import BusinessOverviewPage from "./index";

const echartsRenderMock = vi.hoisted(() => ({
  lastProps: null as null | {
    style?: Record<string, unknown>;
    option?: Record<string, unknown>;
  },
}));

vi.mock("echarts-for-react", () => ({
  default: (props: {
    style?: Record<string, unknown>;
    option?: Record<string, unknown>;
  }) => {
    echartsRenderMock.lastProps = props;
    return <div data-testid="echarts" style={props.style} />;
  },
}));

const tracingApiMock = vi.hoisted(() => ({
  getOverview: vi.fn(),
  getHourlyTrend: vi.fn(),
  getDailyTrend: vi.fn(),
  getUsers: vi.fn(),
  getSkills: vi.fn(),
  getMCPSummary: vi.fn(),
  getTaskStatusSummary: vi.fn(),
  getErrorSummary: vi.fn(),
  getSources: vi.fn(),
}));
const htmlPreviewEventsApiMock = vi.hoisted(() => ({
  getSummary: vi.fn(),
  getEvents: vi.fn(),
  getCustomerSummary: vi.fn(),
  getLists: vi.fn(),
  getCustomerClicks: vi.fn(),
}));
const iframeStoreMock = vi.hoisted(() => ({
  source: "CMSJY",
  bbk: undefined as string | undefined,
}));

vi.mock("../../../api/modules/tracing", () => ({
  tracingApi: tracingApiMock,
  displaySkillName: (skill: {
    display_name?: string | null;
    skill_name?: string | null;
  }) => skill.display_name || skill.skill_name || "-",
}));

vi.mock("../../../api/modules/htmlPreviewEvents", () => ({
  htmlPreviewEventsApi: htmlPreviewEventsApiMock,
}));

vi.mock("../../../stores/iframeStore", () => ({
  useIframeStore: (selector: (state: unknown) => unknown) =>
    selector({
      isSuperManager: true,
      source: iframeStoreMock.source,
      bbk: iframeStoreMock.bbk,
    }),
}));

vi.mock("./components/UserDetailModal", () => ({
  default: () => null,
}));

vi.mock("./components/SkillDetailModal", () => ({
  default: () => null,
}));

describe("BusinessOverview trend chart", () => {
  afterEach(() => {
    cleanup();
  });

  it("renders metric card growth from the overview response", async () => {
    tracingApiMock.getOverview.mockResolvedValueOnce({
      total_users: 1,
      total_sessions: 1,
      total_tokens: 1,
      total_skill_calls: 0,
      total_conversations: 1,
      it_users: 0,
      business_users: 1,
      plan_customers: 0,
      insight_customers: 0,
      phone_customers: 0,
      growth_stats: {
        userGrowth: 5,
        sessionGrowth: 0,
        cronGrowth: 0,
        tokensGrowth: 0,
        planCustomersGrowth: 0,
      },
      branch_breakdown: {
        users: [],
        sessions: [],
        tokens: [],
        skills: [],
        cron_tasks: [],
        customers: [],
      },
    });

    renderBusinessOverview();

    expect(await screen.findByText("环比+5.0%")).toBeInTheDocument();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    iframeStoreMock.source = "CMSJY";
    iframeStoreMock.bbk = undefined;
    tracingApiMock.getOverview.mockResolvedValue({
      it_users: 20,
      business_users: 100,
      total_users: 120,
      total_sessions: 80,
      total_tokens: 56000,
      total_skill_calls: 40,
      total_conversations: 160,
      plan_customers: 30,
      insight_customers: 20,
      phone_customers: 10,
      branch_breakdown: {
        users: [],
        sessions: [],
        tokens: [],
        skills: [],
        cron_tasks: [],
        customers: [
          { bbk_id: "100", bbk_name: "总行", value: 30, percent: 100 },
        ],
      },
    });
    tracingApiMock.getHourlyTrend.mockResolvedValue({
      trendData: [
        {
          date: "2026-05-19 09:00:00",
          users: 3200,
          calls: 15800,
          tokens: 0,
          read_tasks: 3,
          plan_customers: 6,
          insight_customers: 4,
          phone_customers: 2,
        },
        {
          date: "2026-05-19 10:00:00",
          users: 2100,
          calls: 9200,
          tokens: 0,
          read_tasks: 2,
          plan_customers: 3,
          insight_customers: 2,
          phone_customers: 1,
        },
      ],
    });
    tracingApiMock.getDailyTrend.mockResolvedValue({ trendData: [] });
    tracingApiMock.getUsers.mockResolvedValue({ items: [], total: 0 });
    tracingApiMock.getSkills.mockResolvedValue({ items: [], total: 0 });
    tracingApiMock.getMCPSummary.mockResolvedValue({
      total_calls: 0,
      error_count: 0,
      server_count: 0,
    });
    tracingApiMock.getTaskStatusSummary.mockResolvedValue({
      total_tasks: 0,
      success: 0,
      running: 0,
      failed: 0,
      cancelled: 0,
      read_count: 0,
    });
    tracingApiMock.getErrorSummary.mockResolvedValue({
      total_errors: 0,
      model_errors: 0,
      tool_errors: 0,
      other_errors: 0,
    });
    tracingApiMock.getSources.mockResolvedValue({ sources: ["CMSJY"] });
    htmlPreviewEventsApiMock.getSummary.mockResolvedValue({
      items: [
        {
          button_label: "立即跟进",
          button_id: "follow",
          button_name: "立即跟进",
          file_name: "到期客户名单[auto-preview].html",
          click_count: 12,
          last_clicked_at: "2026-05-19T10:30:00",
        },
      ],
    });
    htmlPreviewEventsApiMock.getEvents.mockResolvedValue({
      items: [
        {
          id: 1,
          button_name: "洞察页面",
          file_url: "https://example.com/a.html",
          customer_info: {
            customer_id: "CUST-001",
            客户姓名: "祝话",
          },
          clicked_at: "2026-05-19T10:35:00",
        },
      ],
    });
    htmlPreviewEventsApiMock.getCustomerSummary.mockResolvedValue({
      items: [
        {
          customer_id: "CUST-001",
          customer_name: "祝话",
          insight_count: 2,
          phone_count: 1,
          plan_count: 1,
          last_clicked_at: "2026-05-19T10:35:00",
        },
      ],
    });
    htmlPreviewEventsApiMock.getLists.mockResolvedValue({
      total: 328,
      clicked_list_count: 82,
      page: 1,
      page_size: 20,
      summary: {
        list_key: "all",
        list_name: "全部名单",
        customer_count: 160,
        clicked_customer_count: 40,
        insight_count: 20,
        phone_count: 10,
        plan_count: 30,
        total_click_count: 60,
        last_clicked_at: "2026-05-19T10:35:00",
      },
      items: [
        {
          list_key: "https://example.com/a.html",
          list_name: "到期客户名单[auto-preview].html",
          file_url: "https://example.com/a.html",
          file_name: "到期客户名单[auto-preview].html",
          customer_count: 16,
          clicked_customer_count: 1,
          insight_count: 2,
          phone_count: 1,
          plan_count: 1,
          total_click_count: 4,
          last_clicked_at: "2026-05-19T10:35:00",
        },
      ],
    });
    htmlPreviewEventsApiMock.getCustomerClicks.mockResolvedValue({
      items: [
        {
          customer_id: "CUST-001",
          customer_name: "祝话",
          list_key: "https://example.com/a.html",
          list_name: "到期客户名单[auto-preview].html",
          insight_count: 2,
          phone_count: 1,
          plan_count: 1,
          total_click_count: 4,
          last_clicked_user_id: "manager-1",
          last_clicked_user_name: "张经理",
          manager_clicks: [
            {
              user_id: "manager-1",
              user_name: "张经理",
              insight_count: 2,
              phone_count: 1,
              plan_count: 0,
              total_click_count: 3,
              last_clicked_at: "2026-05-19T10:35:00",
            },
            {
              user_id: "manager-2",
              user_name: "李经理",
              insight_count: 0,
              phone_count: 0,
              plan_count: 1,
              total_click_count: 1,
              last_clicked_at: "2026-05-19T09:35:00",
            },
          ],
          last_clicked_at: "2026-05-19T10:35:00",
        },
      ],
    });
  });

  it("locks branch filter to current branch for branch users", async () => {
    iframeStoreMock.bbk = "110";

    const { container } = render(
      <MemoryRouter>
        <BusinessOverviewPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      const selectedItem = container.querySelector(
        ".ant-select-selection-item",
      );
      expect(selectedItem?.textContent).toContain("北京分行");
    });
    expect(container.querySelector(".ant-select")).toHaveClass(
      "ant-select-disabled",
    );
    await waitFor(() => {
      expect(tracingApiMock.getOverview).toHaveBeenCalledWith(
        expect.any(String),
        expect.any(String),
        "110",
        { detail: "summary", timeRange: "day" },
      );
    });
    expect(container.querySelector(".ant-select-disabled")).toBeInTheDocument();
  });

  function renderBusinessOverview() {
    return render(
      <MemoryRouter>
        <BusinessOverviewPage />
      </MemoryRouter>,
    );
  }

  it("makes the trend chart span the full panel width", async () => {
    renderBusinessOverview();

    const chart = await screen.findByTestId("echarts");
    expect(chart).toHaveStyle({
      height: "280px",
      width: "100%",
      gridColumn: "1 / -1",
    });
  });

  it("renders the trend chart with current hourly trend data", async () => {
    renderBusinessOverview();

    await waitFor(() => {
      expect(tracingApiMock.getHourlyTrend).toHaveBeenCalledTimes(1);
    });
    expect(screen.getByText("调用量趋势")).toBeInTheDocument();
    expect(screen.getByTestId("echarts")).toBeInTheDocument();
  });

  it("renders the trend chart while overview cards are still loading", async () => {
    const pending = new Promise<never>(() => {});
    tracingApiMock.getOverview.mockReturnValueOnce(pending);

    renderBusinessOverview();

    expect(await screen.findByTestId("echarts")).toBeInTheDocument();
    expect(
      screen.getAllByTestId("overview-panel-loading").length,
    ).toBeGreaterThan(0);
  });

  it("ignores stale dashboard responses after a newer refresh", async () => {
    let resolveFirstOverview!: (value: Record<string, unknown>) => void;
    const firstOverview = new Promise<Record<string, unknown>>((resolve) => {
      resolveFirstOverview = resolve;
    });
    const latestOverview = {
      total_users: 99,
      total_sessions: 1,
      total_tokens: 1,
      total_skill_calls: 0,
      total_conversations: 1,
      it_users: 0,
      business_users: 99,
      plan_customers: 0,
      insight_customers: 0,
      phone_customers: 0,
      branch_breakdown: {
        users: [],
        sessions: [],
        tokens: [],
        cron_tasks: [],
        customers: [],
      },
    };
    tracingApiMock.getOverview
      .mockReset()
      .mockReturnValueOnce(firstOverview)
      .mockResolvedValueOnce(latestOverview);

    renderBusinessOverview();
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));

    expect(await screen.findByText("99")).toBeInTheDocument();
    resolveFirstOverview({
      ...latestOverview,
      total_users: 1,
      business_users: 1,
    });
    const firstMetricCard = screen.getAllByTestId("overview-metric-card")[0];
    expect(within(firstMetricCard).getByText("99")).toBeInTheDocument();
    expect(within(firstMetricCard).queryByText("1")).not.toBeInTheDocument();
  });

  it("does not show an error toast when a stale overview request fails", async () => {
    let rejectFirstOverview!: (reason?: unknown) => void;
    const firstOverview = new Promise<Record<string, unknown>>((_, reject) => {
      rejectFirstOverview = reject;
    });
    const latestOverview = {
      total_users: 99,
      total_sessions: 1,
      total_tokens: 1,
      total_skill_calls: 0,
      total_conversations: 1,
      it_users: 0,
      business_users: 99,
      plan_customers: 0,
      insight_customers: 0,
      phone_customers: 0,
      branch_breakdown: {
        users: [],
        sessions: [],
        tokens: [],
        cron_tasks: [],
        customers: [],
      },
    };
    const messageErrorSpy = vi.spyOn(message, "error");
    const consoleErrorSpy = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    tracingApiMock.getOverview
      .mockReset()
      .mockReturnValueOnce(firstOverview)
      .mockResolvedValueOnce(latestOverview);

    renderBusinessOverview();
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));

    expect(await screen.findByText("99")).toBeInTheDocument();
    rejectFirstOverview(new Error("stale overview failed"));
    await waitFor(() => {
      expect(consoleErrorSpy).toHaveBeenCalledWith(
        "Failed to fetch overview stats:",
        expect.any(Error),
      );
    });
    expect(messageErrorSpy).not.toHaveBeenCalledWith("获取总览数据失败");
  });

  it("renders only three trend metrics for non-RMASSIST source", async () => {
    renderBusinessOverview();

    await waitFor(() => {
      expect(echartsRenderMock.lastProps?.option).toBeTruthy();
    });

    const option = echartsRenderMock.lastProps?.option as {
      legend?: { data?: string[] };
      yAxis?: Array<{ name?: string }>;
      series?: Array<{
        name?: string;
        type?: string;
        yAxisIndex?: number;
        z?: number;
      }>;
    };

    expect(option.legend?.data).toEqual(["调用量", "调用用户", "已读任务数"]);
    expect(option.yAxis?.[1]?.name).toBe("任务数");
    expect(option.series?.map((item) => item.name)).toEqual([
      "调用量",
      "调用用户",
      "已读任务数",
    ]);
    expect(option.series?.[0]).toMatchObject({
      name: "调用量",
      type: "bar",
      yAxisIndex: 0,
    });
    expect(option.series?.[1]).toMatchObject({
      name: "调用用户",
      type: "line",
      yAxisIndex: 0,
    });
    expect(option.series?.[2]).toMatchObject({
      name: "已读任务数",
      type: "line",
      yAxisIndex: 1,
    });
    expect(option.series?.some((item) => item.name === "查看方案客户数")).toBe(
      false,
    );
    expect(option.series?.some((item) => item.name === "去洞察客户数")).toBe(
      false,
    );
    expect(option.series?.some((item) => item.name === "去电访客户数")).toBe(
      false,
    );
  });

  it("renders six trend metrics for RMASSIST source", async () => {
    iframeStoreMock.source = "RMASSIST";
    renderBusinessOverview();

    await waitFor(() => {
      expect(echartsRenderMock.lastProps?.option).toBeTruthy();
    });

    const option = echartsRenderMock.lastProps?.option as {
      legend?: { data?: string[] };
      yAxis?: Array<{ name?: string }>;
      series?: Array<{
        name?: string;
        type?: string;
        yAxisIndex?: number;
        z?: number;
      }>;
    };

    expect(option.legend?.data).toEqual([
      "调用量",
      "调用用户",
      "已读任务数",
      "查看方案客户数",
      "去洞察客户数",
      "去电访客户数",
    ]);
    expect(option.yAxis?.[1]?.name).toBe("客户数/任务数");
    expect(option.series?.map((item) => item.name)).toEqual([
      "调用量",
      "调用用户",
      "已读任务数",
      "查看方案客户数",
      "去洞察客户数",
      "去电访客户数",
    ]);
    expect(option.series?.[3]).toMatchObject({
      name: "查看方案客户数",
      type: "line",
      yAxisIndex: 1,
    });
    expect(option.series?.[4]).toMatchObject({
      name: "去洞察客户数",
      type: "line",
      yAxisIndex: 1,
    });
    expect(option.series?.[5]).toMatchObject({
      name: "去电访客户数",
      type: "line",
      yAxisIndex: 1,
    });
    expect(option.series?.some((item) => item.name === "已读任务数")).toBe(
      true,
    );
  });

  it("renders the report-view customer card with current annotations", async () => {
    renderBusinessOverview();

    expect(await screen.findByText("查看报告客户数")).toBeInTheDocument();
    expect(screen.getByText("30")).toBeInTheDocument();
    expect(screen.getByText("去洞察客户数 20")).toBeInTheDocument();
    expect(screen.getByText("去电访客户数 10")).toBeInTheDocument();
  });

  it("shows unified loading placeholders for dashboard cards while summary requests are pending", async () => {
    const pending = new Promise<never>(() => {});
    tracingApiMock.getOverview.mockReturnValueOnce(pending);
    tracingApiMock.getHourlyTrend.mockReturnValueOnce(pending);
    tracingApiMock.getTaskStatusSummary.mockReturnValueOnce(pending);

    renderBusinessOverview();

    const loadingPlaceholders = await screen.findAllByTestId(
      "overview-panel-loading",
    );
    expect(loadingPlaceholders.length).toBeGreaterThanOrEqual(6);
    expect(screen.queryByText("调用量趋势加载中...")).not.toBeInTheDocument();
    expect(screen.queryByText("任务执行概览加载中...")).not.toBeInTheDocument();
  });

  it("hides stale error summary content while error card is loading", async () => {
    const pending = new Promise<never>(() => {});
    tracingApiMock.getErrorSummary.mockReturnValueOnce(pending);

    renderBusinessOverview();

    expect(
      await screen.findByTestId("overview-panel-loading"),
    ).toBeInTheDocument();
    expect(screen.queryByText("报错总数")).not.toBeInTheDocument();
  });

  it("auto-loads more skills when the first page does not fill a scrollable viewport", async () => {
    tracingApiMock.getSkills
      .mockResolvedValueOnce({
        items: Array.from({ length: 10 }, (_, index) => ({
          skill_name: `skill-${index + 1}`,
          count: index + 1,
          avg_duration_ms: 100,
        })),
        total: 14,
        page: 1,
        page_size: 10,
      })
      .mockResolvedValueOnce({
        items: Array.from({ length: 4 }, (_, index) => ({
          skill_name: `skill-${index + 11}`,
          count: index + 11,
          avg_duration_ms: 100,
        })),
        total: 14,
        page: 2,
        page_size: 10,
      });

    renderBusinessOverview();

    await waitFor(() => {
      expect(tracingApiMock.getSkills).toHaveBeenNthCalledWith(
        1,
        1,
        10,
        expect.any(Object),
      );
    });

    await waitFor(() => {
      expect(tracingApiMock.getSkills).toHaveBeenNthCalledWith(
        2,
        2,
        10,
        expect.any(Object),
      );
    });

    expect(await screen.findByText("skill-14")).toBeInTheDocument();
  });

  it("auto-loads more active users when the first page does not fill a scrollable viewport", async () => {
    tracingApiMock.getUsers
      .mockResolvedValueOnce({
        items: Array.from({ length: 10 }, (_, index) => ({
          user_id: `user-${index + 1}`,
          user_name: `用户${index + 1}`,
          bbk_id: "100",
          manual_calls: index + 1,
          cron_executions: 0,
          cron_success: 0,
          cron_reads: 0,
        })),
        total: 14,
        page: 1,
        page_size: 10,
      })
      .mockResolvedValueOnce({
        items: Array.from({ length: 4 }, (_, index) => ({
          user_id: `user-${index + 11}`,
          user_name: `用户${index + 11}`,
          bbk_id: "100",
          manual_calls: index + 11,
          cron_executions: 0,
          cron_success: 0,
          cron_reads: 0,
        })),
        total: 14,
        page: 2,
        page_size: 10,
      });

    renderBusinessOverview();

    await waitFor(() => {
      expect(tracingApiMock.getUsers).toHaveBeenNthCalledWith(
        1,
        1,
        10,
        expect.any(Object),
      );
    });

    await waitFor(() => {
      expect(tracingApiMock.getUsers).toHaveBeenNthCalledWith(
        2,
        2,
        10,
        expect.any(Object),
      );
    });

    expect(await screen.findByText(/用户14/)).toBeInTheDocument();
  });
});

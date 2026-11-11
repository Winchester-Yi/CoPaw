import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";
import Tasks from "./index";
import { fetchInsuranceTracking } from "./InsuranceTracking/api";
import type { DialogState } from "../store";
import type { Customer, Plan } from "../types";
import type { CustomerCounts } from "../api";

const state = vi.hoisted(() => ({
  customers: [] as Customer[],
  customersLoading: false,
  todayCounts: {
    targetCusCount: 1,
    todayTouchCount: 0,
    sevenDayTouchCount: 0,
    unTouchedCount: 1,
  } as CustomerCounts | null,
  pendingCounts: {
    targetCusCount: 1,
    todayTouchCount: 0,
    sevenDayTouchCount: 0,
    unTouchedCount: 1,
  } as CustomerCounts | null,
  doneCounts: {
    targetCusCount: 1,
    todayTouchCount: 0,
    sevenDayTouchCount: 0,
    unTouchedCount: 1,
  } as CustomerCounts | null,
  pendingCustomers: [] as Customer[],
  pendingLoading: false,
  doneCustomers: [] as Customer[],
  doneLoading: false,
  taskListsRefreshing: false,
  plans: [] as Plan[],
  plansLoaded: true,
  refreshTodayCustomers: vi.fn().mockResolvedValue(undefined),
  loadTodayCustomers: vi.fn().mockResolvedValue(undefined),
  loadPendingCustomers: vi.fn().mockResolvedValue(undefined),
  loadDoneCustomers: vi.fn().mockResolvedValue(undefined),
  openDialog: vi.fn(),
}));

vi.mock("../store", () => ({
  useCanAccess: () => true,
  useWealthStore: (selector: (value: typeof state) => unknown) =>
    selector(state),
}));

vi.mock("./InsuranceTracking/api", async (original) => ({
  ...(await original<typeof import("./InsuranceTracking/api")>()),
  fetchInsuranceTracking: vi.fn(),
}));

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchInsuranceTracking).mockRejectedValue(
    new Error("保险跟踪加载失败"),
  );
  state.todayCounts =
    state.pendingCounts =
    state.doneCounts =
      {
        targetCusCount: 1,
        todayTouchCount: 0,
        sevenDayTouchCount: 0,
        unTouchedCount: 1,
      };
  state.plansLoaded = true;
  state.plans = [];
  state.customers = [];
  state.pendingCustomers = [];
  state.doneCustomers = [];
  state.customersLoading = false;
  state.pendingLoading = false;
  state.doneLoading = false;
  state.taskListsRefreshing = false;
});

describe("今日任务加载", () => {
  it.each([
    { page: "today", customerView: false, columns: 7 },
    { page: "today", customerView: true, columns: 9 },
    { page: "pending", customerView: false, columns: 8 },
    { page: "done", customerView: false, columns: 8 },
  ] as const)(
    "$page 客户视角=$customerView 首次加载显示对齐表头的五行骨架，完成后才显示空态",
    ({ page, customerView, columns }) => {
      const view = () => (
        <MemoryRouter>
          <Tasks page={page} />
        </MemoryRouter>
      );
      // 等待规划期间也使用相同骨架，不提前展示无数据提示。
      state.plansLoaded = false;
      const { container, rerender } = render(view());
      if (customerView)
        fireEvent.click(screen.getByRole("button", { name: "客户视角" }));
      const status = screen.getByRole("status");
      expect(status).toHaveTextContent("客户清单加载中");
      expect(status.nextElementSibling).toHaveAttribute("aria-busy", "true");
      expect(screen.queryByText("暂无符合条件的客户")).not.toBeInTheDocument();
      const headers = container.querySelectorAll("thead th");
      expect(headers).toHaveLength(columns);
      const rows = container.querySelectorAll("tbody tr");
      expect(rows).toHaveLength(5);
      for (const row of rows) {
        expect(row).toHaveAttribute("aria-hidden", "true");
        expect(row.querySelectorAll("td")).toHaveLength(columns);
        expect(row.querySelector("button, a, input")).toBeNull();
      }

      state.plansLoaded = true;
      rerender(view());
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
      expect(
        container.querySelector('[aria-busy="false"]'),
      ).toBeInTheDocument();
      expect(screen.getByText("暂无符合条件的客户")).toBeInTheDocument();
      expect(container.querySelectorAll("tbody tr")).toHaveLength(1);
    },
  );

  it.each(["target", "missing"])(
    "规划跳转参数 %s 在规划异步加载后选中对应 ID，无效 ID 回退首项",
    (skillId) => {
      state.plansLoaded = false;
      const renderTasks = () => (
        <MemoryRouter initialEntries={[`/wealth/today?skillId=${skillId}`]}>
          <Tasks page="today" />
        </MemoryRouter>
      );
      const { rerender } = render(renderTasks());
      expect(state.refreshTodayCustomers).not.toHaveBeenCalled();
      state.plans = [
        {
          id: "plan",
          name: "规划",
          source: "我的关注",
          desc: "",
          customers: 0,
          tasks: 0,
          rate: 0,
          status: "已自动下发",
          publishStatus: "published",
          period: "自定义",
          start: "2020-01-01",
          end: "2030-12-31",
          items: ["first", "target"].map((id) => ({
            id,
            sceneName: "同名场景",
            categoryLabel: "理财",
            categoryCode: "finance",
            mcpRelations: [],
            direction: "",
            cycle: "自定义",
            start: "2020-01-01",
            end: "2030-12-31",
            schedule: { type: "daily", hour: 9, minute: 0 },
          })),
        },
      ];
      state.plansLoaded = true;
      rerender(renderTasks());
      const expected = skillId === "target" ? "target" : "first";
      expect(state.refreshTodayCustomers).toHaveBeenCalledExactlyOnceWith(
        "business",
        expected,
      );
      expect(screen.getByText("当前任务：同名场景")).toBeInTheDocument();
    },
  );

  it.each(["today", "pending", "done"] as const)(
    "%s 显示自身后端四项汇总，零值保留，缺失字段显示占位",
    (page) => {
      const counts = {
        targetCusCount: 99,
        todayTouchCount: 0,
        sevenDayTouchCount: 30,
        unTouchedCount: 75,
      };
      state.todayCounts = state.pendingCounts = state.doneCounts = null;
      if (page === "today") state.todayCounts = counts;
      if (page === "pending") state.pendingCounts = counts;
      if (page === "done") state.doneCounts = counts;
      const { rerender } = render(
        <MemoryRouter>
          <Tasks page={page} />
        </MemoryRouter>,
      );
      expect(screen.getByText(/目标客户/).querySelector("b")).toHaveTextContent(
        "99",
      );
      expect(
        screen.getByText(/今日已触达/).querySelector("b"),
      ).toHaveTextContent("0");
      expect(
        screen.getByText(/七日内已触达/).querySelector("b"),
      ).toHaveTextContent("30");
      expect(
        screen.getByText(/^待处理/, { selector: "span" }).querySelector("b"),
      ).toHaveTextContent("75");
      state.todayCounts = state.pendingCounts = state.doneCounts = null;
      rerender(
        <MemoryRouter>
          <Tasks page={page} />
        </MemoryRouter>,
      );
      expect(screen.getByText(/目标客户/).querySelector("b")).toHaveTextContent(
        "--",
      );
      expect(
        screen.getByText(/七日内已触达/).querySelector("b"),
      ).toHaveTextContent("--");
    },
  );

  it("空名单显示所选场景执行时间，切换场景与刷新保留对应技能", () => {
    state.plans = [
      {
        id: "plan",
        name: "规划",
        source: "我的关注",
        desc: "",
        customers: 0,
        tasks: 0,
        rate: 0,
        status: "已自动下发",
        publishStatus: "published",
        period: "自定义",
        start: "2020-01-01",
        end: "2030-12-31",
        items: ["a", "b"].map((id) => ({
          id,
          sceneName: `场景${id}`,
          categoryLabel: "理财",
          categoryCode: "finance",
          mcpRelations: [],
          direction: "",
          cycle: "自定义",
          start: "2020-01-01",
          end: "2030-12-31",
          schedule: { type: "daily", hour: id === "a" ? 9 : 15, minute: 0 },
        })),
      },
    ];
    const { rerender } = render(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    expect(state.refreshTodayCustomers).toHaveBeenCalledExactlyOnceWith(
      "business",
      "a",
    );
    expect(screen.getByText("执行时间：每日 09:00")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "我的关注场景b" }));
    expect(state.loadTodayCustomers).toHaveBeenLastCalledWith("business", "b");
    expect(screen.getByText("执行时间：每日 15:00")).toBeInTheDocument();
    expect(state.refreshTodayCustomers).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "刷新工作任务" }));
    expect(state.refreshTodayCustomers).toHaveBeenLastCalledWith(
      "business",
      "b",
    );
    state.customersLoading = true;
    rerender(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    expect(screen.queryByText(/执行时间：/)).not.toBeInTheDocument();
    state.customersLoading = false;
    state.customers = [
      {
        id: "customer",
        custUid: "customer",
        skillId: "b",
        name: "已生成客户",
        label: "",
        reason: "",
        category: "理财",
        task: "场景b",
        done: false,
        channel: "",
        time: "",
        note: "",
      },
    ];
    rerender(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    expect(screen.queryByText(/执行时间：/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "筛选接触状态" }));
    fireEvent.click(screen.getByRole("button", { name: /^已接触/ }));
    expect(screen.queryByText("已生成客户")).not.toBeInTheDocument();
    expect(screen.queryByText(/执行时间：/)).not.toBeInTheDocument();
    state.customers = [];
    fireEvent.click(screen.getByRole("button", { name: "客户视角" }));
    expect(screen.queryByText(/执行时间：/)).not.toBeInTheDocument();
    rerender(
      <MemoryRouter>
        <Tasks page="pending" />
      </MemoryRouter>,
    );
    expect(screen.queryByText(/执行时间：/)).not.toBeInTheDocument();
    rerender(
      <MemoryRouter>
        <Tasks page="done" />
      </MemoryRouter>,
    );
    expect(screen.queryByText(/执行时间：/)).not.toBeInTheDocument();
  });

  it.each(["today", "pending", "done"] as const)(
    "%s 标签页后台刷新保留旧名单和统计，不显示加载占位，返回后更新数据",
    (page) => {
      const customer: Customer = {
        id: "cached",
        custUid: "cached",
        skillId: "skill",
        name: "现有客户",
        label: "",
        reason: "",
        category: "",
        task: "",
        touched: 0,
        done: page === "done",
        channel: "",
        time: "",
        note: "",
      };
      state.customers = [customer];
      state.pendingCustomers = [customer];
      state.doneCustomers = [customer];
      const { rerender } = render(
        <MemoryRouter>
          <Tasks page={page} />
        </MemoryRouter>,
      );
      if (page === "today")
        fireEvent.click(screen.getByRole("button", { name: "客户视角" }));
      state.taskListsRefreshing = true;
      state.customersLoading = true;
      state.pendingLoading = true;
      state.doneLoading = true;
      rerender(
        <MemoryRouter>
          <Tasks page={page} />
        </MemoryRouter>,
      );
      expect(screen.getByText("现有客户")).toBeInTheDocument();
      expect(screen.getByText(/目标客户/).querySelector("b")).toHaveTextContent(
        "1",
      );
      expect(screen.getByText("共 1 条记录")).toBeInTheDocument();
      expect(screen.queryByText(/客户清单加载中/)).not.toBeInTheDocument();
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
      const updated = { ...customer, id: "updated", name: "最新客户" };
      state.customers = [updated];
      state.pendingCustomers = [updated];
      state.doneCustomers = [updated];
      state.taskListsRefreshing = false;
      state.customersLoading = false;
      state.pendingLoading = false;
      state.doneLoading = false;
      rerender(
        <MemoryRouter>
          <Tasks page={page} />
        </MemoryRouter>,
      );
      expect(screen.getByText("最新客户")).toBeInTheDocument();
      expect(screen.queryByText("现有客户")).not.toBeInTheDocument();
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
    },
  );

  it.each(["today", "pending", "done"] as const)(
    "%s 加载时不展示缓存的26条统计和名单，返回后展示24条真实数据",
    (page) => {
      const cached = Array.from(
        { length: 26 },
        (_, index): Customer => ({
          id: String(index),
          custUid: String(index),
          skillId: "skill",
          name: `缓存客户${index}`,
          label: "",
          reason: "",
          category: "",
          task: "",
          touched: 0,
          done: page === "done",
          channel: "",
          time: "",
          note: "",
        }),
      );
      state.customers = cached;
      state.pendingCustomers = cached;
      state.doneCustomers = cached;
      const { rerender } = render(
        <MemoryRouter>
          <Tasks page={page} />
        </MemoryRouter>,
      );
      if (page === "today")
        fireEvent.click(screen.getByRole("button", { name: "客户视角" }));
      state.customersLoading = true;
      state.pendingLoading = true;
      state.doneLoading = true;
      rerender(
        <MemoryRouter>
          <Tasks page={page} />
        </MemoryRouter>,
      );
      expect(screen.getByText(/目标客户/).querySelector("b")).toHaveTextContent(
        "--",
      );
      expect(
        screen.getByText(/待处理/, { selector: "span" }).querySelector("b"),
      ).toHaveTextContent("--");
      expect(screen.getByRole("status")).toHaveTextContent("客户清单加载中");
      expect(
        screen
          .getByRole("table")
          .querySelectorAll('tbody tr[aria-hidden="true"]'),
      ).toHaveLength(5);
      expect(screen.queryByText("缓存客户0")).not.toBeInTheDocument();
      expect(screen.getByText("共 -- 条记录")).toBeInTheDocument();
      const loaded = cached
        .slice(0, 24)
        .map((customer, index) => ({ ...customer, name: `返回客户${index}` }));
      state.todayCounts =
        state.pendingCounts =
        state.doneCounts =
          {
            targetCusCount: 24,
            todayTouchCount: 5,
            sevenDayTouchCount: 12,
            unTouchedCount: 19,
          };
      state.customers = loaded;
      state.pendingCustomers = loaded;
      state.doneCustomers = loaded;
      state.customersLoading = false;
      state.pendingLoading = false;
      state.doneLoading = false;
      rerender(
        <MemoryRouter>
          <Tasks page={page} />
        </MemoryRouter>,
      );
      expect(screen.getByText(/目标客户/).querySelector("b")).toHaveTextContent(
        "24",
      );
      expect(screen.getByText("共 24 条记录")).toBeInTheDocument();
      expect(screen.getByText("返回客户0")).toBeInTheDocument();
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
    },
  );

  it("进入页先刷新规划；切到客户视角只查名单，切回经营视角再刷新规划", () => {
    render(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    expect(state.refreshTodayCustomers).toHaveBeenCalledExactlyOnceWith(
      "business",
    );
    expect(state.loadTodayCustomers).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "客户视角" }));
    expect(state.loadTodayCustomers).toHaveBeenCalledExactlyOnceWith(
      "customer",
    );
    expect(state.refreshTodayCustomers).toHaveBeenCalledTimes(1);
    expect(
      screen.queryByRole("button", { name: "刷新工作任务" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "经营视角" }));
    expect(state.refreshTodayCustomers).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "刷新工作任务" }));
    expect(state.refreshTodayCustomers).toHaveBeenCalledTimes(3);
    expect(state.loadTodayCustomers).toHaveBeenCalledTimes(1);
  });

  it("从其他任务页返回时，即使保留客户视角也重新刷新规划", () => {
    const { rerender } = render(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "客户视角" }));
    rerender(
      <MemoryRouter>
        <Tasks page="pending" />
      </MemoryRouter>,
    );
    expect(state.loadPendingCustomers).toHaveBeenCalledTimes(1);
    rerender(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    expect(state.refreshTodayCustomers).toHaveBeenLastCalledWith("customer");
    expect(state.refreshTodayCustomers).toHaveBeenCalledTimes(2);
    expect(state.loadTodayCustomers).toHaveBeenCalledTimes(1);
  });

  it("等待初始规划加载完成后再刷新", () => {
    state.plansLoaded = false;
    const { rerender } = render(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    expect(state.refreshTodayCustomers).not.toHaveBeenCalled();
    state.plansLoaded = true;
    rerender(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    expect(state.refreshTodayCustomers).toHaveBeenCalledExactlyOnceWith(
      "business",
    );
  });
});

it("当天没有保险场景也补充固定保险入口，点击后不按虚构技能查询名单", async () => {
  render(
    <MemoryRouter>
      <Tasks page="today" />
    </MemoryRouter>,
  );
  expect(screen.getByText("保险", { selector: "summary" })).toBeInTheDocument();
  state.refreshTodayCustomers.mockClear();
  state.loadTodayCustomers.mockClear();
  fireEvent.click(screen.getByRole("button", { name: "保险核心跟踪" }));
  await screen.findByText("保险跟踪加载失败");
  expect(state.refreshTodayCustomers).not.toHaveBeenCalled();
  expect(state.loadTodayCustomers).not.toHaveBeenCalled();
  expect(
    screen.queryByRole("button", { name: "客户视角" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "经营视角" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "返回今日任务" }),
  ).not.toBeInTheDocument();
});

it("已有保险分类时复用分组，空态引导跳转保险分类下第一个技能", async () => {
  state.plans = [
    {
      id: "insurance-plan",
      name: "保险规划",
      source: "我的关注",
      desc: "",
      customers: 0,
      tasks: 0,
      rate: 0,
      status: "已自动下发",
      publishStatus: "published",
      period: "自定义",
      start: "2020-01-01",
      end: "2030-12-31",
      items: [
        {
          id: "insurance-skill",
          sceneName: "保险初筛识别",
          categoryLabel: "保险",
          categoryCode: "insurance",
          mcpRelations: [],
          direction: "",
          cycle: "自定义",
          start: "2020-01-01",
          end: "2030-12-31",
          schedule: { type: "daily", hour: 9, minute: 0 },
        },
      ],
    },
  ];
  state.plans[0].items.push({
    ...state.plans[0].items[0],
    id: "insurance-skill-2",
    sceneName: "保险需求沟通",
  });
  vi.mocked(fetchInsuranceTracking).mockResolvedValue({
    customers: [],
    counts: { customers: 0, tracking: 0, highIntention: 0 },
  });
  render(
    <MemoryRouter>
      <Tasks page="today" />
    </MemoryRouter>,
  );
  expect(screen.getAllByText("保险", { selector: "summary" })).toHaveLength(1);
  const group = screen
    .getByText("保险", { selector: "summary" })
    .closest("details")!;
  expect(
    [...group.querySelectorAll("button")].map((button) => button.textContent),
  ).toEqual(["保险核心跟踪", "我的关注保险初筛识别", "我的关注保险需求沟通"]);
  fireEvent.click(screen.getByRole("button", { name: /我的关注保险初筛识别/ }));
  expect(screen.getByText("当前任务：保险初筛识别")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /我的关注保险需求沟通/ }));
  fireEvent.click(screen.getByRole("button", { name: "保险核心跟踪" }));
  const viewTasks = await screen.findByRole("button", { name: "查看保险任务" });
  expect(
    screen.queryByRole("button", { name: "客户视角" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "经营视角" }),
  ).not.toBeInTheDocument();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  state.loadTodayCustomers.mockClear();
  fireEvent.click(viewTasks);
  expect(screen.getByRole("button", { name: "客户视角" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "经营视角" })).toBeInTheDocument();
  expect(screen.getByText("当前任务：保险初筛识别")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "保险核心跟踪" })).toHaveAttribute(
    "aria-pressed",
    "false",
  );
  expect(state.loadTodayCustomers).toHaveBeenCalledWith(
    "business",
    "insurance-skill",
  );
});

it.each([
  "https://wealth.example/reportView?resultId=plan-1",
  "https://wealth.example/reportView",
  undefined,
])("保险经营方案复用原预览逻辑，方案地址为 %s", async (link) => {
  vi.mocked(fetchInsuranceTracking).mockResolvedValue({
    customers: [
      {
        id: "insurance-customer",
        custUid: "cust-insurance",
        name: "保险客户",
        intention: 3,
        sceneName: "保险初筛识别",
        link,
      },
    ],
    counts: { customers: 1, highIntention: 1, tracking: 1 },
  });
  render(
    <MemoryRouter>
      <Tasks page="today" />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByRole("button", { name: "保险核心跟踪" }));
  const button = await screen.findByRole("button", { name: "查看经营方案" });
  fireEvent.click(button);
  const first = state.openDialog.mock.calls[0][0] as DialogState;
  if (!link) {
    expect(first.title).toBe("客户经营方案");
    expect(
      (first.body as ReactElement<{ children: string }>).props.children,
    ).toBe("暂无经营方案");
    expect(first.plain).not.toBe(true);
    return;
  }
  expect(first.plain).toBe(true);
  expect(first.buttons).toEqual([{ label: "关闭" }]);
  const firstPreview = first.body as ReactElement<{
    url: string;
    custName: string;
    title: string;
  }>;
  expect(firstPreview.props.custName).toBe("保险客户");
  expect(firstPreview.props.title).toBe("保险初筛识别");
  const firstUrl = new URL(firstPreview.props.url);
  expect(firstUrl.searchParams.get("_t")).toBe("1");
  expect(firstUrl.searchParams.get("resultId")).toBe(
    new URL(link).searchParams.get("resultId"),
  );
  fireEvent.click(button);
  const second = state.openDialog.mock.calls[1][0] as DialogState;
  const secondPreview = second.body as typeof firstPreview;
  expect(new URL(secondPreview.props.url).searchParams.get("_t")).toBe("2");
  expect(secondPreview.key).not.toBe(firstPreview.key);
});

it.each([
  { category: "保险", traced: 0, label: "加入跟踪", disabled: false },
  { category: "保险", traced: 1, label: "已加入跟踪", disabled: true },
  { category: "保险", traced: undefined, label: "加入跟踪", disabled: true },
  { category: "理财", traced: 0, label: null, disabled: false },
])(
  "$category 技能 traced=$traced 的加入跟踪入口状态",
  ({ category, traced, label, disabled }) => {
    state.plans = [
      {
        id: "plan",
        name: "经营规划",
        source: "我的关注",
        desc: "",
        customers: 1,
        tasks: 1,
        rate: 0,
        status: "已自动下发",
        publishStatus: "published",
        period: "自定义",
        start: "2020-01-01",
        end: "2030-12-31",
        items: [
          {
            id: "skill",
            sceneName: "客户需求识别",
            categoryLabel: category,
            categoryCode: category === "保险" ? "insurance" : "finance",
            mcpRelations: [],
            direction: "",
            cycle: "自定义",
            start: "2020-01-01",
            end: "2030-12-31",
            schedule: { type: "daily", hour: 9, minute: 0 },
          },
        ],
      },
    ];
    state.customers = [
      {
        id: "skill|c1",
        custUid: "c1",
        skillId: "skill",
        name: "林芳",
        category,
        task: "客户需求识别",
        label: "",
        reason: "",
        done: false,
        channel: "",
        time: "",
        note: "",
        traced,
      },
    ];
    render(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    if (label) {
      const button = screen.getByRole("button", { name: label });
      if (disabled) expect(button).toBeDisabled();
      else expect(button).toBeEnabled();
    } else {
      expect(
        screen.queryByRole("button", { name: "加入跟踪" }),
      ).not.toBeInTheDocument();
    }
  },
);

it("无保险技能时空态不显示查看保险任务按钮", async () => {
  vi.mocked(fetchInsuranceTracking).mockResolvedValue({
    customers: [],
    counts: { customers: 0, tracking: 0, highIntention: 0 },
  });
  render(
    <MemoryRouter>
      <Tasks page="today" />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByRole("button", { name: "保险核心跟踪" }));
  await screen.findByText("暂未添加跟踪客户");
  expect(
    screen.queryByRole("button", { name: "查看保险任务" }),
  ).not.toBeInTheDocument();
});

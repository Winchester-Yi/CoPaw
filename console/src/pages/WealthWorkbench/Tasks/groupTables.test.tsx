import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Customer } from "../types";
import Tasks from "./index";

const state = vi.hoisted(() => ({
  customers: [] as Customer[],
  customersLoading: false,
  pendingCustomers: [],
  pendingLoading: false,
  doneCustomers: [],
  doneLoading: false,
  plans: [
    {
      id: "plan",
      name: "经营规划",
      source: "我的关注",
      publishStatus: "published",
      start: "2020-01-01",
      end: "2030-12-31",
      items: [
        {
          id: "skill",
          sceneName: "资产到期经营",
          categoryLabel: "理财",
          start: "2020-01-01",
          end: "2030-12-31",
          schedule: { type: "daily" },
        },
      ],
    },
  ],
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

afterEach(cleanup);
beforeEach(() => {
  state.customers = ["48", "48", "50"].map((age, index) => ({
    id: `c${index}`,
    custUid: `c${index}`,
    skillId: "skill",
    name: `客户${index}`,
    label: "",
    reason: "经营机会",
    category: "理财",
    task: "资产到期经营",
    done: false,
    channel: "",
    time: "",
    note: "",
    groupFields: [
      { name: "age", label: "年龄", value: age },
      { name: "risk", label: "风险等级", value: "稳健型" },
    ],
  }));
});

describe("经营分类独立表格", () => {
  it("每个分类有独立表头，分类栏位于表格外并显示人数", () => {
    render(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    const tables = screen.getAllByRole("table");
    expect(tables).toHaveLength(2);
    expect(
      within(tables[0]).getByRole("columnheader", { name: "客户姓名" }),
    ).toBeInTheDocument();
    expect(
      within(tables[1]).getByRole("columnheader", { name: "客户姓名" }),
    ).toBeInTheDocument();
    const heading = screen.getByRole("button", {
      name: "年龄：48 / 风险等级：稳健型，2 人",
    });
    expect(heading).toHaveTextContent("年龄：48 / 风险等级：稳健型");
    expect(heading.closest("table")).toBeNull();
    expect(within(tables[0]).getByText("客户0")).toBeInTheDocument();
    expect(within(tables[0]).queryByText("客户2")).not.toBeInTheDocument();
    expect(within(tables[1]).getByText("客户2")).toBeInTheDocument();
    fireEvent.click(heading);
    expect(screen.getAllByRole("table")).toHaveLength(1);
    expect(heading).toHaveAttribute("aria-expanded", "false");
    expect(
      document.getElementById(heading.getAttribute("aria-controls")!),
    ).toHaveAttribute("hidden");
    fireEvent.click(heading);
    expect(screen.getAllByRole("table")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "客户视角" }));
    expect(screen.getAllByRole("table")).toHaveLength(1);
    expect(
      screen.queryByRole("button", {
        name: "年龄：48 / 风险等级：稳健型，2 人",
      }),
    ).not.toBeInTheDocument();
  });

  it("空分类配置保持一张表格，空名单也保留表头与空态", () => {
    state.customers = state.customers.map((customer) => ({
      ...customer,
      groupFields: [],
    }));
    const { rerender } = render(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    expect(screen.getAllByRole("table")).toHaveLength(1);
    expect(
      screen.queryByRole("heading", { level: 3, name: /年龄/ }),
    ).not.toBeInTheDocument();
    state.customers = [];
    rerender(
      <MemoryRouter>
        <Tasks page="today" />
      </MemoryRouter>,
    );
    expect(screen.getAllByRole("table")).toHaveLength(1);
    expect(
      screen.getByRole("columnheader", { name: "客户姓名" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/暂无客户名单/)).toBeInTheDocument();
  });
});

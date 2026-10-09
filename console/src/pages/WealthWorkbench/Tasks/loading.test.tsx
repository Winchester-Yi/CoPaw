import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Tasks from "./index";

const state = vi.hoisted(() => ({
  customers: [],
  customersLoading: false,
  pendingCustomers: [],
  pendingLoading: false,
  doneCustomers: [],
  doneLoading: false,
  plans: [],
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
  vi.clearAllMocks();
  state.plansLoaded = true;
});

describe("今日任务加载", () => {
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

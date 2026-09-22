import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useWealthStore } from "../store";
import Board from "./index";

vi.mock("../api", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api")>();
  return {
    ...original,
    fetchAvailableSceneCount: vi.fn().mockResolvedValue(0),
    fetchSkillStats: vi.fn().mockResolvedValue({}),
  };
});

describe("Board", () => {
  beforeEach(() => {
    useWealthStore.setState({ plans: [], plansLoaded: false });
  });

  it("规划加载中展示稳定占位，加载完成后才展示真实空态", () => {
    render(
      <MemoryRouter>
        <Board />
      </MemoryRouter>,
    );

    expect(screen.getByRole("status")).toHaveTextContent("正在加载规划数据");
    expect(screen.queryByText("当前筛选条件下暂无规划")).toBeNull();

    act(() => useWealthStore.setState({ plansLoaded: true }));
    fireEvent.click(screen.getByRole("button", { name: "列表模式" }));

    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByText("当前筛选条件下暂无规划")).toBeInTheDocument();
  });
});

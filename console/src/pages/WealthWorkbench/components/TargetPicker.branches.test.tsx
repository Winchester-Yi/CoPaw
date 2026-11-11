import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { request } from "../../../api/request";
import { useIframeStore } from "../../../stores/iframeStore";
import { useWealthStore } from "../store";
import { resetInstitutionCache } from "../distribution/institutionCache";
import { TargetPicker } from "./TargetPicker";
vi.mock("../../../api/request", () => ({ request: vi.fn() }));
const mock = vi.mocked(request);
const tree = {
  mOrgCode: "110",
  mOrgName: "北京分行",
  mOrgLvl: "30",
  children: [
    {
      mOrgCode: "a",
      mOrgName: "罗湖支行",
      mOrgLvl: 70,
      children: [{ mOrgCode: "a1", mOrgName: "东门支行", mOrgLvl: 80 }],
    },
    { mOrgCode: "b", mOrgName: "南山支行", mOrgLvl: 70 },
  ],
};
const people = [
  {
    sapID: "0001",
    userName: "张三",
    branchID: "110",
    subBranchID: "a",
    subBranchName: "罗湖支行",
  },
  {
    sapID: "0002",
    userName: "李四",
    branchID: "110",
    subBranchID: "a1",
    subBranchName: "东门支行",
  },
];
beforeEach(() => {
  mock.mockReset();
  resetInstitutionCache();
  useIframeStore
    .getState()
    .setContext({ bbk: "110", orgCode: "110", orgLvl: "30", sysId: "retail" });
  useWealthStore.setState({
    ...useWealthStore.getInitialState(),
    accountId: "middle",
    plansLoaded: true,
  });
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  mock.mockImplementation(async (path) => ({
    code: "200",
    data: path.endsWith("bbkTree") ? tree : people,
  }));
});
afterEach(() => {
  cleanup();
  resetInstitutionCache();
  vi.unstubAllGlobals();
});
async function ready() {
  render(<TargetPicker />);
  await screen.findByRole("button", { name: "罗湖支行" });
}
async function loadA() {
  fireEvent.click(screen.getByRole("button", { name: "罗湖支行" }));
  await waitFor(() => expect(screen.getByText(/共 2 人/)).toBeInTheDocument());
}

describe("中台紧凑机构与人员卡片", () => {
  it("分行和父级勾选展开所有机构请求并联动子级，含零人支行", async () => {
    const branchB = {
      sapID: "0003",
      userName: "王五",
      branchID: "110",
      subBranchID: "b",
      subBranchName: "南山支行",
    };
    const scopePeople = [people[0], branchB];
    mock.mockImplementation(async (path, options) => {
      if (path.endsWith("bbkTree")) return { code: "200", data: tree };
      const codes = JSON.parse(options!.body as string).orgList.map(
        (item: { mmOrgCode: string }) => item.mmOrgCode,
      );
      return {
        code: "200",
        data: scopePeople.filter((user) => codes.includes(user.subBranchID)),
      };
    });
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "展开罗湖支行" }));
    const rootBox = screen.getByRole("checkbox", {
      name: "选择北京分行全部人员",
    });
    const parentBox = screen.getByRole("checkbox", {
      name: "选择罗湖支行全部人员",
    });
    const childBox = screen.getByRole("checkbox", {
      name: "选择东门支行全部人员",
    });
    const otherBox = screen.getByRole("checkbox", {
      name: "选择南山支行全部人员",
    });
    fireEvent.click(parentBox);
    await waitFor(() => expect(childBox).toBeChecked());
    expect(parentBox).toBeChecked();
    expect(otherBox).not.toBeChecked();
    expect(
      JSON.parse(mock.mock.calls[1][1]!.body as string).orgList.map(
        (item: { mmOrgCode: string }) => item.mmOrgCode,
      ),
    ).toEqual(["a", "a1"]);
    fireEvent.click(rootBox);
    await waitFor(() => expect(otherBox).toBeChecked());
    expect(rootBox).toBeChecked();
    expect(parentBox).toBeChecked();
    expect(childBox).toBeChecked();
    expect(
      JSON.parse(mock.mock.calls[2][1]!.body as string).orgList.map(
        (item: { mmOrgCode: string }) => item.mmOrgCode,
      ),
    ).toEqual(["110", "a", "a1", "b"]);
    expect(useWealthStore.getState().targetSapIds.sort()).toEqual([
      "0001",
      "0003",
    ]);
    fireEvent.click(childBox);
    expect(parentBox).toBePartiallyChecked();
    expect(rootBox).toBePartiallyChecked();
    expect(otherBox).toBeChecked();
    fireEvent.click(rootBox);
    expect(childBox).toBeChecked();
    expect(rootBox).toBeChecked();
    expect(mock).toHaveBeenCalledTimes(3);
  });
  it("空机构可勾选并显示全部零人，可取消、移除和清空且不重复查询", async () => {
    mock.mockImplementation(async (path) => ({
      code: "200",
      data: path.endsWith("bbkTree") ? tree : [],
    }));
    await ready();
    const checkbox = screen.getByRole("checkbox", {
      name: "选择罗湖支行全部人员",
    });
    fireEvent.click(checkbox);
    await screen.findByText("全部 0 人");
    expect(checkbox).toBeChecked();
    expect(screen.getByText("该机构暂无可分发人员")).toBeInTheDocument();
    expect(useWealthStore.getState().targetSapIds).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "取消本机构全选" }));
    expect(checkbox).not.toBeChecked();
    expect(screen.queryByText("全部 0 人")).not.toBeInTheDocument();
    fireEvent.click(checkbox);
    expect(screen.getByText("全部 0 人")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "移除该机构" }));
    expect(checkbox).not.toBeChecked();
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole("button", { name: "清空全部" }));
    expect(checkbox).not.toBeChecked();
    expect(screen.queryByText("全部 0 人")).not.toBeInTheDocument();
    expect(mock).toHaveBeenCalledTimes(2);
  });
  it("勾选同步展示，取消保留视图，切换勾选清空原人员搜索", async () => {
    await ready();
    const checkbox = screen.getByRole("checkbox", {
      name: "选择罗湖支行全部人员",
    });
    fireEvent.click(checkbox);
    expect(
      screen.getByRole("heading", { name: "罗湖支行人员" }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText(/共 2 人/)).toBeInTheDocument(),
    );
    expect(useWealthStore.getState().targetSapIds).toEqual(["0001", "0002"]);
    const search = screen.getByLabelText("搜索分发目标");
    fireEvent.change(search, { target: { value: "李四" } });
    fireEvent.click(checkbox);
    expect(
      screen.getByRole("heading", { name: "罗湖支行人员" }),
    ).toBeInTheDocument();
    expect(search).toHaveValue("李四");
    expect(search).toBeEnabled();
    expect(useWealthStore.getState().targetSapIds).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "南山支行" }));
    expect(useWealthStore.getState().targetSapIds).toEqual([]);
    await waitFor(() => expect(search).toBeEnabled());
    fireEvent.change(search, { target: { value: "无关搜索" } });
    fireEvent.click(checkbox);
    expect(
      screen.getByRole("heading", { name: "罗湖支行人员" }),
    ).toBeInTheDocument();
    expect(search).toHaveValue("");
    expect(useWealthStore.getState().targetSapIds).toEqual(["0001", "0002"]);
    expect(mock).toHaveBeenCalledTimes(3);
  });
  it("只查完整机构树，左侧没有人数，展开和全树搜索不再请求", async () => {
    await ready();
    const group = screen.getByRole("group", { name: "机构列表" });
    expect(within(group).queryByText(/人/)).not.toBeInTheDocument();
    expect(mock).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "展开罗湖支行" }));
    expect(
      screen.getByRole("button", { name: "东门支行" }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("搜索机构"), {
      target: { value: "a1" },
    });
    expect(
      screen.queryByRole("button", { name: "南山支行" }),
    ).not.toBeInTheDocument();
    expect(mock).toHaveBeenCalledTimes(1);
  });
  it("按需查询、搜索、半选补全和父子合并汇总", async () => {
    await ready();
    await loadA();
    fireEvent.change(screen.getByLabelText("搜索分发目标"), {
      target: { value: "李四" },
    });
    fireEvent.click(screen.getByRole("button", { name: "选择搜索结果" }));
    expect(useWealthStore.getState().targetSapIds).toEqual(["0002"]);
    fireEvent.click(
      screen.getByRole("checkbox", { name: "选择罗湖支行全部人员" }),
    );
    expect(useWealthStore.getState().targetSapIds.sort()).toEqual([
      "0001",
      "0002",
    ]);
    expect(screen.getByText("全部 2 人")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "展开罗湖支行" }));
    fireEvent.click(screen.getByRole("button", { name: "东门支行" }));
    expect(mock).toHaveBeenCalledTimes(2);
    expect(
      screen.getByRole("checkbox", { name: "选择东门支行全部人员" }),
    ).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "移除该机构" }));
    expect(useWealthStore.getState().targetSapIds).toEqual([]);
  });
  it("已有选择全选失败后，点击半选框重试并保留已有人员", async () => {
    let fail = true;
    mock.mockImplementation(async (path) => {
      if (path.endsWith("bbkTree")) return { code: "200", data: tree };
      if (fail) {
        fail = false;
        return { code: "500", message: "批量查询失败" };
      }
      return { code: "200", data: people };
    });
    useWealthStore.setState({
      targetSapIds: ["0001"],
      targets: [
        {
          sapId: "0001",
          name: "张三",
          orgName: "罗湖支行",
          bbkId: "110",
          bbkOrgId: "a",
        },
      ],
    });
    await ready();
    const checkbox = screen.getByRole("checkbox", {
      name: "选择罗湖支行全部人员",
    });
    fireEvent.click(checkbox);
    await screen.findByText(/罗湖支行：批量查询失败/);
    expect(useWealthStore.getState().targetSapIds).toEqual(["0001"]);
    expect(useWealthStore.getState().distributionBlocked).toBe(true);
    fireEvent.click(checkbox);
    expect(useWealthStore.getState().targetSapIds).toEqual(["0001"]);
    await waitFor(() =>
      expect(useWealthStore.getState().targetSapIds).toEqual(["0001", "0002"]),
    );
    expect(checkbox).toBeChecked();
    expect(useWealthStore.getState().distributionBlocked).toBe(false);
  });
  it("全量取消父机构人员后只选子机构，汇总不复活旧父分组", async () => {
    await ready();
    await loadA();
    fireEvent.click(
      screen.getByRole("checkbox", { name: "选择罗湖支行全部人员" }),
    );
    fireEvent.change(screen.getByLabelText("搜索分发目标"), {
      target: { value: "支行" },
    });
    fireEvent.click(screen.getByRole("button", { name: "取消选择搜索结果" }));
    expect(useWealthStore.getState().targetSapIds).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "展开罗湖支行" }));
    fireEvent.click(
      screen.getByRole("checkbox", { name: "选择东门支行全部人员" }),
    );
    expect(useWealthStore.getState().targetSapIds).toEqual(["0002"]);
    expect(screen.getByText("全部 1 人")).toBeInTheDocument();
    expect(screen.queryByText("部分 1 / 2 人")).not.toBeInTheDocument();
  });
  it("分行待选及取消待选都不改变已有支行汇总", async () => {
    useWealthStore.setState({
      targetSapIds: ["0001", "0003"],
      targets: [
        { sapId: "0001", name: "张三", orgName: "罗湖支行", bbkOrgId: "a" },
        { sapId: "0003", name: "王五", orgName: "南山支行", bbkOrgId: "b" },
      ],
    });
    await ready();
    const expectOriginalGroups = () => {
      expect(
        screen.getByRole("heading", { name: /罗湖支行/ }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("heading", { name: /南山支行/ }),
      ).toBeInTheDocument();
      expect(useWealthStore.getState().targetSapIds).toEqual(["0001", "0003"]);
    };
    expectOriginalGroups();
    const checkbox = screen.getByRole("checkbox", {
      name: "选择北京分行全部人员",
    });
    fireEvent.click(checkbox);
    expectOriginalGroups();
    fireEvent.click(checkbox);
    expectOriginalGroups();
    expect(mock).toHaveBeenCalledTimes(1);
  });
  it("会话重建清空人员缓存但复用机构树", async () => {
    await ready();
    await loadA();
    act(() => useWealthStore.getState().resetPlanForm());
    expect(screen.getByLabelText("搜索分发目标")).toBeDisabled();
    await loadA();
    expect(mock).toHaveBeenCalledTimes(3);
  });
});

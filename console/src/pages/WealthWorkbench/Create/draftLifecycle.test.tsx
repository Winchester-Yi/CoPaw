import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../api";
import { ExcelRosterSummary } from "../components/ExcelTargetPicker";
import { Sidebar } from "../components/Sidebar";
import { fetchDistributeTargets } from "../distributeTargets";
import { useWealthStore } from "../store";
import { useIframeStore } from "../../../stores/iframeStore";
import {
  importExcelPeople,
  queryInstitutionTree,
  queryInstitutionPeople,
} from "../distribution/api";
import { resetInstitutionCache } from "../distribution/institutionCache";
import Create from "./index";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  fetchScenesByCategory: vi.fn().mockResolvedValue([]),
}));

vi.mock("../distribution/api", async (original) => ({
  ...(await original<typeof import("../distribution/api")>()),
  importExcelPeople: vi.fn(),
  queryInstitutionTree: vi.fn(),
  queryInstitutionPeople: vi.fn(),
}));
vi.mock("../distributeTargets", () => ({
  fetchDistributeTargets: vi
    .fn()
    .mockResolvedValue([
      { sapId: "new-target", name: "可分发人员", orgName: "支行" },
    ]),
  fetchDistributeBranches: vi.fn().mockResolvedValue([]),
}));

beforeEach(() => {
  resetInstitutionCache();
  useIframeStore.getState().setContext({
    userId: "publisher",
    bbk: "110",
    orgLvl: "30",
    sysId: "retail",
  });
  vi.stubGlobal("__env__", {});
  vi.mocked(importExcelPeople).mockReset();
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  vi.mocked(queryInstitutionTree).mockClear();
  vi.mocked(queryInstitutionTree).mockResolvedValue({
    mOrgCode: "110",
    mOrgName: "北京分行",
    mOrgLvl: 30,
    children: [
      { mOrgCode: "121516", mOrgName: "测试支行", mOrgLvl: 70, children: [] },
    ],
  });
  vi.mocked(queryInstitutionPeople).mockResolvedValue([]);
  useWealthStore.setState({
    accountId: "rm",
    plansLoaded: false,
    plans: [],
    scenesByCategory: { "": [] },
    draft: {
      name: "旧规划",
      items: [
        {
          id: "old-scene",
          sceneName: "原经营场景",
          categoryLabel: "保险",
          categoryCode: "insurance",
          mcpRelations: [],
          direction: "原经营方向",
          cycle: "本月",
          start: "2026-10-01",
          end: "2026-10-31",
          schedule: { type: "daily", hour: 10, minute: 0 },
        },
      ],
    },
    editingId: "old-plan",
    savedAt: "10:00",
    targetSapIds: ["old-target"],
    targets: [{ sapId: "old-target", name: "原分发人员", orgName: "支行" }],
    targetsLoaded: true,
    targetBranch: null,
    branchTargetSapIds: ["old-target"],
    dialog: null,
    acting: false,
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderWorkbench() {
  return render(
    <MemoryRouter initialEntries={["/wealth/create"]}>
      <Sidebar collapsed={false} onToggleCollapse={vi.fn()} />
      <Routes>
        <Route path="/wealth/create" element={<Create />} />
        <Route path="/wealth/board" element={<div>看板</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

function expectEmptyForm() {
  expect(screen.getByLabelText("规划名称")).toHaveValue("");
  expect(useWealthStore.getState()).toMatchObject({
    draft: { name: "", items: [] },
    editingId: null,
    savedAt: "",
    targetSapIds: [],
    targets: [],
    targetsLoaded: false,
    targetBranch: null,
    branchTargetSapIds: [],
  });
}

it("修改页保留回填，点击创建计划即使路由不变也清空表单与分发目标", () => {
  renderWorkbench();
  expect(screen.getByLabelText("规划名称")).toHaveValue("旧规划");
  expect(screen.getByRole("heading", { name: "修改工作规划" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "创建计划" }));
  expectEmptyForm();
  expect(screen.getByRole("heading", { name: "新建工作规划" })).toBeVisible();
});

it("取消修改后再次创建进入空白表单", () => {
  renderWorkbench();
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(useWealthStore.getState().draft).toEqual({ name: "", items: [] });
  fireEvent.click(screen.getByRole("button", { name: "创建计划" }));
  expectEmptyForm();
});

it("离开未发布的新建表单后草稿也清空", () => {
  useWealthStore.setState({ editingId: null });
  renderWorkbench();
  fireEvent.click(screen.getByRole("button", { name: "规划看板" }));
  expect(useWealthStore.getState().draft).toEqual({ name: "", items: [] });
});

it("行长在修改页直接新建后重新加载人员并清空搜索条件", async () => {
  useWealthStore.setState({ accountId: "president" });
  renderWorkbench();
  fireEvent.change(screen.getByLabelText("搜索分发目标"), {
    target: { value: "原分发人员" },
  });
  vi.mocked(fetchDistributeTargets).mockClear();
  fireEvent.click(screen.getByRole("button", { name: "创建计划" }));
  await waitFor(() => expect(fetchDistributeTargets).toHaveBeenCalledOnce());
  expect(screen.getByLabelText("搜索分发目标")).toHaveValue("");
  await screen.findByText("可分发人员");
  expect(useWealthStore.getState().targetSapIds).toEqual([]);
  expect(useWealthStore.getState().targetsLoaded).toBe(true);
});

it("中台新建清除旧机构视图和人员缓存，但复用已预加载的完整机构树", async () => {
  useWealthStore.setState({ accountId: "middle", plansLoaded: true });
  renderWorkbench();
  fireEvent.click(await screen.findByRole("button", { name: "测试支行" }));
  await waitFor(() =>
    expect(useWealthStore.getState().targetsLoaded).toBe(true),
  );
  expect(screen.getByLabelText("搜索分发目标")).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "创建计划" }));
  expect(queryInstitutionTree).toHaveBeenCalledOnce();
  expect(screen.getByLabelText("搜索分发目标")).toBeDisabled();
  expect(useWealthStore.getState().targetSapIds).toEqual([]);
});

it("非白名单只显示现有选人方式", () => {
  vi.stubGlobal("__env__", { wealthExcelDistributionUsers: ["someone-else"] });
  useWealthStore.setState({ accountId: "president", plansLoaded: true });
  renderWorkbench();
  expect(screen.queryByRole("radio", { name: "上传 Excel 名单" })).toBeNull();
  expect(screen.getByRole("textbox", { name: "搜索分发目标" })).toBeVisible();
  expect(importExcelPeople).not.toHaveBeenCalled();
});

it("白名单默认 Excel，切换保留手选，发布只提交剔除后的 Excel 名单", async () => {
  vi.stubGlobal("__env__", { wealthExcelDistributionUsers: ["publisher"] });
  useWealthStore.setState({
    accountId: "president",
    editingId: null,
    plansLoaded: true,
  });
  const recipients = [
    {
      sapId: "000123",
      name: "外分行客户经理",
      orgName: "外分行支行",
      bbkId: "120",
      bbkOrgId: "12001",
    },
  ];
  const removed = {
    sapId: "000456",
    name: "待移除人员",
    orgName: "另一机构",
    bbkId: "110",
    bbkOrgId: "11001",
  };
  vi.mocked(importExcelPeople).mockResolvedValue({
    people: [...recipients, removed],
    errors: [],
    duplicates: 0,
  });
  const publish = vi.spyOn(api, "publishPlan").mockResolvedValue({
    created: true,
    plans: [],
    planId: "created",
  });
  try {
    renderWorkbench();
    const excel = screen.getByRole("radio", { name: "上传 Excel 名单" });
    expect(excel).toBeChecked();
    expect(screen.queryByRole("textbox", { name: "搜索分发目标" })).toBeNull();
    fireEvent.change(screen.getByLabelText("选择 Excel 名单文件"), {
      target: { files: [new File(["fixture"], "名单.xlsx")] },
    });
    await screen.findByText("已选 2 人");
    expect(screen.getByText("外分行客户经理")).toBeVisible();
    expect(screen.getByText("待移除人员")).toBeVisible();
    fireEvent.change(screen.getByRole("combobox", { name: "筛选分行" }), {
      target: { value: "110" },
    });
    expect(screen.queryByText("外分行客户经理")).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: "移除 待移除人员 000456" }),
    );
    expect(screen.getByText("已选 1 人")).toBeVisible();
    expect(screen.queryByText("待移除人员")).toBeNull();
    fireEvent.click(screen.getByRole("radio", { name: "按机构选人" }));
    expect(screen.getAllByText("原分发人员")[0]).toBeVisible();
    fireEvent.click(excel);
    fireEvent.click(screen.getAllByRole("button", { name: "发布规划" })[0]);
    const confirm = useWealthStore
      .getState()
      .dialog?.buttons.find((button) => button.label === "确认发布");
    expect(confirm).toBeDefined();
    confirm?.onClick?.();
    await waitFor(() =>
      expect(publish).toHaveBeenCalledWith(
        expect.anything(),
        expect.anything(),
        null,
        recipients,
      ),
    );
  } finally {
    publish.mockRestore();
  }
});

it("非白名单无法通过发布入口提交 Excel 目标", async () => {
  useWealthStore.setState({ accountId: "president" });
  const state = useWealthStore.getState();
  const publish = vi.spyOn(api, "publishPlan");
  try {
    await expect(
      state.publishPlan({
        userId: "publisher",
        contextVersion: state.contextVersion,
        planFormVersion: state.planFormVersion,
        targets: [{ sapId: "000123", name: "外分行人员", orgName: "外地支行" }],
      }),
    ).resolves.toBe(false);
    expect(publish).not.toHaveBeenCalled();
  } finally {
    publish.mockRestore();
  }
});

it("修改规划回显当前名单，追加跳过已有人员并在确认后保存完整名单", async () => {
  vi.stubGlobal("__env__", { wealthExcelDistributionUsers: ["publisher"] });
  useWealthStore.setState({ accountId: "president", plansLoaded: true });
  const existing = useWealthStore.getState().targets[0];
  const added = {
    sapId: "000888",
    name: "新增人员",
    orgName: "外地支行",
    bbkId: "120",
    bbkOrgId: "12001",
  };
  vi.mocked(importExcelPeople).mockResolvedValue({
    people: [existing, added],
    errors: [],
    duplicates: 0,
  });
  const publish = vi.spyOn(api, "publishPlan").mockResolvedValue({
    created: false,
    plans: [],
    planId: "old-plan",
  });
  try {
    renderWorkbench();
    expect(screen.getByRole("radio", { name: "Excel 名单管理" })).toBeChecked();
    expect(screen.getByText(existing.name)).toBeVisible();
    fireEvent.click(
      screen.getByRole("button", { name: "上传 Excel 追加人员" }),
    );
    fireEvent.change(screen.getByLabelText("选择 Excel 名单文件"), {
      target: { files: [new File(["fixture"], "追加名单.xlsx")] },
    });
    await screen.findByText(/新增 1 人 · 1 人已在名单中 · 确认后共 2 人/);
    expect(importExcelPeople).toHaveBeenCalledWith(
      expect.objectContaining({ name: "追加名单.xlsx" }),
      expect.any(AbortSignal),
    );
    expect(
      screen.getAllByRole("button", { name: "保存修改" })[0],
    ).toBeDisabled();
    expect(screen.getByText("已选 1 人")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "确认追加" }));
    expect(screen.getByText("已选 2 人")).toBeVisible();
    fireEvent.click(screen.getAllByRole("button", { name: "保存修改" })[0]);
    useWealthStore
      .getState()
      .dialog?.buttons.find((button) => button.label === "确认修改")
      ?.onClick?.();
    await waitFor(() =>
      expect(publish).toHaveBeenCalledWith(
        expect.anything(),
        expect.anything(),
        "old-plan",
        [existing, added],
      ),
    );
  } finally {
    publish.mockRestore();
  }
});

it("修改规划可取消替换，确认替换后只保留新文件名单", async () => {
  vi.stubGlobal("__env__", { wealthExcelDistributionUsers: ["publisher"] });
  useWealthStore.setState({ accountId: "president", plansLoaded: true });
  vi.mocked(importExcelPeople).mockResolvedValue({
    people: [
      {
        sapId: "000999",
        name: "替换人员",
        orgName: "外地支行",
        bbkId: "120",
        bbkOrgId: "12001",
      },
    ],
    errors: [],
    duplicates: 0,
  });
  renderWorkbench();
  const replace = async () => {
    fireEvent.click(
      screen.getByRole("button", { name: "上传 Excel 替换名单" }),
    );
    fireEvent.change(screen.getByLabelText("选择 Excel 名单文件"), {
      target: { files: [new File(["fixture"], "替换名单.xlsx")] },
    });
    await screen.findByText(/新增 1 人 · 移除 1 人 · 确认后共 1 人/);
  };
  await replace();
  fireEvent.click(screen.getByRole("button", { name: "取消导入" }));
  expect(screen.getByText("原分发人员")).toBeVisible();
  await replace();
  fireEvent.click(screen.getByRole("button", { name: "确认替换" }));
  expect(screen.getByText("替换人员")).toBeVisible();
  expect(screen.queryByText("原分发人员")).toBeNull();
});

it("人员名单默认跨分行展示，每页 24 人，搜索和筛选会回到第一页", () => {
  const people = Array.from({ length: 25 }, (_, index) => ({
    sapId: String(index).padStart(5, "0"),
    name: `人员 ${index + 1}`,
    orgName: index % 2 ? "城西支行" : "城东支行",
    bbkId: index % 2 ? "120" : "110",
    branchName: index % 2 ? "上海分行" : "北京分行",
  }));
  render(<ExcelRosterSummary people={people} />);
  expect(screen.getAllByRole("listitem")).toHaveLength(24);
  expect(screen.getByText("人员 1")).toBeVisible();
  expect(screen.getByText("人员 2")).toBeVisible();
  expect(screen.getByText("1 / 2 页")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  expect(screen.getByText("人员 25")).toBeVisible();
  expect(screen.getByText("2 / 2 页")).toBeVisible();
  fireEvent.change(screen.getByRole("searchbox", { name: "搜索已选人员" }), {
    target: { value: "上海分行" },
  });
  expect(screen.getAllByRole("listitem")).toHaveLength(12);
  expect(screen.getByText("人员 2")).toBeVisible();
  expect(screen.queryByRole("button", { name: "下一页" })).toBeNull();
});

it("机构缺少名称时显示编码，有名称时也能按编码搜索", () => {
  render(
    <ExcelRosterSummary
      people={[
        {
          sapId: "00001",
          name: "历史收件人",
          orgName: "",
          bbkId: "110",
          bbkOrgId: "110001",
        },
        {
          sapId: "00002",
          name: "新收件人",
          orgName: "朝阳支行",
          bbkId: "110",
          bbkOrgId: "110002",
        },
      ]}
    />,
  );
  expect(screen.getByText("00001 · 北京分行 · 110001")).toBeVisible();
  expect(screen.getByText("00002 · 北京分行 · 朝阳支行")).toBeVisible();
  const search = screen.getByRole("searchbox", { name: "搜索已选人员" });
  fireEvent.change(search, { target: { value: "110001" } });
  expect(screen.getByText("历史收件人")).toBeVisible();
  expect(screen.queryByText("新收件人")).toBeNull();
  fireEvent.change(search, { target: { value: "110002" } });
  expect(screen.getByText("新收件人")).toBeVisible();
  expect(screen.queryByText("历史收件人")).toBeNull();
});

it("分行名称优先取同分行人员提供的名称，历史人员也能按名称搜索", () => {
  render(
    <ExcelRosterSummary
      people={[
        {
          sapId: "00001",
          name: "历史收件人",
          orgName: "",
          bbkId: "120",
          bbkOrgId: "120001",
        },
        {
          sapId: "00002",
          name: "新增收件人",
          orgName: "浦东支行",
          bbkId: "120",
          branchName: "上海分行",
        },
        {
          sapId: "00003",
          name: "其他分行收件人",
          orgName: "朝阳支行",
          bbkId: "110",
        },
      ]}
    />,
  );
  expect(screen.getByText("00001 · 上海分行 · 120001")).toBeVisible();
  expect(
    screen.getByRole("option", { name: "上海分行（2 人）" }),
  ).toBeInTheDocument();
  fireEvent.change(screen.getByRole("searchbox", { name: "搜索已选人员" }), {
    target: { value: "上海分行" },
  });
  expect(screen.getByText("历史收件人")).toBeVisible();
  expect(screen.getByText("新增收件人")).toBeVisible();
  expect(screen.queryByText("其他分行收件人")).toBeNull();
});

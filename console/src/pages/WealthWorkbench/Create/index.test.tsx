import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { useIframeStore } from "../../../stores/iframeStore";
import styles from "../index.module.less";
import { useWealthStore } from "../store";
import type { Plan, PlanItem, Scene } from "../types";
import Create, {
  SceneDescription,
  ScheduleEditor,
  StepIndicator,
  TaskSchedule,
} from "./index";

vi.mock("../../../api/request", () => ({
  request: vi.fn().mockResolvedValue({ items: [] }),
}));

describe("SceneDescription", () => {
  it("用两行截断样式展示描述，并保留完整文本供悬停查看", () => {
    const description =
      "这是一段超过两行时仍可通过悬停查看全文的经营场景技能描述";

    render(<SceneDescription description={description} />);

    const element = screen.getByText(description);
    expect(element).toHaveClass(styles.sceneDescription);
    expect(element).toHaveAttribute("title", description);
  });
});

describe("StepIndicator", () => {
  it("区分当前、已完成和未开始步骤的图标状态", () => {
    const { rerender } = render(
      <StepIndicator active completed={false} number={1} />,
    );
    expect(screen.getByText("1")).toHaveClass(styles.stepActive);
    expect(screen.getByText("1")).not.toHaveClass(styles.stepComplete);

    rerender(<StepIndicator active={false} completed number={1} />);
    expect(screen.getByText("✓")).toHaveClass(styles.stepComplete);
    expect(screen.getByText("✓")).not.toHaveClass(styles.stepActive);

    rerender(<StepIndicator active={false} completed={false} number={2} />);
    expect(screen.getByText("2")).not.toHaveClass(styles.stepActive);
    expect(screen.getByText("2")).not.toHaveClass(styles.stepComplete);
  });
});

describe("ScheduleEditor", () => {
  it("保留原执行频率按钮，只在自定义区展示可读规则", () => {
    const item: PlanItem = {
      id: "scene-1",
      sceneName: "保障潜客经营",
      categoryLabel: "保险",
      categoryCode: "insurance",
      mcpRelations: [],
      direction: "优先触达高潜客户",
      cycle: "本月",
      start: "2026-09-01",
      end: "2026-09-30",
      schedule: { type: "custom", rawCron: "*/15 * * * *" },
    };

    render(<ScheduleEditor item={item} sceneName={item.sceneName} />);

    expect(screen.getByRole("button", { name: "每小时" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "每日" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "每周" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "自定义" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    const repeatRule = screen.getByLabelText("保障潜客经营自定义重复规则");
    expect(repeatRule).toHaveValue("frequency");
    expect(repeatRule).toHaveTextContent("每日");
    expect(repeatRule).toHaveTextContent("每周");
    expect(repeatRule).toHaveTextContent("每月");
    expect(repeatRule).toHaveTextContent("每年");
    expect(repeatRule).toHaveTextContent("自定义频率");
    expect(screen.getByLabelText("保障潜客经营自定义频率单位")).toHaveValue(
      "minutes",
    );
    expect(screen.getByText("每隔 15 分钟")).toBeInTheDocument();
    expect(screen.queryByLabelText(/cron/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/cron/i)).not.toBeInTheDocument();
  });
});

describe("TaskSchedule", () => {
  it("点击自定义周期的日期数字区域会打开统一样式的日期面板", () => {
    const item: PlanItem = {
      id: "scene-1",
      sceneName: "保障潜客经营",
      categoryLabel: "保险",
      categoryCode: "insurance",
      mcpRelations: [],
      direction: "优先触达高潜客户",
      cycle: "自定义",
      start: "2026-09-01",
      end: "2026-09-30",
      schedule: { type: "daily", hour: 9, minute: 0 },
    };
    const scene: Scene = {
      id: "scene-1",
      name: "保障潜客经营",
      category: "保险",
      categoryCode: "insurance",
      icon: "shield",
      desc: "挖掘高潜保险客户",
      source: "总部预置",
      mcpRelations: [],
    };

    const { container } = render(<TaskSchedule item={item} scene={scene} />);
    const startDate = screen.getByLabelText("保障潜客经营任务开始日期");

    expect(container.querySelector('input[type="date"]')).toBeNull();
    fireEvent.click(startDate);
    expect(document.querySelector(".ant-picker-dropdown")).toBeInTheDocument();
  });
});

describe("Create", () => {
  it("客户经理选择已被规划占用的场景时列出冲突并禁止发布", () => {
    const item: PlanItem = {
      id: "scene-1",
      sceneName: "保障潜客经营",
      categoryLabel: "保险",
      categoryCode: "insurance",
      mcpRelations: [],
      direction: "优先触达高潜客户",
      cycle: "本月",
      start: "2026-09-01",
      end: "2026-09-30",
      schedule: { type: "daily", hour: 9, minute: 0 },
    };
    const occupiedPlan: Plan = {
      id: "plan-president",
      name: "行长重点经营规划",
      source: "行长关注",
      desc: "",
      customers: 0,
      tasks: 0,
      rate: 0,
      status: "已自动下发",
      publishStatus: "published",
      period: "本月",
      start: "2026-09-01",
      end: "2026-09-30",
      items: [item],
      editable: false,
      targetSapIds: ["rm-1"],
    };
    const otherRmPlan: Plan = {
      ...occupiedPlan,
      id: "plan-other-rm",
      name: "其他客户经理规划",
      source: "我的关注",
      editable: false,
    };
    useIframeStore.setState({ positionId: "RB0101", userId: "rm-1" });
    useWealthStore.setState({
      accountId: "rm",
      plansLoaded: true,
      draft: { name: "客户经理规划", items: [item] },
      plans: [otherRmPlan, occupiedPlan],
      scenesByCategory: { "": [] },
      scenesLoading: false,
      editingId: null,
      dialog: null,
      acting: false,
    });

    render(
      <MemoryRouter>
        <Create />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getAllByRole("button", { name: "发布规划" })[0]);

    const dialog = useWealthStore.getState().dialog;
    expect(dialog?.buttons.find((button) => button.primary)?.disabled).toBe(
      true,
    );
    render(<>{dialog?.body}</>);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveClass(styles.sceneConflictAlert);
    expect(alert).toHaveTextContent("保障潜客经营");
    expect(alert).toHaveTextContent("行长重点经营规划");
    expect(screen.queryByText(/其他客户经理规划/)).not.toBeInTheDocument();
  });

  it("编辑自身已有规划时排除当前规划，修改执行周期后仍可确认", () => {
    const item: PlanItem = {
      id: "scene-1",
      sceneName: "保障潜客经营",
      categoryLabel: "保险",
      categoryCode: "insurance",
      mcpRelations: [],
      direction: "优先触达高潜客户",
      cycle: "本月",
      start: "2026-09-01",
      end: "2026-09-30",
      schedule: { type: "daily", hour: 10, minute: 0 },
    };
    const ownPlan: Plan = {
      id: "plan-self",
      name: "客户经理已有规划",
      source: "我的关注",
      desc: "",
      customers: 0,
      tasks: 0,
      rate: 0,
      status: "已自动下发",
      publishStatus: "published",
      period: "本月",
      start: "2026-09-01",
      end: "2026-09-30",
      items: [item],
      editable: true,
      targetSapIds: ["rm-1"],
    };
    const managementPlan: Plan = {
      ...ownPlan,
      id: "plan-president",
      name: "行长历史规划",
      source: "行长关注",
      editable: false,
    };
    useIframeStore.setState({ positionId: "RB0101", userId: "rm-1" });
    useWealthStore.setState({
      accountId: "rm",
      plansLoaded: true,
      draft: { name: ownPlan.name, items: [item] },
      plans: [ownPlan, managementPlan],
      scenesByCategory: { "": [] },
      scenesLoading: false,
      editingId: ownPlan.id,
      dialog: null,
      acting: false,
    });

    render(
      <MemoryRouter>
        <Create />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getAllByRole("button", { name: "保存修改" })[0]);

    const dialog = useWealthStore.getState().dialog;
    expect(dialog?.buttons.find((button) => button.primary)?.disabled).toBe(
      false,
    );
    const confirmation = render(<>{dialog?.body}</>);
    expect(within(confirmation.container).queryByRole("alert")).toBeNull();
  });

  it("规划数据未就绪时禁用发布，避免跳过已有规划冲突检查", () => {
    useIframeStore.setState({ positionId: "RB0101", userId: "rm-1" });
    useWealthStore.setState({
      accountId: "rm",
      plansLoaded: false,
      draft: { name: "客户经理规划", items: [] },
      plans: [],
      scenesByCategory: { "": [] },
      scenesLoading: false,
      editingId: null,
      dialog: null,
      acting: false,
    });

    const { container } = render(
      <MemoryRouter>
        <Create />
      </MemoryRouter>,
    );

    const publishButtons = within(container).getAllByRole("button", {
      name: "规划加载中…",
    });
    expect(publishButtons).toHaveLength(2);
    publishButtons.forEach((button) => expect(button).toBeDisabled());
    expect(useWealthStore.getState().dialog).toBeNull();
  });

  it("规划数据未就绪时不允许编辑表单，但仍可取消返回", () => {
    useIframeStore.setState({ positionId: "RB0101", userId: "rm-1" });
    useWealthStore.setState({
      accountId: "rm",
      plansLoaded: false,
      draft: { name: "", items: [] },
      plans: [],
      scenesByCategory: { "": [] },
      scenesLoading: false,
      editingId: null,
      dialog: null,
      acting: false,
    });

    const { container } = render(
      <MemoryRouter>
        <Create />
      </MemoryRouter>,
    );

    expect(
      within(container).getByRole("button", { name: "全部" }),
    ).toBeDisabled();
    expect(screen.getByLabelText("规划名称")).toBeDisabled();
    expect(
      within(container).getByRole("button", { name: "上一步" }),
    ).toBeDisabled();
    expect(
      within(container).getByRole("button", { name: "取消" }),
    ).toBeEnabled();
  });
});

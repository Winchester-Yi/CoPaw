/**
 * 智能财富工作台 —— store 行为测试
 * request 层用内存夹具 mock（规划接口已不做假数据回退）；
 * 场景接口按 category 返回 SceneSkillItem 夹具，账户/草稿走真实实现。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { resetMockDb } from "./api";
import { request } from "../../api/request";
import { useIframeStore } from "../../stores/iframeStore";
import {
  useWealthStore,
  selectCurrentAccount,
  validateDraft,
  planScheduledOn,
} from "./store";
import { cycleRange, DEFAULT_SCHEDULE, todayKey } from "./utils";
import type { Plan, PlanItem } from "./types";

vi.mock("../../api/request", () => ({ request: vi.fn() }));

const mockRequest = vi.mocked(request);

const INSURANCE = "skill-wealth-insurance-1";
const INSURANCE_2 = "skill-wealth-insurance-2";
const FINANCE = "skill-wealth-finance-3";
const LOAN = "skill-wealth-loan-4";

/** 客户名单夹具：仅 LOAN 技能返回两个客户，其余技能为空 */
const NAME_LIST_FIXTURE = [
  {
    custUid: "CUST001",
    custNm: "张三",
    sapId: "10086",
    filename: "http://example/cust001",
    recomReason: "命中贷款核验规则",
  },
  {
    custUid: "CUST002",
    custNm: "李四",
    sapId: "10086",
    filename: "http://example/cust002",
    recomReason: "",
  },
];

/** 后端 /wealth/plans 的返回形状（PlanView 夹具） */
interface FixturePlanView {
  id: string;
  name: string;
  description?: string | null;
  source_label?: string | null;
  period_start?: string | null;
  period_end?: string | null;
  publish_status: string;
  board_status: string;
  editable: boolean;
  scenes: {
    scene_id: string;
    scene_name: string;
    category: string;
    category_label: string;
    item_id?: string | null;
    direction?: string | null;
    cycle?: string | null;
    start_date?: string | null;
    end_date?: string | null;
    cron_expr: string;
    mcp_relations: string[];
  }[];
  targets: { sap_id: string; name?: string | null }[];
}

let planViews: FixturePlanView[];

function makeView(patch: Partial<FixturePlanView> = {}): FixturePlanView {
  return {
    id: "plan-x",
    name: "规划",
    source_label: "分行关注",
    period_start: "2026-09-01",
    period_end: "2026-09-30",
    publish_status: "published",
    board_status: "已自动下发",
    editable: true,
    scenes: [],
    targets: [],
    ...patch,
  };
}

function fixturePlanViews(): FixturePlanView[] {
  return [
    makeView({ id: "plan-demo-1", name: "保险重点经营" }),
    makeView({
      id: "plan-demo-2",
      name: "产品到期承接",
      source_label: "行长关注",
      scenes: [
        {
          scene_id: FINANCE,
          scene_name: "产品到期承接",
          category: "finance",
          category_label: "理财",
          item_id: "item-wealth-finance-3",
          direction: "优先承接近期到期资金",
          cycle: "本月",
          start_date: "2026-09-01",
          end_date: "2026-09-30",
          cron_expr: "0 9 * * *",
          mcp_relations: ["mcp-wealth"],
        },
      ],
    }),
    makeView({ id: "plan-demo-3", name: "代发客户经营" }),
    makeView({ id: "plan-demo-4", name: "高价值动账" }),
    makeView({ id: "plan-demo-5", name: "理财重点客户经营" }),
    makeView({
      id: "plan-demo-6",
      name: "基金定投提升",
      publish_status: "publishing",
      board_status: "发布中",
    }),
  ];
}

/** 场景接口夹具：SceneSkillItem 形状，按 category 过滤，空串返回全部 */
const SCENE_FIXTURES = [
  {
    skillId: INSURANCE,
    itemId: "item-wealth-insurance-1",
    senceName: "保障潜客经营",
    category: "insurance",
    senceDesc: "挖掘高潜保险客户",
    mcpRelationList: ["mcp-customer", "mcp-insurance"],
    skillBbkLabel: "总部预置",
  },
  {
    skillId: INSURANCE_2,
    itemId: "item-wealth-insurance-2",
    senceName: "保障缺口经营",
    category: "insurance",
    senceDesc: "识别客户保障缺口",
    mcpRelationList: ["mcp-customer", "mcp-insurance"],
    skillBbkLabel: "总部预置",
  },
  {
    skillId: FINANCE,
    itemId: "item-wealth-finance-3",
    senceName: "产品到期承接",
    category: "finance",
    senceDesc: "优先承接近期到期资金",
    mcpRelationList: ["mcp-wealth"],
    skillBbkLabel: "总部预置",
  },
  {
    skillId: LOAN,
    itemId: "item-wealth-loan-4",
    senceName: "信贷需求挖掘",
    category: "loan",
    senceDesc: "识别潜在信贷客户",
    mcpRelationList: ["mcp-loan"],
    skillBbkLabel: "总部预置",
  },
];

/** request 夹具：规划 CRUD 走内存表；场景接口按 category 参数返回夹具 */
async function requestHandler(
  path: string,
  options: RequestInit = {},
): Promise<unknown> {
  const method = options.method ?? "GET";
  if (path.startsWith("/wealth/scene-skills")) {
    const category =
      new URL(path, "http://test").searchParams.get("category") ?? "";
    return {
      items: category
        ? SCENE_FIXTURES.filter((s) => s.category === category)
        : SCENE_FIXTURES,
    };
  }
  if (path.startsWith("/wealth/name-list")) {
    const skillId =
      new URL(path, "http://test").searchParams.get("skill_id") ?? "";
    return { items: skillId === LOAN ? NAME_LIST_FIXTURE : [] };
  }
  if (path === "/wealth/plans" && method === "GET") {
    return { items: planViews };
  }
  if (path === "/wealth/plans" && method === "POST") {
    const body = JSON.parse(String(options.body));
    const view = makeView({
      id: `plan-new-${planViews.length}`,
      name: body.name,
      source_label: body.source_label,
      publish_status: "publishing",
      board_status: "发布中",
      targets: body.targets,
    });
    planViews = [view, ...planViews];
    return { id: view.id, publish_status: view.publish_status };
  }
  const match = /^\/wealth\/plans\/([^/]+)$/.exec(path);
  if (match && method === "DELETE") {
    const id = decodeURIComponent(match[1]);
    planViews = planViews.filter((p) => p.id !== id);
    return { deleted: true };
  }
  throw new Error(`未 mock 的请求：${method} ${path}`);
}

/** 构造通过校验的草稿条目 */
function makeItem(patch: Partial<PlanItem> = {}): PlanItem {
  return {
    id: INSURANCE,
    sceneName: "保障潜客经营",
    categoryLabel: "保险",
    categoryCode: "insurance",
    mcpRelations: [],
    direction: "d",
    cycle: "本月",
    start: "2026-09-01",
    end: "2026-09-30",
    schedule: { ...DEFAULT_SCHEDULE },
    ...patch,
  };
}

/** 构造一个「今天有排程」的已发布规划（日期按运行当天动态计算，保证测试与日期无关） */
function makeTodayPlan(): Plan {
  const today = todayKey();
  return {
    id: "plan-today",
    name: "今日规划",
    source: "我的关注",
    desc: "",
    customers: 0,
    tasks: 0,
    rate: 0,
    status: "已自动下发",
    publishStatus: "published",
    period: "自定义",
    start: today,
    end: today,
    items: [
      makeItem({
        id: LOAN,
        sceneName: "信贷需求挖掘",
        categoryLabel: "贷款",
        categoryCode: "loan",
        start: today,
        end: today,
      }),
    ],
  };
}

async function initStore() {
  resetMockDb();
  planViews = fixturePlanViews();
  mockRequest.mockImplementation(requestHandler as never);
  // 显式声明测试身份为客户经理（RB0101），不依赖 FALLBACK_ROLE 兜底
  useIframeStore.setState({ positionId: "RB0101", userId: null });
  useWealthStore.setState({
    initialized: false,
    accountId: "rm",
    scenesByCategory: {},
    scenesLoading: false,
    plans: [],
    customers: [],
    customersLoading: false,
    history: [],
    draft: { name: "", items: [] },
    savedAt: "",
    editingId: null,
    targets: [],
    targetsLoaded: false,
    targetSapIds: [],
    dialog: null,
    toastText: "",
    toastSeq: 0,
  });
  await useWealthStore.getState().init();
}

/** 直接塞入一份可发布的草稿（替代已移除的预填 mock 草稿） */
function seedDraft() {
  useWealthStore.setState({
    draft: {
      name: "九月规划",
      items: [
        makeItem(),
        makeItem({
          id: FINANCE,
          sceneName: "产品到期承接",
          categoryLabel: "理财",
          categoryCode: "finance",
        }),
      ],
    },
  });
}

describe("WealthWorkbench store", () => {
  beforeEach(async () => {
    await initStore();
  });

  it("init 加载工作台数据并推导真实身份账户；场景不进系统时预加载", () => {
    const s = useWealthStore.getState();
    // initStore 声明岗位 RB0101 → 客户经理，账户信息来自真实身份推导而非 mock 列表
    expect(selectCurrentAccount(s).role).toBe("客户经理");
    expect(selectCurrentAccount(s).source).toBe("我的关注");
    expect(s.scenesByCategory).toEqual({});
    expect(s.plans).toHaveLength(6);
    expect(s.draft).toEqual({ name: "", items: [] });
  });

  it("loadScenes 按大类查询且每次都取最新；toggleScene 选择/移除场景", async () => {
    const sceneCalls = () =>
      mockRequest.mock.calls.filter(([p]) =>
        String(p).startsWith("/wealth/scene-skills"),
      ).length;
    await useWealthStore.getState().loadScenes("");
    await useWealthStore.getState().loadScenes(""); // 不做缓存，每次都重新查询
    expect(sceneCalls()).toBe(2);
    expect(
      useWealthStore.getState().scenesByCategory[""]?.map((s) => s.id),
    ).toEqual([INSURANCE, INSURANCE_2, FINANCE, LOAN]);

    const { toggleScene } = useWealthStore.getState();
    const ids = () => useWealthStore.getState().draft.items.map((x) => x.id);
    toggleScene(INSURANCE);
    expect(ids()).toEqual([INSURANCE]);
    toggleScene(INSURANCE_2);
    expect(ids()).toEqual([INSURANCE, INSURANCE_2]);
    toggleScene("skill-not-in-pool"); // 场景池外的 id 为无效操作
    expect(ids()).toEqual([INSURANCE, INSURANCE_2]);
    toggleScene(INSURANCE_2);
    expect(ids()).toEqual([INSURANCE]);
  });

  it("moveScene / dropScene 调整顺序", () => {
    seedDraft();
    const { moveScene, dropScene } = useWealthStore.getState();
    const ids = () => useWealthStore.getState().draft.items.map((x) => x.id);
    moveScene(0, 1);
    expect(ids()).toEqual([FINANCE, INSURANCE]);
    dropScene(FINANCE, INSURANCE);
    expect(ids()).toEqual([INSURANCE, FINANCE]);
  });

  it("setTaskCycle 预设周期自动带出起止日期", () => {
    useWealthStore.setState({ draft: { name: "x", items: [makeItem()] } });
    const { setTaskCycle } = useWealthStore.getState();
    setTaskCycle(INSURANCE, "本季");
    const item = useWealthStore
      .getState()
      .draft.items.find((x) => x.id === INSURANCE);
    const [start, end] = cycleRange("本季") ?? [];
    expect(item?.cycle).toBe("本季");
    expect(item?.start).toBe(start);
    expect(item?.end).toBe(end);
  });

  it("updateSchedule 修改场景排程", () => {
    useWealthStore.setState({ draft: { name: "x", items: [makeItem()] } });
    const { updateSchedule } = useWealthStore.getState();
    updateSchedule(INSURANCE, {
      type: "weekly",
      hour: 8,
      minute: 30,
      daysOfWeek: ["mon", "thu"],
    });
    const item = useWealthStore
      .getState()
      .draft.items.find((x) => x.id === INSURANCE);
    expect(item?.schedule.type).toBe("weekly");
    expect(item?.schedule.daysOfWeek).toEqual(["mon", "thu"]);
  });

  it("saveDraft 记录保存时间并弹出提示", async () => {
    await useWealthStore.getState().saveDraft();
    const s = useWealthStore.getState();
    expect(s.savedAt).toMatch(/^\d{2}:\d{2}$/);
    expect(s.toastText).toContain("草稿已保存");
  });

  it("validateDraft 校验空场景、空名称与非法日期", () => {
    expect(validateDraft({ name: "x", items: [] })).toBe(
      "请至少选择一个经营场景",
    );
    expect(validateDraft({ name: "  ", items: [makeItem()] })).toBe(
      "请填写规划名称",
    );
    expect(
      validateDraft({
        name: "x",
        items: [
          makeItem({
            cycle: "自定义",
            start: "2026-09-30",
            end: "2026-09-01",
          }),
        ],
      }),
    ).toContain("请设置");
  });

  it("validateDraft 校验每周执行日与自定义 cron 表达式", () => {
    expect(
      validateDraft({
        name: "x",
        items: [makeItem({ schedule: { type: "weekly", hour: 9, minute: 0 } })],
      }),
    ).toBe("请选择「保障潜客经营」的每周执行日");
    expect(
      validateDraft({
        name: "x",
        items: [makeItem({ schedule: { type: "custom", rawCron: "abc" } })],
      }),
    ).toContain("cron");
  });

  it("publishPlan 校验失败时不发请求并提示", async () => {
    useWealthStore.setState({ draft: { name: "x", items: [] } });
    const posts = () =>
      mockRequest.mock.calls.filter(
        ([, o]) => (o as RequestInit | undefined)?.method === "POST",
      ).length;
    const before = posts();
    const ok = await useWealthStore.getState().publishPlan();
    expect(ok).toBe(false);
    expect(posts()).toBe(before);
    expect(useWealthStore.getState().plans).toHaveLength(6);
    expect(useWealthStore.getState().toastText).toBe("请至少选择一个经营场景");
  });

  it("publishPlan 成功后更新列表并清除编辑态", async () => {
    seedDraft();
    const ok = await useWealthStore.getState().publishPlan();
    expect(ok).toBe(true);
    const s = useWealthStore.getState();
    expect(s.plans).toHaveLength(7);
    expect(s.editingId).toBeNull();
    expect(s.toastText).toContain("规划已发布");
  });

  it("publishPlan 客户经理默认分发给自己", async () => {
    seedDraft();
    await useWealthStore.getState().publishPlan();
    expect(useWealthStore.getState().plans[0]?.targetSapIds).toEqual(["rm"]);
  });

  it("publishPlan 行长未选分发目标时校验失败", async () => {
    useWealthStore.setState({ accountId: "president" });
    seedDraft();
    const ok = await useWealthStore.getState().publishPlan();
    expect(ok).toBe(false);
    expect(useWealthStore.getState().plans).toHaveLength(6);
    expect(useWealthStore.getState().toastText).toBe("请至少选择一个分发目标");
  });

  it("publishPlan 行长选择分发目标后发布成功并记录 sapIds", async () => {
    useWealthStore.setState({ accountId: "president" });
    seedDraft();
    useWealthStore.getState().toggleTarget("zhangwl");
    useWealthStore.getState().toggleTarget("chenjy");
    const ok = await useWealthStore.getState().publishPlan();
    expect(ok).toBe(true);
    const s = useWealthStore.getState();
    expect(s.plans[0]?.targetSapIds).toEqual(["zhangwl", "chenjy"]);
    expect(s.targetSapIds).toEqual([]);
  });

  it("toggleTarget 选中/取消分发目标", () => {
    useWealthStore.getState().toggleTarget("zhangwl");
    expect(useWealthStore.getState().targetSapIds).toEqual(["zhangwl"]);
    useWealthStore.getState().toggleTarget("zhangwl");
    expect(useWealthStore.getState().targetSapIds).toEqual([]);
  });

  it("loadTodayCustomers 客户视角传 sapId 拉取今日名单，经营视角不传", async () => {
    useIframeStore.setState({ userId: "10086" });
    useWealthStore.setState({ plans: [makeTodayPlan()] });
    const nameListCalls = () =>
      mockRequest.mock.calls
        .filter(([p]) => String(p).startsWith("/wealth/name-list"))
        .map(([p]) => String(p));

    await useWealthStore.getState().loadTodayCustomers("customer");
    expect(nameListCalls().slice(-1)[0]).toContain("sap_id=10086");
    const customers = useWealthStore.getState().customers;
    expect(customers).toHaveLength(2);
    expect(customers[0]).toMatchObject({
      id: `${LOAN}|CUST001`,
      task: "信贷需求挖掘",
      category: "贷款",
      reason: "命中贷款核验规则",
      done: false,
    });

    await useWealthStore.getState().loadTodayCustomers("business");
    expect(nameListCalls().slice(-1)[0]).not.toContain("sap_id=");
  });

  it("触达登记后切换视角重新拉取，触达结果回填", async () => {
    useWealthStore.setState({ plans: [makeTodayPlan()] });
    await useWealthStore.getState().loadTodayCustomers("business");
    const id = useWealthStore.getState().customers[0]?.id ?? "";
    await useWealthStore.getState().reportContact(id, "电话", "done", "已沟通");
    expect(
      useWealthStore.getState().customers.find((c) => c.id === id)?.done,
    ).toBe(true);

    await useWealthStore.getState().loadTodayCustomers("customer");
    expect(
      useWealthStore.getState().customers.find((c) => c.id === id)?.done,
    ).toBe(true);
  });

  it("editPlan 将规划内容载入草稿并设置编辑态", () => {
    useWealthStore.getState().editPlan("plan-demo-2");
    const s = useWealthStore.getState();
    expect(s.editingId).toBe("plan-demo-2");
    expect(s.draft.name).toBe("产品到期承接");
    expect(s.draft.items.length).toBeGreaterThan(0);
  });

  it("removePlan 移除规划并提示", async () => {
    await useWealthStore.getState().removePlan("plan-demo-3");
    const s = useWealthStore.getState();
    expect(s.plans.some((p) => p.id === "plan-demo-3")).toBe(false);
    expect(s.toastText).toBe("规划已移除");
  });

  it("planScheduledOn 按排程推算（weekly 按执行日匹配）", () => {
    const plan: Plan = {
      id: "plan-x",
      name: "",
      source: "",
      desc: "",
      customers: 0,
      tasks: 0,
      rate: 0,
      status: "",
      period: "",
      start: "2026-09-01",
      end: "2026-09-30",
      items: [
        makeItem({
          cycle: "自定义",
          schedule: {
            type: "weekly",
            hour: 9,
            minute: 0,
            daysOfWeek: ["mon", "thu"],
          },
        }),
      ],
    };
    expect(planScheduledOn(plan, "2026-09-07")).toBe(true); // 周一
    expect(planScheduledOn(plan, "2026-09-08")).toBe(false); // 周二
    expect(planScheduledOn(plan, "2026-09-10")).toBe(true); // 周四
    expect(planScheduledOn(plan, "2026-10-01")).toBe(false); // 超出周期
  });

  it("planScheduledOn 非 weekly 排程在周期内每日可见", () => {
    const plan: Plan = {
      id: "plan-y",
      name: "",
      source: "",
      desc: "",
      customers: 0,
      tasks: 0,
      rate: 0,
      status: "",
      period: "",
      start: "2026-09-01",
      end: "2026-09-30",
      items: [makeItem({ cycle: "自定义" })],
    };
    expect(planScheduledOn(plan, "2026-09-08")).toBe(true);
    expect(planScheduledOn(plan, "2026-08-31")).toBe(false);
  });
});

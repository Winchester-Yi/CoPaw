/**
 * 智能财富工作台 —— 数据访问层
 *
 * 规划、场景与客户名单走真实后端接口（/wealth/plans、/wealth/scene-skills、
 * /wealth/name-list），不做假数据回退：接口不可达时读路径返回空、写路径直接
 * 抛错，避免联调期被 mock 掩盖问题。
 * 触达历史/触达登记/草稿仍为本期外的内存实现，待外部接口就绪后接入。
 */
import { parseCron, serializeCron } from "@/utils/parseCron";
import { request } from "../../api/request";
import { buildHistory, SCENE_CATEGORIES } from "./mock/data";
import { DEFAULT_SCHEDULE } from "./utils";
import type {
  Account,
  Customer,
  DistributeTarget,
  Draft,
  Plan,
  PlanItem,
  Scene,
} from "./types";

/** 模拟网络延迟（毫秒），让离线 mock 的异步行为贴近真实接口 */
const MOCK_LATENCY_MS = 60;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

// ---------------------------------------------------------------------------
// 后端接口契约（与 src/swe/app/wealth_plans/models.py 对齐）
// ---------------------------------------------------------------------------

interface PlanSceneView {
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
}

interface PlanTargetView {
  sap_id: string;
  name?: string | null;
}

interface PlanView {
  id: string;
  name: string;
  description?: string | null;
  source_label?: string | null;
  period_start?: string | null;
  period_end?: string | null;
  publish_status: string;
  board_status: string;
  editable: boolean;
  scenes: PlanSceneView[];
  targets: PlanTargetView[];
}

interface PlanListResponse {
  items: PlanView[];
}

interface PlanCreateResponse {
  id: string;
  publish_status: string;
}

interface SceneSkillItem {
  skillId: string;
  itemId?: string | null;
  senceName: string;
  category: string;
  senceDesc?: string | null;
  cronExample?: string | null;
  mcpRelationList: string[];
  skillBbkLabel?: string | null;
}

interface SceneSkillListResponse {
  items: SceneSkillItem[];
}

// ---------------------------------------------------------------------------
// 视图 → 展示模型映射
// ---------------------------------------------------------------------------

/** 大类英文 code → 场景图标（原型图标集） */
const CATEGORY_ICON: Record<string, string> = {
  insurance: "shield",
  loan: "bank",
  deposit: "safe",
  finance: "chart",
  fund: "pie",
  payroll: "money",
};

const CATEGORY_LABEL: Record<string, string> = Object.fromEntries(
  SCENE_CATEGORIES.map((c) => [c.code, c.label]),
);

function mapScene(item: SceneSkillItem): Scene {
  return {
    id: item.skillId,
    itemId: item.itemId,
    name: item.senceName,
    category: CATEGORY_LABEL[item.category] ?? item.category,
    categoryCode: item.category,
    icon: CATEGORY_ICON[item.category] ?? "layer",
    desc: item.senceDesc ?? "",
    source: item.skillBbkLabel ?? "",
    cronExample: item.cronExample,
    mcpRelations: item.mcpRelationList ?? [],
  };
}

function mapSceneItem(scene: PlanSceneView): PlanItem {
  return {
    id: scene.scene_id,
    sceneName: scene.scene_name,
    categoryLabel: scene.category_label,
    categoryCode: scene.category,
    itemId: scene.item_id,
    mcpRelations: scene.mcp_relations ?? [],
    direction: scene.direction ?? "",
    cycle: scene.cycle ?? "自定义",
    start: scene.start_date ?? undefined,
    end: scene.end_date ?? undefined,
    schedule: parseCron(scene.cron_expr),
  };
}

function mapPlan(view: PlanView): Plan {
  const items = view.scenes.map(mapSceneItem);
  const cycles = [...new Set(items.map((x) => x.cycle))];
  return {
    id: view.id,
    name: view.name,
    desc: view.description ?? items[0]?.direction ?? "",
    source: view.source_label ?? "我的关注",
    customers: 0,
    tasks: 0,
    rate: 0,
    status: view.board_status,
    publishStatus: view.publish_status,
    editable: view.editable,
    period: cycles.length === 1 ? cycles[0] : "自定义",
    start: view.period_start ?? "",
    end: view.period_end ?? "",
    items,
    targetSapIds: view.targets.map((t) => t.sap_id),
  };
}

// ---------------------------------------------------------------------------
// 场景查询：/wealth/scene-skills（外部接口代理）
// ---------------------------------------------------------------------------

/**
 * 按产品大类查询经营场景；categoryCode 传空串表示查询全部大类。
 * 接口不可达时返回空列表，由页面展示空态，不做假数据兜底。
 */
export async function fetchScenesByCategory(
  categoryCode: string,
): Promise<Scene[]> {
  try {
    const resp = await request<SceneSkillListResponse>(
      `/wealth/scene-skills?category=${encodeURIComponent(categoryCode)}`,
    );
    return resp.items.map(mapScene);
  } catch (error) {
    console.warn("[Wealth] 场景接口不可用，返回空列表", error);
    return [];
  }
}

// ---------------------------------------------------------------------------
// 客户名单查询：/wealth/name-list（外部接口代理）
// ---------------------------------------------------------------------------

interface NameListItemView {
  custUid: string;
  custNm: string;
  sapId?: string | null;
  bbkOrgId?: string | null;
  filename?: string | null;
  recomReason?: string | null;
}

interface NameListResponse {
  items: NameListItemView[];
}

/**
 * 按技能查询客户名单；传 sapId 为客户视角（该经理名下客户），
 * 不传为经营视角（不限定客户经理，全分行客户池）。
 * 接口不可达时返回空列表，由页面展示空态，不做假数据兜底。
 */
export async function fetchNameList(
  skillId: string,
  sapId?: string,
): Promise<NameListItemView[]> {
  try {
    const params = new URLSearchParams({ skill_id: skillId });
    if (sapId) {
      params.set("sap_id", sapId);
    }
    const resp = await request<NameListResponse>(
      `/wealth/name-list?${params.toString()}`,
    );
    return resp.items;
  } catch (error) {
    console.warn("[Wealth] 客户名单接口不可用，返回空列表", error);
    return [];
  }
}

/** 今日有排程的经营场景（客户名单查询的入参上下文） */
export interface TodayTaskRef {
  skillId: string;
  sceneName: string;
  category: string;
}

/**
 * 拉取今日任务对应的客户经营清单：按技能去重并发查询 name-list，
 * 合并为页面客户列表并回填会话级触达登记；同一客户在同一任务下只出现一次。
 */
export async function fetchTodayCustomers(
  tasks: TodayTaskRef[],
  sapId?: string,
): Promise<Customer[]> {
  const uniqueTasks = [...new Map(tasks.map((t) => [t.skillId, t])).values()];
  const groups = await Promise.all(
    uniqueTasks.map(async (t) => ({
      task: t,
      list: await fetchNameList(t.skillId, sapId),
    })),
  );
  const seen = new Set<string>();
  const customers: Customer[] = [];
  for (const { task, list } of groups) {
    for (const item of list) {
      const id = `${task.skillId}|${item.custUid}`;
      if (seen.has(id)) {
        continue;
      }
      seen.add(id);
      const mark = db.contacts[id];
      const reason = item.recomReason ?? "";
      customers.push({
        id,
        custUid: item.custUid,
        skillId: task.skillId,
        name: item.custNm,
        label: "",
        reason,
        category: task.category,
        task: task.sceneName,
        done: mark?.done ?? false,
        channel: mark?.channel ?? "",
        time: mark?.time ?? "",
        note: mark?.note ?? "",
        opportunities: reason ? [reason] : [],
        link: item.filename ?? undefined,
      });
    }
  }
  db.customers = clone(customers);
  return customers;
}

// ---------------------------------------------------------------------------
// 规划接口：/wealth/plans
// ---------------------------------------------------------------------------

interface PlanScenePayload {
  scene_id: string;
  scene_name: string;
  category: string;
  item_id?: string | null;
  direction?: string;
  cycle?: string;
  start_date?: string;
  end_date?: string;
  cron_expr: string;
  mcp_relations: string[];
}

interface PlanUpsertPayload {
  name: string;
  description?: string;
  source_label?: string;
  scenes: PlanScenePayload[];
  targets: { sap_id: string; name?: string }[];
}

function buildScenePayload(item: PlanItem): PlanScenePayload {
  return {
    scene_id: item.id,
    scene_name: item.sceneName,
    category: item.categoryCode,
    item_id: item.itemId ?? null,
    direction: item.direction,
    cycle: item.cycle,
    start_date: item.start,
    end_date: item.end,
    cron_expr: serializeCron(item.schedule ?? DEFAULT_SCHEDULE),
    mcp_relations: item.mcpRelations,
  };
}

async function fetchPlans(): Promise<Plan[]> {
  const resp = await request<PlanListResponse>("/wealth/plans");
  return resp.items.map(mapPlan);
}

/** 轮询场景的接口告警只打一次，恢复后重置 */
let planListErrorWarned = false;

/** 看板轮询用：拉取最新规划列表，接口不可达时返回空（read 路径容忍，绝不回退假数据） */
export async function fetchPlanList(): Promise<Plan[]> {
  try {
    const plans = await fetchPlans();
    planListErrorWarned = false;
    return plans;
  } catch (error) {
    if (!planListErrorWarned) {
      console.warn("[Wealth] 规划接口不可用，返回空列表", error);
      planListErrorWarned = true;
    }
    return [];
  }
}

// ---------------------------------------------------------------------------
// 内存实现（触达登记覆盖层/触达历史/草稿，刷新即复原；
// 规划、场景与客户名单已全程走真实接口）
// ---------------------------------------------------------------------------

/** 触达登记结果：按 `${skillId}|${custUid}` 记录，切换视角重新拉名单后回填 */
interface ContactMark {
  done: boolean;
  channel: string;
  time: string;
  note: string;
}

interface DraftEntry {
  draft: Draft;
  at: string;
}

interface WealthDb {
  /** 当前视图的客户清单（fetchTodayCustomers 写入，reportContact 原地更新） */
  customers: Customer[];
  /** 触达登记覆盖层（触达记录接口未出前的会话级实现） */
  contacts: Record<string, ContactMark>;
  history: Customer[];
  /** 按账户隔离的会话级草稿 */
  drafts: Record<string, DraftEntry>;
}

let db: WealthDb = createInitialDb();

function createInitialDb(): WealthDb {
  return {
    customers: [],
    contacts: {},
    history: buildHistory(),
    drafts: {},
  };
}

/** 测试辅助：重置内存后端到初始状态（模拟刷新） */
export function resetMockDb(): void {
  db = createInitialDb();
}

export function newAccountDraft(): Draft {
  return { name: "", items: [] };
}

export interface BootstrapData {
  plans: Plan[];
  history: Customer[];
  draft: Draft;
  savedAt: string;
}

/** 拉取账户视角下的工作台基础数据（规划走真实接口；客户名单由 fetchTodayCustomers 单独加载） */
export async function fetchBootstrap(
  accountId: string,
): Promise<BootstrapData> {
  const cached = db.drafts[accountId];
  let plans: Plan[];
  try {
    plans = await fetchPlans();
  } catch (error) {
    console.warn("[Wealth] 规划接口不可用，返回空列表", error);
    plans = [];
  }
  await sleep(MOCK_LATENCY_MS);
  return {
    plans,
    history: clone(db.history),
    draft: cached ? clone(cached.draft) : newAccountDraft(),
    savedAt: cached?.at ?? "",
  };
}

/** 保存草稿：仅记录保存时间并写内存表（草稿不进后端，见 CONTEXT.md） */
export async function saveDraft(
  accountId: string,
  draft: Draft,
): Promise<{ savedAt: string }> {
  await sleep(MOCK_LATENCY_MS);
  const savedAt = new Date().toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
  db.drafts[accountId] = { draft: clone(draft), at: savedAt };
  return { savedAt };
}

export interface PublishResult {
  plans: Plan[];
  created: boolean;
}

/** 发布新规划或保存对既有规划的修改，返回最新规划列表；任何失败均上抛，由调用方提示 */
export async function publishPlan(
  account: Account,
  draft: Draft,
  editingId: string | null,
  targets: DistributeTarget[],
): Promise<PublishResult> {
  const payload: PlanUpsertPayload = {
    name: draft.name.trim(),
    source_label: account.source,
    scenes: draft.items.map(buildScenePayload),
    targets: targets.map((t) => ({ sap_id: t.sapId, name: t.name })),
  };
  if (editingId != null) {
    await request<PlanCreateResponse>(
      `/wealth/plans/${encodeURIComponent(editingId)}`,
      { method: "PUT", body: JSON.stringify(payload) },
    );
  } else {
    await request<PlanCreateResponse>("/wealth/plans", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }
  return { plans: await fetchPlans(), created: editingId == null };
}

/** 移除规划，返回最新规划列表；任何失败均上抛 */
export async function removePlan(id: string): Promise<{ plans: Plan[] }> {
  await request<{ deleted: boolean }>(
    `/wealth/plans/${encodeURIComponent(id)}`,
    { method: "DELETE" },
  );
  return { plans: await fetchPlans() };
}

export interface ContactPayload {
  /** 客户页面内标识：`${skillId}|${custUid}` */
  id: string;
  channel: string;
  outcome: "done" | "pending";
  note: string;
}

/**
 * 登记触达结果，返回最新客户清单。
 * 触达记录接口未出前为会话级内存实现：写入覆盖层（切换视角重新拉名单后回填），
 * 并同步更新当前视图的客户清单。
 */
export async function reportContact(
  payload: ContactPayload,
  today: string,
): Promise<{ customers: Customer[] }> {
  await sleep(MOCK_LATENCY_MS);
  const mark: ContactMark = {
    note: payload.note,
    channel: payload.channel,
    time:
      today +
      " " +
      new Date().toLocaleTimeString("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
      }),
    done: payload.outcome === "done",
  };
  db.contacts[payload.id] = mark;
  const c = db.customers.find((x) => x.id === payload.id);
  if (c) {
    Object.assign(c, mark);
  }
  return { customers: clone(db.customers) };
}

/**
 * 智能财富工作台 —— 数据访问层
 *
 * 规划与场景走真实后端接口（/wealth/plans、/wealth/scene-skills），不做假数据
 * 回退：接口不可达时读路径返回空、写路径直接抛错，避免联调期被 mock 掩盖问题。
 * 客户/触达/草稿仍为本期外的 mock 实现，待外部接口就绪后接入。
 */
import { parseCron, serializeCron } from "@/utils/parseCron";
import { request } from "../../api/request";
import {
  accounts,
  buildCustomers,
  buildHistory,
  SCENE_CATEGORIES,
} from "./mock/data";
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

/**
 * 是否显示「角色预览」入口。
 * 岗位映射（POSITION_ROLE_MAP）已生效，生产身份由父系统 positionId 唯一决定，
 * 预览器随之关闭（见 Topbar / AccountSwitcher）；本地演示如需临时打开可置回 true。
 */
export const IS_MOCK = false;

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
  ready: boolean;
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
  finance: "layer",
  deposit: "chart",
  payroll: "user",
  cross_border: "globe",
  fund: "layer",
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
    ready: item.ready,
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
// 内存 mock（客户/触达/草稿，刷新即复原；规划已全程走真实接口）
// ---------------------------------------------------------------------------

interface DraftEntry {
  draft: Draft;
  at: string;
}

interface WealthDb {
  customers: Customer[];
  history: Customer[];
  /** 按账户隔离的会话级草稿 */
  drafts: Record<string, DraftEntry>;
}

let db: WealthDb = createInitialDb();

function createInitialDb(): WealthDb {
  return {
    customers: buildCustomers(),
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
  customers: Customer[];
  history: Customer[];
  draft: Draft;
  savedAt: string;
}

export async function fetchAccounts(): Promise<Account[]> {
  await sleep(MOCK_LATENCY_MS);
  return clone(accounts);
}

/** 拉取账户视角下的工作台全量数据（规划走真实接口，其余本期仍为 mock） */
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
    customers: clone(db.customers),
    history: clone(db.history),
    draft: cached ? clone(cached.draft) : newAccountDraft(),
    savedAt: cached?.at ?? "",
  };
}

/** 切换账户前暂存当前账户草稿（会话级，不落盘） */
export async function stashDraft(
  accountId: string,
  draft: Draft,
  savedAt: string,
): Promise<void> {
  await sleep(0);
  db.drafts[accountId] = { draft: clone(draft), at: savedAt };
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
  id: number;
  channel: string;
  outcome: "done" | "pending";
  note: string;
}

/** 登记触达结果，返回最新客户清单（本期仍为 mock） */
export async function reportContact(
  payload: ContactPayload,
  today: string,
): Promise<{ customers: Customer[] }> {
  await sleep(MOCK_LATENCY_MS);
  const c = db.customers.find((x) => x.id === payload.id);
  if (c) {
    c.note = payload.note;
    c.channel = payload.channel;
    c.time =
      today +
      " " +
      new Date().toLocaleTimeString("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
      });
    c.done = payload.outcome === "done";
  }
  return { customers: clone(db.customers) };
}

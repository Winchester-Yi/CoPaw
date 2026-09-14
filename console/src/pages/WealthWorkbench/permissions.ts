/**
 * 智能财富工作台 —— 角色与页面权限
 *
 * 权限模型：
 * - 角色（WealthRole）是唯一权限主体，矩阵表 ROLE_PERMISSIONS 是唯一判定点；
 *   组件与路由守卫一律经 canAccessPage 判定，不再各自读账户字段。
 * - 生产环境角色由父系统身份解析：cookie positionID 经 handleUrlOriginParam
 *   写入 iframeStore.positionId，再由 resolveRole 映射为角色。
 * - mock 期的「角色预览」只是临时覆盖生效角色，见 store.previewRole。
 *
 * 矩阵（与原型一致的最小版本）：
 * - 三个角色均可访问 规划看板 / 创建计划；
 * - 仅客户经理可访问 任务三页（今日任务 / 待触达客户 / 已完成）；
 * - 未识别身份（unknown）无任何页面权限，入口整页拦截。
 * 操作级权限（发布 / 编辑 / 删除 / 触达登记）暂不按角色区分，矩阵预留扩展。
 */

/** 工作台角色；unknown 为未识别身份的伪角色（deny by default，无任何权限） */
export type WealthRole = "rm" | "president" | "middle" | "unknown";

/** 受权限控制的页面组；tasks 代表今日任务 / 待触达客户 / 已完成三页 */
export type WealthPage = "board" | "create" | "tasks";

/** 角色 × 页面矩阵：唯一权限判定来源，新增权限点只改这里 */
export const ROLE_PERMISSIONS: Record<WealthRole, readonly WealthPage[]> = {
  rm: ["board", "create", "tasks"],
  president: ["board", "create"],
  middle: ["board", "create"],
  unknown: [],
};

/**
 * 兜底角色：positionId 缺失或未命中映射表时使用（deny by default）。
 * 未识别身份无任何页面权限：入口整页拦截为「暂无访问权限」提示，
 * 不加载业务数据，见 index.tsx 的 unknown 分支。
 */
export const FALLBACK_ROLE: WealthRole = "unknown";

/**
 * positionId → 角色映射表（父系统岗位编号清单）：
 * - RB0101 客户经理；RB0208 支行行长；RB0304 / RB0906 均为分行中台。
 * 新增岗位类型时在此登记；未知编号由 resolveRole 回退 FALLBACK_ROLE 并留痕。
 */
export const POSITION_ROLE_MAP: Record<string, WealthRole> = {
  RB0101: "rm",
  RB0208: "president",
  RB0304: "middle",
  RB0906: "middle",
};

/** 由父系统岗位编号解析工作台角色；未命中时回退 FALLBACK_ROLE 并留痕 */
export function resolveRole(positionId: string | null | undefined): WealthRole {
  if (!positionId) return FALLBACK_ROLE;
  const role = POSITION_ROLE_MAP[positionId];
  if (role) return role;
  console.warn(
    `[WealthWorkbench] 未知 positionId「${positionId}」，回退为角色 ${FALLBACK_ROLE}`,
  );
  return FALLBACK_ROLE;
}

/** 角色是否可访问某页面组 */
export function canAccessPage(role: WealthRole, page: WealthPage): boolean {
  return ROLE_PERMISSIONS[role].includes(page);
}

/**
 * 角色发布规划时是否需要选择分发目标。
 * 客户经理的规划只发给自己（sapId 即本人 userId），行长/中台需分发给下属客户经理。
 */
export function needsDistributeTargets(role: WealthRole): boolean {
  return role !== "rm";
}

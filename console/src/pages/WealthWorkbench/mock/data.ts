/**
 * 智能财富工作台 —— 静态配置
 * 规划、账户、分发用户池、今日客户名单与触达历史均已走真实接口；
 * 本文件仅保留产品大类的静态映射（接口未提供大类清单，前端写死）。
 */

/** 产品大类：中文名 ↔ 英文 code，与后端 CATEGORY_CODE_BY_LABEL 一致 */
export const SCENE_CATEGORIES = [
  { label: "保险", code: "insurance" },
  { label: "贷款", code: "loan" },
  { label: "存款", code: "deposit" },
  { label: "理财", code: "finance" },
  { label: "基金", code: "fund" },
  { label: "代发", code: "payroll" },
] as const;

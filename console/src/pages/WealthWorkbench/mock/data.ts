/**
 * 智能财富工作台 —— mock 静态数据
 * 规划、账户、分发用户池与今日客户名单已随接口闭环移除；本文件仅保留
 * 触达历史（已完成页）的占位数据，待触达记录接口就绪后删除。
 */
import type { Customer } from "../types";

/** 平台可用能力总量（示例），不随本期覆盖场景筛选缩减 */
export const PLATFORM_CAPABILITY_COUNT = 12;

/** 产品大类：中文名 ↔ 英文 code，与后端 CATEGORY_CODE_BY_LABEL 一致 */
export const SCENE_CATEGORIES = [
  { label: "保险", code: "insurance" },
  { label: "贷款", code: "loan" },
  { label: "存款", code: "deposit" },
  { label: "理财", code: "finance" },
  { label: "基金", code: "fund" },
  { label: "代发", code: "payroll" },
] as const;

export const labels = ["总行重点", "分行重点", "行长指派"];

const names = [
  "张",
  "李",
  "王",
  "陈",
  "刘",
  "赵",
  "周",
  "吴",
  "孙",
  "郑",
  "钱",
  "冯",
  "朱",
  "许",
  "何",
  "吕",
  "施",
  "沈",
  "韩",
  "杨",
  "蒋",
  "曹",
  "严",
  "华",
  "金",
  "魏",
  "陶",
  "姜",
];

const reasons = [
  "产品将于 3 日后到期，预计释放资金 50 万元",
  "近期发生高价值动账，存在承接机会",
  "代发客户沉淀资金增长，具备进一步经营潜力",
  "理财产品 5 日后到期，预计释放资金 100 万元",
  "当前资产配置偏低风险，可适当优化配置结构",
  "到期资金暂未配置，存在承接机会",
  "基金持仓收益良好，存在加仓机会",
  "存款产品 7 日后到期，预计释放资金 80 万元",
  "近期活跃度提升，存在多产品交叉营销机会",
  "薪资代发客户，具备理财配置潜力",
];

/** 已完成页的触达历史占位数据；今日客户名单已走 /wealth/name-list 真实接口 */
export function buildHistory(): Customer[] {
  return Array.from({ length: 34 }, (_, i) => ({
    id: `hist-${i}`,
    custUid: "",
    skillId: "",
    name: names[i % 28] + "**",
    label: labels[i % 3],
    reason: reasons[i % 10],
    category: "理财",
    task: "产品到期承接",
    done: true,
    channel: ["电话", "企微", "线上面访"][i % 3],
    time:
      "2026-09-07 " +
      (i % 2 ? "14" : "10") +
      ":" +
      String(i % 60).padStart(2, "0"),
    note: "已完成客户触达与需求记录。",
  }));
}

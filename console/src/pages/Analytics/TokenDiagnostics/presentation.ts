import type { TokenCategory } from "../../../api/modules/tokenDiagnostics";

export const categories: Record<string, { label: string; color: string }> = {
  system: { label: "系统提示词", color: "#6268d8" },
  tools: { label: "工具定义", color: "#c28a21" },
  user: { label: "用户消息", color: "#459858" },
  inject: { label: "注入内容", color: "#9561bb" },
  skill: { label: "技能注入", color: "#d86f36" },
  assistant: { label: "助手历史", color: "#427acd" },
  tool_result: { label: "工具结果", color: "#168d87" },
  unclassified: { label: "未归类", color: "#7c8594" },
  output: { label: "本次输出", color: "#c45b89" },
};
export const ruleNames: Record<string, string> = {
  repeated_tool_result: "工具结果重复携带",
  duplicate_injection: "同次输入重复注入",
  repeated_tool_call: "工具反复执行",
  history_growth: "历史持续增长",
  output_budget: "输出预算",
};
export const states: Record<string, string> = {
  running: "进行中",
  completed: "已完成",
  error: "执行失败",
  cancelled: "已取消",
  pending: "待分析",
  ready: "已分析",
  failed: "分析失败",
  insufficient_evidence: "证据不足",
  complete: "记录完整",
  incomplete: "记录有缺口",
  unverified: "完整性待确认",
  reported: "已报告",
  partial: "部分报告",
  unavailable: "未知",
};
export function formatTokens(value: number | null | undefined): string {
  return value == null ? "未知" : new Intl.NumberFormat("zh-CN").format(value);
}
export function usageLabel(complete: boolean, known: number): string {
  return complete ? "总量" : known > 0 ? "已知部分" : "未知";
}
export function composition(
  rows: TokenCategory[],
  input: number | null,
  output: number | null,
  anchor: boolean,
) {
  const total = rows.reduce((sum, x) => sum + (x.estimated_tokens ?? 0), 0);
  if (!total) return [];
  const anchored = anchor && input != null && output != null;
  const result = rows.map((x) => ({
    category: x.category,
    value: anchored
      ? ((x.estimated_tokens ?? 0) / total) * input
      : x.estimated_tokens ?? 0,
    approximate: true,
  }));
  if (anchored)
    result.push({ category: "output", value: output, approximate: false });
  return result;
}

import {
  DIAGNOSIS_RULE_IDS,
  type DiagnosisFilters,
} from "../../../api/modules/tokenDiagnosisV2";
export const RULE_LABELS: Record<(typeof DIAGNOSIS_RULE_IDS)[number], string> =
  {
    context_growth: "R01 上下文增长",
    segment_carryover: "R02 内容持续携带",
    tool_context_volume: "R03 工具结果规模",
    safe_tool_repeat: "R04 安全工具重复",
    suspected_loop: "R05 疑似循环",
    text_prefix_stability: "R06 文本前缀稳定性",
    static_context_volume: "R07 静态上下文规模",
    cohort_usage_outlier: "R08 同类任务用量异常",
  };
export const QUERY_STATES = [
  "candidate",
  "not_hit",
  "insufficient_evidence",
  "not_applicable",
  "pending",
  "failed",
  "running",
  "ready",
  "stale",
  "completed",
  "cancelled",
  "error",
  "prepared",
  "abandoned",
  "reported",
  "partial",
  "unavailable",
  "complete",
  "unverified",
  "incomplete",
] as const;
const STATE_LABELS: Record<string, string> = {
  candidate: "候选问题",
  not_hit: "未命中",
  insufficient_evidence: "证据不足",
  not_applicable: "不适用",
  pending: "待评估",
  failed: "分析失败",
  running: "进行中",
  ready: "分析完成",
  stale: "历史结果（已落后）",
  current: "当前结果",
  completed: "已完成",
  cancelled: "已取消",
  error: "执行失败",
  prepared: "已准备",
  abandoned: "已放弃",
  reported: "用量已报告",
  partial: "部分采集",
  unavailable: "不可用",
  complete: "完整",
  unverified: "完整性未验证",
  incomplete: "采集不完整",
  missing: "证据缺失",
  limited: "查询受限",
  expired: "证据已过期",
  unknown: "未知",
};
const REASON_LABELS: Record<string, string> = {
  candidate_limit: "候选检索超限",
  no_history: "无兼容历史候选",
  missing_evidence: "缺少比较证据",
  missing_context: "上下文证据缺失",
  partial_context: "上下文仅部分采集",
  missing_key: "缺少指纹密钥",
  missing_identity: "缺少可靠身份",
  missing_usage: "真实用量缺失",
  missing_denominator: "缺少可信分母",
  missing_step: "缺少业务 Step 身份",
  missing_progress: "缺少进度证据",
  missing_tool_properties: "工具属性证据不足",
  missing_cohort: "同类任务样本不足",
  missing_static_identity: "静态来源身份不足",
  incompatible_version: "表示或版本不兼容",
  capture_limit: "采集预算超限",
  evidence_expired: "证据已过期",
  analysis_failed: "分析失败",
  analysis_timeout: "分析时间耗尽",
  analysis_pending: "待评估",
  stale_analysis: "历史分析已落后",
  empty_text: "可比较文本为空",
  media: "含未覆盖的媒体",
  untrusted_composition: "构成归因或估算不完整",
  deployment_limits_unavailable: "部署预算不可用",
  no_eligible_tasks: "无完整生命周期样本",
  missing_context_pair: "操作前后上下文缺失",
  partial_context_pair: "操作前后仅部分采集",
  incompatible_context_pair: "操作前后归属不兼容",
  incompatible_representation: "操作前后表示不兼容",
  incompatible_estimator: "操作前后估算器不兼容",
  missing_estimated_tokens: "缺少规模估算",
  operation_not_successful: "操作未成功",
  step_order_not_recorded: "业务 Step 顺序未记录",
  revision_or_snapshot_changed: "输入修订或分析快照已改变",
};
export function formatCount(value: number | null | undefined): string {
  return value == null ? "未知" : value.toLocaleString("zh-CN");
}
export function formatEstimate(value: number | null | undefined): string {
  return value == null ? "估算未知" : `估算 ${formatCount(value)} Token`;
}
export function formatRatio(value: number | null | undefined): string {
  return value == null ? "不可用" : `${(value * 100).toFixed(1)}%`;
}
export function stateLabel(state: string | null | undefined): string {
  return state == null ? "未知" : STATE_LABELS[state] || state;
}
export function reasonLabel(reason: string | null | undefined): string {
  return reason ? REASON_LABELS[reason] || reason : "";
}
const FILTER_KEYS = [
  "start",
  "end",
  "agent_id",
  "root_agent_id",
  "model_name",
  "rule_id",
  "state",
  "branch_run_id",
  "limit",
  "cursor",
] as const;
export function parseDiagnosisFilters(
  search: URLSearchParams,
): DiagnosisFilters {
  const output: DiagnosisFilters = {};
  for (const key of FILTER_KEYS) {
    const value = search.get(key);
    if (!value) continue;
    if (key === "limit") {
      const limit = Number(value);
      if (Number.isInteger(limit) && limit >= 1 && limit <= 100)
        output.limit = limit;
    } else if (key === "rule_id") {
      if (
        DIAGNOSIS_RULE_IDS.includes(
          value as (typeof DIAGNOSIS_RULE_IDS)[number],
        )
      )
        output.rule_id = value as (typeof DIAGNOSIS_RULE_IDS)[number];
    } else if (key === "state") {
      if ((QUERY_STATES as readonly string[]).includes(value))
        output.state = value;
    } else if (key === "start" || key === "end") {
      if (
        /(Z|[+-]\d\d:\d\d)$/.test(value) &&
        Number.isFinite(Date.parse(value))
      )
        output[key] = value;
    } else if (
      value.length <=
        (key === "cursor" ? 2048 : key === "model_name" ? 256 : 64) &&
      !Array.from(value).some((char) => char.charCodeAt(0) < 32)
    )
      output[key] = value;
  }
  return output;
}
export function updateDiagnosisSearch(
  search: URLSearchParams,
  updates: Partial<DiagnosisFilters>,
): URLSearchParams {
  const next = new URLSearchParams(search);
  let changed = false;
  for (const key of FILTER_KEYS) {
    if (!(key in updates)) continue;
    const value = updates[key];
    const text = value == null || value === "" ? null : String(value);
    if (key !== "cursor" && next.get(key) !== text) changed = true;
    if (text === null) next.delete(key);
    else next.set(key, text);
  }
  if (changed) {
    next.delete("cursor");
    next.delete("relation_cursor");
  }
  return next;
}
export function validateReturnTo(
  value: string | null | undefined,
): string | null {
  if (
    !value ||
    !value.startsWith("/analytics") ||
    value.includes("\\") ||
    Array.from(value).some((char) => char.charCodeAt(0) < 32) ||
    /%5c/i.test(value)
  )
    return null;
  try {
    const url = new URL(value, "https://console.invalid");
    if (
      url.origin !== "https://console.invalid" ||
      !/^\/analytics(?:\/|$)/.test(url.pathname)
    )
      return null;
    return url.pathname + url.search + url.hash;
  } catch {
    return null;
  }
}

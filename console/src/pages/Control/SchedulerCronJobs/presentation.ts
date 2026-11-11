import type { ManagedJob } from "@/api/types/schedulerCron";

export const MANAGEMENT_LABELS: Record<string, string> = {
  ready: "正常",
  creating: "创建中",
  updating: "修改中",
  deleting: "删除中",
  deleted: "已删除",
  create_failed: "创建失败",
  update_failed: "修改失败",
  delete_failed: "删除失败",
  set_enabled_failed: "启停失败",
  pause_group_failed: "整组暂停失败",
  state_missing: "状态缺失",
};
export const RESULT_LABELS: Record<string, string> = {
  idle: "未执行",
  running: "运行中",
  success: "成功",
  error: "失败",
  cancelled: "已取消",
  skipped: "已跳过",
};
export function managementLabel(value: string) {
  return (
    MANAGEMENT_LABELS[value] ??
    (value?.endsWith("_failed") ? "操作失败" : "处理中")
  );
}
export function beijingDay(now = new Date()) {
  const parts = new Intl.DateTimeFormat("en", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  return ["year", "month", "day"]
    .map((key) => parts.find((p) => p.type === key)?.value)
    .join("-");
}
export function schedulingReasons(job: ManagedJob, today = beijingDay()) {
  if (job.deleted) return ["已删除"];
  const result: string[] = [];
  const period = job.run_date_range;
  if (period && today < period.start_date) result.push("未到生效日期");
  if (period && today > period.end_date) result.push("已过期");
  const pause = job.task?.pause_reason as string | null | undefined;
  if (pause === "auto_unread_threshold") result.push("未读过多自动暂停");
  else if (job.enabled === false || pause === "manual")
    result.push("本任务已停用");
  else if (job.task?.is_paused && !pause?.startsWith("date_"))
    result.push("本任务已暂停");
  if (job.dispatch_state === "batch_paused") result.push("批入口已暂停");
  return result.length ? result : ["可调度"];
}
export function beijingRunTime(value?: string | null) {
  if (!value) return "未记录";
  // 新版状态时间是 UTC；无偏移的持久时间也按 UTC 解读。
  const date = new Date(
    /[zZ]$|[+-]\d\d:\d\d$/.test(value) ? value : `${value.replace(" ", "T")}Z`,
  );
  if (Number.isNaN(date.getTime())) return "时间格式异常";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  })
    .format(date)
    .replace(/\//g, "-");
}

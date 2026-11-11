import { expect, it } from "vitest";
import type { ManagedJob } from "@/api/types/schedulerCron";
import { beijingDay, beijingRunTime, schedulingReasons } from "./presentation";

it("北京时间跨日正确，朴素 UTC 状态时间也按北京时间显示", () => {
  expect(beijingDay(new Date("2026-11-02T16:30:00Z"))).toBe("2026-11-03");
  expect(beijingRunTime("2026-11-02T16:30:00")).toBe("2026-11-03 00:30:00");
  expect(beijingRunTime("2026-11-03T00:30:00+08:00")).toBe(
    "2026-11-03 00:30:00",
  );
});
it("所有限制逐项显示，日期不会隐藏人工停用及批入口暂停", () => {
  const job = {
    enabled: false,
    dispatch_state: "batch_paused",
    run_date_range: { start_date: "2026-10-01", end_date: "2026-10-31" },
  } as ManagedJob;
  expect(schedulingReasons(job, "2026-11-03")).toEqual([
    "已过期",
    "本任务已停用",
    "批入口已暂停",
  ]);
  expect(schedulingReasons({ ...job, deleted: true }, "2026-11-03")).toEqual([
    "已删除",
  ]);
});
it("自动暂停不重复标注人工停用，过时日期标记不覆盖实际日期", () => {
  const job = {
    enabled: false,
    dispatch_state: "ordinary",
    task: { is_paused: true, pause_reason: "auto_unread_threshold" },
  } as ManagedJob;
  expect(schedulingReasons(job)).toEqual(["未读过多自动暂停"]);
  expect(
    schedulingReasons({
      ...job,
      enabled: true,
      task: { ...job.task!, pause_reason: "date_before_start" as never },
    }),
  ).toEqual(["可调度"]);
});

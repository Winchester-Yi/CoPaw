import type { CronJobSpecOutput } from "@/api/types";
import dayjs, { type Dayjs } from "dayjs";
import type {
  ManagedCronSpec,
  ManagedCronSchedule,
  ManagedRunDateRange,
  ManagedRecheckRule,
} from "@/api/types/schedulerCron";

import {
  buildCronJobFormValues,
  buildCronJobSubmitPayload,
  type CronJobFormValues,
} from "../CronJobs/helpers";
import {
  CronType,
  parseCron,
  serializeCron,
} from "../CronJobs/components/parseCron";

export type ManagedCronFormValues = CronJobFormValues & {
  schedule: ManagedCronSchedule;
  runDates?: [Dayjs, Dayjs] | null;
  recheckEnabled?: boolean;
  recheckDelay?: number | null;
  recheckUnit?: "minutes" | "hours";
  recheckInstruction?: string;
};
type DatedJob = CronJobSpecOutput & {
  run_date_range?: ManagedRunDateRange | null;
  recheck?: ManagedRecheckRule | null;
};

const LAST_WORKDAY = "lastWorkday";

function lastWorkdayParts(cron: string) {
  const match = cron.trim().match(/^(\d{1,2})\s+(\d{1,2})\s+LW\s+\*\s+\*$/i);
  if (!match || Number(match[1]) > 59 || Number(match[2]) > 23) return null;
  return { minute: Number(match[1]), hour: Number(match[2]) };
}

/** 只适配新版编辑器，不改变旧页面的表单转换行为。 */
export function buildManagedCronFormValues(job: DatedJob) {
  const values: ManagedCronFormValues = buildCronJobFormValues(job);
  values.runDates = job.run_date_range
    ? [dayjs(job.run_date_range.start_date), dayjs(job.run_date_range.end_date)]
    : null;
  values.recheckEnabled = Boolean(job.recheck);
  values.recheckUnit =
    job.recheck?.delay_seconds && job.recheck.delay_seconds % 3600 === 0
      ? "hours"
      : "minutes";
  values.recheckDelay = job.recheck
    ? job.recheck.delay_seconds / (values.recheckUnit === "hours" ? 3600 : 60)
    : 10;
  values.recheckInstruction = job.recheck?.instruction ?? "";
  const schedule = job.schedule as ManagedCronSchedule;
  values.schedule = {
    ...schedule,
    run_on_non_workday: schedule.run_on_non_workday ?? false,
  };

  const cron = job.schedule.cron;
  const parts = parseCron(cron);
  const workday = lastWorkdayParts(cron);
  if (workday && schedule.timezone === "Asia/Shanghai") {
    values.cronType = LAST_WORKDAY;
    values.cronTime = dayjs().hour(workday.hour).minute(workday.minute);
  } else if (
    parts.type === CronType.daily &&
    (parts.hours?.length ?? 1) === 1
  ) {
    values.cronTime = values.cronTime?.hour(
      parts.hours?.[0] ?? parts.hour ?? 9,
    );
  } else if (parts.type !== CronType.hourly && parts.type !== CronType.weekly) {
    // 原表单没有多小时、月/年或分钟间隔控件，使用已有自定义输入。
    values.cronType = CronType.custom;
    values.cronCustom = cron;
  }
  return values;
}

export function buildManagedCronSubmitPayload(
  values: ManagedCronFormValues,
  original?: DatedJob | null,
) {
  const lastWorkday = values.cronType === LAST_WORKDAY;
  const time = values.cronTime ?? dayjs().hour(9).minute(0);
  const source = lastWorkday
    ? {
        ...values,
        cronType: CronType.custom,
        cronCustom: `${time.minute()} ${time.hour()} LW * *`,
        schedule: { ...values.schedule, timezone: "Asia/Shanghai" },
      }
    : values;
  const {
    runDates,
    recheckEnabled,
    recheckDelay,
    recheckUnit,
    recheckInstruction,
    ...baseValues
  } = source;
  const payload: ManagedCronSpec = buildCronJobSubmitPayload(baseValues);
  if (payload.task_type === "agent" && recheckEnabled) {
    const seconds = (recheckDelay ?? 0) * (recheckUnit === "hours" ? 3600 : 60);
    const delay = Math.round(seconds);
    const instruction = recheckInstruction?.trim() ?? "";
    if (Math.abs(seconds - delay) > 1e-6 || delay < 60 || delay > 604800)
      throw new Error("回检延迟须为 1 分钟至 7 天，精确到秒");
    if (!instruction || Array.from(instruction).length > 4000)
      throw new Error("回检指令须为 1 至 4000 个字符");
    payload.recheck = { delay_seconds: delay, instruction };
  } else if (original?.recheck) {
    payload.recheck = null;
  }
  payload.run_date_range = original?.meta?.broadcast_source_job_id
    ? original.run_date_range ?? null
    : runDates
    ? {
        start_date: runDates[0].format("YYYY-MM-DD"),
        end_date: runDates[1].format("YYYY-MM-DD"),
      }
    : null;
  const previous = original ? lastWorkdayParts(original.schedule.cron) : null;

  if (
    original &&
    ((lastWorkday &&
      previous?.hour === time.hour() &&
      previous.minute === time.minute()) ||
      serializeCron(parseCron(payload.schedule.cron)) ===
        serializeCron(parseCron(original.schedule.cron)))
  ) {
    // 只改名称等其他字段时，保留原Cron文本（包括数字星期等写法）。
    payload.schedule.cron = original.schedule.cron;
  }
  return {
    ...payload,
    schedule: {
      ...payload.schedule,
      run_on_non_workday: values.schedule.run_on_non_workday ?? false,
    },
  };
}

import { expect, it } from "vitest";
import dayjs from "dayjs";
import type { CronJobSpecOutput } from "@/api/types";
import {
  buildManagedCronFormValues,
  buildManagedCronSubmitPayload,
} from "./scheduleForm";

function jobWithCron(cron: string): CronJobSpecOutput {
  return {
    id: "time-job",
    name: "时间测试",
    enabled: true,
    task_type: "text",
    text: "test",
    schedule: { type: "cron", cron, timezone: "Asia/Shanghai" },
    meta: {},
  } as CronJobSpecOutput;
}

it("round trips an Agent recheck and explicitly clears it when disabled", () => {
  const job = {
    ...jobWithCron("0 10 * * *"),
    task_type: "agent" as const,
    request: { input: "提交报表" },
    recheck: { delay_seconds: 7200, instruction: "核对报表" },
  };
  const values = buildManagedCronFormValues(job);
  expect(buildManagedCronSubmitPayload(values, job).recheck).toEqual(
    job.recheck,
  );
  expect(
    buildManagedCronSubmitPayload({ ...values, recheckEnabled: false }, job)
      .recheck,
  ).toBeNull();
});

it.each([
  ["0 10 * * *", "10:00"],
  ["0 0 * * *", "00:00"],
  ["7 23 * * *", "23:07"],
])("每日单时间%s正确回显%s且原样保存", (cron, time) => {
  const job = jobWithCron(cron);
  const values = buildManagedCronFormValues(job);
  expect(values.cronType).toBe("daily");
  expect(values.cronTime?.format("HH:mm")).toBe(time);
  expect(buildManagedCronSubmitPayload(values, job).schedule.cron).toBe(cron);
});

it.each([
  "0 9,10,11 * * *",
  "15 9-11 * * *",
  "*/5 * * * *",
  "* * * * *",
  "0 9 15 * *",
  "0 9 20 5 *",
  "0 9,18 * * mon",
  "0,30 9 * * *",
  "0 9 * * * 2026,2027",
  "0 9 * * MON 2026-2029",
])("复杂排程%s使用自定义且不丢失", (cron) => {
  const job = jobWithCron(cron);
  const values = buildManagedCronFormValues(job);
  expect(values.cronType).toBe("custom");
  expect(values.cronCustom).toBe(cron);
  expect(buildManagedCronSubmitPayload(values, job).schedule.cron).toBe(cron);
});

it.each(["0 * * * *", "15 10 * * mon,wed", "0 10 * * 1,3", "00 10 * * *"])(
  "仅改名称时保留原Cron文本%s",
  (cron) => {
    const job = jobWithCron(cron);
    const values = { ...buildManagedCronFormValues(job), name: "新名称" };
    const payload = buildManagedCronSubmitPayload(values, job);
    expect(payload.name).toBe("新名称");
    expect(payload.schedule).toMatchObject({ cron, timezone: "Asia/Shanghai" });
  },
);

it("主动修改时间时提交新时间", () => {
  const job = jobWithCron("0 10 * * *");
  const values = {
    ...buildManagedCronFormValues(job),
    cronTime: dayjs().hour(11).minute(30),
  };
  expect(buildManagedCronSubmitPayload(values, job).schedule.cron).toBe(
    "30 11 * * *",
  );
});

it("主动修改自定义或星期时不恢复旧排程", () => {
  const job = jobWithCron("0 9,18 * * *");
  const custom = {
    ...buildManagedCronFormValues(job),
    cronCustom: "0 8,20 * * *",
  };
  expect(buildManagedCronSubmitPayload(custom, job).schedule.cron).toBe(
    "0 8,20 * * *",
  );
  const weekly = jobWithCron("0 10 * * mon");
  const values = {
    ...buildManagedCronFormValues(weekly),
    cronDaysOfWeek: ["fri"],
  };
  expect(buildManagedCronSubmitPayload(values, weekly).schedule.cron).toBe(
    "0 10 * * fri",
  );
});

it("新建任务不依赖原始快照", () => {
  const values = buildManagedCronFormValues(jobWithCron("0 10 * * *"));
  expect(buildManagedCronSubmitPayload(values).schedule.cron).toBe(
    "0 10 * * *",
  );
});

it("日期区间随创建提交，清空时明确为不限日期", () => {
  const original = {
    ...jobWithCron("0 9 * * *"),
    run_date_range: { start_date: "2026-11-01", end_date: "2026-11-30" },
  };
  const values = buildManagedCronFormValues(original);
  expect(values.runDates?.[0].format("YYYY-MM-DD")).toBe("2026-11-01");
  expect(buildManagedCronSubmitPayload(values).run_date_range).toEqual(
    original.run_date_range,
  );
  expect(
    buildManagedCronSubmitPayload({ ...values, runDates: null }).run_date_range,
  ).toBeNull();
});

it("子任务编辑正文不会清空继承日期", () => {
  const original = {
    ...jobWithCron("0 9 * * *"),
    meta: { broadcast_source_job_id: "parent" },
    run_date_range: { start_date: "2026-11-01", end_date: "2026-11-30" },
  };
  expect(
    buildManagedCronSubmitPayload(
      { ...buildManagedCronFormValues(original), runDates: null },
      original,
    ).run_date_range,
  ).toEqual(original.run_date_range);
});

it.each([false, true])("新版排程保留非工作日开关%s", (allowed) => {
  const job = jobWithCron("0 10 * * *");
  Object.assign(job.schedule, { run_on_non_workday: allowed });
  const values = buildManagedCronFormValues(job);
  expect(buildManagedCronSubmitPayload(values, job).schedule).toMatchObject({
    run_on_non_workday: allowed,
  });
});

it("缺少非工作日开关时，新页面按false显示并提交", () => {
  const values = buildManagedCronFormValues(jobWithCron("0 10 * * *"));
  expect(values.schedule.run_on_non_workday).toBe(false);
  expect(
    buildManagedCronSubmitPayload(values).schedule.run_on_non_workday,
  ).toBe(false);
});

it("北京时间单时间LW回显为月末工作日预设", () => {
  const job = jobWithCron("15 10 LW * *");
  const values = buildManagedCronFormValues(job);
  expect(values.cronType).toBe("lastWorkday");
  expect(values.cronTime?.format("HH:mm")).toBe("10:15");
  expect(buildManagedCronSubmitPayload(values, job).schedule).toMatchObject({
    cron: "15 10 LW * *",
    timezone: "Asia/Shanghai",
  });
});

it("明确选择LW预设时使用北京时间并读取时间控件", () => {
  const values = buildManagedCronFormValues(jobWithCron("0 10 * * *"));
  values.cronType = "lastWorkday";
  values.cronTime = dayjs().hour(11).minute(30);
  expect(buildManagedCronSubmitPayload(values).schedule).toMatchObject({
    cron: "30 11 LW * *",
    timezone: "Asia/Shanghai",
    run_on_non_workday: false,
  });
});

it.each(["0 10 15W * *", "0 9,18 LW * *", "0 10 LW 1,2 *"])(
  "复杂工作日规则%s保持自定义原样保存",
  (cron) => {
    const job = jobWithCron(cron);
    const values = buildManagedCronFormValues(job);
    expect(values.cronType).toBe("custom");
    expect(buildManagedCronSubmitPayload(values, job).schedule.cron).toBe(cron);
  },
);

it("已有非北京时区的LW不被打开编辑器时偷偷转换", () => {
  const job = jobWithCron("0 10 LW * *");
  job.schedule.timezone = "America/New_York";
  const values = buildManagedCronFormValues(job);
  expect(values.cronType).toBe("custom");
  expect(buildManagedCronSubmitPayload(values, job).schedule).toMatchObject({
    cron: "0 10 LW * *",
    timezone: "America/New_York",
  });
});

it("年份工作日表达式、非工作日开关和日期区间同时往返不丢失", () => {
  const job = {
    ...jobWithCron("0 9 LW * * 2027"),
    run_date_range: { start_date: "2027-01-01", end_date: "2027-12-31" },
  };
  Object.assign(job.schedule, { run_on_non_workday: true });
  const values = buildManagedCronFormValues(job);
  expect(values.cronType).toBe("custom");
  const payload = buildManagedCronSubmitPayload(values, job);
  expect(payload.schedule.cron).toBe("0 9 LW * * 2027");
  expect(payload.schedule.run_on_non_workday).toBe(true);
  expect(payload.run_date_range).toEqual(job.run_date_range);
});

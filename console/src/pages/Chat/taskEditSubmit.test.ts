import dayjs from "dayjs";
import { describe, expect, it, vi } from "vitest";
import type { CronJobSpecOutput } from "@/api/types";
import { extractTaskContentText, submitCronTaskEdit } from "./taskEditSubmit";

function buildCronJob(
  overrides: Partial<CronJobSpecOutput> = {},
): CronJobSpecOutput {
  return {
    id: "job-1",
    name: "每日巡检",
    enabled: true,
    schedule: {
      type: "cron",
      cron: "0 9 * * *",
      timezone: "Asia/Shanghai",
    },
    task_type: "agent",
    request: {
      input: [{ role: "user", content: [{ type: "text", text: "hello" }] }],
      session_id: "session-1",
      user_id: "user-1",
    },
    dispatch: {
      type: "channel",
      channel: "console",
      target: {
        user_id: "user-1",
        session_id: "session-1",
      },
      mode: "final",
    },
    runtime: {
      max_concurrency: 1,
      timeout_seconds: 7200,
      misfire_grace_seconds: 300,
    },
    meta: {},
    ...overrides,
  };
}

describe("submitCronTaskEdit", () => {
  it("extracts plain text from existing request input content", () => {
    expect(
      extractTaskContentText(
        JSON.stringify([
          {
            role: "user",
            content: [{ type: "text", text: "查询成都今天的天气" }],
          },
        ]),
      ),
    ).toBe("查询成都今天的天气");
  });

  it("wraps plain task content text into request input before saving", async () => {
    const replaceCronJob = vi.fn().mockResolvedValue({});
    const task = buildCronJob();

    const payload = await submitCronTaskEdit(
      task,
      {
        ...task,
        cronType: "daily",
        cronTime: dayjs().hour(5).minute(0),
        taskContentText: "查询成都今天的天气",
      },
      replaceCronJob,
    );

    expect(payload.request?.input).toEqual([
      {
        role: "user",
        content: [
          {
            text: "查询成都今天的天气",
            type: "text",
          },
        ],
      },
    ]);
    expect(replaceCronJob).toHaveBeenCalledWith("job-1", payload);
  });

  it("writes plain task content text to text field for text jobs", async () => {
    const replaceCronJob = vi.fn().mockResolvedValue({});
    const task = buildCronJob({
      task_type: "text",
      text: "旧消息",
      request: undefined,
    });

    const payload = await submitCronTaskEdit(
      task,
      {
        ...task,
        cronType: "daily",
        cronTime: dayjs().hour(5).minute(0),
        taskContentText: "今天 18 点提醒我整理日报",
      },
      replaceCronJob,
    );

    expect(payload.task_type).toBe("text");
    expect(payload.text).toBe("今天 18 点提醒我整理日报");
    expect(payload.request).toBeUndefined();
    expect(replaceCronJob).toHaveBeenCalledWith("job-1", payload);
  });

  it("edits a workflow task without creating an Agent request", async () => {
    const replaceCronJob = vi.fn().mockResolvedValue({});
    const task = buildCronJob({
      task_type: "workflow",
      skill_ids: "skill-a",
      workflow_binding_id: "binding-1",
      request: undefined,
    });

    const payload = await submitCronTaskEdit(
      task,
      {
        ...task,
        cronType: "daily",
        cronTime: dayjs().hour(6).minute(0),
        taskContentText: "stale form text",
      },
      replaceCronJob,
    );

    expect(payload.task_type).toBe("workflow");
    expect(payload.workflow_binding_id).toBe("binding-1");
    expect(payload.request).toBeUndefined();
    expect(payload.text).toBeUndefined();
  });

  it("normalizes form values and calls replaceCronJob for the edited task", async () => {
    const replaceCronJob = vi.fn().mockResolvedValue({});
    const task = buildCronJob();

    const payload = await submitCronTaskEdit(
      task,
      {
        ...task,
        cronType: "daily",
        cronTime: dayjs().hour(5).minute(0),
        request: {
          ...task.request,
          input: JSON.stringify([{ role: "user", content: [] }]),
        },
        notificationDelayValue: 2,
        notificationDelayUnit: "hours",
      },
      replaceCronJob,
    );

    expect(replaceCronJob).toHaveBeenCalledWith("job-1", payload);
    expect(payload.schedule.cron).toBe("0 5 * * *");
    expect(payload.meta?.notification_delay_minutes).toBe(120);
    expect(payload.request?.input).toEqual([{ role: "user", content: [] }]);
  });

  it("does not call replaceCronJob when request JSON is invalid", async () => {
    const replaceCronJob = vi.fn().mockResolvedValue({});
    const task = buildCronJob();

    await expect(
      submitCronTaskEdit(
        task,
        {
          ...task,
          cronType: "daily",
          cronTime: dayjs().hour(5).minute(0),
          request: {
            ...task.request,
            input: "{invalid-json",
          },
        },
        replaceCronJob,
      ),
    ).rejects.toThrow(SyntaxError);
    expect(replaceCronJob).not.toHaveBeenCalled();
  });
});

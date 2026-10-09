import dayjs from "dayjs";
import { describe, expect, it } from "vitest";
import type { CronJobSpecOutput } from "@/api/types";
import {
  buildSkillSelectOptions,
  buildCronJobFormValues,
  buildCronJobSubmitPayload,
  getBroadcastResultMessage,
  getBroadcastTaskProgressText,
  normalizeSkillIdsInput,
} from "./helpers";

function buildCronJob(
  overrides: Partial<CronJobSpecOutput> = {},
): CronJobSpecOutput {
  return {
    id: "job-1",
    name: "test job",
    enabled: true,
    schedule: {
      type: "cron",
      cron: "00 09 * * 1,3",
      timezone: "UTC",
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
    },
    meta: {},
    ...overrides,
  };
}

describe("CronJobs helpers", () => {
  it("hydrates edit form values with execution_model_key and parsed cron fields", () => {
    const job = buildCronJob({
      model_slot: {
        provider_id: "openai",
        model: "gpt-5.4",
      },
    });

    const result = buildCronJobFormValues(job);

    expect(result.execution_model_key).toBe("openai::gpt-5.4");
    expect(result.request?.input).toBe(
      JSON.stringify(job.request?.input, null, 2),
    );
    expect(dayjs.isDayjs(result.cronTime)).toBe(true);
    expect(result.cronTime?.hour()).toBe(9);
    expect(result.cronTime?.minute()).toBe(0);
    expect(result.cronDaysOfWeek).toEqual(["mon", "wed"]);
  });

  it("hydrates notification delay as hours when stored minutes divide by 60", () => {
    const result = buildCronJobFormValues(
      buildCronJob({
        meta: {
          notification_delay_minutes: 120,
        },
      }),
    );

    expect(result.notificationDelayValue).toBe(2);
    expect(result.notificationDelayUnit).toBe("hours");
  });

  it("does not hydrate legacy dispatch intent meta into the broadcast switch", () => {
    const result = buildCronJobFormValues(
      buildCronJob({
        meta: {
          dispatch_intents_enabled: true,
        },
      }),
    );

    expect(result.meta?.broadcast_dispatch_intents_enabled).toBeUndefined();
  });

  it("builds submit payload with explicit model_slot for agent jobs", () => {
    const result = buildCronJobSubmitPayload({
      ...buildCronJob(),
      cronType: "weekly",
      cronTime: dayjs().hour(8).minute(30),
      cronDaysOfWeek: ["mon", "fri"],
      execution_model_key: "openai::gpt-5.4",
      notificationDelayValue: 2,
      notificationDelayUnit: "hours",
      request: {
        input: JSON.stringify([{ role: "user", content: [] }]),
      },
    });

    expect(result.schedule.cron).toBe("30 8 * * mon,fri");
    expect(result.model_slot).toEqual({
      provider_id: "openai",
      model: "gpt-5.4",
    });
    expect(result.meta?.notification_delay_minutes).toBe(120);
    expect(result.request?.input).toEqual([{ role: "user", content: [] }]);
  });

  it("submits workflow as a skill task without stale Agent fields", () => {
    const result = buildCronJobSubmitPayload({
      ...buildCronJob({
        task_type: "workflow",
        workflow_binding_id: "binding-1",
        text: "old text",
      }),
      skillIds: ["skill-a", "skill-b"],
      cronType: "daily",
      cronTime: dayjs().hour(9).minute(0),
    });

    expect(result.task_type).toBe("workflow");
    expect(result.skill_ids).toBe("skill-a,skill-b");
    expect(result.workflow_binding_id).toBe("binding-1");
    expect(result.request).toBeUndefined();
    expect(result.text).toBeUndefined();
    expect(result.model_slot).toBeUndefined();
  });

  it("removes hidden broadcast dispatch intent flag on submit", () => {
    const result = buildCronJobSubmitPayload({
      ...buildCronJob({
        meta: {
          existing_meta: "kept",
          broadcast_dispatch_intents_enabled: true,
        },
      }),
    });

    expect(result.meta).toMatchObject({
      existing_meta: "kept",
      notification_delay_minutes: 0,
    });
    expect(result.meta).not.toHaveProperty(
      "broadcast_dispatch_intents_enabled",
    );
  });

  it("removes disabled broadcast dispatch intent flag on submit", () => {
    const result = buildCronJobSubmitPayload({
      ...buildCronJob({
        meta: {
          existing_meta: "kept",
          broadcast_dispatch_intents_enabled: false,
        },
      }),
    });

    expect(result.meta?.existing_meta).toBe("kept");
    expect(result.meta).not.toHaveProperty(
      "broadcast_dispatch_intents_enabled",
    );
  });

  it("removes legacy dispatch intent flag on submit", () => {
    const result = buildCronJobSubmitPayload({
      ...buildCronJob({
        meta: {
          dispatch_intents_enabled: true,
          broadcast_dispatch_intents_enabled: false,
        },
      }),
    });

    expect(result.meta).not.toHaveProperty("dispatch_intents_enabled");
    expect(result.meta).not.toHaveProperty(
      "broadcast_dispatch_intents_enabled",
    );
  });

  it("does not persist hidden broadcast dispatch switch on child jobs", () => {
    const result = buildCronJobSubmitPayload({
      ...buildCronJob({
        meta: {
          broadcast_source_job_id: "parent-job",
          broadcast_dispatch_intents_enabled: true,
        },
      }),
    });

    expect(result.meta?.broadcast_source_job_id).toBe("parent-job");
    expect(result.meta).not.toHaveProperty(
      "broadcast_dispatch_intents_enabled",
    );
  });

  it("normalizes manually entered skill ids before submit", () => {
    expect(normalizeSkillIdsInput("a, b\nc a")).toBe("a,b,c");
  });

  it("normalizes selected skill ids before submit", () => {
    expect(normalizeSkillIdsInput(["a", "b", "a"])).toBe("a,b");
  });

  it("deduplicates dropdown options by skill_id", () => {
    expect(
      buildSkillSelectOptions([
        {
          skill_id: "same-skill-id",
          skill_name: "first_skill_name",
          cn_name: "首次展示",
        },
        {
          skill_id: "same-skill-id",
          skill_name: "second_skill_name",
          cn_name: "重复展示",
        },
      ]),
    ).toEqual([
      {
        value: "same-skill-id",
        label: "首次展示 (same-skill-id)",
      },
    ]);
  });

  it("accepts selected database skill ids without frontend character filtering", () => {
    expect(normalizeSkillIdsInput(["数据分析/技能_001", "skill:demo"])).toBe(
      "数据分析/技能_001,skill:demo",
    );
  });

  it("rejects skill ids beyond the API length limit", () => {
    expect(() => normalizeSkillIdsInput("x".repeat(201))).toThrow();
  });

  it("maps form skillIds to API skill_ids in submit payload", () => {
    const result = buildCronJobSubmitPayload({
      ...buildCronJob(),
      skillIds: ["a", "b"],
    });

    expect(result.skill_ids).toBe("a,b");
  });

  it("hydrates API skill_ids into form skillIds when editing", () => {
    const result = buildCronJobFormValues(
      buildCronJob({
        skill_ids: "a,b",
      }),
    );

    expect(result.skillIds).toEqual(["a", "b"]);
  });

  it("clears model_slot for text jobs on submit", () => {
    const result = buildCronJobSubmitPayload({
      ...buildCronJob({
        task_type: "text",
        text: "hello",
        request: undefined,
      }),
      cronType: "custom",
      cronCustom: "15 10 * * *",
      execution_model_key: "openai::gpt-5.4",
    });

    expect(result.task_type).toBe("text");
    expect(result.schedule.cron).toBe("15 10 * * *");
    expect(result.model_slot).toBeUndefined();
  });

  it("summarizes broadcast results with warning precedence", () => {
    expect(
      getBroadcastResultMessage([
        {
          tenant_id: "tenant-a",
          success: true,
          job_id: "job-1",
          cron: "0 9 * * *",
          timezone: "UTC",
          offset_minutes: 0,
          notification_timezone: "UTC",
          error: "",
          warning: "model_slot not copied",
        },
      ]),
    ).toEqual({
      tone: "warning",
      text: "Broadcasted 1 tenants, 1 with warnings",
    });
  });

  it("prefers failure summary over warning summary for mixed broadcast results", () => {
    expect(
      getBroadcastResultMessage([
        {
          tenant_id: "tenant-a",
          success: true,
          job_id: "job-1",
          cron: "0 9 * * *",
          timezone: "UTC",
          offset_minutes: 0,
          notification_timezone: "UTC",
          error: "",
          warning: "model_slot not copied",
        },
        {
          tenant_id: "tenant-b",
          success: false,
          job_id: "",
          cron: "0 9 * * *",
          timezone: "UTC",
          offset_minutes: 60,
          notification_timezone: "UTC",
          error: "boom",
          warning: "",
        },
      ]),
    ).toEqual({
      tone: "warning",
      text: "Broadcasted 1, failed 1",
    });
  });

  it("summarizes running broadcast task progress", () => {
    expect(
      getBroadcastTaskProgressText({
        task_id: "task-1",
        status: "running",
        tenant_count: 5,
        completed_count: 2,
        failed_count: 1,
        results: [],
        reused: false,
      }),
    ).toBe("Broadcasting 2/5 tenants, failed 1");
  });

  it("summarizes completed broadcast task progress", () => {
    expect(
      getBroadcastTaskProgressText({
        task_id: "task-1",
        status: "completed",
        tenant_count: 3,
        completed_count: 3,
        failed_count: 0,
        results: [],
        reused: false,
      }),
    ).toBe("Broadcast completed 3/3 tenants");
  });

  it("uses task-level broadcast failure summary", () => {
    expect(
      getBroadcastTaskProgressText({
        task_id: "task-1",
        status: "failed",
        tenant_count: 3,
        completed_count: 1,
        failed_count: 0,
        results: [],
        failure_summary: "database unavailable",
        reused: false,
      }),
    ).toBe("Broadcast failed: database unavailable");
  });
});

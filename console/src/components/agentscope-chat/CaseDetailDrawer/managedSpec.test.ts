import { expect, it } from "vitest";
import { buildManagedCaseSpec } from "./managedSpec";

it("精选案例不随机生成ID/会话，保留正文模型并固定非工作日不执行", () => {
  const input = {
    caseData: { id: 12, label: "每日提醒", value: "查询进展" },
    context: {
      actor_id: "alice",
      tenant_id: "alice",
      source_id: "bank",
      agent_id: "default",
    },
    schedule: {
      type: "cron" as const,
      cron: "0 10 * * *",
      timezone: "Asia/Shanghai",
      run_on_non_workday: true,
    },
    channel: "console",
    modelSlot: { provider_id: "provider", model: "model" },
    notificationDelayMinutes: 60,
  };
  const first = buildManagedCaseSpec(input);
  expect(buildManagedCaseSpec(input)).toEqual(first);
  expect(first).not.toHaveProperty("id");
  expect(first.request).not.toHaveProperty("session_id");
  expect(first.schedule).toEqual({
    type: "cron",
    cron: "0 10 * * *",
    timezone: "Asia/Shanghai",
    run_on_non_workday: false,
  });
  expect(first.meta).toMatchObject({
    subscription_key: "featured_case:12",
    notification_delay_minutes: 60,
  });
  expect(first.model_slot).toEqual(input.modelSlot);
  expect(first.request?.input).toEqual([
    {
      role: "user",
      type: "message",
      content: [{ type: "text", text: "查询进展" }],
    },
  ]);
});

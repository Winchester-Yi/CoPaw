import type { FeaturedCase } from "@/api/types/featuredCases";
import type { ModelSlotConfig } from "@/api/types";
import type {
  ManagedCronContext,
  ManagedCronSchedule,
  ManagedCronSpec,
} from "@/api/types/schedulerCron";
import { normalizeNotificationDelayMinutes } from "@/utils/cron";

interface Input {
  caseData: Pick<FeaturedCase, "id" | "label" | "value">;
  context: ManagedCronContext;
  schedule: ManagedCronSchedule;
  channel: string;
  modelSlot?: ModelSlotConfig;
  notificationDelayMinutes?: number;
}

/** 新入口不生成任务或会话ID，保留原案例分组键，后端准备资源后给出权威绑定。 */
export function buildManagedCaseSpec(input: Input): ManagedCronSpec {
  return {
    name: input.caseData.label || "定时任务",
    enabled: true,
    schedule: { ...input.schedule, run_on_non_workday: false },
    task_type: "agent",
    request: {
      input: [
        {
          role: "user",
          type: "message",
          content: [{ type: "text", text: input.caseData.value }],
        },
      ],
    },
    dispatch: {
      type: "channel",
      channel: input.channel || "console",
      target: { user_id: input.context.actor_id, session_id: "" },
      mode: "final",
    },
    meta: {
      subscription_key: `featured_case:${input.caseData.id}`,
      notification_delay_minutes: normalizeNotificationDelayMinutes(
        input.notificationDelayMinutes,
      ),
    },
    model_slot: input.modelSlot,
  };
}

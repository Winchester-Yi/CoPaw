import type { SourceSystemConfig } from "@/api/types/sourceSystemConfig";

export const FOLLOW_UP_SUGGESTIONS_CONFIG_KEY = "follow_up_suggestions";
export const DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS = 5;
const MIN_FOLLOW_UP_TIMEOUT_SECONDS = 1;
const MAX_FOLLOW_UP_TIMEOUT_SECONDS = 15;

export const DEFAULT_FOLLOW_UP_PROMPT_TEMPLATE =
  "根据以下对话，生成{max_count}个用户可能想问的后续问题。\n" +
  "问题要简短（不超过20字）、具体、自然，符合用户的真实提问习惯。\n\n" +
  "用户问题：{user_message}\n" +
  "助手回答（摘要）：{assistant_response}\n\n" +
  '直接输出JSON数组格式，如：["问题1", "问题2", "问题3"]\n' +
  "如果没有合适的问题，输出空数组 []。\n" +
  "不要输出任何其他内容、解释或前缀后缀。";

export interface FollowUpSuggestionsConfig {
  enabled: boolean;
  prompt_template: string;
  timeout_seconds: number;
}

function normalizeTimeoutSeconds(value: unknown): number {
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value < MIN_FOLLOW_UP_TIMEOUT_SECONDS ||
    value > MAX_FOLLOW_UP_TIMEOUT_SECONDS
  ) {
    return DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS;
  }
  return value;
}

export function readFollowUpSuggestionsConfig(
  config: SourceSystemConfig | null | undefined,
): FollowUpSuggestionsConfig {
  const raw = config?.[FOLLOW_UP_SUGGESTIONS_CONFIG_KEY];
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return {
      enabled: true,
      prompt_template: DEFAULT_FOLLOW_UP_PROMPT_TEMPLATE,
      timeout_seconds: DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS,
    };
  }

  const record = raw as Record<string, unknown>;
  const prompt =
    typeof record.prompt_template === "string" &&
    record.prompt_template.trim()
      ? record.prompt_template
      : DEFAULT_FOLLOW_UP_PROMPT_TEMPLATE;

  return {
    enabled: typeof record.enabled === "boolean" ? record.enabled : true,
    prompt_template: prompt,
    timeout_seconds: normalizeTimeoutSeconds(record.timeout_seconds),
  };
}

export function writeFollowUpSuggestionsConfig(
  config: SourceSystemConfig,
  followUpConfig: FollowUpSuggestionsConfig,
): SourceSystemConfig {
  return {
    ...config,
    [FOLLOW_UP_SUGGESTIONS_CONFIG_KEY]: {
      enabled: followUpConfig.enabled,
      prompt_template:
        followUpConfig.prompt_template.trim() ||
        DEFAULT_FOLLOW_UP_PROMPT_TEMPLATE,
      timeout_seconds: normalizeTimeoutSeconds(
        followUpConfig.timeout_seconds,
      ),
    },
  };
}

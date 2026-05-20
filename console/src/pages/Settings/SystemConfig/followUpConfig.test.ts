import { describe, expect, it } from "vitest";
import {
  DEFAULT_FOLLOW_UP_PROMPT_TEMPLATE,
  DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS,
  FOLLOW_UP_SUGGESTIONS_CONFIG_KEY,
  readFollowUpSuggestionsConfig,
  writeFollowUpSuggestionsConfig,
} from "./followUpConfig";

describe("followUpConfig", () => {
  it("returns defaults when config is missing", () => {
    expect(readFollowUpSuggestionsConfig({})).toEqual({
      enabled: true,
      prompt_template: DEFAULT_FOLLOW_UP_PROMPT_TEMPLATE,
      timeout_seconds: DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS,
    });
  });

  it("reads nested follow-up suggestions config", () => {
    expect(
      readFollowUpSuggestionsConfig({
        [FOLLOW_UP_SUGGESTIONS_CONFIG_KEY]: {
          enabled: false,
          prompt_template: "自定义 {user_message}",
          timeout_seconds: 7.5,
        },
      }),
    ).toEqual({
      enabled: false,
      prompt_template: "自定义 {user_message}",
      timeout_seconds: 7.5,
    });
  });

  it("uses defaults for invalid nested values", () => {
    expect(
      readFollowUpSuggestionsConfig({
        [FOLLOW_UP_SUGGESTIONS_CONFIG_KEY]: {
          enabled: "false",
          prompt_template: "   ",
          timeout_seconds: "bad",
        },
      }),
    ).toEqual({
      enabled: true,
      prompt_template: DEFAULT_FOLLOW_UP_PROMPT_TEMPLATE,
      timeout_seconds: DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS,
    });
  });

  it("preserves unrelated keys when writing follow-up config", () => {
    const next = writeFollowUpSuggestionsConfig(
      { provider_policy: { default_model: "qwen" } },
      {
        enabled: false,
        prompt_template: " 新提示 ",
        timeout_seconds: 6.5,
      },
    );

    expect(next).toEqual({
      provider_policy: { default_model: "qwen" },
      [FOLLOW_UP_SUGGESTIONS_CONFIG_KEY]: {
        enabled: false,
        prompt_template: "新提示",
        timeout_seconds: 6.5,
      },
    });
  });
});

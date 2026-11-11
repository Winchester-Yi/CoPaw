import { describe, expect, it } from "vitest";
import {
  getMultiAgentModeEnabled,
  withMultiAgentIntent,
} from "./multiAgentMode";

describe("multi-agent mode", () => {
  it("defaults off and reads only an explicit session choice", () => {
    expect(getMultiAgentModeEnabled(undefined)).toBe(false);
    expect(
      getMultiAgentModeEnabled({ meta: { multi_agent_mode_enabled: true } }),
    ).toBe(true);
    expect(
      getMultiAgentModeEnabled({ meta: { multi_agent_mode_enabled: "true" } }),
    ).toBe(false);
  });

  it("captures intent without changing the request mode", () => {
    expect(withMultiAgentIntent({ mode: "plan" }, true)).toEqual({
      mode: "plan",
      enable_subagents: true,
    });
    expect(withMultiAgentIntent({ mode: "normal" }, false)).toEqual({
      mode: "normal",
      enable_subagents: false,
    });
  });
});

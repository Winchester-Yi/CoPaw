import { describe, expect, it } from "vitest";

import {
  addHandler,
  createHandler,
  defaultContext,
  isScriptReference,
  moveHandler,
  removeHandler,
  replaceEvent,
} from "./draft";
import { createScenarioEvent } from "./scenarioTemplates";
import type { HookConfigDraft } from "./types";

const emptyConfig: HookConfigDraft = { enabled: true, events: {} };

const config: HookConfigDraft = {
  enabled: true,
  events: {
    PreToolUse: [
      {
        id: "tool-guards",
        matcher: { tools: [] },
        hooks: [
          {
            id: "guard-shell",
            type: "command",
            argv: ["python", "hooks/scripts/guard.py"],
          },
        ],
      },
    ],
  },
};

const twoHandlerConfig: HookConfigDraft = {
  ...config,
  events: {
    PreToolUse: [
      {
        ...config.events.PreToolUse[0]!,
        hooks: [
          ...config.events.PreToolUse[0]!.hooks,
          {
            id: "second-handler",
            type: "command",
            argv: ["echo", "second"],
          },
        ],
      },
    ],
  },
};

describe("Hook configuration draft helpers", () => {
  it.each(["command", "http", "prompt"] as const)(
    "defaults %s append mode to false",
    (type) => {
      expect(createHandler(type).outputAppend).toBe(false);
    },
  );
  it("adds a command Handler without mutating the source draft", () => {
    const next = addHandler(config, "PreToolUse", "tool-guards", "command");

    expect(next.events.PreToolUse[0]?.hooks.slice(-1)[0]).toMatchObject({
      type: "command",
      argv: [""],
    });
    expect(config.events.PreToolUse[0]?.hooks).toHaveLength(1);
  });

  it("removes only the selected Handler", () => {
    const next = removeHandler(
      config,
      "PreToolUse",
      "tool-guards",
      "guard-shell",
    );

    expect(next.events.PreToolUse[0]?.hooks).toEqual([]);
    expect(config.events.PreToolUse[0]?.hooks).toHaveLength(1);
  });

  it("recognises only controlled script references", () => {
    expect(isScriptReference("hooks/scripts/guard.py")).toBe(true);
    expect(isScriptReference("python")).toBe(false);
    expect(isScriptReference("/tmp/guard.py")).toBe(false);
  });

  it("leaves an unrelated empty configuration structurally valid", () => {
    expect(emptyConfig).toEqual({ enabled: true, events: {} });
  });

  it("moves a Handler without crossing its Matcher Group boundary", () => {
    const next = moveHandler(
      twoHandlerConfig,
      "PreToolUse",
      "tool-guards",
      0,
      1,
    );

    expect(next.events.PreToolUse[0]?.hooks.map((hook) => hook.id)).toEqual([
      "second-handler",
      "guard-shell",
    ]);
    expect(twoHandlerConfig.events.PreToolUse[0]?.hooks[0]?.id).toBe(
      "guard-shell",
    );
  });

  it("replaces one event with a scenario event without mutating another event", () => {
    const scenario = createScenarioEvent("tool-audit");
    const next = replaceEvent(config, scenario.event, scenario.groups);

    expect(next.events.PostToolUse).toEqual(scenario.groups);
    expect(next.events.PreToolUse).toEqual(config.events.PreToolUse);
  });

  it("prefills a candidate assistant response only for Stop manual tests", () => {
    expect(defaultContext("Stop")).toMatchObject({
      assistant_response: "这是用于测试的候选回复。",
    });
    expect(defaultContext("PreToolUse")).not.toHaveProperty(
      "assistant_response",
    );
  });
});

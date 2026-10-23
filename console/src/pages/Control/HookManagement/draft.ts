import { produce } from "immer";

import type {
  HookConfigDraft,
  HookEventName,
  HookHandlerDraft,
  HookHandlerType,
  HookMatcherGroupDraft,
} from "./types";

const scriptReferencePattern = /^hooks\/scripts\/[^/]+\.(py|sh|bash|zsh)$/i;

export function isScriptReference(value: string): boolean {
  return scriptReferencePattern.test(value);
}

function generatedId(prefix: string): string {
  return `${prefix}-${crypto.randomUUID().slice(0, 8)}`;
}

export function createHandler(type: HookHandlerType): HookHandlerDraft {
  const base = {
    id: generatedId("handler"),
    if: "",
    timeout: 10,
    statusMessage: "",
    once: false,
    outputTransform: false,
    outputAppend: false,
    includeConversationSnapshot: false,
    conversationSnapshotLimit: 50,
    failPolicy: "allow",
  };

  if (type === "http") {
    return {
      ...base,
      id: generatedId("http"),
      type,
      url: "",
      headers: {},
      headerSecretRefs: {},
      allowedEnvVars: [],
    };
  }
  if (type === "prompt") {
    return {
      ...base,
      id: generatedId("prompt"),
      type,
      prompt: "",
      failPolicy: "block",
    };
  }
  return {
    ...base,
    id: generatedId("command"),
    type,
    command: "",
    argv: [""],
    cwd: "",
    env: {},
  };
}

export function addEvent(
  config: HookConfigDraft,
  event: HookEventName,
): HookConfigDraft {
  return produce(config, (draft) => {
    if (!draft.events[event]) draft.events[event] = [];
  });
}

export function addGroup(
  config: HookConfigDraft,
  event: HookEventName,
): HookConfigDraft {
  return produce(config, (draft) => {
    const groups = (draft.events[event] ??= []);
    groups.push({
      id: generatedId("group"),
      matcher: { tools: [] },
      hooks: [],
    });
  });
}

export function addHandler(
  config: HookConfigDraft,
  event: HookEventName,
  groupId: string,
  type: HookHandlerType,
): HookConfigDraft {
  return produce(config, (draft) => {
    const group = draft.events[event]?.find((item) => item.id === groupId);
    if (group) group.hooks.push(createHandler(type));
  });
}

export function removeHandler(
  config: HookConfigDraft,
  event: HookEventName,
  groupId: string,
  handlerId: string,
): HookConfigDraft {
  return produce(config, (draft) => {
    const group = draft.events[event]?.find((item) => item.id === groupId);
    if (group)
      group.hooks = group.hooks.filter((handler) => handler.id !== handlerId);
  });
}

export function removeGroup(
  config: HookConfigDraft,
  event: HookEventName,
  groupId: string,
): HookConfigDraft {
  return produce(config, (draft) => {
    const groups = draft.events[event];
    if (groups)
      draft.events[event] = groups.filter((group) => group.id !== groupId);
  });
}

export function moveHandler(
  config: HookConfigDraft,
  event: HookEventName,
  groupId: string,
  fromIndex: number,
  toIndex: number,
): HookConfigDraft {
  return produce(config, (draft) => {
    const group = draft.events[event]?.find((item) => item.id === groupId);
    if (
      !group ||
      fromIndex === toIndex ||
      fromIndex < 0 ||
      toIndex < 0 ||
      fromIndex >= group.hooks.length ||
      toIndex >= group.hooks.length
    ) {
      return;
    }
    const [handler] = group.hooks.splice(fromIndex, 1);
    group.hooks.splice(toIndex, 0, handler!);
  });
}

export function replaceEvent(
  config: HookConfigDraft,
  event: HookEventName,
  groups: HookMatcherGroupDraft[],
): HookConfigDraft {
  return produce(config, (draft) => {
    draft.events[event] = structuredClone(groups);
  });
}

export function defaultContext(event: HookEventName): Record<string, unknown> {
  return {
    session_id: "manual-test-session",
    transcript_path: "manual-test.jsonl",
    cwd: ".",
    hook_event_name: event,
    tenant_id: "current-tenant",
    effective_tenant_id: "current-tenant",
    user_id: "current-user",
    agent_id: "default",
    channel: "console",
    ...(event === "Stop"
      ? { assistant_response: "这是用于测试的候选回复。" }
      : {}),
  };
}

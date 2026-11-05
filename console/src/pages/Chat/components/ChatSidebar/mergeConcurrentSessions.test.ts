import { expect, it } from "vitest";
import { mergeConcurrentSessions } from "./mergeConcurrentSessions";
import type { IAgentScopeRuntimeWebUISession as Session } from "@/components/agentscope-chat";

it("以最早匹配的当前会话保留消息、生成状态，排除已删除别名", () => {
  const current = [
    {
      id: "temp",
      realId: "real",
      messages: [{ id: "live" }],
      generating: true,
      name: "live",
    },
    { id: "other", sessionId: "logical", messages: [], name: "second" },
    { id: "removed", realId: "deleted", messages: [] },
  ];
  const incoming = [
    { id: "real", sessionId: "logical", name: "server", messages: [] },
  ];
  const result = mergeConcurrentSessions(
    incoming as never,
    current as never,
    false,
    [{ id: "deleted" }],
  );
  expect(result).toEqual([
    {
      ...incoming[0],
      realId: "real",
      name: "server",
      messages: [{ id: "live" }],
      generating: true,
    },
  ]);
});

it("长列表投影的身份读取量保持线性", () => {
  const count = 3000;
  let reads = 0;
  const current = Array.from({ length: count }, (_, index) => ({
    get id() {
      reads += 1;
      return `id-${index}`;
    },
    messages: [],
  })) as Session[];
  const incoming = Array.from({ length: count }, (_, index) => ({
    id: `id-${index}`,
    messages: [],
  })) as Session[];
  expect(mergeConcurrentSessions(incoming, current, true)).toHaveLength(count);
  expect(reads).toBeLessThan(count * 50);
});

import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useIframeStore } from "@/stores/iframeStore";
import { useAgentStore } from "@/stores/agentStore";
import { getCurrentCronContext, useCronContext } from "./useCronContext";

vi.mock("@/utils/identity", () => ({ getUserId: () => "fallback-user" }));
beforeEach(() => {
  useIframeStore.setState({ userId: "alice", source: "bank", manager: false });
  useAgentStore.setState({ selectedAgent: "default" });
});
afterEach(cleanup);

it("设置页和事件回调读取同一真实操作者/来源/Agent，不从任务记录推导身份", () => {
  const { result } = renderHook(useCronContext);
  expect(result.current).toEqual({
    actor_id: "alice",
    tenant_id: "alice",
    source_id: "bank",
    agent_id: "default",
  });
  expect(getCurrentCronContext()).toEqual(result.current);
});

it("缺少iframe用户时沿用现有用户helper，不生成管理员身份", () => {
  useIframeStore.setState({ userId: null });
  const context = getCurrentCronContext();
  expect(context.actor_id).toBe("fallback-user");
  expect(context).not.toHaveProperty("is_manager");
});

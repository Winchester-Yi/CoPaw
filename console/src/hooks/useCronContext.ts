import { useMemo } from "react";
import { useIframeStore } from "@/stores/iframeStore";
import { useAgentStore } from "@/stores/agentStore";
import { getUserId } from "@/utils/identity";
import { DEFAULT_SOURCE_ID } from "@/constants/identity";
import type { ManagedCronContext } from "@/api/types/schedulerCron";

function context(
  user: string | null,
  source: string | null,
  agent: string,
): ManagedCronContext {
  const actor = user || getUserId();
  return {
    actor_id: actor,
    tenant_id: actor,
    source_id: source || DEFAULT_SOURCE_ID,
    agent_id: agent,
  };
}

/** 异步事件结束时读取当前身份，用于拒绝把旧工作区响应贴到新工作区。 */
export function getCurrentCronContext() {
  const frame = useIframeStore.getState();
  return context(
    frame.userId,
    frame.source,
    useAgentStore.getState().selectedAgent,
  );
}

/** 只订阅定位维度，不从任务记录猜测操作者、角色或来源级空维度。 */
export function useCronContext() {
  const user = useIframeStore((state) => state.userId);
  const source = useIframeStore((state) => state.source);
  const agent = useAgentStore((state) => state.selectedAgent);
  return useMemo(() => context(user, source, agent), [user, source, agent]);
}
